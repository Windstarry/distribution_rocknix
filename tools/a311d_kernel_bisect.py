#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A311D 启动盘单变量实验工具（kernel.img 可逆替换）

背景
----
20261111 新构建刷到 CBM2199 (048d:1234) 盘上上电仍报
"Unable to find LABEL=ROCKNIX"。同期另一块 Kingston (346d:5678) 盘装
20261109 GOLD 镜像可正常进系统。两块盘的 U-Boot 脚本、dtb.img、卷标、
cmdline、ramdisk_size 全部字节级一致，唯一实质差异是 kernel.img
（6.18.21 vs 6.18.49，各带自己的内建 initramfs）与 SYSTEM。

"内核版本" 与 "启动盘" 两个变量始终共线，必须做单变量实验才能归因。
本工具固定启动盘，只替换 kernel.img，把变量收敛为一个。

2026-11-11 22:14 实验 A 已执行完毕（判读 A2）
--------------------------------------------
固定 F 盘（048d:1234 CBM2199），仅把 G 盘 6.18.49 kernel.img 换过去，
上电后不再报 "Unable to find LABEL=ROCKNIX"，进入系统初始化界面。
结论：盘侧不兼容已排除；阶段 B 六特征不是故障源；故障收敛到
20261111 的 6.18.21 kernel.img 内容差异（内核本体 + 内建 initramfs
作为整体，尚未再分离）。不得据此写「已修复」或「前端已启动」。
详见 tools/a311d_experiment_a_result_20261111.txt。
本工具仍保留用于还原（--restore）与后续交叉实验。

功能
----
  --status    只读显示两盘卷标 / kernel.img md5 / Android 头 / 内核版本串
  --backup    把目标盘的 kernel.img 与 kernel.img.md5 备份到本地目录
  --apply     把源盘的 kernel.img 复制到目标盘并重算 kernel.img.md5
  --restore   从备份目录还原目标盘

安全约定
--------
  * --apply / --restore 默认 dry-run，必须显式加 --write 才会写盘。
  * 写盘前校验：两盘 FAT32 BPB 卷标均为 ROCKNIX；
    源 kernel.img 为 ANDROID! v0 格式；目标盘存在 kernel.img。
  * 不动 SYSTEM、cfgload、aml_autoscript、dtb.img、config.ini 及任何分区表。
  * 不修改源盘（G 盘）。
  * 替换后重新计算 md5 并校验一次，失败即报错退出。

用法
----
  python tools/a311d_kernel_bisect.py --status --dst F --src G
  python tools/a311d_kernel_bisect.py --backup E:/A311d/bisect_backup --dst F
  python tools/a311d_kernel_bisect.py --apply --dst F --src G            # 预演
  python tools/a311d_kernel_bisect.py --apply --dst F --src G --write     # 执行
  python tools/a311d_kernel_bisect.py --restore E:/A311d/bisect_backup --dst F --write

