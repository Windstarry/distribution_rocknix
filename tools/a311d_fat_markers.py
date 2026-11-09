#!/usr/bin/env python3
"""A311D bootsafe: inspect and restore the first-boot FAT markers.

Problem
-------
The bootsafe image is produced by copying the ROCKNIX S922X-BOX reference
image and replacing only SYSTEM + SYSTEM.MD5. The reference /flash carries
NO `resize_storage_10G` and NO `ms_unsupported`. A CI-built AURKNIX image
gets both from scripts/mkimage:284/286.

Consequence on first boot (proven by /flash/fs-resize.log on the real box):

    fs-resize:  RESIZE_FILE=$(find /flash -name 'resize_storage_*G')
    empty    -> parted resizepart 2 100%  -> STORAGE (ext4) eats the
                whole remaining 29.2 GiB; no GAMES partition is created.
    present  -> STORAGE stays at 10 GiB and parted creates a third
                "fat32"-type partition which is then mkfs.exfat -n GAMES,
                i.e. the extra Windows-visible games drive.

`ms_unsupported` additionally selects the OTA update path in
busybox/scripts/init:838 (`/update_part/.update` instead of
`/update_part/roms/.update`).

This tool parses FAT32 long file names (LFN) properly - a raw byte scan for
the marker names cannot work because LFN characters are stored in 5/6/2
character chunks separated by attribute/checksum bytes.

Usage
-----
  python tools/a311d_fat_markers.py --list  <image.img>
  python tools/a311d_fat_markers.py --check <image.img>
  python tools/a311d_fat_markers.py --add   <image.img> --out <new.img>
  python tools/a311d_fat_markers.py --drive F:

--add copies the image and injects two ZERO-BYTE files (no cluster
allocation is needed: fs-resize and init only test for existence).
--drive writes the same two markers directly onto an already mounted FAT
boot volume; note this does NOT re-run fs-resize on a disk that has already
been expanded (the .please_resize_me trigger is gone and /storage is
initialised), a reflash is required for the GAMES partition to appear.

Standard library only. All image operations are read-only except --add,
which always writes to a NEW file.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
from pathlib import Path

SECTOR = 512
ATTR_LFN = 0x0F
ATTR_VOL = 0x08
ATTR_DIR = 0x10

MARKERS = [
    ("resize_storage_10G", "RESIZE~1"),
    ("ms_unsupported", "MSUNSU~1"),
]


class Fat32:
    """Minimal FAT32 root-directory reader/writer (LFN aware)."""

    def __init__(self, path: str, writable: bool = False, part_offset: int | None = None):
        self.f = open(path, "r+b" if writable else "rb")
        if part_offset is None:
            mbr = self.f.read(SECTOR)
            self.f.seek(0)
            if mbr[510:512] != b"\x55\xaa":
                raise ValueError("invalid MBR signature")
            part_offset = struct.unpack_from("<I", mbr, 0x1BE + 8)[0] * SECTOR
        self.base = part_offset
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
        self.label = bpb[71:82].decode("latin1", "replace").rstrip("\x00 ")
        self.fat_off = self.base + self.reserved * self.bps
        self.data_off = self.base + (self.reserved + self.numfats * self.fatsz) * self.bps
        self.cluster_size = self.spc * self.bps
        self.f.seek(self.fat_off)
        self.fat = self.f.read(self.fatsz * self.bps)

    def close(self) -> None:
        self.f.close()

    def _next(self, c: int) -> int:
        off = c * 4
        if off + 4 > len(self.fat):
            return 0x0FFFFFFF
        return struct.unpack_from("<I", self.fat, off)[0] & 0x0FFFFFFF

    def chain(self, start: int) -> list[int]:
        out, c, guard = [], start, 0
        while 2 <= c < 0x0FFFFFF8 and guard < 4_000_000:
            out.append(c)
            c = self._next(c)
            guard += 1
        return out

    def cluster_offset(self, c: int) -> int:
        return self.data_off + (c - 2) * self.cluster_size

    def _cluster_bytes(self, c: int) -> bytes:
        self.f.seek(self.cluster_offset(c))
        return self.f.read(self.cluster_size)

    def slots(self) -> list[tuple[int, bytes]]:
        """All 32-byte root directory slots in physical order."""
        out = []
        for c in self.chain(self.rootclus):
            raw = self._cluster_bytes(c)
            base = self.cluster_offset(c)
            for pos in range(0, len(raw) - 31, 32):
                out.append((base + pos, raw[pos:pos + 32]))
        return out

    def names(self) -> list[str]:
        """Long (or short) file names in the root directory."""
        out, pending = [], {}
        for _off, e in self.slots():
            if e[0] == 0x00:
                break
            if e[0] == 0xE5:
                pending.clear()
                continue
            if e[11] == ATTR_LFN:
                order = e[0] & 0x1F
                pending[order] = e[1:11] + e[14:26] + e[28:32]
                continue
            if e[11] & ATTR_VOL:
                pending.clear()
                continue
            if pending:
                text = b"".join(pending[i] for i in sorted(pending))
                name = text.decode("utf-16-le", "replace")
                name = name.split("\x00", 1)[0].rstrip("\uffff")
                pending.clear()
            else:
                raw = e[0:11].decode("latin1", "replace")
                name = raw[:8].rstrip()
                ext = raw[8:].rstrip()
                if ext:
                    name += "." + ext
            out.append(name)
        return out

    def short_names(self) -> set[str]:
        return {e[0:11].decode("latin1", "replace").upper()
                for _o, e in self.slots()
                if e[0] not in (0x00, 0xE5) and e[11] != ATTR_LFN and not (e[11] & ATTR_VOL)}

    def free_slot_offsets(self, count: int) -> list[int]:
        s = self.slots()
        for i, (off, e) in enumerate(s):
            if e[0] != 0x00:
                continue
            run = s[i:i + count]
            if len(run) == count and all(x[1][0] == 0x00 for x in run):
                # keep all slots inside the allocated root cluster chain
                if run[-1][0] <= s[-1][0]:
                    return [x[0] for x in run]
        raise RuntimeError("not enough contiguous free root directory slots")

    def add_empty_lfn_file(self, longname: str, shortbase: str) -> None:
        if longname.lower() in [n.lower() for n in self.names()]:
            return
        short11 = (shortbase.upper()[:8] + "   ")[:11].ljust(11)
        if short11 in self.short_names():
            raise RuntimeError("short name collision: %r" % short11)

        checksum = 0
        for b in short11.encode("latin1"):
            checksum = (((checksum & 1) << 7) + (checksum >> 1) + b) & 0xFF

        raw = longname.encode("utf-16-le")
        units = [raw[i:i + 2] for i in range(0, len(raw), 2)]
        units.append(b"\x00\x00")
        while len(units) % 13:
            units.append(b"\xff\xff")
        chunks = [units[i:i + 13] for i in range(0, len(units), 13)]
        n = len(chunks)
        entries = []
        for idx in range(n - 1, -1, -1):  # physical order: highest ord first
            ch = chunks[idx]
            order = idx + 1
            if idx == n - 1:
                order |= 0x40
            e = bytearray(32)
            e[0] = order
            e[1:11] = b"".join(ch[0:5])
            e[11] = ATTR_LFN
            e[13] = checksum
            e[14:26] = b"".join(ch[5:11])
            e[28:32] = b"".join(ch[11:13])
            entries.append(bytes(e))

        short = bytearray(32)
        short[0:11] = short11.encode("latin1")
        short[11] = 0x20  # ARCHIVE
        # size = 0, first cluster = 0: no data cluster needed
        entries.append(bytes(short))

        offs = self.free_slot_offsets(len(entries))
        for off, e in zip(offs, entries):
            self.f.seek(off)
            self.f.write(e)
        self.f.flush()


def inject_markers(image_path: str) -> list[str]:
    """Add both markers to an image in place. Returns names actually added."""
    fs = Fat32(image_path, writable=True)
    try:
        before = set(n.lower() for n in fs.names())
        for longname, shortbase in MARKERS:
            fs.add_empty_lfn_file(longname, shortbase)
        after = set(n.lower() for n in fs.names())
        return sorted(after - before)
    finally:
        fs.close()


def p2_has_resize_trigger(path: str) -> bool:
    """Check the 2nd partition's ext4 directory area for .please_resize_me."""
    with open(path, "rb") as f:
        mbr = f.read(SECTOR)
        if mbr[510:512] != b"\x55\xaa":
            return False
        start = struct.unpack_from("<I", mbr, 0x1BE + 16 + 8)[0] * SECTOR
        f.seek(start)
        blob = f.read(2 * 1024 * 1024)
    return b"please_resize_me" in blob


