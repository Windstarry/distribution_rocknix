#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
a311d_fix_boot_label.py -- A311D boot label consistency fixer (read/write).

Background
----------
The A311D board's U-Boot environment stored in eMMC still resolves
boot=LABEL=ROCKNIX.  The 20261110 build labelled the FAT boot partition
AURKNIX and emptied the kernel.img Android header cmdline.  initramfs
mount_common() retries 15 x 1s, prints
  "Unable to find LABEL=ROCKNIX, powering off and on should correct it."
and powers off -> black screen.

Windows SetVolumeLabelW only updates the FAT32 root-directory volume label
entry; the BPB label (offset 71) stays stale.  Linux blkid reads the BPB,
so a "half-fixed" disk still fails.  This tool fixes BOTH plus every other
place the label appears, using equal-length patches only:

  1. FAT32 BPB volume label @ boot sector + 71 (11 bytes)
  2. FAT32 root directory volume label entry (attr 0x08, 11 bytes)
  3. cfgload      (U-Boot legacy uImage script): boot=LABEL=... + CRC
  4. cfgload_env  (U-Boot text env):             boot=LABEL=...
  5. kernel.img   (Android boot image header cmdline @64, 512 bytes)
  6. kernel.img.md5 manifest (recomputed, same 48-byte format)
  7. zero-byte "ROCKNIX" marker in the root directory (if absent)

Targets
-------
  --target <image.img>   patch a raw disk image (MBR + FAT32 first partition)
  --target <X:>          patch a mounted Windows volume via raw device I/O
  --scan                 list mounted volumes and their current FAT labels
  --dry-run              report what would change, write nothing
  --backup <dir>         save every byte that will be modified, plus a
                         README with rollback instructions

All replacements are equal length (AURKNIX/ROCKNIX are both 7 chars), so
file sizes, FAT chains and uImage data_size are untouched.  U-Boot script
header CRC (hcrc) and data CRC (dcrc) are recomputed with zlib.crc32.

Examples
--------
  python3 tools/a311d_fix_boot_label.py --scan
  python3 tools/a311d_fix_boot_label.py --target F: --backup E:/backup --dry-run
  python3 tools/a311d_fix_boot_label.py --target F: --backup E:/backup
  python3 tools/a311d_fix_boot_label.py --target new.img --label ROCKNIX
  python3 tools/a311d_image_verify.py new.img --expect-label ROCKNIX
