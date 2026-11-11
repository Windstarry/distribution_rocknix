#!/usr/bin/env python3
r"""A311D first-boot GAMES partition check (read-only, Windows host).

After fs-resize runs, ROCKNIX should have three MBR partitions:
  P1 0x0c FAT32  ROCKNIX (boot)
  P2 0x83 ext4   STORAGE
  P3 0x07 exFAT  GAMES   <- exFAT itself, NOT a FAT32-tagged partition

The historical bug: fs-resize created P3 with `mkpart primary fat32`
(MBR type 0x0c) and then formatted it with mkfs.exfat. Linux blkid
probes by content and works, but Windows mounts 0x0c with its FAT32
driver and reports "damaged / not formatted". exFAT must use type 0x07.

This tool only reads. It never writes to the disk or the volume.
Python 3 standard library only.

Target-disk-change scenario (2026-11-11):
  The USB stick is removed, a new target disk is attached and Windows assigns
  F: to it. The disk has three MBR partitions; P3 carries type 0x07, which
  Windows displays as "NTFS" although it is only the shared IFS/NTFS/exFAT
  partition-type tag written by fs-resize ("mkpart primary ntfs"). If the
  partition was created but mkfs.exfat did not finish, P3 has no valid
  filesystem, Windows reports "not formatted" and assigns no drive letter.
  In that case --volume F: points at P1 (FAT32 ROCKNIX boot), not at P3, and
  the failure must not be attributed to the 5.12 FAT32-type bug, a stale RAW
  cache, or a real NTFS filesystem. Only this scenario gets the extra
  TC-GP-07/08 diagnosis; MBR parsing, exFAT BPB/label handling, P1/P2 layout
  checks and all other disk/partition behaviour are unchanged.

Usage:
  python a311d_games_partition_check.py --volume G:
  python a311d_games_partition_check.py --volume G: --disk 2
  python a311d_games_partition_check.py --volume G: --disk 2 --json report.json

--disk needs administrator rights to open \\.\PhysicalDriveN; without it
the MBR section is reported as SKIP (the volume section still works).
"""

import argparse
import json
import os
import string
import struct
import sys

EXFAT_MAGIC = b"EXFAT   "
MBR_TYPES = {
    0x07: "IFS / NTFS / exFAT (correct for GAMES)",
    0x0B: "FAT32 CHS (WRONG for exFAT)",
    0x0C: "FAT32 LBA XINT13 (WRONG for exFAT)",
    0x83: "Linux native (ext4 STORAGE)",
    0x00: "empty",
}


class Report(object):
    def __init__(self):
        self.items = []

    def add(self, cid, status, detail, note=""):
        self.items.append(dict(id=cid, status=status, detail=detail, note=note))
        print("%-10s %-5s %s" % (cid, status, detail))
        if note:
            print(" " * 17 + "-> " + note)

    def summary(self):
        n = {}
        for it in self.items:
            n[it["status"]] = n.get(it["status"], 0) + 1
        return n


def volume_path(letter):
    v = letter.rstrip(":\\").upper()
    return r"\\.\%s:" % v


def disk_path(number):
    return r"\\.\PhysicalDrive%d" % number


def read_at(path, offset, size):
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(size)


def parse_exfat(head):
    """Parse boot sector + BPB. head must contain at least 512 bytes."""
    if len(head) < 112 or head[3:11] != EXFAT_MAGIC:
        return None
    b = {}
    b["jump"] = head[0:3].hex()
    b["fs_name"] = head[3:11].decode("ascii", "replace").strip()
    b["partition_offset_sectors"] = struct.unpack_from("<Q", head, 64)[0]
    b["volume_length_sectors"] = struct.unpack_from("<Q", head, 72)[0]
    b["fat_offset_sectors"] = struct.unpack_from("<I", head, 80)[0]
    b["fat_length_sectors"] = struct.unpack_from("<I", head, 84)[0]
    b["heap_offset_sectors"] = struct.unpack_from("<I", head, 88)[0]
    b["cluster_count"] = struct.unpack_from("<I", head, 92)[0]
    b["root_dir_cluster"] = struct.unpack_from("<I", head, 96)[0]
    b["bytes_per_sector"] = 1 << head[108]
    b["sectors_per_cluster"] = 1 << head[109]
    b["number_of_fats"] = head[110]
    b["root_dir_offset"] = (
        b["heap_offset_sectors"]
        + (b["root_dir_cluster"] - 2) * b["sectors_per_cluster"]
    ) * b["bytes_per_sector"]
    b["volume_bytes"] = b["volume_length_sectors"] * b["bytes_per_sector"]
    return b


