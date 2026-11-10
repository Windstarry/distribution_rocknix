#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A311D: restore the boot chain on a built .img / mounted FAT volume.

The 20261110 A311D build replaced the U-Boot boot chain with a new
cfgload(3012B)/cfgload_env/ceboot flow and shipped an 80688-byte DTB built
from the cainiao-cniot-core DTS.  On the real board the initramfs fails with
"Unable to find LABEL=ROCKNIX ... Power off in 3s", because the USB mass
storage device is not visible to the kernel with that chain.

The last known-good chain was captured from a running device
(ROCKNIX 20261108, kernel 6.18.49, 192.168.31.7):

    cfgload        508 B    uImage script, sets bootargs directly
    aml_autoscript 1466 B   bootfromusb fatload+bootm, saveenv
    dtb.img       54984 B   real-board DTB (bifrost okay, no midgard node)

This tool replaces those three files in a built .img (editing the FAT32 root
directory entries and clusters in place) or on an already mounted volume
(F:/), leaving kernel.img, SYSTEM and the volume label untouched.

Usage:
  python3 tools/a311d_restore_boot_chain.py --scan
  python3 tools/a311d_restore_boot_chain.py --ref <dir> --target F:
  python3 tools/a311d_restore_boot_chain.py --ref <dir> --target image.img
  python3 tools/a311d_restore_boot_chain.py --ref <dir> --target image.img \
      --backup <dir> [--dry-run]

