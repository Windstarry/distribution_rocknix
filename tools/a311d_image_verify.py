#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A311D 刷盘前离线镜像核查（只读，纯标准库，无外部依赖）。

用法:
  python3 tools/a311d_image_verify.py <image.img|image.img.gz> [--expect-label <LABEL>]
  # A311D 刷盘前建议: --expect-label ROCKNIX（设备 U-Boot env 基线）

检查项:
  T1  MBR / FAT32 启动分区可读
  T2  必需文件齐全 (KERNEL.IMG, SYSTEM, DTB.IMG, CONFIG.INI, CFGLOAD)
  T3  DTB GPU 节点: arm,mali-bifrost 必须 okay；arm,mali-midgard 若存在须 disabled
  T4  CONFIG.INI 未启用悬空 gpu_dtb（本树不产出 dtb-panfrost.img）
  T5  CFGLOAD 默认 dtbfile=dtb.img
  T6  KERNEL.IMG / SYSTEM 与镜像内附 .md5 清单一致（仅 .img；.gz 跳过）
  T7  SYSTEM squashfs 头有效 (hsqs)
  T8  KERNEL.IMG 内嵌内核版本字符串（Android boot image）
  T9  .gz 自身 sha256 与同目录 .sha256 清单一致（存在时）
  T10 启动 label 一致性（FAT32 BPB 卷标 / FAT32 根目录卷标项 / cfgload /
      cfgload_env / kernel.img Android header cmdline）；--expect-label 时
      对照已验证基线（A311D 用 ROCKNIX）：不一致即 FAIL，避免 initramfs
      挂不上 /flash
  T10d BPB 卷标与根目录卷标项必须一致：Windows SetVolumeLabelW 只改
      根目录项，Linux blkid 读 BPB，"半改"卷标仍会黑屏