def read_volume_label(path, bpb):
    """Return (label_or_None, entries_seen) from the exFAT root directory."""
    cluster = bpb["bytes_per_sector"] * bpb["sectors_per_cluster"]
    data = read_at(path, bpb["root_dir_offset"], cluster)
    seen = 0
    for i in range(0, len(data), 32):
        e = data[i:i + 32]
        if len(e) < 32:
            break
        t = e[0]
        if t == 0x00:
            break
        seen += 1
        if t == 0x83:                       # volume label directory entry
            count = min(e[1], 11)
            return e[2:2 + count * 2].decode("utf-16-le", "replace"), seen
    return None, seen


def check_volume(rep, letter):
    path = volume_path(letter)
    try:
        head = read_at(path, 0, 4096)
    except OSError as e:
        rep.add("TC-GP-01", "FAIL", "cannot open %s: %s" % (path, e),
                "plug the USB drive back in; Windows may still cache a RAW "
                "volume. After a target-disk change, %s may instead be P1 "
                "(FAT32 boot) while an unformatted 0x07 P3 has no drive letter"
                % letter)
        return None
    bpb = parse_exfat(head)
    rep.add("TC-GP-01", "PASS" if bpb else "FAIL",
            "%s boot sector %s" % (path, head[:11].hex() if head else "empty"),
            "" if bpb else
            "no EXFAT signature on this drive letter; after a target-disk "
            "change it can be P1 (FAT32 boot), while P3 is 0x07/NTFS-tagged, "
            "unformatted and has no drive letter")
    if not bpb:
        return None

    sane = (512 <= bpb["bytes_per_sector"] <= 4096
            and 1 <= bpb["sectors_per_cluster"] <= 128
            and bpb["number_of_fats"] in (1, 2)
            and 2 <= bpb["root_dir_cluster"] < bpb["cluster_count"] + 2
            and bpb["volume_bytes"] > 0)
    rep.add("TC-GP-02", "PASS" if sane else "FAIL",
            "bytes/sector=%d sectors/cluster=%d fats=%d root=%d volume=%.2f GiB"
            % (bpb["bytes_per_sector"], bpb["sectors_per_cluster"],
               bpb["number_of_fats"], bpb["root_dir_cluster"],
               bpb["volume_bytes"] / 2 ** 30),
            "exFAT BPB fields")
    if not sane:
        return bpb

    try:
        label, entries = read_volume_label(path, bpb)
        rep.add("TC-GP-03", "PASS",
                "root dir readable, %d entries, label=%s" % (entries, label or "<empty>"),
                "ROCKNIX automount selects by blkid TYPE=exfat, not by label; "
                "empty label is harmless (cmd /c \"label %s GAMES\" to restore)" % letter)
    except OSError as e:
        rep.add("TC-GP-03", "FAIL", "cannot read root directory: %s" % e)
    return bpb


def check_mbr(rep, number):
    path = disk_path(number)
    try:
        mbr = read_at(path, 0, 512)
    except OSError as e:
        rep.add("TC-GP-04", "SKIP", "cannot open %s: %s" % (path, e),
                "run as administrator, or use PowerShell Get-Partition -DiskNumber %d" % number)
        rep.add("TC-GP-05", "SKIP", "MBR unavailable")
        rep.add("TC-GP-06", "SKIP", "MBR unavailable")
        return
    if mbr[510:512] != b"\x55\xaa":
        rep.add("TC-GP-04", "FAIL", "MBR signature missing")
        return
    parts = []
    for i in range(4):
        e = mbr[446 + i * 16:446 + (i + 1) * 16]
        typ = e[4]
        lba, size = struct.unpack_from("<II", e, 8)
        parts.append(dict(num=i + 1, type=typ, lba=lba, size=size,
                          boot=e[0] == 0x80))
    txt = " | ".join("P%d=0x%02x LBA=%d %.2fGiB" %
                     (p["num"], p["type"], p["lba"], p["size"] * 512 / 2 ** 30)
                     for p in parts)
    rep.add("TC-GP-04", "PASS", txt, "MBR partition table read")

    p3 = parts[2]
    ok = p3["type"] == 0x07 and p3["size"] > 0
    rep.add("TC-GP-05", "PASS" if ok else "FAIL",
            "P3 type=0x%02x (%s)" % (p3["type"], MBR_TYPES.get(p3["type"], "unknown")),
            "" if ok else "0x0b/0x0c makes Windows report exFAT as FAT32 / "
            "damaged / not formatted; use diskpart 'set id=07' or reflash "
            "a system with the fs-resize fix")

    p1, p2 = parts[0], parts[1]
    layout_ok = p1["type"] in (0x0B, 0x0C) and p2["type"] == 0x83 and p3["size"] > 0
    rep.add("TC-GP-06", "PASS" if layout_ok else "FAIL",
            "P1=0x%02x P2=0x%02x P3=0x%02x, P3 starts at %.2f GiB"
            % (p1["type"], p2["type"], p3["type"], p3["lba"] * 512 / 2 ** 30),
            "expected ROCKNIX(FAT32) / STORAGE(ext4) / GAMES(exFAT)")
    return parts


