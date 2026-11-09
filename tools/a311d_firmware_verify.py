#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Verify the A311D bootsafe firmware image and a Rufus-flashed USB drive.

Why this exists
---------------
The CI-built AURKNIX A311D image black-screens on the CAINIAO CNIoT-CORE box:
its Android boot header has an EMPTY cmdline and an external zstd ramdisk.
The bootsafe image keeps the proven ROCKNIX S922X-BOX boot chain (classic
CoreELEC aml_autoscript/cfgload, kernel-embedded initramfs, full cmdline,
cainiao DTB) and swaps in the AURKNIX SYSTEM squashfs.

This tool turns that construction into machine-checkable acceptance tests:
  * static image checks  (TC-IMG-xx) - run before flashing
  * USB drive checks     (TC-USB-xx) - run after Rufus, before power-on
  * optional marker fix  (--fix-markers) - opt-in, writes two empty files

Stdlib only. Reads images in place; never modifies the image. The only write
operation is --fix-markers on the USB drive, and it requires --yes.

Usage
-----
  python a311d_firmware_verify.py --list-checks
  python a311d_firmware_verify.py                          # image only, auto paths
  python a311d_firmware_verify.py --drive H:              # image + flashed USB
  python a311d_firmware_verify.py --drive H: --json report.json
  python a311d_firmware_verify.py --drive H: --raw-disk 2  # also check physical MBR (admin)
  python a311d_firmware_verify.py --drive H: --fix-markers --yes

Exit codes: 0 = no FAIL, 1 = at least one FAIL, 2 = usage/IO error.
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys

SECTOR = 512
DEFAULT_DIR = r"E:\A311d\AURKNIX-image-A311D-20261031"
DEF_IMAGE = os.path.join(DEFAULT_DIR, "AURKNIX-A311D.aarch64-20261109-bootsafe-edidmode.img")
DEF_REFERENCE = os.path.join(DEFAULT_DIR, "ROCKNIX-S922X-BOX.aarch64-20261108.img")
DEF_AURKNIX = os.path.join(DEFAULT_DIR, "AURKNIX-A311D.aarch64-20261031.img")

CHAIN = ("aml_autoscript", "cfgload", "kernel.img", "dtb.img")
BOOT_REQUIRED = CHAIN + ("SYSTEM", "SYSTEM.MD5")

# The bootsafe chain hard-codes video=HDMI-A-1:1920x1080@60 in cfgload and in
# the Android headers. That creates a DRM_MODE_TYPE_USERDEF mode which the
# mainline meson DRM driver refuses ("User-defined mode not supported"). The
# authorised fix is video=HDMI-A-1:D (or no video= at all), so the exact token
# is normalised when comparing the chain against the pristine reference.
VIDEO_TOKEN_RE = re.compile(rb"video=HDMI-A-1:[^\s\x00'\"]+")
FORCED_VIDEO_RE = re.compile(rb"video=HDMI-A-1:[^\s\x00'\"]*\d+x\d+[^\s\x00'\"]*")


def chain_md5_norm(fs, want, listing):
    """md5 of a chain file with the video= mode token removed.

    cfgload is text (its length changes when the token is shortened), while
    the Android headers keep a fixed 512-byte cmdline slot. Normalise each
    case separately so both the pristine and the patched chain hash equally.
    Files without a video= token are hashed verbatim (binary DTBs must not be
    touched by whitespace folding).
    """
    _name, data = fs.read(want, listing)
    if data is None:
        return None
    if not VIDEO_TOKEN_RE.search(data):
        return hashlib.md5(data).hexdigest()
    folded = re.sub(rb" +", b" ", VIDEO_TOKEN_RE.sub(b"", data))
    if data[:8] == b"ANDROID!":
        slot = re.sub(rb" +", b" ", VIDEO_TOKEN_RE.sub(b"", data[64:576]))
        folded = data[:64] + slot.ljust(512, b"\0") + data[576:]
    return hashlib.md5(folded).hexdigest()
EXPECTED_LABEL = "ROCKNIX"
EXPECTED_STORAGE = "STORAGE"
EXPECTED_CMDLINE_BITS = ("boot=LABEL=ROCKNIX", "disk=LABEL=STORAGE", "console=ttyAML0")
EXPECTED_DTB_HITS = ("cainiao", "cniot", "a311d")