作者备注：纯标准库实现，不依赖第三方包。
"""

import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import time

KERNEL = "kernel.img"
MD5REC = "kernel.img.md5"


def root(letter):
    letter = letter.strip().rstrip(":/\\")
    if len(letter) != 1 or not letter.isalpha():
        raise ValueError("盘符必须是单个字母，例如 F 或 G，收到 %r" % letter)
    return letter.upper() + ":/"


def raw_label(letter):
    """读 FAT32 BPB (offset 71) 的卷标，需能打开 \\\\.\\X:"""
    path = "\\\\.\\" + letter.upper() + ":"
    with open(path, "rb") as f:
        boot = f.read(512)
    if len(boot) < 512 or boot[510:512] != b"\x55\xaa":
        raise RuntimeError("%s 引导扇区无效（无 55AA 签名）" % path)
    return boot[71:82].decode("latin1").strip()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            c = f.read(8 * 1024 * 1024)
            if not c:
                break
            h.update(c)
    return h.hexdigest()


def android_head(path):
    with open(path, "rb") as f:
        d = f.read(4096)
    if d[:8] != b"ANDROID!":
        return None
    ks, ka, rs, ra, ss, sa, ta, ps = struct.unpack_from("<8I", d, 8)
    hv = struct.unpack_from("<I", d, 40)[0]
    cmd = d[64:64 + 512].split(b"\x00")[0].decode("ascii", "replace")
    return dict(size=os.path.getsize(path), kernel_size=ks, ramdisk_size=rs,
                second_size=ss, page_size=ps, header_version=hv, cmdline=cmd)


def kernel_version(path):
    """从 lzop 压缩流里搜内核版本串（LZO literal 块会保留明文）。"""
    ks = struct.unpack_from("<I", open(path, "rb").read(16), 8)[0]
    ps = 2048
    with open(path, "rb") as f:
        f.seek(ps)
        payload = f.read(ks)
    found = []
    for v in (b"6.18.21", b"6.18.49", b"6.18.50", b"6.18.20"):
        if payload.count(v):
            found.append("%s x%d" % (v.decode(), payload.count(v)))
    return ", ".join(found) if found else "(未识别)"


def md5_record_suffix(drive):
    """沿用目标盘现有 .md5 文件的第二字段，保持格式不变。"""
    p = root(drive) + MD5REC
    if not os.path.isfile(p):
        return "target/KERNEL"
    txt = open(p, "rb").read().decode("ascii", "replace").strip()
    parts = txt.split()
    return parts[1] if len(parts) >= 2 else "target/KERNEL"


def show_status(dst, src):
    print("=" * 78)
    print("A311D 启动盘 kernel.img 单变量实验 —— 当前状态（只读）")
    print("=" * 78)
    info = {}
    for tag, drv in (("目标盘 DST（拟修改）", dst), ("源盘 SRC（只读）", src)):
        r = root(drv)
        print("\n--- %s  %s ---" % (tag, r))
        try:
            lab = raw_label(drv)
            print("  FAT32 BPB 卷标 : %r  %s" % (lab, "OK" if lab == "ROCKNIX" else "<<< 非 ROCKNIX"))
        except Exception as e:
            print("  FAT32 BPB 卷标 : 读取失败 %s" % e)
            lab = None
        kp = r + KERNEL
        if os.path.isfile(kp):
            h = android_head(kp)
            print("  %-16s %14d B  md5=%s" % (KERNEL, os.path.getsize(kp), md5_file(kp)))
            if h:
                print("    Android v%d  kernel_size=%d  ramdisk_size=%d  page=%d" %
                      (h["header_version"], h["kernel_size"], h["ramdisk_size"], h["page_size"]))
                print("    cmdline(%d B): %s" % (len(h["cmdline"]), h["cmdline"]))
            else:
                print("    <<< 非 ANDROID! 格式")
            print("    版本串命中     : %s" % kernel_version(kp))
            info[drv] = h
        else:
            print("  %-16s 不存在" % KERNEL)
        mp = r + MD5REC
        if os.path.isfile(mp):
            print("  %-16s %r" % (MD5REC, open(mp, "rb").read().decode("ascii", "replace").strip()))
        else:
            print("  %-16s 不存在" % MD5REC)
    print("\n--- 两盘是否仅 kernel.img 不同 ---")
    a, b = info.get(dst), info.get(src)
    if a and b:
        print("  cmdline 相同     : %s" % (a["cmdline"] == b["cmdline"]))
        print("  ramdisk_size 相同: %s (%d / %d)" %
              (a["ramdisk_size"] == b["ramdisk_size"], a["ramdisk_size"], b["ramdisk_size"]))
        print("  kernel_size 差异 : %d B" % (b["kernel_size"] - a["kernel_size"]))
    print("=" * 78)
    return 0


def precheck(dst, src, need_src=True):
    rd, rs = root(dst), root(src)
    for lab, drv in (("目标", dst), ("源", src)):
        if not os.path.isdir(root(drv)):
            raise RuntimeError("%s盘 %s 不可访问" % (lab, root(drv)))
        try:
            l = raw_label(drv)
        except Exception as e:
            raise RuntimeError("%s盘 %s 卷标读取失败: %s" % (lab, root(drv), e))
        if l != "ROCKNIX":
            raise RuntimeError("%s盘 %s 卷标为 %r，不是 ROCKNIX，拒绝操作" % (lab, root(drv), l))
        print("  [OK] %s盘 %s 卷标 = ROCKNIX" % (lab, root(drv)))
    if not os.path.isfile(rd + KERNEL):
        raise RuntimeError("目标盘缺少 %s" % KERNEL)
    if need_src and not os.path.isfile(rs + KERNEL):
        raise RuntimeError("源盘缺少 %s" % KERNEL)
    if need_src:
        h = android_head(rs + KERNEL)
        if not h:
            raise RuntimeError("源盘 %s 不是 ANDROID! 格式，拒绝操作" % KERNEL)
        print("  [OK] 源 kernel.img Android v%d, ramdisk_size=%d, cmdline %d B" %
              (h["header_version"], h["ramdisk_size"], len(h["cmdline"])))
    return rd, rs


def do_backup(dst, out):
    print("\n=== 备份目标盘 %s ===" % root(dst))
    precheck(dst, dst, need_src=False)
    rd = root(dst)
    os.makedirs(out, exist_ok=True)
    manifest = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "drive": root(dst), "files": {}}
    for name in (KERNEL, MD5REC):
        p = rd + name
        if not os.path.isfile(p):
            print("  [跳过] %s 不存在" % name)
            continue
        shutil.copy2(p, os.path.join(out, name))
        manifest["files"][name] = {"size": os.path.getsize(p), "md5": md5_file(p)}
        print("  [备份] %-16s %14d B  md5=%s" %
              (name, os.path.getsize(p), md5_file(p)))
    mp = os.path.join(out, "manifest.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("  清单 -> %s" % os.path.abspath(mp))
    print("\n  备份完成，目标盘未做任何修改。")
    return 0


def write_kernel(dst, src_bytes, md5_text):
    rd = root(dst)
    kp = rd + KERNEL
    with open(kp, "wb") as f:
        f.write(src_bytes)
    with open(rd + MD5REC, "wb") as f:
        f.write(md5_text.encode("ascii"))
    # 复核
    got = md5_file(kp)
    want = md5_text.decode("ascii").split()[0]
    if got != want:
        raise RuntimeError("写入后 md5 复核失败: 实际 %s / 记录 %s" % (got, want))
    h = android_head(kp)
    if not h:
        raise RuntimeError("写入后 Android 头无效")
    print("  [复核] %-16s %14d B  md5=%s  一致" % (KERNEL, os.path.getsize(kp), got))
    print("  [复核] ramdisk_size=%d  cmdline %d B 未变" %
          (h["ramdisk_size"], len(h["cmdline"])))


def do_apply(dst, src, write):
    print("\n=== 单变量实验：把源盘 kernel.img 装到目标盘 ===")
    print("  DST=%s  SRC=%s  模式=%s" % (root(dst), root(src), "写盘" if write else "DRY-RUN 预演"))
    rd, rs = precheck(dst, src)
    src_bytes = open(rs + KERNEL, "rb").read()
    suffix = md5_record_suffix(dst)
    new_md5 = "%s  %s\n" % (hashlib.md5(src_bytes).hexdigest(), suffix)
    cur_md5 = md5_file(rd + KERNEL)
    print("\n  当前 DST kernel.img : %14d B  md5=%s" % (os.path.getsize(rd + KERNEL), cur_md5))
    print("  拟写入 SRC kernel.img: %14d B  md5=%s" % (len(src_bytes), hashlib.md5(src_bytes).hexdigest()))
    print("  拟写入 %s     : %r" % (MD5REC, new_md5.strip()))
    if cur_md5 == hashlib.md5(src_bytes).hexdigest():
        print("\n  DST 与 SRC 的 kernel.img 已相同，无需替换。")
        return 0
    print("\n  变更范围：仅 %s 与 %s 两个文件。" % (KERNEL, MD5REC))
    print("  不改动 SYSTEM / cfgload / aml_autoscript / dtb.img / 卷标 / 分区表。")
    if not write:
        print("\n  [DRY-RUN] 未写盘。确认后加 --write 执行。")
        print("  建议先执行 --backup 保存当前两个文件。")
        return 0
    write_kernel(dst, src_bytes, new_md5)
    print("\n  替换完成。现在可以把 DST 盘插回设备上电。")
    print("  判读见 tools/a311d_boot_disk_diagnosis_20261111.txt 实验 A。")
    return 0


def do_restore(dst, backup, write):
    print("\n=== 从备份还原目标盘 %s ===" % root(dst))
    print("  备份目录=%s  模式=%s" % (os.path.abspath(backup), "写盘" if write else "DRY-RUN 预演"))
    precheck(dst, dst, need_src=False)
    mp = os.path.join(backup, "manifest.json")
    if not os.path.isfile(mp):
        raise RuntimeError("备份清单不存在: %s" % mp)
    man = json.load(open(mp, encoding="utf-8"))
    rd = root(dst)
    for name in (KERNEL, MD5REC):
        p = os.path.join(backup, name)
        if not os.path.isfile(p):
            print("  [跳过] 备份中无 %s" % name)
            continue
        b = open(p, "rb").read()
        exp = man["files"].get(name, {}).get("md5")
        if exp and hashlib.md5(b).hexdigest() != exp:
            raise RuntimeError("备份文件 %s 校验失败" % name)
        print("  [备份] %-16s %14d B  md5=%s" % (name, len(b), hashlib.md5(b).hexdigest()))
    if not write:
        print("\n  [DRY-RUN] 未写盘。确认后加 --write 执行。")
        return 0
    kb = open(os.path.join(backup, KERNEL), "rb").read()
    rb = open(os.path.join(backup, MD5REC), "rb").read()
    with open(rd + KERNEL, "wb") as f:
        f.write(kb)
    with open(rd + MD5REC, "wb") as f:
        f.write(rb)
    print("  还原完成，md5 复核: %s" % ("一致" if md5_file(rd + KERNEL) == man["files"][KERNEL]["md5"] else "不一致 <<<"))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="A311D 启动盘 kernel.img 单变量实验（可逆）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dst", default="F", help="目标盘符（拟修改），默认 F")
    ap.add_argument("--src", default="G", help="源盘符（只读），默认 G")
    ap.add_argument("--status", action="store_true", help="只读显示两盘状态")
    ap.add_argument("--backup", metavar="DIR", help="备份目标盘到 DIR")
    ap.add_argument("--apply", action="store_true", help="把源盘 kernel.img 装到目标盘")
    ap.add_argument("--restore", metavar="DIR", help="从备份目录还原")
    ap.add_argument("--write", action="store_true", help="实际写盘（默认 dry-run）")
    a = ap.parse_args()

    acts = sum(bool(x) for x in (a.status, a.backup, a.apply, a.restore))
    if acts != 1:
        ap.error("必须且只能指定 --status / --backup / --apply / --restore 中的一个")
    try:
        if a.status:
            return show_status(a.dst, a.src)
        if a.backup:
            return do_backup(a.dst, a.backup)
        if a.apply:
            return do_apply(a.dst, a.src, a.write)
        if a.restore:
            return do_restore(a.dst, a.restore, a.write)
    except Exception as e:
        print("\n[错误] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
