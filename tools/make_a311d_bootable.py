#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Generate a flashable AURKNIX image for the CAINIAO CNIoT-CORE / A311D board.

Background
----------
The CI-built AURKNIX A311D image uses a self-written aml_autoscript/cfgload
chain and an Android boot image with an EMPTY cmdline plus an external zstd
ramdisk.  The stock Amlogic U-Boot on this board does not accept that chain.
The ROCKNIX S922X-BOX community image (same board DTB: cainiao,cniot-core /
amlogic,a311d) boots reliably: its aml_autoscript + cfgload are the classic
CoreELEC chain and its kernel.img carries the full kernel cmdline in the
Android boot image header, with the initramfs built into the kernel.

This tool keeps that *proven* boot chain and swaps in the AURKNIX rootfs
(SYSTEM squashfs), then refreshes SYSTEM.MD5.  Everything is done with plain
read-only FAT32 parsing plus in-place writes: no mount, no root, no mtools.

Usage
-----
  python3 tools/make_a311d_bootable.py \
      --aurknix  imgs/AURKNIX-A311D.aarch64-XXXXXXXX.img[.gz] \
      --reference imgs/ROCKNIX-S922X-BOX.aarch64-XXXXXXXX.img \
      --output   imgs/AURKNIX-A311D.aarch64-XXXXXXXX-bootsafe.img