# id, phase, title, expected
CHECKS = [
    ("TC-IMG-01", "image", "image exists / size equals reference", "2198863872 bytes or == reference"),
    ("TC-IMG-02", "image", "MBR + P1 FAT32 layout", "55AA, P1 type 0x0b/0x0c, LBA 32768"),
    ("TC-IMG-03", "image", "P2 ext4 STORAGE present", "type 0x83, 32 MiB, label STORAGE"),
    ("TC-IMG-04", "image", "FAT32 BPB valid / boot label", "label ROCKNIX (matches cmdline)"),
    ("TC-IMG-05", "image", "boot files complete", "all of " + ", ".join(BOOT_REQUIRED)),
    ("TC-IMG-06", "image", "Android boot header", "ANDROID!, ramdisk_size=0, full cmdline"),
    ("TC-IMG-07", "image", "dtb.img is a cainiao/A311D DTB", "d00dfeed + cainiao/cniot/a311d"),
    ("TC-IMG-08", "image", "SYSTEM is a squashfs image", "magic hsqs"),
    ("TC-IMG-09", "image", "SYSTEM md5 == SYSTEM.MD5", "md5 digest identical"),
    ("TC-IMG-10", "image", "boot chain byte-identical to reference", "4 chain files SAME"),
    ("TC-IMG-11", "image", "SYSTEM is the AURKNIX rootfs", "md5 == CI AURKNIX image SYSTEM"),
    ("TC-IMG-12", "image", "first-boot resize trigger + 10G marker",
     ".please_resize_me AND resize_storage_10G => 10G STORAGE + exFAT GAMES"),
    ("TC-IMG-13", "image", "cfgload bootargs == kernel cmdline", "same boot=/disk= labels"),
    ("TC-IMG-14", "image", "whole-image sha256 (--sha256)", "digest recorded, no write"),
    ("TC-IMG-15", "image", "ms_unsupported OTA path marker", "ms_unsupported present on /flash"),
    ("TC-IMG-16", "image", "no forced DRM user-defined mode",
     "no video=HDMI-A-1:<WxH@Hz> in cfgload / Android header"),
    ("TC-USB-01", "usb", "drive reachable / label", "label ROCKNIX"),
    ("TC-USB-02", "usb", "flashed boot files complete", "all of " + ", ".join(BOOT_REQUIRED)),
    ("TC-USB-03", "usb", "flashed files byte-identical to image", "md5+size SAME for 6 files"),
    ("TC-USB-04", "usb", "flashed kernel.img header", "same cmdline as image"),
    ("TC-USB-05", "usb", "flashed dtb.img", "d00dfeed + cainiao"),
    ("TC-USB-06", "usb", "no CI-chain leftovers", "no cfgload_env / device_trees / config.ini / kernel.img.md5"),
    ("TC-USB-07", "usb", "raw MBR (--raw-disk N)", "P1 LBA/type == image MBR"),
    ("TC-USB-08", "usb", "first-boot markers on USB",
     "resize_storage_*G AND ms_unsupported both present (TC-IMG-12/15)"),
    ("TC-USB-09", "usb", "no forced DRM mode on USB",
     "cfgload + kernel.img header free of video=HDMI-A-1:<WxH@Hz>"),
]


class Report(object):
    def __init__(self):
        self.items = []

    def add(self, cid, status, actual, detail=""):
        self.items.append({"id": cid, "status": status, "actual": str(actual), "detail": detail})

    def get(self, cid):
        for i in self.items:
            if i["id"] == cid:
                return i
        return None

    def fails(self):
        return [i for i in self.items if i["status"] == "FAIL"]

    def render(self):
        by_id = {i["id"]: i for i in self.items}
        print("")
        print("=" * 78)
        print("A311D bootsafe firmware - verification report")
        print("=" * 78)
        for cid, phase, title, expected in CHECKS:
            item = by_id.get(cid)
            if item is None:
                print("  %-10s SKIP  %-46s (not run)" % (cid, title))
                continue
            print("  %-10s %-5s %s" % (cid, item["status"], title))
            print("               expected: %s" % expected)
            print("               actual  : %s" % item["actual"][:160])
            if item["detail"]:
                print("               detail  : %s" % item["detail"][:160])
        print("-" * 78)
        n = {s: sum(1 for i in self.items if i["status"] == s)
             for s in ("PASS", "FAIL", "SKIP", "INFO")}
        print("  PASS=%d  FAIL=%d  SKIP=%d  INFO=%d" % (n["PASS"], n["FAIL"], n["SKIP"], n["INFO"]))
        if self.fails():
            print("  RESULT: FAIL - do NOT power on with this image/drive")
        else:
            print("  RESULT: PASS - safe to proceed to power-on checks")
        print("=" * 78)
        return 1 if self.fails() else 0


