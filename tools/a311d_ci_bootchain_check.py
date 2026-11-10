#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A311D 阶段 B 引导链固化核查（CI 防回归闸门）。

把「20261109 能进系统」的六项特征变成可执行判据，防止 CI 完整重建时
再次把旧启动链带回来。纯标准库 + 同目录既有解析器，只读，不改任何文件。

用法
----
  # 静态核查仓库源码与 bootkit（秒级，适合 PR / push 闸门）
  python3 tools/a311d_ci_bootchain_check.py --repo .

  # 核查已构建的镜像（CI 出图后）
  python3 tools/a311d_ci_bootchain_check.py --img path/to/AURKNIX-A311D...img[.gz]

  # 两种都跑 + 输出 JSON
  python3 tools/a311d_ci_bootchain_check.py --repo . --img xxx.img --json report.json

退出码
------
  0  全部 PASS（WARN/INFO 不阻断）
  1  存在 FAIL
  2  用法或环境错误

六项特征
--------
  1 cmdline 完整          2 ramdisk_size=0 / 内建 initramfs
  3 经典 cfgload / aml_autoscript（无 ceboot）
  4 实机 54,984 B dtb.img
  5 卷标 ROCKNIX
  6 不引入 ceboot / cfgload_env

证据来源：tools/a311d_aurknix_runnable_proof_20261110.txt
          活机 192.168.31.7 (2026-11-10) / 本地 20261109-bootsafe-edidmode.img
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# 实机验证基线（三方一致：活机 /flash == GOLD .img == bootkit）
# ---------------------------------------------------------------------------
EXPECT = {
    "cfgload": {
        "size": 497,
        "md5": "1a3a799f88b29c2843106f7d971de372",
        "sha256": "8ccc28b0ec3fce55602fcded9c846b1a34e7913211dca840908e2c9f079a0e44",
    },
    "aml_autoscript": {
        "size": 1466,
        "md5": "21e3fb29c6dd0f0e20efcffc7b3ac31f",
        "sha256": "9c0f8248c0eeaace996726252ac4f54a413655121d238e617738b2af5191ddcc",
    },
    "dtb.img": {
        "size": 54984,
        "md5": "0fee9ffd2a2f3cb4a891b71176e3ee6a",
        "sha256": "90e42d53e8fc090fdfc74a69fa913c417dee818b033343c7cb804af41e89c5a9",
    },
}
GOLD_CMDLINE = (
    "boot=LABEL=ROCKNIX disk=LABEL=STORAGE console=ttyAML0,115200n8 "
    "console=tty0 no_console_suspend net.ifnames=0 "
    "video=HDMI-A-1:D systemd.debug_shell=ttyAML0"
)
UIMAGE_MAGIC = 0x27051956

DEV = "projects/ROCKNIX/devices/A311D"
BOOTKIT = DEV + "/bootloader/bootkit"
LINUX_MK = "projects/ROCKNIX/packages/linux/package.mk"
UBOOT_MK = DEV + "/packages/u-boot/package.mk"
PROJECT_MKIMAGE = "projects/ROCKNIX/bootloader/mkimage"