The output keeps the reference FAT volume label (ROCKNIX) because the
reference kernel cmdline is "boot=LABEL=ROCKNIX".  Do not rename that label
unless you also patch the cmdline inside kernel.img.
"""

import argparse
import gzip
import hashlib
import os
import shutil
import struct
import sys
import tempfile

SECTOR = 512
ATTR_LFN = 0x0F
ATTR_VOL = 0x08
ATTR_DIR = 0x10


class FAT32:
    """Minimal read-only FAT32 parser with in-place data/directory writes."""

    def __init__(self, path, part_offset=None, writable=False):
        self.path = path
        self.mode = "r+b" if writable else "rb"
        self.f = open(path, self.mode)
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
        self.totsec = struct.unpack_from("<I", bpb, 32)[0]
        self.label = bpb[71:82].decode("latin1", "replace").strip("\x00 ")
        self.fat_off = self.base + self.reserved * self.bps
        self.data_off = self.base + (self.reserved + self.numfats * self.fatsz) * self.bps
        self.cluster_size = self.spc * self.bps
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
        out, c, guard = [], start, 0
        while 2 <= c < 0x0FFFFFF8 and guard < 4_000_000:
            out.append(c)
            c = self._next(c)
            guard += 1
        return out

    def cluster_offset(self, c):
        return self.data_off + (c - 2) * self.cluster_size

    def _cluster_bytes(self, c):
        self.f.seek(self.cluster_offset(c))
        return self.f.read(self.cluster_size)

    def read_stream(self, start, size):
        """Yield exactly `size` bytes of a file, cluster by cluster."""
        left = size
        for c in self.chain(start):
            if left <= 0:
                break
            chunk = self._cluster_bytes(c)
            take = min(len(chunk), left)
            yield chunk[:take]
            left -= take
        if left > 0:
            raise IOError("FAT chain shorter than file size")

    def entries(self, start=None):
        """Yield (absolute_offset, 32-byte raw entry) for a directory."""
        if start is None:
            start = self.rootclus
        for c in self.chain(start):
            raw = self._cluster_bytes(c)
            base = self.cluster_offset(c)
            for pos in range(0, len(raw), 32):
                e = raw[pos:pos + 32]
                if len(e) < 32:
                    break
                if e[0] == 0x00:
                    return
                yield base + pos, e

    def find_entry(self, short11, start=None):
        want = short11.upper().ljust(11)[:11].encode("latin1")
        for off, e in self.entries(start):
            if e[11] in (ATTR_LFN, ATTR_VOL):
                continue
            if e[0] == 0xE5:
                continue
            if e[0:11].upper() == want:
                return off, e
        raise KeyError("FAT entry not found: %r" % short11)

    def entry_size(self, e):
        return struct.unpack_from("<I", e, 28)[0]

    def entry_cluster(self, e):
        return (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]

    def patch_size(self, entry_offset, new_size):
        self.f.seek(entry_offset + 28)
        self.f.write(struct.pack("<I", new_size))
        self.f.flush()

    def write_stream(self, start, size, source_iter):
        """Write `size` bytes into an existing cluster chain."""
        clusters = self.chain(start)
        need = (size + self.cluster_size - 1) // self.cluster_size
        if need > len(clusters):
            raise ValueError("target SYSTEM is larger than the reference SYSTEM: "
                             "cannot grow in place")
        left, pending = size, b""
        for c in clusters[:need]:
            if left <= 0:
                break
            buf = bytearray(self.cluster_size)
            pos = 0
            while pos < self.cluster_size and left > 0:
                if not pending:
                    try:
                        pending = next(source_iter)
                    except StopIteration:
                        raise IOError("source exhausted early")
                n = min(len(pending), self.cluster_size - pos, left)
                buf[pos:pos + n] = pending[:n]
                pending = pending[n:]
                pos += n
                left -= n
            self.f.seek(self.cluster_offset(c))
            self.f.write(buf)
        self.f.flush()
        if left != 0:
            raise IOError("short write: %d bytes left" % left)


def maybe_decompress(path, workdir):
    if not path.lower().endswith(".gz"):
        return path
    tmp = os.path.join(workdir, "aurknix_source.img")
    print("  decompressing %s -> %s" % (os.path.basename(path), tmp))
    with gzip.open(path, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
    return tmp


def verify(fs, expected_md5, expected_size):
    off, e = fs.find_entry("SYSTEM     ")
    size = fs.entry_size(e)
    if size != expected_size:
        raise ValueError("SYSTEM directory size mismatch: %d != %d" % (size, expected_size))
    h = hashlib.md5()
    for chunk in fs.read_stream(fs.entry_cluster(e), size):
        h.update(chunk)
    if h.hexdigest() != expected_md5:
        raise ValueError("SYSTEM md5 mismatch after write")
    md5_off, md5_e = fs.find_entry("SYSTEM  MD5")
    data = b"".join(fs.read_stream(fs.entry_cluster(md5_e), fs.entry_size(md5_e)))
    if expected_md5.encode() not in data:
        raise ValueError("SYSTEM.MD5 not updated")
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aurknix", required=True, help="CI-built AURKNIX .img or .img.gz")
    ap.add_argument("--reference", required=True, help="known-good ROCKNIX S922X-BOX .img")
    ap.add_argument("--output", required=True, help="output flashable .img")
    args = ap.parse_args()

    for p in (args.aurknix, args.reference):
        if not os.path.isfile(p):
            ap.error("file not found: %s" % p)
    if os.path.abspath(args.output) == os.path.abspath(args.reference):
        ap.error("output must not overwrite the reference image")

    with tempfile.TemporaryDirectory(prefix="a311d-bootfix-") as workdir:
        src_path = maybe_decompress(args.aurknix, workdir)

        print("[1/5] reading AURKNIX SYSTEM")
        src = FAT32(src_path)
        s_off, s_entry = src.find_entry("SYSTEM     ")
        size = src.entry_size(s_entry)
        print("      source SYSTEM: %d bytes, FAT label %s" % (size, src.label))

        print("[2/5] copying reference image as base")
        shutil.copyfile(args.reference, args.output)
        ref = FAT32(args.reference)
        r_off, r_entry = ref.find_entry("SYSTEM     ")
        ref_size = ref.entry_size(r_entry)
        print("      reference SYSTEM: %d bytes, FAT label %s" % (ref_size, ref.label))
        if size > ref_size:
            raise ValueError("AURKNIX SYSTEM (%d) is larger than reference (%d); "
                             "need a bigger BOOT partition" % (size, ref_size))
        ref.close()

        print("[3/5] streaming AURKNIX SYSTEM into the bootable base + md5")
        md5 = hashlib.md5()

        def source_iter():
            for chunk in src.read_stream(src.entry_cluster(s_entry), size):
                md5.update(chunk)
                yield chunk

        dst = FAT32(args.output, writable=True)
        d_off, d_entry = dst.find_entry("SYSTEM     ")
        dst.write_stream(dst.entry_cluster(d_entry), size, source_iter())
        dst.patch_size(d_off, size)
        digest = md5.hexdigest()

        md5_off, md5_entry = dst.find_entry("SYSTEM  MD5")
        content = ("%s  target/SYSTEM\n" % digest).encode("ascii")
        dst.write_stream(dst.entry_cluster(md5_entry), len(content), iter([content]))
        dst.patch_size(md5_off, len(content))
        dst.close()
        src.close()

        print("[4/5] verifying output image")
        check = FAT32(args.output)
        got = verify(check, digest, size)
        print("      SYSTEM md5  : %s" % got)
        print("      SYSTEM size : %d" % size)
        print("      BOOT label  : %s (matches reference kernel cmdline)" % check.label)
        print("      BOOT files  :")
        for _off, e in check.entries():
            if e[11] in (ATTR_LFN, ATTR_VOL) or e[0] == 0xE5:
                continue
            if e[11] & ATTR_DIR:
                continue
            short = e[0:11].decode("latin1").strip()
            print("        %-14s %d bytes" % (short, check.entry_size(e)))
        check.close()

        print("[5/5] sha256")
        h = hashlib.sha256()
        with open(args.output, "rb") as f:
            for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
                h.update(block)
        sha = h.hexdigest()
        with open(args.output + ".sha256", "w", encoding="utf-8") as f:
            f.write("%s  %s\n" % (sha, os.path.basename(args.output)))
        print("      %s" % sha)
        print("\nDONE: %s" % os.path.abspath(args.output))
        print("NOTE: keep the reference image's boot chain. AURKNIX rootfs is the")
        print("      only replaced payload; flash with the .img directly.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        sys.exit(1)