class Fat32(object):
    """Minimal read-only FAT32 parser (LFN aware), same layout as make_a311d_bootable.py."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        mbr = self.f.read(SECTOR)
        if mbr[510:512] != b"\x55\xaa":
            raise ValueError("invalid MBR signature in %s" % path)
        self.mbr = mbr
        self.parts = []
        for i in range(4):
            e = mbr[446 + i * 16:446 + i * 16 + 16]
            lba, num = struct.unpack("<II", e[8:16])
            if num:
                self.parts.append({"n": i + 1, "type": e[4], "lba": lba,
                                   "num": num, "off": lba * SECTOR})
        self.boot = next((p for p in self.parts if p["type"] in (0x0B, 0x0C)), None)
        if self.boot is None:
            raise ValueError("no FAT32 partition in %s" % path)
        self.base = self.boot["off"]
        self.f.seek(self.base)
        bpb = self.f.read(SECTOR)
        if bpb[510:512] != b"\x55\xaa":
            raise ValueError("invalid FAT32 boot sector")
        self.bps = struct.unpack_from("<H", bpb, 11)[0]
        self.spc = bpb[13]
        self.reserved = struct.unpack_from("<H", bpb, 14)[0]
        self.numfats = bpb[16]
        self.fatsz = struct.unpack_from("<I", bpb, 36)[0]
        self.rootclus = struct.unpack_from("<I", bpb, 44)[0]
        self.label = bpb[71:82].decode("latin1", "replace").strip("\x00 ")
        self.fat_off = self.base + self.reserved * self.bps
        self.data_off = self.base + (self.reserved + self.numfats * self.fatsz) * self.bps
        self.clustersz = self.spc * self.bps
        self.f.seek(self.fat_off)
        self.fat = self.f.read(self.fatsz * self.bps)

    def close(self):
        self.f.close()

    def _next(self, c):
        off = c * 4
        if off + 4 > len(self.fat):
            return 0x0FFFFFFF
        return struct.unpack_from("<I", self.fat, off)[0] & 0x0FFFFFFF

    def chain(self, start):
        out, c, seen = [], start, set()
        while 2 <= c < 0x0FFFFFF8 and c not in seen and len(out) < 4_000_000:
            seen.add(c)
            out.append(c)
            c = self._next(c)
        return out

    def coff(self, c):
        return self.data_off + (c - 2) * self.clustersz

    def files(self):
        result, lfn = {}, ""
        for c in self.chain(self.rootclus):
            self.f.seek(self.coff(c))
            raw = self.f.read(self.clustersz)
            for pos in range(0, len(raw), 32):
                e = raw[pos:pos + 32]
                if len(e) < 32 or e[0] == 0x00:
                    return result
                if e[0] == 0xE5:
                    lfn = ""
                    continue
                attr = e[11]
                if attr == 0x0F:
                    chars = e[1:11] + e[14:26] + e[28:32]
                    lfn = chars.decode("utf-16le", "ignore").split("\x00")[0] + lfn
                    continue
                if attr & 0x08:
                    lfn = ""
                    continue
                short = e[0:11].decode("latin1").strip()
                name = lfn if lfn else short
                lfn = ""
                size = struct.unpack_from("<I", e, 28)[0]
                start = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
                result[name] = (size, start, attr)
        return result

    def resolve(self, want, listing=None):
        listing = listing if listing is not None else self.files()
        low = want.lower()
        for n, v in listing.items():
            if n.lower() == low:
                return n, v
        base, ext = os.path.splitext(want)
        s83 = (base.upper().ljust(8) + ext.lstrip(".").upper().ljust(3))[:11]
        for n, v in listing.items():
            if n.upper().ljust(11)[:11] == s83:
                return n, v
        return None, None

    def stream(self, size, start):
        left = size
        for c in self.chain(start):
            if left <= 0:
                break
            self.f.seek(self.coff(c))
            chunk = self.f.read(min(self.clustersz, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk
        if left > 0:
            raise IOError("FAT chain shorter than file size (%d left)" % left)

    def read(self, want, listing=None):
        name, v = self.resolve(want, listing)
        if v is None:
            return None, None
        return name, b"".join(self.stream(v[0], v[1]))

    def md5(self, want, listing=None):
        name, v = self.resolve(want, listing)
        if v is None:
            return None, None
        h = hashlib.md5()
        for chunk in self.stream(v[0], v[1]):
            h.update(chunk)
        return name, (h.hexdigest(), v[0])


def ext4_label(f, part):
    f.seek(part["off"] + 1024 + 0x78)
    return f.read(16).split(b"\x00")[0].decode("latin1", "replace")


def has_resize_trigger(f, part):
    needle = b".please_resize_me"
    f.seek(part["off"])
    left, tail = part["num"] * SECTOR, b""
    while left > 0:
        blk = f.read(min(4 * 1024 * 1024, left))
        if not blk:
            break
        left -= len(blk)
        if needle in tail + blk:
            return True
        tail = blk[-len(needle):]
    return False


def boot_header(data):
    if data[:8] != b"ANDROID!":
        return {"android": False, "magic": data[:8].hex()}
    ks, _ka, rs, _ra, _ds, _sa, _tags, ps, _v, _osv = struct.unpack("<10I", data[8:48])
    cmd = data[64:576].split(b"\x00")[0].decode("ascii", "replace")
    extra = data[608:1632].split(b"\x00")[0].decode("ascii", "replace")
    return {"android": True, "kernel_size": ks, "ramdisk_size": rs,
            "page_size": ps, "cmdline": (cmd + " " + extra).strip()}


def dtb_info(data):
    if data[:4] != b"\xd0\x0d\xfe\xed":
        return {"valid": False, "magic": data[:4].hex()}
    return {"valid": True, "size": len(data),
            "hits": [w for w in EXPECTED_DTB_HITS if w.encode() in data.lower()]}


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest(), os.path.getsize(path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def parse_md5_text(data):
    m = re.match(rb"([0-9a-fA-F]{32})", data or b"")
    return m.group(1).decode().lower() if m else None


def labels_in_cmdline(cmdline):
    boot = re.search(r"boot=LABEL=([A-Za-z0-9_-]+)", cmdline or "")
    disk = re.search(r"disk=LABEL=([A-Za-z0-9_-]+)", cmdline or "")
    return (boot.group(1) if boot else None), (disk.group(1) if disk else None)


def check_image(args, rep):
    for p in (args.image,):
        if not os.path.isfile(p):
            raise IOError("image not found: %s" % p)
    size = os.path.getsize(args.image)
    fs = Fat32(args.image)
    listing = fs.files()
    md5s = {}
    kernel = fs.read("kernel.img", listing)[1]
    dtb = fs.read("dtb.img", listing)[1]
    cfgload = fs.read("cfgload", listing)[1]
    system_md5_text = fs.read("SYSTEM.MD5", listing)[1]
    hdr = boot_header(kernel or b"")

    ref_size = os.path.getsize(args.reference) if os.path.isfile(args.reference) else None
    ok = (size == 2198863872) or (ref_size is not None and size == ref_size)
    rep.add("TC-IMG-01", "PASS" if ok else "FAIL", "%d bytes (ref=%s)" % (size, ref_size),
            "image path=%s" % args.image)

    p1 = fs.boot
    ok = p1["lba"] == 32768 and p1["type"] in (0x0B, 0x0C)
    rep.add("TC-IMG-02", "PASS" if ok else "FAIL",
            "P1 type=0x%02x lba=%d sectors=%d" % (p1["type"], p1["lba"], p1["num"]),
            "MBR signature 55AA OK" if fs.mbr[510:512] == b"\x55\xaa" else "MBR signature invalid")

    p2 = next((p for p in fs.parts if p is not p1 and p["type"] == 0x83), None)
    if p2:
        lbl = ext4_label(fs.f, p2)
        ok = lbl == EXPECTED_STORAGE and p2["num"] == 65536
        rep.add("TC-IMG-03", "PASS" if ok else "FAIL",
                "P%d type=0x83 %d sectors label=%r" % (p2["n"], p2["num"], lbl))
    else:
        p2 = None
        rep.add("TC-IMG-03", "FAIL", "no ext4 P2 found")

    ok = fs.label == EXPECTED_LABEL and fs.bps == 512
    rep.add("TC-IMG-04", "PASS" if ok else "FAIL",
            "label=%r bps=%d spc=%d rootclus=%d" % (fs.label, fs.bps, fs.spc, fs.rootclus))

    missing = [w for w in BOOT_REQUIRED if fs.resolve(w, listing)[0] is None]
    rep.add("TC-IMG-05", "FAIL" if missing else "PASS",
            "missing=%s" % (missing or "none"),
            "%d root entries" % len(listing))

    cmdline = hdr.get("cmdline", "")
    bits_ok = all(b in cmdline for b in EXPECTED_CMDLINE_BITS)
    ok = hdr.get("android") and hdr.get("ramdisk_size") == 0 and bool(cmdline) and bits_ok
    rep.add("TC-IMG-06", "PASS" if ok else "FAIL",
            "magic=%s ramdisk_size=%s kernel_size=%s page=%s" %
            ("ANDROID!" if hdr.get("android") else hdr.get("magic"),
             hdr.get("ramdisk_size"), hdr.get("kernel_size"), hdr.get("page_size")),
            "cmdline=%r" % cmdline)

    di = dtb_info(dtb or b"")
    ok = di["valid"] and len(di["hits"]) == len(EXPECTED_DTB_HITS)
    rep.add("TC-IMG-07", "PASS" if ok else "FAIL",
            "valid=%s size=%s hits=%s" % (di["valid"], di.get("size"), di.get("hits", [])))

    system_head = b""
    name, v = fs.resolve("SYSTEM", listing)
    if v:
        for chunk in fs.stream(min(v[0], 4096), v[1]):
            system_head += chunk
            break
    ok = system_head[:4] == b"hsqs"
    rep.add("TC-IMG-08", "PASS" if ok else "FAIL", "magic=%r" % system_head[:4])

    _, sysinfo = fs.md5("SYSTEM", listing)
    md5s["SYSTEM"] = sysinfo
    declared = parse_md5_text(system_md5_text)
    ok = bool(sysinfo and declared and sysinfo[0] == declared)
    rep.add("TC-IMG-09", "PASS" if ok else "FAIL",
            "computed=%s declared=%s" % (sysinfo[0] if sysinfo else None, declared),
            "SYSTEM.MD5 raw=%r" % (system_md5_text or b"")[:80])

    # boot chain md5 (also reused by USB checks)
    for w in list(CHAIN) + ["SYSTEM.MD5"]:
        _, info = fs.md5(w, listing)
        md5s[w] = info

    if os.path.isfile(args.reference):
        ref = Fat32(args.reference)
        rl = ref.files()
        diffs = []
        for w in CHAIN:
            a = chain_md5_norm(ref, w, rl)
            b = chain_md5_norm(fs, w, listing)
            if not a or not b or a != b:
                diffs.append(w)
        ref.close()
        rep.add("TC-IMG-10", "FAIL" if diffs else "PASS",
                "different=%s" % (diffs or "none"),
                "only SYSTEM/SYSTEM.MD5 may differ; the video= mode token is "
                "masked so tools/a311d_video_mode.py patches stay PASS")
    else:
        rep.add("TC-IMG-10", "SKIP", "reference image not found: %s" % args.reference)

    if os.path.isfile(args.aurknix):
        orig = Fat32(args.aurknix)
        _, oinfo = orig.md5("SYSTEM", orig.files())
        orig.close()
        ok = bool(oinfo and sysinfo and oinfo[0] == sysinfo[0])
        rep.add("TC-IMG-11", "PASS" if ok else "FAIL",
                "bootsafe=%s ci=%s" % (sysinfo[0] if sysinfo else None, oinfo[0] if oinfo else None))
    else:
        rep.add("TC-IMG-11", "SKIP", "CI AURKNIX image not found: %s" % args.aurknix)

    trigger = has_resize_trigger(fs.f, p2) if p2 else False
    # fs-resize uses `find /flash -name 'resize_storage_*G'` (case sensitive):
    # without this marker it takes resizepart 2 100%, so STORAGE (ext4) eats
    # the whole disk and the third exFAT GAMES partition is never created.
    markers = [n for n in listing if re.fullmatch(r"resize_storage_\d+G", n)]
    ok = bool(trigger and markers)
    rep.add("TC-IMG-12", "PASS" if ok else "FAIL",
            ".please_resize_me=%s flash_markers=%s" % (trigger, markers or "none"),
            "missing marker => 100% ext4 STORAGE, NO GAMES partition")
    ms_marker = "ms_unsupported" in [n.lower() for n in listing]
    rep.add("TC-IMG-15", "PASS" if ms_marker else "FAIL",
            "ms_unsupported=%s" % ms_marker,
            "init:838 selects /update_part/.update for OTA packages")
    forced_mode = [n for n, b in
                   (("header", (cmdline or "").encode("ascii", "replace")),
                    ("cfgload", cfgload or b"")) if FORCED_VIDEO_RE.search(b)]
    rep.add("TC-IMG-16", "FAIL" if forced_mode else "PASS",
            "forced_mode_in=%s" % (forced_mode or "none"),
            "video=<WxH@Hz> creates a USERDEF mode rejected by meson DRM; "
            "use tools/a311d_video_mode.py to switch to video=HDMI-A-1:D")

    cfg_txt = (cfgload or b"").decode("latin1", "replace")
    cb, cd = labels_in_cmdline(cfg_txt)
    hb, hd = labels_in_cmdline(cmdline)
    ok = bool(cb and hb and cb == hb and cd == hd)
    rep.add("TC-IMG-13", "PASS" if ok else "FAIL",
            "cfgload boot=%s disk=%s / header boot=%s disk=%s" % (cb, cd, hb, hd))

    if args.sha256:
        digest = sha256_file(args.image)
        rep.add("TC-IMG-14", "PASS", digest, "computed over %d bytes" % size)
    else:
        rep.add("TC-IMG-14", "SKIP", "use --sha256 to compute", "2.05 GiB read, opt-in")

    fs.close()
    return {"md5s": md5s, "cmdline": cmdline, "label": fs.label,
            "mbr": fs.mbr, "p1": p1}


def norm_drive(drive):
    d = drive.strip().rstrip("\\/")
    if len(d) == 1:
        d += ":"
    return d + "\\"


def find_on_drive(drive, want, names):
    # FAT 8.3 entries ("SYSTEM  MD5", "KERNEL  IMG") are shown by Windows as
    # "SYSTEM.MD5" / "KERNEL.IMG"; strip every non-alphanumeric char so both
    # spellings resolve to the same key.
    key = re.sub(r"[^a-z0-9]", "", want.lower())
    for n in names:
        if re.sub(r"[^a-z0-9]", "", n.lower()) == key:
            return os.path.join(drive, n)
    return None


def check_drive(args, rep, baseline):
    drive = norm_drive(args.drive)
    if not os.path.isdir(drive):
        rep.add("TC-USB-01", "FAIL", "drive not accessible: %s" % drive,
                "Rufus may still be writing, or the drive has no letter yet")
        for cid, _p, _t, _e in CHECKS:
            if cid.startswith("TC-USB-") and cid != "TC-USB-01" and rep.get(cid) is None:
                rep.add(cid, "SKIP", "drive not accessible")
        return
    names = os.listdir(drive)
    # volume label via a tiny Windows-safe probe (no external dependency)
    label = None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        fsbuf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive), buf, 1024, None, None, None, fsbuf, 1024)
        label = buf.value
    except Exception:
        label = None
    ok = (label or "").upper() == EXPECTED_LABEL
    rep.add("TC-USB-01", "PASS" if ok else "FAIL",
            "drive=%s label=%r fs_entries=%d" % (drive, label, len(names)),
            "expected ROCKNIX (kernel cmdline boot=LABEL=ROCKNIX)")

    paths = {w: find_on_drive(drive, w, names) for w in BOOT_REQUIRED}
    missing = [w for w, p in paths.items() if p is None]
    rep.add("TC-USB-02", "FAIL" if missing else "PASS", "missing=%s" % (missing or "none"))

    diffs = []
    for w, p in paths.items():
        if p is None:
            continue
        got = md5_file(p)
        want = baseline["md5s"].get(w)
        if not want or got != want:
            diffs.append("%s (usb=%s img=%s)" % (w, got, want))
    if missing:
        status = "FAIL"
    else:
        status = "FAIL" if diffs else "PASS"
    rep.add("TC-USB-03", status, "different=%s" % (diffs or "none"),
            "compares amd64-side md5 of each flashed file with the image")

    kp = paths.get("kernel.img")
    if kp:
        with open(kp, "rb") as f:
            khead = f.read(2048)
        h = boot_header(khead)
        ok = h.get("android") and h.get("ramdisk_size") == 0 and h.get("cmdline") == baseline["cmdline"]
        rep.add("TC-USB-04", "PASS" if ok else "FAIL",
                "ramdisk_size=%s cmdline_match=%s" %
                (h.get("ramdisk_size"), h.get("cmdline") == baseline["cmdline"]),
                "cmdline=%r" % h.get("cmdline", "")[:120])
    else:
        rep.add("TC-USB-04", "SKIP", "kernel.img missing")

    dp = paths.get("dtb.img")
    if dp:
        with open(dp, "rb") as f:
            d = f.read()
        di = dtb_info(d)
        ok = di["valid"] and len(di["hits"]) == len(EXPECTED_DTB_HITS)
        rep.add("TC-USB-05", "PASS" if ok else "FAIL", "valid=%s hits=%s" % (di["valid"], di.get("hits")))
    else:
        rep.add("TC-USB-05", "SKIP", "dtb.img missing")

    # NOTE: ms_unsupported used to be listed here as a "CI chain leftover".
    # That was wrong: it is a deliberate first-boot marker (init:838 OTA path)
    # and TC-IMG-15 requires it to be present. Keep only genuine CI-chain
    # leftovers in this blacklist; assert the markers separately in TC-USB-08.
    leftovers = [n for n in names if n.lower() in
                 ("cfgload_env", "device_trees", "config.ini", "kernel.img.md5")]
    rep.add("TC-USB-06", "FAIL" if leftovers else "PASS", "leftovers=%s" % (leftovers or "none"),
            "presence means the CI (black-screen) chain was flashed instead")

    lower_names = [n.lower() for n in names]
    resize_markers = [n for n in names if re.match(r"resize_storage_\d+G", n, re.I)]
    ms_marker = "ms_unsupported" in lower_names
    markers_ok = bool(resize_markers) and ms_marker
    rep.add("TC-USB-08", "PASS" if markers_ok else "FAIL",
            "resize_markers=%s ms_unsupported=%s" % (resize_markers or "none", ms_marker),
            "without resize_storage_*G fs-resize grows STORAGE to 100% and skips the exFAT GAMES partition")

    forced = []
    for w in ("cfgload", "kernel.img", "kernel2.img"):
        p = paths.get(w) or find_on_drive(drive, w, names)
        if not p:
            continue
        with open(p, "rb") as f:
            head = f.read(2048) if w != "cfgload" else f.read()
        if FORCED_VIDEO_RE.search(head):
            forced.append(w)
    rep.add("TC-USB-09", "FAIL" if forced else "PASS",
            "forced_mode_in=%s" % (forced or "none"),
            "a forced video=<WxH@Hz> makes meson DRM reject the USERDEF mode "
            "and can stall the display stack")

    if args.raw_disk is not None:
        raw = r"\\.\PhysicalDrive%d" % args.raw_disk
        try:
            with open(raw, "rb") as f:
                m = f.read(512)
            p1 = baseline["p1"]
            lba, num = struct.unpack("<II", m[446 + 8:446 + 16])
            ok = m[510:512] == b"\x55\xaa" and lba == p1["lba"] and m[446 + 4] == p1["type"]
            rep.add("TC-USB-07", "PASS" if ok else "FAIL",
                    "P1 type=0x%02x lba=%d (image 0x%02x/%d)" % (m[446 + 4], lba, p1["type"], p1["lba"]))
        except Exception as exc:
            rep.add("TC-USB-07", "SKIP", "raw read failed: %s" % exc,
                    "needs administrator privileges; file-level checks still apply")
    else:
        rep.add("TC-USB-07", "SKIP", "use --raw-disk N to check the physical MBR")


def fix_markers(args):
    drive = norm_drive(args.drive)
    if not os.path.isdir(drive):
        print("ERROR: drive not accessible: %s" % drive)
        return 2
    names = os.listdir(drive)
    todo = []
    if not [n for n in names if re.match(r"resize_storage_\d+G", n, re.I)]:
        todo.append("resize_storage_10G")
    if "ms_unsupported" not in [n.lower() for n in names]:
        todo.append("ms_unsupported")
    if not todo:
        print("INFO: both markers already present (nothing to do)")
        return 0
    print("About to create: %s" % ", ".join(os.path.join(drive, n) for n in todo))
    print("Effect: first boot expands STORAGE to 10 GiB and creates an EXFAT GAMES")
    print("        partition in the remaining space (fs-resize lines 22-26, 84-106).")
    print("        ms_unsupported selects the OTA path /update_part/.update (init:838).")
    print("Without resize_storage_10G STORAGE grows to 100% and NO GAMES is made.")
    print("NOTE: on an already-expanded disk fs-resize refuses to run again;")
    print("      reflash the image to actually obtain the GAMES partition.")
    if not args.yes:
        print("ABORT: pass --yes to apply. No file was written.")
        return 0
    for n in todo:
        target = os.path.join(drive, n)
        with open(target, "wb"):
            pass
        print("CREATED: %s (%d bytes)" % (target, os.path.getsize(target)))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default=DEF_IMAGE, help="bootsafe .img (default: %(default)s)")
    ap.add_argument("--reference", default=DEF_REFERENCE, help="known-good S922X-BOX .img")
    ap.add_argument("--aurknix", default=DEF_AURKNIX, help="CI AURKNIX .img (rootfs donor)")
    ap.add_argument("--drive", help="flashed USB drive letter, e.g. H:")
    ap.add_argument("--raw-disk", type=int, help="also check \\\\.\\PhysicalDriveN (needs admin)")
    ap.add_argument("--sha256", action="store_true", help="compute whole-image sha256")
    ap.add_argument("--fix-markers", action="store_true", help="create resize_storage_10G on USB")
    ap.add_argument("--yes", action="store_true", help="confirm the marker write")
    ap.add_argument("--json", help="write machine-readable report to this path")
    ap.add_argument("--list-checks", action="store_true", help="print all test cases and exit")
    args = ap.parse_args()

    if args.list_checks:
        for cid, phase, title, expected in CHECKS:
            print("%-10s [%-5s] %-46s expected: %s" % (cid, phase, title, expected))
        return 0

    if args.fix_markers:
        if not args.drive:
            ap.error("--fix-markers requires --drive")
        return fix_markers(args)

    try:
        rep = Report()
        print("Inspecting image: %s" % args.image)
        baseline = check_image(args, rep)
        if args.drive:
            print("Inspecting USB drive: %s" % args.drive)
            check_drive(args, rep, baseline)
        else:
            for cid, _p, _t, _e in CHECKS:
                if cid.startswith("TC-USB-"):
                    rep.add(cid, "SKIP", "use --drive H: after Rufus finishes")
    except Exception as exc:
        print("ERROR: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    rc = rep.render()
    if args.json:
        payload = {"image": args.image, "drive": args.drive,
                   "checks": [dict((k, v) for k, v in i.items()) for i in rep.items],
                   "result": "FAIL" if rep.fails() else "PASS"}
        # attach expected/title for traceability
        meta = {c[0]: {"phase": c[1], "title": c[2], "expected": c[3]} for c in CHECKS}
        for i in payload["checks"]:
            i.update(meta.get(i["id"], {}))
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print("  JSON report: %s" % args.json)
    return rc


if __name__ == "__main__":
    sys.exit(main())