def cmd_list(path: str) -> int:
    fs = Fat32(path)
    try:
        print("FAT label      : %s" % fs.label)
        print("cluster size   : %d bytes" % fs.cluster_size)
        print("root files     :")
        for n in fs.names():
            mark = "  <== first-boot marker" if n.lower() in [m[0].lower() for m in MARKERS] else ""
            print("   %-28s%s" % (n, mark))
    finally:
        fs.close()
    print("P2 resize trigger: %s" % ("PRESENT" if p2_has_resize_trigger(path) else "MISSING"))
    return 0


def cmd_check(path: str) -> int:
    fs = Fat32(path)
    try:
        names = set(n.lower() for n in fs.names())
    finally:
        fs.close()
    missing = [m[0] for m in MARKERS if m[0].lower() not in names]
    trigger = p2_has_resize_trigger(path)
    print("image      : %s" % path)
    print("markers    : %s" % ("COMPLETE" if not missing else "MISSING " + ", ".join(missing)))
    print("P2 trigger : %s" % ("PRESENT" if trigger else "MISSING"))
    print("first boot : %s" % ("10G STORAGE + exFAT GAMES partition" if not missing and trigger
                              else "100% ext4 STORAGE, NO GAMES partition"))
    return 0 if not missing else 1


def cmd_add(path: str, out: str) -> int:
    if os.path.abspath(path) == os.path.abspath(out):
        raise ValueError("--out must be a different file (source stays untouched)")
    shutil.copyfile(path, out)
    added = inject_markers(out)
    print("copied  : %s -> %s" % (path, out))
    print("added   : %s" % (", ".join(added) if added else "nothing (already present)"))
    return cmd_check(out)