All operations are read-only unless --dry-run is omitted.  Backups are
strongly recommended before writing.
"""

import argparse
import os
import shutil
import struct
import sys

NAMES = ("cfgload", "aml_autoscript", "dtb.img")


# ---------------------------------------------------------------------------
# FAT32 (image / raw volume) access
# ---------------------------------------------------------------------------
class Fat32RW(object):
    """Minimal FAT32 read/write for the root directory of an .img file."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "r+b")
        mbr = self.read(0, 512)
        if mbr[510:512] != b"\x55\xAA":
            raise ValueError("MBR signature missing: %s" % path)
        p0 = mbr[446:462]
        self.base = struct.unpack_from("<I", p0, 8)[0] * 512
        boot = self.read(self.base, 512)
        self.bps = struct.unpack_from("<H", boot, 11)[0]
        self.spc = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        nfat = boot[16]
        spf = struct.unpack_from("<I", boot, 36)[0]
        self.root = struct.unpack_from("<I", boot, 44)[0]
        self.fat_start = self.base + reserved * self.bps
        self.data_start = self.fat_start + nfat * spf * self.bps
        self.cluster_bytes = self.spc * self.bps

    def read(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def write(self, off, data):
        self.f.seek(off)
        self.f.write(data)

    def fat_entry(self, c):
        v = self.read(self.fat_start + c * 4, 4)
        return struct.unpack_from("<I", v, 0)[0] & 0x0FFFFFFF

    def chain(self, first):
        out = []
        c = first
        while 2 <= c < 0x0FFFFFF8 and c not in out:
            out.append(c)
            c = self.fat_entry(c)
        return out

    def cluster_off(self, c):
        return self.data_start + (c - 2) * self.cluster_bytes

    def root_clusters(self):
        return self.chain(self.root)

    @staticmethod
    def lfn_name(entries):
        s = ""
        for e in reversed(entries):
            for i in (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 26, 28, 30):
                ch = struct.unpack_from("<H", e, i)[0]
                if ch not in (0, 0xFFFF):
                    s += chr(ch)
        return s

    def root_entries(self):
        """Return [(name, entry_abs_offset, first_cluster, size, is_dir)]."""
        clusters = self.root_clusters()
        raw = bytearray()
        for c in clusters:
            raw.extend(self.read(self.cluster_off(c), self.cluster_bytes))
        per_cluster = self.cluster_bytes // 32

        def abs_off(index):
            ci, within = divmod(index, per_cluster)
            return self.cluster_off(clusters[ci]) + within * 32

        lfn = []
        out = []
        i = 0
        while i + 32 <= len(raw):
            e = raw[i:i + 32]
            off = abs_off(i // 32)
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
            name = self.lfn_name(lfn) or short
            lfn = []
            first = (struct.unpack_from("<H", e, 20)[0] << 16) | \
                    struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            out.append((name, off, first, size, bool(attr & 0x10)))
        return out

    def replace_file(self, entry, data):
        """Overwrite a file's clusters in place and update its size field."""
        name, entry_off, first, old_size, is_dir = entry
        clusters = self.chain(first)
        needed = (len(data) + self.cluster_bytes - 1) // self.cluster_bytes
        if needed > len(clusters):
            raise ValueError("%s needs %d clusters, only %d allocated; "
                             "new content must not be larger than the "
                             "original file" % (name, needed, len(clusters)))
        for c in clusters:
            self.write(self.cluster_off(c), b"\x00" * self.cluster_bytes)
        for i, c in enumerate(clusters):
            chunk = data[i * self.cluster_bytes:(i + 1) * self.cluster_bytes]
            self.write(self.cluster_off(c), chunk.ljust(self.cluster_bytes, b"\x00"))
        self.write(entry_off + 28, struct.pack("<I", len(data)))
        return old_size, len(clusters) * self.cluster_bytes

    def close(self):
        self.f.flush()
        self.f.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def load_ref(ref):
    out = {}
    for n in NAMES:
        p = os.path.join(ref, n)
        if not os.path.isfile(p):
            raise SystemExit("参考文件缺失: %s" % p)
        out[n] = open(p, "rb").read()
    return out


def show(label, data):
    import hashlib
    print("    %-18s %8d B  md5=%s" %
          (label, len(data), hashlib.md5(data).hexdigest()[:16]))


def restore_image(target, ref, dry_run, backup):
    fat = Fat32RW(target)
    entries = {e[0].lower(): e for e in fat.root_entries()}
    print("  目标: %s" % target)
    print("  FAT32: bps=%d spc=%d root_cluster=%d 根目录 %d 项" %
          (fat.bps, fat.spc, fat.root, len(entries)))
    plan = []
    for n in NAMES:
        if n not in entries:
            raise SystemExit("根目录缺少 %s" % n)
        e = entries[n]
        old = fat.read(fat.cluster_off(e[2]), e[3])
        plan.append((n, e, old, ref[n]))
        print("  %-18s 原 %7d B -> 新 %7d B" % (n, e[3], len(ref[n])))
        show("原文件", old)
        show("新文件", ref[n])
    if dry_run:
        print("\n  [dry-run] 未写入任何数据")
        fat.close()
        return 0
    if backup:
        os.makedirs(backup, exist_ok=True)
        for n, _, old, _ in plan:
            open(os.path.join(backup, n + ".orig"), "wb").write(old)
        print("\n  已备份原文件 -> %s" % backup)
    for n, e, _, new in plan:
        old_size, capacity = fat.replace_file(e, new)
        print("  写入 %-18s size %d -> %d (簇容量 %d B)" %
              (n, old_size, len(new), capacity))
    fat.close()

    # verify by re-opening
    chk = Fat32RW(target)
    ents = {e[0].lower(): e for e in chk.root_entries()}
    ok = True
    for n in NAMES:
        e = ents[n]
        got = chk.read(chk.cluster_off(e[2]), e[3])
        same = (got == ref[n])
        ok &= same
        print("  复核 %-18s size=%7d %s" % (n, e[3], "SAME" if same else "DIFF"))
    chk.close()
    print("\n  结果: %s" % ("全部通过" if ok else "存在不一致"))
    return 0 if ok else 1


def restore_volume(target, ref, dry_run, backup):
    d = target.rstrip("\\/")
    if not d.endswith(":"):
        d += ":"
    root = d + "\\"
    if not os.path.isdir(root):
        raise SystemExit("卷不可访问: %s" % root)
    print("  目标: %s (已挂载卷，走文件系统驱动)" % root)
    plan = []
    for n in NAMES:
        p = os.path.join(root, n)
        if not os.path.isfile(p):
            raise SystemExit("缺少 %s" % p)
        old = open(p, "rb").read()
        plan.append((n, p, old, ref[n]))
        print("  %-18s 原 %7d B -> 新 %7d B" % (n, len(old), len(ref[n])))
    if dry_run:
        print("\n  [dry-run] 未写入任何数据")
        return 0
    if backup:
        os.makedirs(backup, exist_ok=True)
        for n, _, old, _ in plan:
            open(os.path.join(backup, n + ".orig"), "wb").write(old)
        print("\n  已备份原文件 -> %s" % backup)
    for n, p, _, new in plan:
        with open(p, "wb") as f:
            f.write(new)
        print("  写入 %-18s %d B" % (n, len(new)))
    ok = True
    for n, p, _, new in plan:
        same = open(p, "rb").read() == new
        ok &= same
        print("  复核 %-18s %s" % (n, "SAME" if same else "DIFF"))
    print("\n  结果: %s" % ("全部通过" if ok else "存在不一致"))
    return 0 if ok else 1


def scan():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    mask = k.GetLogicalDrives()
    types = {2: "REMOVABLE", 3: "FIXED"}
    print("=== 本机可移动卷 ===")
    for i in range(26):
        if not (mask >> i & 1):
            continue
        d = "%s:\\" % chr(65 + i)
        if k.GetDriveTypeW(d) != 2:
            continue
        vb = ctypes.create_unicode_buffer(256)
        fs = ctypes.create_unicode_buffer(256)
        sn = wintypes.DWORD()
        k.GetVolumeInformationW(d, vb, 256, ctypes.byref(sn), None, None, fs, 256)
        have = [n for n in NAMES if os.path.isfile(os.path.join(d, n))]
        print("  %-4s label=%-14r fs=%-8r 启动文件=%s" %
              (d, vb.value, fs.value, ",".join(have) or "无"))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", help="含 cfgload/aml_autoscript/dtb.img 的参考目录")
    ap.add_argument("--target", help=".img 文件或已挂载卷 (如 F:)")
    ap.add_argument("--backup", help="写入前把原文件备份到该目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    ap.add_argument("--scan", action="store_true", help="列出本机可移动卷")
    a = ap.parse_args()
    if a.scan:
        return scan()
    if not a.ref or not a.target:
        ap.print_help()
        return 2
    ref = load_ref(a.ref)
    print("=== 参考基线 (实机已验证) ===")
    for n in NAMES:
        show(n, ref[n])
    print()
    if a.target.lower().endswith((".img", ".bin")):
        return restore_image(a.target, ref, a.dry_run, a.backup)
    return restore_volume(a.target, ref, a.dry_run, a.backup)


if __name__ == "__main__":
    sys.exit(main())