def list_exfat_letters():
    """Read-only: drive letters whose first sector carries the exFAT magic."""
    found = []
    for d in string.ascii_uppercase:
        try:
            head = read_at(volume_path(d), 0, 512)
        except OSError:
            continue
        if len(head) >= 11 and head[3:11] == EXFAT_MAGIC:
            found.append(d)
    return found


def check_target_disk_change(rep, letter, parts, bpb):
    """Extra diagnosis for the target-disk-change scenario only:
    three MBR partitions, P3=0x07 (shown as NTFS), unformatted, no drive
    letter. Every other disk/partition layout returns without any report
    item, leaving the existing checks untouched.
    """
    if not parts or bpb is not None:
        return
    present = [p for p in parts if p["type"] != 0x00]
    if (len(present) != 3 or parts[2]["type"] != 0x07
            or parts[2]["size"] == 0):
        return
    others = [d for d in list_exfat_letters() if d != letter.rstrip(":\\").upper()]
    rep.add("TC-GP-07", "FAIL",
            "three-partition target disk: P3=0x07 shown as NTFS, no valid "
            "filesystem, no drive letter",
            "0x07 is only the shared IFS/NTFS/exFAT partition-type tag "
            "written by fs-resize 'mkpart primary ntfs'; it is NOT a real "
            "NTFS filesystem. mkfs.exfat did not finish, so P3 has no valid "
            "BPB/FAT and Windows reports 'not formatted' and assigns no "
            "drive letter. %s is P1 (FAT32 ROCKNIX boot), not P3." % letter)
    rep.add("TC-GP-08", "NOTE",
            "correct failure reason here: NTFS tag + unformatted + no drive "
            "letter",
            "check /flash/fs-resize.log for 'mkfs.exfat -n GAMES' and "
            "'exFAT format complete!'; if absent, reflash or re-run "
            "first-boot resize. Do not attribute this to the 5.12 "
            "FAT32-type error, do not format P3 as real NTFS, and do not "
            "blame a stale RAW cache. Other exFAT volumes on this host: %s "
            "(may belong to a different physical disk; this tool cannot map "
            "partitions to drive letters without WMI)."
            % (", ".join(others) or "none"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--volume", "--drive", dest="volume", default="G:",
                    help="GAMES volume letter, e.g. G: (default G:)")
    ap.add_argument("--disk", type=int, default=None,
                    help="physical disk number for MBR check (admin required)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write JSON report to this path")
    args = ap.parse_args()

    rep = Report()
    print("check  : volume %s, disk %s (read-only)" %
          (args.volume, args.disk if args.disk is not None else "n/a"))
    bpb = check_volume(rep, args.volume)
    parts = None
    if args.disk is not None:
        parts = check_mbr(rep, args.disk)
    else:
        rep.add("TC-GP-04", "SKIP", "no --disk given, MBR type not checked",
                "add --disk N (administrator) to verify P3 type 0x07")
        rep.add("TC-GP-05", "SKIP", "no --disk given")
        rep.add("TC-GP-06", "SKIP", "no --disk given")
    check_target_disk_change(rep, args.volume, parts, bpb)

    n = rep.summary()
    verdict = "FAIL" if n.get("FAIL") else "PASS"
    print("\nRESULT : %s  (PASS %d / FAIL %d / SKIP %d)" %
          (verdict, n.get("PASS", 0), n.get("FAIL", 0), n.get("SKIP", 0)))
    if args.json_out:
        payload = dict(volume=args.volume, disk=args.disk, result=verdict,
                       summary=n, items=rep.items,
                       exfat=dict((k, v) for k, v in (bpb or {}).items()
                                  if isinstance(v, (int, str))))
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print("report: %s" % args.json_out)
    return 0 if not n.get("FAIL") else 1


if __name__ == "__main__":
    sys.exit(main())
