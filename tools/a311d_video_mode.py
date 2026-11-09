#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A311D bootsafe: remove the forced HDMI ``video=`` mode from every boot path.

Problem
-------
The bootsafe image inherits the S922X-BOX reference boot chain. Its
``cfgload`` U-Boot script AND both Android boot headers carry::

    video=HDMI-A-1:1920x1080@60

On the CAINIAO CNIoT-CORE (A311D / G12B) the kernel prints::

    meson drm ff900000.vpu drm user-defined mode not support

and the display never initialises. The mainline 6.18 meson DRM driver
contains the literal ``"User-defined mode not supported: \\"%s\\": %d ..."``
and rejects any drm_display_mode with DRM_MODE_TYPE_USERDEF, because the
VENC/ENCP encoder can only be programmed from the fixed timing table or
from an EDID-derived mode. ``video=<connector>:<xres>x<yres>@<refresh>``
makes drm_kms_helper synthesise exactly such a USERDEF mode, so KMS has no
valid mode and the console/EmulationStation stays stuck.

Fix
---
Replace the forced mode with ``video=HDMI-A-1:D`` (the same spelling used by
ROCKNIX's own getedid / update-bootloader-edid-amlogic scripts): 'D' only
marks the connector as digital/enabled and creates NO user-defined mode, so
the driver picks the monitor's EDID preferred mode. ``--mode none`` removes
the parameter entirely.

All copies must be changed together, otherwise the surviving parameter is
still seen by the kernel:

  * /flash/cfgload                      (U-Boot bootargs)
  * /flash/kernel.img  Android header  (cmdline @64, extra_cmdline @576)
  * /flash/kernel2.img Android header  (fallback boot path in cfgload)

Usage
-----
  python a311d_video_mode.py --check --drive F:
  python a311d_video_mode.py --check --image <img>
  python a311d_video_mode.py --drive F: --mode D --yes          # patch USB
  python a311d_video_mode.py --drive F: --restore --yes         # undo
  python a311d_video_mode.py --image <img> --out <new.img>      # patch a copy
  python a311d_video_mode.py --drive F: --dry-run               # show, no write

Stdlib only. --drive creates <name>.bak once and never overwrites an existing
backup. --image never touches the source image (a copy is written to --out).
"""

import argparse
import os
import re
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from a311d_firmware_verify import Fat32, SECTOR
except ImportError:
    print("ERROR: a311d_firmware_verify.py must sit next to this script")
    raise

ANDROID_MAGIC = b"ANDROID!"
CMD_OFF, CMD_LEN = 64, 512
EXTRA_OFF, EXTRA_LEN = 576, 1024
FORCED_RE = re.compile(rb"video=HDMI-A-1:[^\s\x00'\"]+")
VIDEO_ANY_RE = re.compile(rb"video=[^\s\x00'\"]+")
TARGETS = ("cfgload", "kernel.img", "kernel2.img")


def norm_drive(d):
    d = d.strip().replace("/", "\\")
    if len(d) == 1 or (len(d) == 2 and d[1] in ":："):
        d = d[0] + ":\\"
    elif len(d) == 2 and d[1] in ":：":
        d = d[0] + ":\\"
    return d


def new_video_bytes(mode):
    if mode == "none":
        return b""
    return b"video=HDMI-A-1:D"


def replace_video(blob, mode):
    """Replace a forced video= token inside a cmdline/script byte string."""
    new = new_video_bytes(mode)
    if new:
        return FORCED_RE.sub(new, blob), True
    # remove the token plus ONE preceding separator to avoid a double space
    return re.sub(rb"[ ]?" + FORCED_RE.pattern, b"", blob), False


def describe(blob):
    m = VIDEO_ANY_RE.search(blob or b"")
    if not m:
        return "none (EDID autodetect)", "OK"
    tok = m.group(0).decode("ascii", "replace")
    if ":" in tok and re.search(r"\d+x\d+", tok):
        return tok, "RISK (user-defined mode rejected by meson DRM)"
    return tok, "OK"


def header_view(blob):
    """Return (cmdline_bytes, extra_bytes, kernel_size, ramdisk_size)."""
    if len(blob) < EXTRA_OFF + EXTRA_LEN or blob[:8] != ANDROID_MAGIC:
        return None
    ks, _, rs, _ = struct.unpack_from("<4I", blob, 8)
    cmd = blob[CMD_OFF:CMD_OFF + CMD_LEN].split(b"\0")[0]
    extra = blob[EXTRA_OFF:EXTRA_OFF + EXTRA_LEN].split(b"\0")[0]
    return cmd, extra, ks, rs


# --------------------------------------------------------------------------
# drive (mounted FAT volume)
# --------------------------------------------------------------------------
def drive_paths(drive):
    return [os.path.join(drive, n) for n in TARGETS if os.path.isfile(os.path.join(drive, n))]


def check_drive(drive):
    print("check  : %s" % drive)
    risk = 0
    for p in drive_paths(drive):
        name = os.path.basename(p)
        with open(p, "rb") as f:
            blob = f.read()
        if name == "cfgload":
            tok, verdict = describe(blob)
            print("  %-14s %6d B  %-40s %s" % (name, len(blob), tok, verdict))
            line = next((l for l in blob.split(b"\n") if b"bootargs" in l), b"")
            print("       bootargs: %s" % line.decode("latin1", "replace").strip()[:220])
        else:
            hv = header_view(blob)
            if hv is None:
                print("  %-14s %6d B  (not an Android boot image, skipped)" % (name, len(blob)))
                continue
            cmd, extra, ks, rs = hv
            tok, verdict = describe(cmd)
            print("  %-14s %6d B  %-40s %s" % (name, len(blob), tok, verdict))
            print("       cmdline: %s" % cmd.decode("ascii", "replace")[:220])
            print("       kernel_size=%d ramdisk_size=%d extra=%r" %
                  (ks, rs, extra.decode("ascii", "replace")[:80]))
        if verdict.startswith("RISK"):
            risk += 1
    print("  => %d forced-mode occurrence(s)" % risk)
    return risk


def patch_drive(args):
    drive = norm_drive(args.drive)
    if not os.path.isdir(drive):
        print("ERROR: drive not accessible: %s" % drive)
        return 2
    if args.restore:
        if not args.yes:
            print("ABORT: --restore needs --yes")
            return 0
        restored = 0
        for p in drive_paths(drive):
            bak = p + ".bak"
            if os.path.isfile(bak):
                shutil.copyfile(bak, p)
                print("RESTORED %s <- %s" % (p, bak))
                restored += 1
        print("restored %d file(s)" % restored)
        return 0

    changes = []
    for p in drive_paths(drive):
        name = os.path.basename(p)
        with open(p, "rb") as f:
            blob = f.read()
        if name == "cfgload":
            if not FORCED_RE.search(blob):
                continue
            new = replace_video(blob, args.mode)[0]
            changes.append({"path": p, "name": name, "kind": "full",
                            "data": new, "offset": 0,
                            "old": len(blob), "new": len(new), "cmd": b""})
            continue
        hv = header_view(blob)
        if hv is None:
            continue
        cmd, extra, ks, rs = hv
        if not FORCED_RE.search(cmd) and not FORCED_RE.search(extra):
            continue
        # Only rewrite the 512-byte cmdline slot. The bytes right after it
        # can be the boot image 'id' in header v0/v1, so never touch anything
        # past offset 576 (the shipped extra_cmdline is empty anyway).
        newcmd, _ = replace_video(cmd, args.mode)
        if len(newcmd) > CMD_LEN:
            print("ERROR: cmdline too long in %s" % name)
            return 2
        if FORCED_RE.search(extra):
            print("WARNING: %s has a forced video= in extra_cmdline; it is left"
                  " untouched to avoid clobbering the header id." % name)
        block = bytearray(CMD_LEN)
        block[:len(newcmd)] = newcmd
        changes.append({"path": p, "name": name, "kind": "header",
                        "data": bytes(block), "offset": CMD_OFF,
                        "old": len(blob), "new": len(blob), "cmd": newcmd})

    if not changes:
        print("INFO: no forced video= mode found, nothing to do (already EDID mode)")
        return 0
    print("plan   : %s -> %s" % (drive, describe(new_video_bytes(args.mode))[0]))
    for c in changes:
        print("  %-14s %d B -> %d B (%s)" %
              (c["name"], c["old"], c["new"], c["kind"]))
        if c["cmd"]:
            print("       new cmdline: %s" % c["cmd"].decode("ascii", "replace")[:220])
    if args.dry_run or not args.yes:
        print("DRY RUN: no file written. Add --yes to apply (a .bak copy is kept).")
        return 0

    for c in changes:
        p = c["path"]
        bak = p + ".bak"
        if not os.path.isfile(bak):
            shutil.copyfile(p, bak)
            print("backup : %s" % bak)
        with open(p, "r+b" if c["kind"] == "header" else "wb") as f:
            f.seek(c["offset"])
            f.write(c["data"])
        print("PATCHED: %s" % p)
    print("\npost-check:")
    check_drive(drive)
    print("\nNOTE: kernel.img/kernel2.img md5 now differs from the pristine image.")
    print("      This is an intentional /flash patch, not a bad flash. Use")
    print("      --restore --yes to put the .bak files back.")
    return 0


# --------------------------------------------------------------------------
# image (raw .img with MBR + FAT32 P1)
# --------------------------------------------------------------------------
def fat_entry(fs, want):
    """Locate the 32-byte directory entry of a short-name file in the root dir."""
    base, ext = os.path.splitext(want)
    s83 = (base.upper().ljust(8) + ext.lstrip(".").upper().ljust(3))[:11].encode("latin1")
    for c in fs.chain(fs.rootclus):
        off = fs.coff(c)
        fs.f.seek(off)
        raw = fs.f.read(fs.clustersz)
        for pos in range(0, len(raw), 32):
            e = raw[pos:pos + 32]
            if len(e) < 32 or e[0] == 0x00:
                return None
            if e[0] == 0xE5 or e[11] == 0x0F or (e[11] & 0x08):
                continue
            if e[:11] == s83 or e[:11].rstrip() == s83.rstrip():
                size = struct.unpack_from("<I", e, 28)[0]
                start = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
                return off + pos, size, start
    return None


def check_image(path):
    print("check  : %s" % path)
    fs = Fat32(path)
    risk = 0
    try:
        for want in TARGETS:
            name, data = fs.read(want)
            if data is None:
                print("  %-14s MISSING" % want)
                continue
            if want == "cfgload":
                tok, verdict = describe(data)
                print("  %-14s %6d B  %-40s %s" % (want, len(data), tok, verdict))
            else:
                hv = header_view(data)
                if hv is None:
                    print("  %-14s %6d B  (not Android header)" % (want, len(data)))
                    continue
                cmd, extra, ks, rs = hv
                tok, verdict = describe(cmd)
                print("  %-14s %6d B  %-40s %s" % (want, len(data), tok, verdict))
                print("       cmdline: %s" % cmd.decode("ascii", "replace")[:220])
            if verdict.startswith("RISK"):
                risk += 1
    finally:
        fs.close()
    print("  => %d forced-mode occurrence(s)" % risk)
    return risk


def patch_image(args):
    if not args.out:
        print("ERROR: --image requires --out <new.img> (the source is never modified)")
        return 2
    if os.path.abspath(args.image) == os.path.abspath(args.out):
        print("ERROR: --out must differ from --image")
        return 2
    if os.path.exists(args.out) and not args.yes:
        print("ERROR: %s already exists (pass --yes to overwrite)" % args.out)
        return 2
    risk = check_image(args.image)
    if not risk and not args.yes:
        print("INFO: nothing to patch; still writing a copy would be pointless. Abort.")
        return 0
    print("\ncopy   : %s -> %s" % (args.image, args.out))
    shutil.copyfile(args.image, args.out)

    fs = Fat32(args.out)
    try:
        patches = []
        for want in TARGETS:
            ent = fat_entry(fs, want)
            if ent is None:
                continue
            ent_off, size, start = ent
            foff = fs.coff(start)
            name, data = fs.read(want)
            if data is None:
                continue
            if want == "cfgload":
                if not FORCED_RE.search(data):
                    continue
                new = replace_video(data, args.mode)[0]
                patches.append(("cfgload", foff, ent_off, size, new))
            else:
                hv = header_view(data)
                if hv is None or (not FORCED_RE.search(hv[0]) and not FORCED_RE.search(hv[1])):
                    continue
                cmd, extra, ks, rs = hv
                # Rewrite ONLY the 512-byte cmdline slot; bytes at 576 can be
                # the boot image 'id' (header v0/v1) and must survive intact.
                newcmd, _ = replace_video(cmd, args.mode)
                if len(newcmd) > CMD_LEN:
                    print("ERROR: cmdline too long in %s" % want)
                    return 2
                if FORCED_RE.search(extra):
                    print("WARNING: %s has a forced video= in extra_cmdline;"
                          " left untouched to protect the header id." % want)
                block = bytearray(CMD_LEN)
                block[:len(newcmd)] = newcmd
                patches.append((want, foff + CMD_OFF, None, CMD_LEN, bytes(block)))
                print("  %-14s new cmdline: %s" % (want, newcmd.decode("ascii", "replace")[:200]))
        if not patches:
            print("INFO: no forced mode found; removing the untouched copy.")
            fs.close()
            os.remove(args.out)
            return 0
        fs.close()

        with open(args.out, "r+b") as f:
            for name, foff, ent_off, oldsize, new in patches:
                f.seek(foff)
                f.write(new)
                if name == "cfgload":
                    # zero the stale tail and shrink the directory entry size
                    f.seek(foff + len(new))
                    f.write(b"\0" * max(0, oldsize - len(new)))
                    f.seek(ent_off + 28)
                    f.write(struct.pack("<I", len(new)))
                    print("  %-14s %d B -> %d B (dir entry size updated)" % (name, oldsize, len(new)))
                else:
                    print("  %-14s Android cmdline slot (512 B) rewritten in place" % name)
    finally:
        try:
            fs.close()
        except Exception:
            pass
    print("\npost-check:")
    check_image(args.out)
    print("\ndone   : %s" % args.out)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--drive", help="mounted FAT boot volume, e.g. F:")
    g.add_argument("--image", help="raw bootsafe .img (read-only source)")
    ap.add_argument("--out", help="patched image copy (required with --image)")
    ap.add_argument("--mode", choices=("D", "none"), default="D",
                    help="D = video=HDMI-A-1:D (EDID, default); none = drop video= entirely")
    ap.add_argument("--check", action="store_true", help="read-only inspection")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    ap.add_argument("--restore", action="store_true", help="restore <name>.bak on --drive")
    ap.add_argument("--yes", action="store_true", help="confirm a write operation")
    args = ap.parse_args()

    if args.drive:
        if args.check:
            return 0 if check_drive(norm_drive(args.drive)) == 0 else 1
        return patch_drive(args)
    if args.check:
        return 0 if check_image(args.image) == 0 else 1
    return patch_image(args)


if __name__ == "__main__":
    sys.exit(main())