class Result:
    def __init__(self):
        self.items = []

    def add(self, level, code, msg, note=""):
        self.items.append({"level": level, "code": code, "msg": msg, "note": note})

    def ok(self, code, msg, note=""):
        self.add("PASS", code, msg, note)

    def bad(self, code, msg, note=""):
        self.add("FAIL", code, msg, note)

    def warn(self, code, msg, note=""):
        self.add("WARN", code, msg, note)

    def info(self, code, msg, note=""):
        self.add("INFO", code, msg, note)

    def failed(self):
        return any(x["level"] == "FAIL" for x in self.items)

    def render(self):
        icons = {"PASS": "  OK ", "FAIL": "FAIL ", "WARN": "WARN ", "INFO": "INFO "}
        out = []
        for x in self.items:
            out.append("%s %-6s %s" % (icons[x["level"]], x["code"], x["msg"]))
            if x["note"]:
                for line in str(x["note"]).splitlines():
                    out.append("           " + line)
        n = {k: sum(1 for x in self.items if x["level"] == k)
             for k in ("PASS", "FAIL", "WARN", "INFO")}
        out.append("-" * 78)
        out.append("汇总: %d PASS / %d FAIL / %d WARN / %d INFO" %
                   (n["PASS"], n["FAIL"], n["WARN"], n["INFO"]))
        return "\n".join(out)


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def uimage_script(data):
    """返回 (header, payload, text)；非 uImage script 返回 None。"""
    if len(data) < 72 or struct.unpack_from(">I", data, 0)[0] != UIMAGE_MAGIC:
        return None
    hcrc, ts, dsize, load, ep, dcrc = struct.unpack_from(">6I", data, 4)
    os_, arch, typ, comp = struct.unpack_from(">4B", data, 28)
    payload = data[64:64 + dsize]
    text = payload[8:].decode("utf-8", "replace") if len(payload) > 8 else ""
    return ({"hcrc": hcrc, "ts": ts, "dsize": dsize, "type": typ,
             "dcrc": dcrc, "declared_len": struct.unpack_from(">I", data, 64)[0]},
            payload, text)


def bootargs_from(text):
    m = re.search(r"setenv\s+bootargs\s+'([^']*)'", text)
    return m.group(1) if m else None


def option_value(text, key):
    m = re.search(r'^\s*%s="([^"]*)"' % re.escape(key), text, re.M)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# 静态核查