输出逐项 PASS/FAIL/WARN/SKIP 与总结论；存在 FAIL 时退出码为 1。
工具只做静态离线检查，不替代板级启动验证。
"""
import gzip
import hashlib
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import a311d_dtb_inspect as fdt


class FileReader:
    """普通 .img：随机 seek，零缓存。"""

    def __init__(self, path):
        self.f = open(path, "rb")

    def read_at(self, off, n):
        self.f.seek(off)
        return self.f.read(n)


class GzipLazy:
    """.gz：按需顺序解压并缓存，避免 2.2GB 落盘；仅适合读取前部小文件。"""

    def __init__(self, path):
        self.g = gzip.open(path, "rb")
        self.buf = bytearray()
        self.eof = False

    def read_at(self, off, n):
        while len(self.buf) < off + n and not self.eof:
            chunk = self.g.read(1 << 20)
            if not chunk:
                self.eof = True
                break
            self.buf.extend(chunk)
        return bytes(self.buf[off:off + n])

    def cached_mb(self):
        return len(self.buf) / 1048576.0


def lfn_name(entries):
    """FAT32 LFN：每个目录项 13 个 UTF-16 码元，未用位填 0xFFFF。"""
    out = ""
    for e in reversed(entries):
        for off in (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 26, 28, 30):
            ch = struct.unpack_from("<H", e, off)[0]
            if ch in (0, 0xFFFF):
                continue
            out += chr(ch)
    return out


class Fat32:
    def __init__(self, reader):
        mbr = reader.read_at(0, 512)
        if mbr[510:512] != b"\x55\xAA":
            raise ValueError("MBR 签名缺失")
        part = mbr[446:462]
        self.ptype = part[4]
        self.start_lba = struct.unpack_from("<I", part, 8)[0]
        boot = reader.read_at(self.start_lba * 512, 512)
        self.bps = struct.unpack_from("<H", boot, 11)[0]
        self.spc = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        num_fats = boot[16]
        spf = struct.unpack_from("<I", boot, 36)[0]
        self.root = struct.unpack_from("<I", boot, 44)[0]
        self.fat_start = self.start_lba + reserved
        self.data_start = self.fat_start + num_fats * spf
        self.r = reader

    def fat_entry(self, cluster):
        raw = self.r.read_at(self.fat_start * self.bps + cluster * 4, 4)
        return struct.unpack_from("<I", raw, 0)[0] & 0x0FFFFFFF

    def cluster_offset(self, cluster):
        return (self.data_start + (cluster - 2) * self.spc) * self.bps

    def chain_bytes(self, first, size):
        """按 FAT 簇链读取 size 字节（用于小文件）。"""
        out = bytearray()
        cluster, seen = first, set()
        while len(out) < size and 2 <= cluster < 0x0FFFFFF8:
            if cluster in seen:
                break
            seen.add(cluster)
            out.extend(self.r.read_at(self.cluster_offset(cluster), self.spc * self.bps))
            cluster = self.fat_entry(cluster)
        return bytes(out[:size])

    def chain_md5(self, first, size):
        """流式计算大文件 md5，不把整个 SYSTEM 装进内存。"""
        h = hashlib.md5()
        remain, cluster, seen, read = size, first, set(), 0
        while remain > 0 and 2 <= cluster < 0x0FFFFFF8:
            if cluster in seen:
                break
            seen.add(cluster)
            chunk = self.r.read_at(self.cluster_offset(cluster), min(self.spc * self.bps, remain))
            if not chunk:
                break
            h.update(chunk)
            remain -= len(chunk)
            read += len(chunk)
            cluster = self.fat_entry(cluster)
        return h.hexdigest(), read

    def dir(self, cluster):
        raw = self.chain_bytes(cluster, 64 << 20)
        i, lfn, out = 0, [], []
        while i + 32 <= len(raw):
            e = raw[i:i + 32]
            i += 32
            if e[0] == 0x00:
                break
            if e[0] == 0xE5:
                lfn = []
                continue
            attr = e[11]
            if attr == 0x0F:
                lfn.append(e)
                continue
            short = (e[0:8].decode("ascii", "replace").rstrip() + "." +
                     e[8:11].decode("ascii", "replace").rstrip()).rstrip(".")
            name = lfn_name(lfn) or short
            lfn = []
            first = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            out.append((name, bool(attr & 0x10), first, size))
        return out

    def files(self):
        return {n.lower(): (c, s) for n, d, c, s in self.dir(self.root) if not d}

    def dirs(self):
        return {n.lower(): c for n, d, c, s in self.dir(self.root) if d and n not in (".", "..")}


class Report:
    def __init__(self):
        self.items = []

    def add(self, code, ok, msg, note=""):
        state = "PASS" if ok else "FAIL"
        self.items.append((code, state, msg, note))
        print("  [%s] %-3s %s%s" % (state, code, msg, ("  -- " + note) if note else ""))

    def skip(self, code, msg, note=""):
        self.items.append((code, "SKIP", msg, note))
        print("  [SKIP] %-3s %s%s" % (code, msg, ("  -- " + note) if note else ""))

    def warn(self, code, msg, note=""):
        self.items.append((code, "WARN", msg, note))
        print("  [WARN] %-3s %s%s" % (code, msg, ("  -- " + note) if note else ""))

    def failed(self):
        return any(s == "FAIL" for _, s, _, _ in self.items)


def check_dtb(data, rep):
    root = fdt.parse_fdt(data)
    nodes = fdt.walk(root)
    bifrost = [(p, n) for p, n in nodes if "bifrost" in fdt.compat_of(n).lower()]
    midgard = [(p, n) for p, n in nodes if "midgard" in fdt.compat_of(n).lower()]
    bif_ok = bool(bifrost) and all(fdt.status_of(n).strip() == "okay" for _, n in bifrost)
    detail = "bifrost=%d(okay=%s), midgard=%d" % (
        len(bifrost), bif_ok, len(midgard))
    rep.add("T3", bif_ok, "DTB bifrost 节点已启用", detail)
    if midgard:
        mid_off = all(fdt.status_of(n).strip() == "disabled" for _, n in midgard)
        rep.add("T3b", mid_off, "DTB midgard 节点已禁用（A311D 为 Bifrost，Midgard 是错架构）")
    else:
        rep.warn("T3b", "DTB 无 midgard 节点", "与现网可用 DTB 一致，属正常情形")
    for p, n in bifrost + midgard:
        print("        %-38s status=%s  compatible=%s" %
              (p, fdt.status_of(n), fdt.compat_of(n)))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path, expect_label=None):
    is_gz = path.lower().endswith(".gz")
    print("=" * 76)
    print("镜像: %s" % path)
    print("大小: %d 字节  格式: %s" % (os.path.getsize(path), "gzip 压缩" if is_gz else "裸镜像"))
    rep = Report()
    reader = GzipLazy(path) if is_gz else FileReader(path)
    try:
        fat = Fat32(reader)
    except Exception as e:
        rep.add("T1", False, "启动分区可读", str(e))
        return rep, None, None
    rep.add("T1", True, "MBR/FAT32 启动分区可读",
            "ptype=0x%02X start_lba=%d bps=%d spc=%d" %
            (fat.ptype, fat.start_lba, fat.bps, fat.spc))
    files = fat.files()

    required = ("kernel.img", "system", "dtb.img", "config.ini", "cfgload")
    missing = [k for k in required if k not in files]
    rep.add("T2", not missing, "必需文件齐全",
            "缺失=%s" % (",".join(missing) if missing else "无"))
    if missing:
        return rep, fat, files

    check_dtb(fat.chain_bytes(*files["dtb.img"]), rep)

    cfg = fat.chain_bytes(*files["config.ini"]).decode("ascii", "replace")
    active = [l.strip() for l in cfg.splitlines()
              if re.match(r"\s*gpu_dtb\s*=", l)]
    rep.add("T4", not active, "CONFIG.INI 未启用悬空 gpu_dtb",
            "启用行=%s" % (active or "无"))

    load = fat.chain_bytes(*files["cfgload"]).decode("ascii", "replace")
    rep.add("T5", 'setenv dtbfile "dtb.img"' in load, "CFGLOAD 默认加载 dtb.img")

    if is_gz:
        rep.skip("T6", "KERNEL/SYSTEM md5 清单校验",
                 "压缩档请用同名 .img 校验，或先解压")
        rep.skip("T7", "SYSTEM squashfs 头", "SYSTEM 位于 ~1.4GB 偏移，避免解压整包")
        rep.skip("T8", "内核版本字符串", "压缩档需解压到 ~1.4GB 偏移，跳过")
    else:
        squash = fat.chain_bytes(files["system"][0], 96)
        rep.add("T7", squash[:4] == b"hsqs", "SYSTEM squashfs 头有效",
                "magic=%r block_size=%d comp_id=%d" %
                (squash[:4], struct.unpack_from("<I", squash, 12)[0],
                 struct.unpack_from("<H", squash, 20)[0]))
        all_ok = True
        for key, md5name in (("kernel.img", "kernel.img.md5"), ("system", "system.md5")):
            if md5name not in files:
                rep.add("T6", False, "md5 清单缺失 %s" % md5name)
                all_ok = False
                continue
            expect = fat.chain_bytes(*files[md5name]).decode("ascii", "replace").split()[0].lower()
            got, n = fat.chain_md5(*files[key])
            ok = (got == expect and n == files[key][1])
            all_ok &= ok
            print("        %-12s expect=%s got=%s bytes=%d/%d" %
                  (key, expect, got, n, files[key][1]))
            rep.add("T6", ok, "md5 清单一致: %s" % key)
        kernel = fat.chain_bytes(files["kernel.img"][0], min(files["kernel.img"][1], 32 << 20))
        m = re.search(rb"\b6\.\d+\.\d+\b", kernel)
        rep.add("T8", m is not None, "内核版本可读",
                ("version=%s header=%r" % (m.group().decode(), kernel[:8])) if m else "未找到")

    sha_file = path + ".sha256"
    if os.path.exists(sha_file):
        with open(sha_file, "r", encoding="ascii", errors="replace") as f:
            expect = f.read().split()[0].lower()
        got = sha256_of(path)
        rep.add("T9", got == expect, "压缩档 sha256 与清单一致",
                "expect=%s got=%s" % (expect[:16], got[:16]))
    else:
        rep.skip("T9", "sha256 清单校验", "同目录无 .sha256 文件")

    # T10 启动 label 一致性。initramfs 用 boot=LABEL=xxx 挂 /flash，挂不上
    # 会在 15 次重试后打印 "Unable to find LABEL=xxx" 并 poweroff（黑屏）。
    # 卷标 / cfgload / cfgload_env / kernel.img header cmdline 必须一致，
    # 且要与设备 U-Boot env 中已保存的基线一致。
    labels = {}
    bpb = reader.read_at(fat.start_lba * fat.bps + 71, 11)
    labels["FAT32 BPB"] = bpb.decode("ascii", "replace").strip() or "(空)"
    # The root-directory volume label entry (attr 0x08) is a SECOND label.
    # Windows SetVolumeLabelW updates only this entry, while Linux blkid
    # reads the BPB.  Both must match.
    root_label = "(无)"
    root_raw = fat.chain_bytes(fat.root, 512)
    i = 0
    while i + 32 <= len(root_raw):
        e = root_raw[i:i + 32]
        i += 32
        if e[0] == 0x00:
            break
        if e[11] == 0x08:
            root_label = e[0:11].decode("ascii", "replace").strip() or "(空)"
            break
    labels["FAT32 根目录项"] = root_label
    for key in ("cfgload", "cfgload_env"):
        if key in files:
            txt = fat.chain_bytes(*files[key]).decode("ascii", "replace")
            m = re.search(r"boot=LABEL=([A-Za-z0-9_-]+)", txt)
            labels[key] = m.group(1) if m else "(未找到)"
    if not is_gz and "kernel.img" in files:
        khead = fat.chain_bytes(files["kernel.img"][0], min(files["kernel.img"][1], 1024))
        cmd = khead[64:576].split(b"\x00")[0].decode("ascii", "replace")
        m = re.search(r"boot=LABEL=([A-Za-z0-9_-]+)", cmd)
        labels["kernel.img cmdline"] = m.group(1) if m else "(空)"
    for k, v in labels.items():
        print("        %-22s %s" % (k, v))
    unknown = ("(空)", "(未找到)", "(无)")
    known = set(v for v in labels.values() if v not in unknown)
    rep.add("T10", len(known) <= 1, "启动 label 内部一致",
            "取值=%s" % (",".join(sorted(known)) or "无"))
    rep.add("T10d",
            labels["FAT32 BPB"] == labels["FAT32 根目录项"],
            "BPB 与根目录卷标项一致",
            "BPB=%s 目录项=%s" % (labels["FAT32 BPB"],
                                  labels["FAT32 根目录项"]))
    if expect_label:
        bad = [k for k, v in labels.items()
               if v not in unknown and v.upper() != expect_label.upper()]
        rep.add("T10b", not bad, "启动 label 符合基线 %s" % expect_label,
                "不符项=%s" % (",".join(bad) or "无"))
    if labels.get("kernel.img cmdline") == "(空)":
        rep.warn("T10c", "kernel.img Android header 的 cmdline 为空",
                 "启动完全依赖 U-Boot env；建议写入 boot=LABEL=<卷标> 兜底")

    for name, cluster in fat.dirs().items():
        entries = [e[0] for e in fat.dir(cluster) if e[0] and e[0] not in (".", "..")]
        if entries:
            print("        目录 /%s: %s" % (name, ", ".join(entries)))
    if isinstance(reader, GzipLazy):
        print("        惰性解压缓存: %.1f MB（未全量解压）" % reader.cached_mb())
    return rep, fat, files


def main():
    args = sys.argv[1:]
    expect_label = None
    if "--expect-label" in args:
        i = args.index("--expect-label")
        if i + 1 >= len(args):
            print("--expect-label 缺少参数值")
            return 2
        expect_label = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1:
        print("用法: python3 tools/a311d_image_verify.py "
              "<image.img|image.img.gz> [--expect-label <LABEL>]")
        return 2
    path = args[0]
    if not os.path.isfile(path):
        print("文件不存在: %s" % path)
        return 2
    rep, _, _ = verify(path, expect_label)
    print("=" * 76)
    if rep.failed():
        print("结论: FAIL —— 存在阻塞项，禁止刷盘。")
        return 1
    print("结论: 离线检查全部通过，可以刷盘。")
    print("注意: 本机未做板级验证；首启前清 /storage 残留，实机复核 UI_SERVICE、")
    print("      gpu.driver、uname -r 与 /lib/modules 及 vermagic 一致性、/dev/dri。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