def cmd_drive(drive: str) -> int:
    root = Path(drive)
    if not root.is_dir():
        raise ValueError("not a mounted directory: %s" % drive)
    made = []
    for longname, _short in MARKERS:
        target = root / longname
        if target.exists():
            continue
        target.write_bytes(b"")  # zero-byte marker, matches mkimage's touch
        made.append(longname)
    print("drive   : %s" % drive)
    print("added   : %s" % (", ".join(made) if made else "nothing (already present)"))
    print("NOTE    : on an already-expanded disk fs-resize will refuse to run again")
    print("          (trigger removed, /storage initialised). Reflash for the GAMES")
    print("          partition; ms_unsupported takes effect for OTA immediately.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", metavar="IMG", help="list root files (LFN aware)")
    g.add_argument("--check", metavar="IMG", help="verify markers + P2 trigger")
    g.add_argument("--add", metavar="IMG", help="copy image and inject markers")
    g.add_argument("--drive", metavar="F:", help="add markers to a mounted FAT volume")
    ap.add_argument("--out", metavar="NEW_IMG", help="output for --add")
    args = ap.parse_args()
    try:
        if args.list:
            return cmd_list(args.list)
        if args.check:
            return cmd_check(args.check)
        if args.add:
            if not args.out:
                ap.error("--add requires --out")
            return cmd_add(args.add, args.out)
        return cmd_drive(args.drive)
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