# ---------------------------------------------------------------------------
def check_repo(root, r):
    p = lambda x: os.path.join(root, x)

    r.info("S00", "静态核查仓库：%s" % os.path.abspath(root))

    # --- S1 bootkit 三件套 ---
    for name, exp in EXPECT.items():
        fp = p(BOOTKIT + "/" + name)
        if not os.path.isfile(fp):
            r.bad("S1", "bootkit/%s 缺失" % name, fp)
            continue
        d = read_bytes(fp)
        md5, sha, size = (hashlib.md5(d).hexdigest(),
                          hashlib.sha256(d).hexdigest(), len(d))
        if (size, md5, sha) != (exp["size"], exp["md5"], exp["sha256"]):
            r.bad("S1", "bootkit/%s 与实机验证字节不符" % name,
                  "size=%d (期望 %d)\nmd5=%s (期望 %s)\nsha256=%s" %
                  (size, exp["size"], md5, exp["md5"], sha))
        else:
            r.ok("S1", "bootkit/%-16s %6d B  md5 与活机 /flash 一致" % (name, size))

    # --- S2 U-Boot script 形态 + 无 ceboot ---
    scripts = {}
    for name in ("cfgload", "aml_autoscript"):
        fp = p(BOOTKIT + "/" + name)
        if not os.path.isfile(fp):
            continue
        u = uimage_script(read_bytes(fp))
        if not u:
            r.bad("S2", "bootkit/%s 不是 uImage script" % name)
            continue
        hdr, payload, text = u
        scripts[name] = text
        if hdr["type"] != 6:
            r.bad("S2", "%s uImage type=%d，期望 6 (script)" % (name, hdr["type"]))
        elif "ceboot" in text or "cfgload_env" in text:
            r.bad("S2", "%s 含 ceboot / cfgload_env，不是经典链" % name)
        else:
            r.ok("S2", "%-16s uImage script type=6，无 ceboot" % name)
        # 8 字节长度前缀：cfgload 因 11-09 EDID 等长修正而历史性不一致
        if hdr["declared_len"] == len(text):
            r.ok("S2", "%-16s 长度前缀 %d 与文本一致" % (name, len(text)))
        elif name == "cfgload":
            r.warn("S2", "cfgload 长度前缀 %d != 文本 %d（EDID 修正遗留）" %
                   (hdr["declared_len"], len(text)),
                   "11-09 把 1920x1080@60 换成 D，文件 508->497 B，uImage "
                   "dsize/dcrc 未重算；Amlogic 2015.01 不校验且实机启动正常，保持原样。")
        else:
            r.bad("S2", "%s 长度前缀 %d != 文本 %d" %
                  (name, hdr["declared_len"], len(text)))

    # --- S3/S4 options: 卷标与 cmdline 单一数据源 ---
    op = p(DEV + "/options")
    if not os.path.isfile(op):
        r.bad("S4", "devices/A311D/options 缺失")
        optext = ""
    else:
        optext = read_text(op)
        label = option_value(optext, "DISTRO_BOOTLABEL")
        cmd = option_value(optext, "BOOTIMG_CMDLINE")
        if label != "ROCKNIX":
            r.bad("S4", "DISTRO_BOOTLABEL=%r，期望 ROCKNIX" % label)
        else:
            r.ok("S4", "DISTRO_BOOTLABEL=ROCKNIX（设备级覆盖发行版 AURKNIX）")
        if cmd is None:
            r.bad("S4", "BOOTIMG_CMDLINE 未定义")
        elif cmd != GOLD_CMDLINE:
            r.bad("S4", "BOOTIMG_CMDLINE 与实机验证串不符",
                  "got : %r\nwant: %r" % (cmd, GOLD_CMDLINE))
        else:
            r.ok("S4", "BOOTIMG_CMDLINE 与实机 /proc/cmdline 尾部一致 (%d B)" % len(cmd))
        ba = bootargs_from(scripts.get("cfgload", ""))
        if ba is None:
            r.bad("S3", "cfgload 中未找到 setenv bootargs")
        elif cmd and ba != cmd:
            r.bad("S3", "cfgload bootargs 与 BOOTIMG_CMDLINE 不一致",
                  "cfgload: %r\noptions: %r" % (ba, cmd))
        else:
            r.ok("S3", "cfgload bootargs 与 BOOTIMG_CMDLINE 逐字一致")
        for token in ("boot=LABEL=ROCKNIX", "disk=LABEL=STORAGE", "video=HDMI-A-1:D"):
            if cmd and token not in cmd:
                r.bad("S4", "BOOTIMG_CMDLINE 缺少 %s" % token)

    # --- S5 黄金 DTB 的 GPU 节点 ---
    dp = p(BOOTKIT + "/dtb.img")
    if os.path.isfile(dp):
        d = read_bytes(dp)
        try:
            import a311d_dtb_inspect as I
            nodes = I.walk(I.parse_fdt(d))
            gpu = [(path, n) for path, n in nodes
                   if path == "/soc/gpu@ffe40000"]
            if not gpu:
                r.bad("S5", "dtb.img 中无 /soc/gpu@ffe40000")
            else:
                path, node = gpu[0]
                compat, status = I.compat_of(node), I.status_of(node)
                if "arm,mali-bifrost" not in compat:
                    r.bad("S5", "GPU compatible 不含 arm,mali-bifrost", compat)
                elif status.startswith("(absent") or status == "okay":
                    r.ok("S5", "dtb.img 54,984 B，Bifrost GPU 节点默认 okay（双栈可用）")
                else:
                    r.bad("S5", "GPU status=%s（会阻断 libmali/panfrost）" % status)
        except Exception as e:
            r.bad("S5", "dtb.img FDT 解析失败：%s" % e)

    # --- S6 mkbootimg：无外部 ramdisk + 完整 cmdline + 自检 ---
    mp = p(DEV + "/bootloader/mkbootimg")
    if os.path.isfile(mp):
        t = read_text(mp)
        # 只看代码行：注释里出现 "--ramdisk" 是说明文字，不是实际参数。
        code = "\n".join(l for l in t.splitlines()
                         if not l.lstrip().startswith("#"))
        if re.search(r"--ramdisk\b", code):
            r.bad("S6", "bootloader/mkbootimg 仍带 --ramdisk（会产生 ramdisk_size>0）")
        else:
            r.ok("S6", "mkbootimg 不带 --ramdisk（ramdisk_size=0 内建 initramfs）")
        if '--cmdline "${BOOTIMG_CMDLINE}"' not in t:
            r.bad("S6", "mkbootimg 未把 BOOTIMG_CMDLINE 写入 header")
        else:
            r.ok("S6", "mkbootimg 写入完整 header cmdline")
        if "ramdisk_size != 0" not in t or 'head[:8] != b"ANDROID!"' not in t:
            r.bad("S6", "mkbootimg 缺少 boot.img 形态自检")
        else:
            r.ok("S6", "mkbootimg 内置 ramdisk_size / cmdline 自检（构建期即失败）")

    # --- S7 linux/package.mk：内建 initramfs、不装本地 DTB ---
    lp = p(LINUX_MK)
    if os.path.isfile(lp):
        t = read_text(lp)
        # 精确判据：紧跟在「清空 INITRAMFS_SOURCE」前面的 if 条件只能含 S905。
        # makeinstall_target 里 "S905 -o A311D" 的合并分支是合法的（两者都要
        # 复制 boot.img 到 .image），不能一概判失败。
        m = re.search(
            r'if \[ ([^\]]*?) \]; then\s*\n\s*'
            r'\$\{PKG_BUILD\}/scripts/config --set-str CONFIG_INITRAMFS_SOURCE ""', t)
        if not m:
            r.bad("S7", "pre_make_target 未把「清空 INITRAMFS_SOURCE」限制为仅 S905")
        elif "A311D" in m.group(1):
            r.bad("S7", "清空 INITRAMFS_SOURCE 的条件仍包含 A311D", m.group(1))
        else:
            r.ok("S7", "A311D 走内建 initramfs，仅 S905 保留外部 ramdisk")
        m = re.search(r'\(cd \$\{BUILD\}/initramfs && find \. \| cpio[^)]*\)', t)
        if m:
            before = t[:m.start()]
            if before.rfind('if [ "${DEVICE}" = "S905" ]; then') > before.rfind("fi"):
                r.ok("S7", "外部 cpio 生成位于 S905 分支内，A311D 不生成")
            else:
                r.bad("S7", "外部 cpio 生成未限定在 S905 分支内")
        else:
            r.warn("S7", "未定位到 cpio 生成语句（构建脚本可能被重构，请人工确认）")
        a311d_dt = re.search(r'elif \[ "\$\{DEVICE\}" = "A311D" \]; then(.*?)\n    else', t, re.S)
        if a311d_dt and "cp -v ${dtb}" in a311d_dt.group(1):
            r.bad("S7", "A311D 仍安装本地编译 DTB，会覆盖黄金 dtb.img")
        else:
            r.ok("S7", "A311D 不安装本地编译 DTB（由 bootkit 提供）")

    # --- S8/S9 u-boot 包、.src、ceboot 守卫 ---
    up = p(UBOOT_MK)
    if os.path.isfile(up):
        t = read_text(up)
        if "> Generic_cfgload_env" in t:
            r.bad("S8", "u-boot/package.mk 仍生成 Generic_cfgload_env (ceboot)")
        else:
            r.ok("S8", "u-boot/package.mk 不生成 ceboot / cfgload_env")
        if "bootkit" not in t:
            r.bad("S8", "u-boot/package.mk 未从 bootkit 安装引导链")
        else:
            r.ok("S8", "u-boot/package.mk 直接从 bootkit 复制验证过的字节")
        if "device_trees/meson-g12b-a311d-cainiao-cniot-core.dtb" not in t:
            r.bad("S8", "未安装与 dtb.img 同源的 device_trees dtb（OTA cmp 会失败）")
        else:
            r.ok("S8", "device_trees 与 dtb.img 同源，OTA cmp 可通过")

    sdir = p(DEV + "/bootloader/scripts")
    leftovers = [f for f in os.listdir(sdir) if f.endswith(".src")] if os.path.isdir(sdir) else []
    if leftovers:
        r.bad("S9", "仍有停用的 .src：%s" % ", ".join(leftovers))
    else:
        r.ok("S9", "停用的 Generic_cfgload.src / aml_autoscript.src 已移除")

    pm = p(PROJECT_MKIMAGE)
    if os.path.isfile(pm):
        t = read_text(pm)
        if '${DEVICE}" != "A311D"' in t and "Generic_cfgload_env" in t:
            r.ok("S9", "通用 mkimage_aml_boot 对 A311D 有 cfgload_env 排除守卫")
        else:
            r.bad("S9", "通用 mkimage 缺少 A311D cfgload_env 排除守卫")
    ush = p(DEV + "/bootloader/update.sh")
    if os.path.isfile(ush):
        t = read_text(ush)
        if "cp -p $SYSTEM_ROOT/usr/share/bootloader/Generic_cfgload_env" in t:
            r.bad("S9", "update.sh 仍会复制 cfgload_env")
        else:
            r.ok("S9", "update.sh 不复制 cfgload_env（并清理旧文件）")

    # --- S10 release ---
    rp = p(DEV + "/bootloader/release")
    if os.path.isfile(rp):
        t = read_text(rp)
        if "cp -v \"${DSTDIR}/device_trees/${DEFAULT_DTB}\" \"${DSTDIR}/dtb.img\"" in t:
            r.bad("S10", "release 仍用本地 dtb 覆盖 dtb.img")
        elif "cmp -s" not in t:
            r.bad("S10", "release 缺少 dtb.img / device_trees 同源校验")
        else:
            r.ok("S10", "release 固定 bootkit dtb.img 并做同源 cmp")

    # --- S11 S905 回归保护 ---
    s9 = p("projects/ROCKNIX/devices/S905/bootloader/mkbootimg")
    if os.path.isfile(s9) and "--ramdisk" in read_text(s9):
        r.ok("S11", "S905 mkbootimg 未被波及，仍使用外部 zstd ramdisk")
    else:
        r.bad("S11", "S905 mkbootimg 疑似被改动，需回归检查")


