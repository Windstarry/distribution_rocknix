#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A311D DTB 离线核查工具（纯标准库，无外部依赖）。

用途：
  1. 解析独立的 .dtb 文件（FDT v16/17）。
  2. 从 ROCKNIX/AURKNIX 的 .img 启动分区（MBR + FAT32）里抽取 dtb.img 再解析。
  3. 列出所有 GPU 相关节点（compatible 含 mali / 节点名含 gpu 或 mali），
     并打印其 status、compatible、reg 基址、interrupts 等关键信息。

只做只读检查，不修改任何文件。
"""
import sys
import struct

FDT_MAGIC = 0xD00DFEED
FDT_BEGIN_NODE = 0x1
FDT_END_NODE = 0x2
FDT_PROP = 0x3
FDT_NOP = 0x4
FDT_END = 0x9


def read_be32(b, off):
    return struct.unpack_from(">I", b, off)[0]


def parse_fdt(data):
    """解析 FDT 二进制，返回根节点 dict。"""
    if len(data) < 40 or read_be32(data, 0) != FDT_MAGIC:
        raise ValueError("不是有效的 FDT（magic 不匹配）")
    off_dt_struct = read_be32(data, 8)
    off_dt_strings = read_be32(data, 12)
    off_mem_rsvmap = read_be32(data, 16)
    size_dt_strings = read_be32(data, 32)

    def get_string(off):
        end = data.index(b"\x00", off_dt_strings + off)
        return data[off_dt_strings + off:end].decode("ascii", "replace")

    pos = off_dt_struct
    end_struct = off_dt_struct + read_be32(data, 36)  # size_dt_struct
    root = {"#name": "", "children": {}, "props": {}}
    stack = [root]

    while pos < end_struct:
        tok = read_be32(data, pos)
        if tok == FDT_BEGIN_NODE:
            pos += 4
            nul = data.index(b"\x00", pos)
            name = data[pos:nul].decode("ascii", "replace")
            pos = (nul + 4) & ~3
            node = {"#name": name, "children": {}, "props": {}}
            stack[-1]["children"][name] = node
            stack.append(node)
        elif tok == FDT_END_NODE:
            pos += 4
            stack.pop()
        elif tok == FDT_PROP:
            pos += 4
            plen = read_be32(data, pos); nameoff = read_be32(data, pos + 4)
            pos += 8
            val = data[pos:pos + plen]
            pos += (plen + 3) & ~3
            pname = get_string(nameoff)
            stack[-1]["props"][pname] = val
        elif tok == FDT_NOP:
            pos += 4
        elif tok == FDT_END:
            break
        else:
            raise ValueError("未知的 FDT token 0x%X @ %d" % (tok, pos))

    return root


def node_path(node, parent=""):
    return (parent + "/" + node["#name"]) if node["#name"] else parent


def walk(node, parent="", out=None):
    if out is None:
        out = []
    for name, child in node["children"].items():
        p = node_path(child, parent)
        out.append((p, child))
        walk(child, p, out)
    return out


def status_of(node):
    if "status" not in node["props"]:
        return "(absent -> 默认 okay)"
    return node["props"]["status"].split(b"\x00")[0].decode("ascii", "replace")


def compat_of(node):
    if "compatible" not in node["props"]:
        return ""
    return node["props"]["compatible"].replace(b"\x00", b",").decode("ascii", "replace").rstrip(",")


def is_gpu_node(path, node):
    c = compat_of(node).lower()
    p = path.lower()
    return ("mali" in c) or ("gpu" in p) or ("mali" in p)


def reg_summary(node):
    if "reg" not in node["props"]:
        return ""
    r = node["props"]["reg"]
    # 仅打印前若干个 64 位地址（u64 pair: addr, size）
    words = len(r) // 4
    parts = []
    for i in range(0, min(words, 8), 2):
        addr = struct.unpack_from(">I", r, i * 4)[0]
        nxt = struct.unpack_from(">I", r, (i + 1) * 4)[0]
        val = (addr << 32) | nxt if False else (addr * 0x100000000 + nxt)
        parts.append("0x%X" % val)
    return " ".join(parts)


def report_dtb(label, data):
    print("=" * 70)
    print("DTB 源: %s" % label)
    print("大小: %d 字节  magic: 0x%08X" % (len(data), read_be32(data, 0) if len(data) >= 4 else 0))
    try:
        root = parse_fdt(data)
    except Exception as e:
        print("  解析失败: %s" % e)
        return
    nodes = walk(root)
    gpus = [(p, n) for (p, n) in nodes if is_gpu_node(p, n)]
    if not gpus:
        print("  未发现任何 GPU / mali 节点。")
        return
    for p, n in gpus:
        print("  节点: %s" % p)
        print("    compatible : %s" % compat_of(n))
        print("    status     : %s" % status_of(n))
        print("    reg        : %s" % reg_summary(n))
        if "interrupts" in n["props"]:
            print("    interrupts : %d 字节" % len(n["props"]["interrupts"]))
    # 额外：检查是否有双节点（bifrost 与 midgard 并存）
    bifrost = [p for (p, n) in gpus if "bifrost" in compat_of(n).lower()]
    midgard = [p for (p, n) in gpus if "midgard" in compat_of(n).lower()]
    print("  小结: bifrost 节点=%d, midgard 节点=%d" % (len(bifrost), len(midgard)))


# ---------------- 从 .img (MBR + FAT32) 抽取 dtb.img ----------------
def extract_dtb_from_img(path):
    with open(path, "rb") as f:
        mbr = f.read(512)
        if mbr[510:512] != b"\x55\xAA":
            raise ValueError("不是有效 MBR（签名缺失）")
        # 分区表起始 446，每项 16 字节
        part0 = mbr[446:462]
        ptype = part0[4]
        start_lba = struct.unpack_from("<I", part0, 8)[0]
        bps = 512
        f.seek(start_lba * bps)
        boot = f.read(bps)
        if boot[0:3] != b"\xEB\x58\x90" and boot[0:3] != b"\xEB\x00\x00":
            # 可能 boot 标记不同，FAT32 一般 EB 58 90
            pass
        bps = struct.unpack_from("<H", boot, 11)[0]
        spc = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        num_fats = boot[16]
        spf = struct.unpack_from("<I", boot, 36)[0]
        root_cluster = struct.unpack_from("<I", boot, 44)[0]
        fat_start = start_lba + reserved
        data_start = start_lba + reserved + num_fats * spf

        def cluster_sector(c):
            return data_start + (c - 2) * spc

        def read_cluster(c):
            f.seek(cluster_sector(c) * bps)
            return f.read(spc * bps)

        def fat_entry(c):
            off = fat_start * bps + c * 4
            f.seek(off)
            v = struct.unpack_from("<I", f.read(4), 0)[0]
            return v & 0x0FFFFFFF

        def read_chain(first):
            chain = []
            c = first
            while c and c < 0x0FFFFFF8 and c >= 2:
                chain.append(c)
                c = fat_entry(c)
            data = b"".join(read_cluster(x) for x in chain)
            return data

        def parse_lfn(entries):
            name = ""
            for e in reversed(entries):
                for i in (1, 3, 5, 7, 9):
                    ch = struct.unpack_from("<H", e, i)[0]
                    if ch == 0xFFFF:
                        ch = 0
                    if ch:
                        name += chr(ch)
                for i in (14, 16):
                    ch = struct.unpack_from("<H", e, i)[0]
                    if ch == 0xFFFF:
                        ch = 0
                    if ch:
                        name += chr(ch)
            return name

        def walk_dir(cluster, depth=0):
            raw = read_chain(cluster)
            # 按 32 字节目录项解析
            entries = []
            i = 0
            lfn = []
            while i + 32 <= len(raw):
                e = raw[i:i + 32]
                i += 32
                attr = e[11]
                if e[0] == 0x00:
                    break
                if e[0] == 0xE5:
                    lfn = []
                    continue
                if attr == 0x0F:
                    lfn.append(e)
                    continue
                # 普通目录项
                name8 = e[0:8].decode("ascii", "replace").rstrip(" ")
                ext = e[8:11].decode("ascii", "replace").rstrip(" ")
                short = (name8 + ("." + ext if ext else "")).upper()
                longname = parse_lfn(lfn) if lfn else ""
                lfn = []
                fname = longname if longname else short
                first = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
                size = struct.unpack_from("<I", e, 28)[0]
                is_dir = bool(attr & 0x10)
                entries.append((fname, is_dir, first, size))
            return entries

        # 根目录从 root_cluster 开始
        found = None
        stack = [("ROOT", root_cluster)]
        while stack:
            label, cl = stack.pop()
            for fname, is_dir, first, size in walk_dir(cl):
                if fname.lower() == "dtb.img":
                    found = (first, size)
                    break
                if is_dir and fname not in (".", ".."):
                    stack.append((fname, first))
            if found:
                break
        if not found:
            raise ValueError("FAT32 分区未找到 dtb.img")
        first, size = found
        raw = read_chain(first)[:size]
        return raw


def main():
    targets = sys.argv[1:]
    for t in targets:
        if t.lower().endswith(".img"):
            try:
                print(">>> 尝试从镜像抽取 dtb.img: %s" % t)
                dtb = extract_dtb_from_img(t)
                report_dtb("镜像内 dtb.img (%s)" % t, dtb)
            except Exception as e:
                print("  抽取/解析失败: %s" % e)
        else:
            with open(t, "rb") as f:
                report_dtb(t, f.read())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 a311d_dtb_inspect.py <file.dtb|file.img> [...]")
        sys.exit(1)
    main()