"""
import argparse
import ctypes
import hashlib
import os
import re
import shutil
import string
import struct
import sys
import zlib
from ctypes import wintypes

DEFAULT_LABEL = "ROCKNIX"
# Proven cmdline from the last image that boots this board
# (AURKNIX-A311D.aarch64-20261109-bootsafe-edidmode.img).
REF_CMDLINE = (
    "boot=LABEL=ROCKNIX disk=LABEL=STORAGE "
    "console=ttyAML0,115200n8 console=tty0 no_console_suspend "
    "net.ifnames=0 video=HDMI-A-1:D systemd.debug_shell=ttyAML0"
)
UIMAGE_MAGIC = b"\x27\x05\x19\x56"
SECTOR = 512


# --------------------------------------------------------------------------
# Raw I/O backends.  read_at / write_at use ABSOLUTE byte offsets.
# --------------------------------------------------------------------------
class ImageIO:
    """Raw .img file: MBR at 0, FAT32 partition at start_lba * 512."""

    def __init__(self, path, rw=True, quiet=False):
        self.path = path
        self.f = open(path, "r+b" if rw else "rb")
        mbr = self.read_at(0, SECTOR)
        if mbr[510:512] != b"\x55\xAA":
            raise ValueError("MBR signature missing: %s" % path)
        p0 = mbr[446:462]
        self.part_lba = struct.unpack_from("<I", p0, 8)[0]
        self.ptype = p0[4]
        self.base = self.part_lba * SECTOR
        if not quiet:
            print("  目标 : 镜像 %s" % path)
            print("  分区 : type=0x%02X start_lba=%d (byte %d)" %
                  (self.ptype, self.part_lba, self.base))

    def read_at(self, off, n):
        self.f.seek(off)
        return self.f.read(n)

    def write_at(self, off, data):
        self.f.seek(off)
        self.f.write(data)

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.close()


class VolumeIO:
    """Windows volume (\\\\.\\X:) raw device I/O."""

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_RW = 0x00000003
    OPEN_EXISTING = 3
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_UNLOCK_VOLUME = 0x0009001C

    def __init__(self, drive, rw=True, quiet=False):
        self.drive = drive.strip().rstrip("\\/:") + ":"
        self.base = 0  # a volume device starts at the partition boot sector
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
        k.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                               wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD),
                               wintypes.LPVOID]
        k.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID,
                                wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD),
                                wintypes.LPVOID]
        k.SetFilePointerEx.restype = wintypes.BOOL
        k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                       ctypes.POINTER(ctypes.c_longlong),
                                       wintypes.DWORD]
        k.DeviceIoControl.restype = wintypes.BOOL
        self.k = k
        access = self.GENERIC_READ | (self.GENERIC_WRITE if rw else 0)
        self.h = k.CreateFileW("\\\\.\\" + self.drive, access,
                               self.FILE_SHARE_RW, None,
                               self.OPEN_EXISTING, 0, None)
        invalid = wintypes.HANDLE(-1).value
        if not self.h or self.h == invalid:
            raise OSError("CreateFileW(\\\\.\\%s) failed, err=%d" %
                          (self.drive, ctypes.get_last_error()))
        if rw:
            dummy = wintypes.DWORD()
            self.locked = bool(k.DeviceIoControl(
                self.h, self.FSCTL_LOCK_VOLUME, None, 0, None, 0,
                ctypes.byref(dummy), None))
        else:
            self.locked = False
        if not quiet:
            print("  目标 : 卷 %s (raw I/O, locked=%s)" %
                  (self.drive, self.locked))

    def _seek(self, off):
        newpos = ctypes.c_longlong()
        self.k.SetFilePointerEx(self.h, off, ctypes.byref(newpos), 0)

    def read_at(self, off, n):
        # Windows raw volume I/O requires sector-aligned offset and length.
        start = off - (off % SECTOR)
        end = ((off + n) + SECTOR - 1) // SECTOR * SECTOR
        self._seek(start)
        buf = ctypes.create_string_buffer(end - start)
        got = wintypes.DWORD()
        ok = self.k.ReadFile(self.h, buf, end - start,
                             ctypes.byref(got), None)
        if not ok:
            raise OSError("ReadFile @%d failed, err=%d" %
                          (off, ctypes.get_last_error()))
        data = buf.raw[:got.value]
        pad = off - start
        if len(data) < pad + n:
            raise OSError("short raw read @%d (got %d, need %d)" %
                          (off, len(data), pad + n))
        return data[pad:pad + n]

    def write_at(self, off, data):
        if off % SECTOR or len(data) % SECTOR:
            raise ValueError("raw volume writes must be sector aligned")
        self._seek(off)
        buf = ctypes.create_string_buffer(data, len(data))
        wrote = wintypes.DWORD()
        ok = self.k.WriteFile(self.h, buf, len(data),
                              ctypes.byref(wrote), None)
        if not ok or wrote.value != len(data):
            raise OSError("WriteFile @%d failed, err=%d wrote=%d" %
                          (off, ctypes.get_last_error(), wrote.value))

    def flush(self):
        pass

    def close(self):
        if self.locked:
            dummy = wintypes.DWORD()
            self.k.DeviceIoControl(self.h, self.FSCTL_UNLOCK_VOLUME,
                                   None, 0, None, 0,
                                   ctypes.byref(dummy), None)
        self.k.CloseHandle(self.h)


def aligned_write(io, off, data):
    """Sector-aligned read-modify-write, safe for both backends."""
    start = off - (off % SECTOR)
    end = ((off + len(data)) + SECTOR - 1) // SECTOR * SECTOR
    buf = bytearray(io.read_at(start, end - start))
    if len(buf) < end - start:
        raise ValueError("short read during aligned_write @%d" % off)
    buf[off - start:off - start + len(data)] = data
    io.write_at(start, bytes(buf))


# --------------------------------------------------------------------------
# Minimal FAT32 reader/writer with LFN support
# --------------------------------------------------------------------------
def lfn_name(entries):
    out = ""
    for e in reversed(entries):
        for i in (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 26, 28, 30):
            ch = struct.unpack_from("<H", e, i)[0]
            if ch in (0, 0xFFFF):
                continue
            out += chr(ch)
    return out


class Fat32:
    def __init__(self, io):
        self.io = io
        boot = io.read_at(io.base, SECTOR)
        if boot[510:512] != b"\x55\xAA":
            raise ValueError("FAT32 boot sector signature missing")
        self.bps = struct.unpack_from("<H", boot, 11)[0]
        self.spc = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        nfat = boot[16]
        spf = struct.unpack_from("<I", boot, 36)[0]
        self.root = struct.unpack_from("<I", boot, 44)[0]
        self.fat_start = io.base + reserved * self.bps
        self.data_start = io.base + (reserved + nfat * spf) * self.bps
        self.cluster_bytes = self.spc * self.bps
        if not self.bps or not self.spc:
            raise ValueError("invalid FAT32 geometry")
        print("  FAT32: bps=%d spc=%d reserved=%d nfat=%d spf=%d "
              "data_start=byte %d root_cluster=%d" %
              (self.bps, self.spc, reserved, nfat, spf,
               self.data_start, self.root))

    def fat_entry(self, c):
        v = self.io.read_at(self.fat_start + c * 4, 4)
        return struct.unpack_from("<I", v, 0)[0] & 0x0FFFFFFF

    def chain(self, first, max_clusters=1 << 20):
        out = []
        c = first
        seen = set()
        while 2 <= c < 0x0FFFFFF8 and c not in seen and len(out) < max_clusters:
            seen.add(c)
            out.append(c)
            c = self.fat_entry(c)
        return out

    def cluster_offset(self, c):
        return self.data_start + (c - 2) * self.cluster_bytes

    def read_file(self, first, size):
        out = bytearray()
        for c in self.chain(first):
            out.extend(self.io.read_at(self.cluster_offset(c),
                                       self.cluster_bytes))
            if len(out) >= size:
                break
        return bytes(out[:size])

    def write_file(self, first, size, data):
        if len(data) != size:
            raise ValueError("write_file requires identical size (%d != %d)" %
                             (len(data), size))
        off = 0
        for c in self.chain(first):
            n = min(self.cluster_bytes, size - off)
            aligned_write(self.io, self.cluster_offset(c), data[off:off + n])
            off += n
            if off >= size:
                return
        raise ValueError("FAT chain shorter than file size")

    def dir_entries(self, cluster):
        """Yield (absolute_offset, 32-byte entry, long_name)."""
        raw = bytearray()
        for c in self.chain(cluster):
            raw.extend(self.io.read_at(self.cluster_offset(c),
                                       self.cluster_bytes))
        entries = []
        lfn = []
        i = 0
        while i + 32 <= len(raw):
            e = raw[i:i + 32]
            abs_off = None
            # map back to an absolute offset (root/short dirs are contiguous
            # in practice; recompute per cluster boundary)
            cl_index = i // self.cluster_bytes
            within = i % self.cluster_bytes
            chain = self.chain(cluster)
            if cl_index < len(chain):
                abs_off = self.cluster_offset(chain[cl_index]) + within
            i += 32
            if e[0] == 0x00:
                entries.append((abs_off, e, None, "END"))
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
            first = (struct.unpack_from("<H", e, 20)[0] << 16) | \
                    struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            entries.append((abs_off, e, name, (bool(attr & 0x10),
                                               first, size, attr)))
        return entries, self.chain(cluster)

    def files(self):
        entries, _ = self.dir_entries(self.root)
        out = {}
        for off, e, name, info in entries:
            if name is None or info == "END":
                continue
            isdir, first, size, attr = info
            if not isdir and attr != 0x08:
                out[name.lower()] = (first, size, off, e)
        return out


# --------------------------------------------------------------------------
# Patches
# --------------------------------------------------------------------------
def patch_script(data, old, new, label):
    """Replace equal-length label inside a U-Boot script; recompute CRC."""
    if len(old) != len(new):
        raise ValueError("label lengths differ, cannot do equal-length patch")
    is_uimage = data[:4] == UIMAGE_MAGIC
    if is_uimage:
        size = struct.unpack_from(">I", data, 12)[0]
        body = bytearray(data[64:64 + size])
        if len(body) != size:
            raise ValueError("%s: uImage data truncated" % label)
    else:
        body = bytearray(data)
    hits = body.count(old.encode("ascii"))
    if hits == 0:
        print("    %-16s 已为 %s，无需替换" % (label, new))
        return data, 0
    body = body.replace(old.encode("ascii"), new.encode("ascii"))
    out = bytearray(data)
    if is_uimage:
        out[64:64 + size] = body
        dcrc = zlib.crc32(bytes(body)) & 0xFFFFFFFF
        struct.pack_into(">I", out, 24, dcrc)
        struct.pack_into(">I", out, 4, 0)
        hcrc = zlib.crc32(bytes(out[:64])) & 0xFFFFFFFF
        struct.pack_into(">I", out, 4, hcrc)
        print("    %-16s 替换 %d 处，重算 dcrc=0x%08X hcrc=0x%08X" %
              (label, hits, dcrc, hcrc))
    else:
        out = body
        print("    %-16s 替换 %d 处（纯文本脚本）" % (label, hits))
    if len(out) != len(data):
        raise ValueError("%s: size changed" % label)
    return bytes(out), hits


def read_fat_label(io, fat):
    boot = io.read_at(io.base, SECTOR)
    bpb = boot[71:82].decode("ascii", "replace").strip()
    root = ""
    entries, _ = fat.dir_entries(fat.root)
    for off, e, name, info in entries:
        if info == "END":
            break
        if name is None:
            continue
        if isinstance(info, tuple) and info[3] == 0x08:
            root = e[0:11].decode("ascii", "replace").strip()
            break
    return bpb, root


def fix(io, target_label=DEFAULT_LABEL, cmdline=REF_CMDLINE,
        backup=None, dry_run=False, do_kernel_cmdline=True,
        do_marker=True):
    fat = Fat32(io)
    label_field = target_label.ljust(11)[:11].encode("ascii")
    bpb_before, root_before = read_fat_label(io, fat)
    print("  现状 : BPB label=%r  根目录卷标项=%r" %
          (bpb_before, root_before))

    if backup:
        os.makedirs(backup, exist_ok=True)
        boot = io.read_at(io.base, SECTOR)
        open(os.path.join(backup, "bpb.bin"), "wb").write(boot)
        root_raw = bytearray()
        for c in fat.chain(fat.root):
            root_raw.extend(io.read_at(fat.cluster_offset(c),
                                       fat.cluster_bytes))
        open(os.path.join(backup, "rootdir.bin"), "wb").write(bytes(root_raw))
        print("  备份 : %s (bpb.bin / rootdir.bin)" % backup)

    changes = []
    md5_target = None  # (name, first_cluster, size, original_bytes)

    # 1. BPB volume label -------------------------------------------------
    boot = bytearray(io.read_at(io.base, SECTOR))
    old = bytes(boot[71:82])
    boot[71:82] = label_field
    changes.append(("BPB label", old.decode("ascii", "replace").strip(),
                    target_label, io.base + 71, bytes(boot[71:82])))

    # 2. root directory volume label entry --------------------------------
    entries, _ = fat.dir_entries(fat.root)
    for off, e, name, info in entries:
        if info == "END":
            break
        if name is None:
            continue
        if isinstance(info, tuple) and info[3] == 0x08:
            new_e = bytearray(e)
            new_e[0:11] = label_field
            changes.append(("根目录卷标项",
                            e[0:11].decode("ascii", "replace").strip(),
                            target_label, off, bytes(new_e[0:11])))
            break

    # 3/4. U-Boot scripts -------------------------------------------------
    files = fat.files()
    for name in ("cfgload", "cfgload_env"):
        if name not in files:
            print("    %-16s 不存在，跳过" % name)
            continue
        first, size, _, _ = files[name]
        data = fat.read_file(first, size)
        if backup:
            open(os.path.join(backup, name + ".orig"), "wb").write(data)
        old_label = "AURKNIX"
        new_data, hits = patch_script(data, old_label, target_label, name)
        if hits:
            changes.append((name, old_label, target_label, None, new_data,
                            first, size))

    # 5. kernel.img Android header cmdline --------------------------------
    if do_kernel_cmdline and "kernel.img" in files:
        first, size, _, _ = files["kernel.img"]
        head = fat.read_file(first, 1024)
        if backup:
            open(os.path.join(backup, "kernel.img.head.orig"), "wb").write(head)
        if head[:8] != b"ANDROID!":
            print("    kernel.img    Android magic 缺失，跳过 cmdline")
        else:
            cur = head[64:576].split(b"\x00")[0].decode("ascii", "replace")
            payload = cmdline.encode("ascii")[:512].ljust(512, b"\x00")
            # cmdline must also carry the target label
            payload = re.sub(rb"boot=LABEL=[A-Za-z0-9_-]+",
                             ("boot=LABEL=" + target_label).encode("ascii"),
                             payload)
            print("    kernel.img    cmdline: %r -> %r" %
                  (cur[:60], payload.split(b"\x00")[0][:80].decode()))
            changes.append(("kernel.img cmdline", cur[:40] or "(空)",
                            "参考基线", fat.cluster_offset(first) + 64,
                            payload))
            # 6. md5 manifest -- must be recomputed AFTER the header is
            #    written, otherwise we would just write the old digest back.
            for md5name in ("kernel.img.md5", "kernel.md5"):
                if md5name in files:
                    mfirst, msize, _, _ = files[md5name]
                    oldm = fat.read_file(mfirst, msize)
                    if backup:
                        open(os.path.join(backup, md5name + ".orig"),
                             "wb").write(oldm)
                    md5_target = (md5name, mfirst, msize, oldm)
                    print("    kernel.img    %s 将在写入后按新 header 重算" %
                          md5name)
                    break

    # 7. ROCKNIX marker ---------------------------------------------------
    if do_marker and target_label.lower() not in files:
        entries, _ = fat.dir_entries(fat.root)
        free = None
        for off, e, name, info in entries:
            if info == "END" and off is not None:
                free = off
                break
        if free is None:
            print("    marker        根目录无空闲槽，跳过")
        else:
            short = (target_label.upper()[:8].ljust(8) + "   ").encode("ascii")
            e = bytearray(32)
            e[0:11] = short
            e[11] = 0x20
            changes.append(("marker", "(无)", target_label.upper(),
                            free, bytes(e)))
            print("    marker        根目录 @byte %d 写入 %r" %
                  (free, target_label.upper()))

    # print plan -----------------------------------------------------------
    print("\n  修复计划（%d 项，全部等长）:" %
          (len(changes) + (1 if md5_target else 0)))
    for c in changes:
        print("    - %-20s %r -> %r" % (c[0], c[1], c[2]))
    if md5_target:
        print("    - %-20s 写入后按新 kernel.img 重算" % md5_target[0])
    if dry_run:
        print("\n  [dry-run] 未写入任何数据")
        return 0

    # apply ----------------------------------------------------------------
    for c in changes:
        if len(c) == 5:
            _, _, _, off, data = c
            aligned_write(io, off, data)
        else:
            _, _, _, _, data, first, size = c
            fat.write_file(first, size, data)
    io.flush()
    print("\n  已写入 %d 项" % len(changes))

    if md5_target:
        md5name, mfirst, msize, oldm = md5_target
        kfirst, ksize, _, _ = files["kernel.img"]
        h = hashlib.md5()
        remain = ksize
        for c in fat.chain(kfirst):
            n = min(fat.cluster_bytes, remain)
            h.update(io.read_at(fat.cluster_offset(c), n))
            remain -= n
            if remain <= 0:
                break
        new_md5 = h.hexdigest()
        txt = oldm.decode("ascii", "replace")
        newtxt = re.sub(r"[0-9a-fA-F]{32}", new_md5, txt, count=1)
        newm = newtxt.encode("ascii")
        if len(newm) != msize:
            raise ValueError("%s: md5 manifest size would change" % md5name)
        fat.write_file(mfirst, msize, newm)
        io.flush()
        print("  %s: %s -> %s" %
              (md5name, txt.strip()[:32], new_md5))

    bpb_after, root_after = read_fat_label(io, fat)
    print("  复核 : BPB label=%r  根目录卷标项=%r" %
          (bpb_after, root_after))
    if bpb_after.upper() != target_label.upper() or \
       root_after.upper() != target_label.upper():
        print("  [FAIL] 卷标复核不一致")
        return 1
    return 0


def scan_volumes():
    print("=== 本机卷扫描（只读） ===")
    for letter in string.ascii_uppercase:
        path = "%s:\\" % letter
        if not os.path.exists(path):
            continue
        vol = letter + ":"
        io = None
        try:
            io = VolumeIO(vol, rw=False, quiet=True)
            boot = io.read_at(0, SECTOR)
            if boot[510:512] != b"\x55\xAA":
                print("  %s  (非 FAT 引导扇区，跳过)" % vol)
                continue
            bps = struct.unpack_from("<H", boot, 11)[0]
            print("  %s  BPB label=%-12r fstype=%r bps=%d  %s" %
                  (vol, boot[71:82].decode("ascii", "replace").strip(),
                   boot[82:90].decode("ascii", "replace").strip(), bps,
                   "含 SYSTEM" if os.path.exists(path + "SYSTEM") else ""))
        except Exception as exc:
            print("  %s  读取失败: %s" % (vol, exc))
        finally:
            if io:
                io.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", help="image.img 或 Windows 卷 (如 F:)")
    ap.add_argument("--label", default=DEFAULT_LABEL)
    ap.add_argument("--backup", help="备份目录")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scan", action="store_true", help="列出本机卷及 FAT 卷标")
    ap.add_argument("--no-kernel-cmdline", action="store_true",
                    help="不写 kernel.img header cmdline（也不改 md5 清单）")
    ap.add_argument("--no-marker", action="store_true")
    args = ap.parse_args()

    if args.scan:
        scan_volumes()
        return 0
    if not args.target:
        ap.print_help()
        return 2
    if len(args.label) > 11 or not re.match(r"^[A-Za-z0-9_-]+$", args.label):
        print("label 必须是 <=11 字符的 ASCII 字母数字/下划线/连字符")
        return 2

    print("=" * 76)
    print("A311D 启动 label 修复  ->  %s" % args.label)
    print("=" * 76)
    is_vol = bool(re.match(r"^[A-Za-z]:\\?$", args.target))
    io = None
    try:
        if is_vol:
            io = VolumeIO(args.target, rw=not args.dry_run)
        else:
            if not os.path.isfile(args.target):
                print("文件不存在: %s" % args.target)
                return 2
            io = ImageIO(args.target, rw=not args.dry_run)
        rc = fix(io, target_label=args.label, backup=args.backup,
                 dry_run=args.dry_run,
                 do_kernel_cmdline=not args.no_kernel_cmdline,
                 do_marker=not args.no_marker)
        return rc
    finally:
        if io:
            io.close()


if __name__ == "__main__":
    sys.exit(main())