# ---------------------------------------------------------------------------
# 镜像核查
# ---------------------------------------------------------------------------
def check_img(path, root, r, skip_integrity=False):
    if not os.path.isfile(path):
        r.bad("I0", "镜像不存在：%s" % path)
        return
    try:
        from a311d_image_verify import Fat32, FileReader, GzipLazy
    except Exception as e:
        r.bad("I0", "无法导入 FAT 解析器：%s" % e)
        return
    reader = GzipLazy(path) if path.lower().endswith(".gz") else FileReader(path)
    try:
        fs = Fat32(reader)
    except Exception as e:
        r.bad("I0", "镜像 FAT32 解析失败：%s" % e)
        return
    r.info("I00", "镜像核查：%s" % os.path.abspath(path))
    boot = fs.r.read_at(fs.start_lba * 512, 512)
    label = boot[71:82].decode("latin1", "replace").strip()
    if label != "ROCKNIX":
        r.bad("I1", "BPB 卷标=%r，期望 ROCKNIX" % label)
    else:
        r.ok("I1", "FAT32 BPB 卷标 = ROCKNIX")
    # 根目录卷标项
    vol = None
    try:
        raw = fs.chain_bytes(fs.root, 64 << 20)
        for i in range(0, min(len(raw), 32 * 64), 32):
            e = raw[i:i + 32]
            if e[0] == 0:
                break
            if e[11] == 0x08:
                vol = e[0:11].decode("ascii", "replace").strip()
                break
    except Exception:
        pass
    if vol and vol != "ROCKNIX":
        r.bad("I1", "根目录卷标项=%r 与 BPB 不一致" % vol)
    elif vol:
        r.ok("I1", "根目录卷标项与 BPB 一致")

    files = fs.files()
    for name, exp in EXPECT.items():
        if name not in files:
            r.bad("I3", "镜像内缺少 %s" % name)
            continue
        c, size = files[name]
        d = fs.chain_bytes(c, size)
        md5 = hashlib.md5(d).hexdigest()
        if (size, md5) != (exp["size"], exp["md5"]):
            r.bad("I3", "镜像内 %-16s size=%d md5=%s 与 bootkit 不符" % (name, size, md5))
        else:
            r.ok("I3", "镜像内 %-16s %6d B 与实机验证字节一致" % (name, size))
    if "cfgload_env" in files:
        r.bad("I4", "镜像含 cfgload_env（ceboot 链残留）")
    else:
        r.ok("I4", "镜像不含 cfgload_env")
    if "aml_autoscript" in files:
        c, size = files["aml_autoscript"]
        u = uimage_script(fs.chain_bytes(c, size))
        if u and ("ceboot" in u[2] or "cfgload_env" in u[2]):
            r.bad("I4", "aml_autoscript payload 含 ceboot")
        elif u:
            r.ok("I4", "aml_autoscript 为经典链（autoscr，无 ceboot）")
    if "config.ini" in files:
        r.info("I4", "镜像含 config.ini；黄金 cfgload 不读取它，无启动影响",
               "实机 20261109 /flash 无此文件，属 CI 兼容配置，非阻断差异。")

    # kernel.img Android header
    if "kernel.img" not in files:
        r.bad("I5", "镜像内缺少 kernel.img")
    else:
        c, size = files["kernel.img"]
        h = fs.chain_bytes(c, min(size, 2048))
        if h[:8] != b"ANDROID!":
            r.bad("I5", "kernel.img 不是 Android boot image（magic=%r）" % h[:8])
        else:
            rs = struct.unpack_from("<I", h, 16)[0]
            cmd = h[64:576].split(b"\x00")[0].decode("ascii", "replace")
            if rs != 0:
                r.bad("I5", "ramdisk_size=%d，期望 0（内建 initramfs）" % rs)
            else:
                r.ok("I5", "kernel.img ramdisk_size=0（内建 initramfs）")
            if not cmd:
                r.bad("I6", "kernel.img header cmdline 为空")
            elif cmd != GOLD_CMDLINE:
                r.bad("I6", "header cmdline 与实机验证串不符",
                      "got : %r\nwant: %r" % (cmd, GOLD_CMDLINE))
            else:
                r.ok("I6", "kernel.img header cmdline 完整且与 cfgload 一致 (%d B)" % len(cmd))

    # md5 记录一致性
    if skip_integrity:
        r.info("I7", "已跳过 SYSTEM/kernel md5 全量校验（--skip-integrity）",
               "用于 CI 的 .img.gz 按需解压模式，避免 2.2 GB gzip 流全量驻留内存；"
               "镜像完整性由构建脚本与 .sha256 保证。")
    else:
        for target, rec in (("kernel.img", "kernel.img.md5"), ("system", "system.md5")):
            if target not in files or rec not in files:
                continue
            c, size = files[target]
            h = hashlib.md5()
            remain, cluster, seen = size, c, set()
            while remain > 0 and 2 <= cluster < 0x0FFFFFF8 and cluster not in seen:
                seen.add(cluster)
                chunk = fs.r.read_at(fs.cluster_offset(cluster),
                                     min(fs.spc * fs.bps, remain))
                if not chunk:
                    break
                h.update(chunk)
                remain -= len(chunk)
                cluster = fs.fat_entry(cluster)
            cur = h.hexdigest() if remain == 0 else "<short read>"
            rc, rs = files[rec]
            ref = fs.chain_bytes(rc, rs).decode("ascii", "replace").split()[0] \
                if rs else ""
            if cur == ref:
                r.ok("I7", "%-12s md5 与 %s 记录一致" % (target, rec))
            else:
                r.bad("I7", "%-12s md5=%s，记录=%s" % (target, cur, ref))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="静态核查仓库根目录")
    ap.add_argument("--img", help="核查已构建的 .img / .img.gz")
    ap.add_argument("--json", help="把结果另存为 JSON")
    ap.add_argument("--skip-integrity", action="store_true",
                    help="跳过 SYSTEM/kernel md5 全量校验（CI 的 .img.gz 按需解压模式）")
    a = ap.parse_args()
    if not a.repo and not a.img:
        ap.error("至少指定 --repo 或 --img")

    r = Result()
    print("=" * 78)
    print("A311D 阶段 B 引导链固化核查（cmdline / ramdisk / 经典链 / dtb / 卷标 / 无 ceboot）")
    print("=" * 78)
    if a.repo:
        check_repo(a.repo, r)
    if a.img:
        check_img(a.img, a.repo or ".", r, skip_integrity=a.skip_integrity)
    print(r.render())
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"items": r.items, "failed": r.failed()}, f,
                      ensure_ascii=False, indent=2)
        print("JSON -> %s" % os.path.abspath(a.json))
    return 1 if r.failed() else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("ERROR: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(2)
