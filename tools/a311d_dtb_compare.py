#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A311D DTB provenance check and isolation-test helper.

Three different DTB families have been in play on this box:

  A. CI-built AURKNIX DTB (74,382 B in AURKNIX-A311D.aarch64-20261031.img)
     mainline-style, from the hand-written board DTS in this repo, but built
     on 2026-10-31 BEFORE the P2/NPU commits: ethernet/cec disabled, npu
     disabled, and - important for HDMI - NO /hdmi-connector and NO
     hdmi-tx port@1/endpoint. Without a connector endpoint the mainline
     dw-hdmi bridge cannot reach the DDC/EDID bus.

  B. bootsafe / S922X-BOX reference DTB (54,984 B, md5 0fee9ffd...)
     also mainline-style (model CAINIAO CNIoT-CORE), but from the batocera
     lineage: HAS /hdmi-connector + port@1/endpoint, ethernet/cec/npu
     enabled, P2 peripherals present. This is what the bootsafe image
     actually boots.

  C. EE / EmuELEC vendor DTB (74,839 B, md5 1b285a56...)
     vendor 4.9 style: amhdmitx / cvbsout / auge_sound, compatible
     "amlogic, g12b". NOT compatible with a mainline 6.x meson DRM kernel.
     Swapping this into a mainline image was already proven to still black
     screen, so do not use it for the bootsafe chain.

Conclusion for the "meson drm user-defined mode not support" stall: A != B
is real, but B is the MORE complete HDMI graph; the immediate cause of that
specific message is the forced video=HDMI-A-1:1920x1080@60 cmdline token
(see tools/a311d_video_mode.py). Keep B, fix the cmdline first. Use
--replace only for a controlled A/B isolation test afterwards.

Usage
-----
  python a311d_dtb_compare.py --list <dtb> [--list <dtb2> ...]
  python a311d_dtb_compare.py --diff <a.dtb> <b.dtb>
  python a311d_dtb_compare.py --extract --image <img> --out <dtb>
  python a311d_dtb_compare.py --replace --drive F: --dtb <dtb> --yes

Stdlib only. --replace keeps dtb.img.bak and never overwrites an existing
backup; --extract never modifies the image.
"""

import argparse
import hashlib
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from a311d_firmware_verify import Fat32
except ImportError:
    print("ERROR: a311d_firmware_verify.py must sit next to this script")
    raise

KEYS = ("vpu", "hdmi", "connector", "cec", "gpu", "npu", "ethernet",
        "sound", "pwm-fan", "wifi", "spidev", "cvbs", "amhdmitx")
VPU = "/soc/vpu@ff900000"
HDITX = "/soc/bus@ff600000/hdmi-tx@0"
ENDPOINT = HDITX + "/port@1/endpoint"
CONNECTOR = "/hdmi-connector"


def parse_fdt(data):
    magic, total, offs, off_str, off_rsv, ver, last, bootcpu, size_str, size_st = \
        struct.unpack_from(">10I", data, 0)
    if magic != 0xD00DFEED:
        raise ValueError("not a flattened device tree (magic 0x%08x)" % magic)
    strings = data[off_str:off_str + size_str]

    def s(o):
        e = strings.find(b"\0", o)
        return strings[o:e].decode("ascii", "replace")

    pos, parts, nodes, cur = offs, [], {}, "/"
    for _ in range(200000):
        tok = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if tok == 1:                                     # FDT_BEGIN_NODE
            e = data.find(b"\0", pos)
            name = data[pos:e].decode("ascii", "replace")
            pos = (e + 4) & ~3
            if name:
                parts.append(name)
            cur = "/" + "/".join(parts)
            nodes.setdefault(cur, {})
        elif tok == 2:                                   # FDT_END_NODE
            if parts:
                parts.pop()
        elif tok == 3:                                   # FDT_PROP
            ln, no = struct.unpack_from(">2I", data, pos)
            pos += 8
            val = data[pos:pos + ln]
            pos = (pos + ln + 3) & ~3
            nodes.setdefault(cur, {})[s(no)] = val
        elif tok == 9:                                   # FDT_END
            break
    return nodes, ver


def strlist(v):
    return [x.decode("ascii", "replace") for x in (v or b"").split(b"\0") if x]


def load(path):
    d = open(path, "rb").read()
    nodes, ver = parse_fdt(d)
    return d, nodes, ver


def hdmi_graph(nodes):
    conn = CONNECTOR in nodes
    ep = ENDPOINT in nodes
    vpu = VPU in nodes
    tx = HDITX in nodes
    ok = conn and ep and vpu and tx
    return ("COMPLETE (connector endpoint -> EDID reachable)" if ok else
            "INCOMPLETE (no connector/endpoint -> dw-hdmi cannot read EDID)"), conn, ep, vpu, tx


def report(path):
    d, nodes, ver = load(path)
    root = nodes.get("/", {})
    graph, conn, ep, vpu, tx = hdmi_graph(nodes)
    print("file   : %s" % path)
    print("  size=%d md5=%s fdt_v%d nodes=%d" %
          (len(d), hashlib.md5(d).hexdigest(), ver, len(nodes)))
    print("  model=%s compatible=%s" %
          (strlist(root.get("model")), strlist(root.get("compatible"))))
    print("  hdmi-connector=%s port@1/endpoint=%s vpu=%s hdmi-tx=%s" %
          (conn, ep, vpu, tx))
    print("  HDMI graph: %s" % graph)
    for label, pth in (("vpu", VPU), ("hdmi-tx", HDITX), ("npu", "/npu@ff100000"),
                       ("ethernet", "/soc/ethernet@ff3f0000"),
                       ("cec@280", "/soc/bus@ff800000/cec@280"),
                       ("gpu", "/soc/gpu@ffe40000")):
        if pth in nodes:
            pr = nodes[pth]
            print("  %-10s compatible=%-58s status=%s" %
                  (label, ",".join(strlist(pr.get("compatible")))[:58],
                   ",".join(strlist(pr.get("status"))) or "(default okay)"))
    interesting = sorted(p for p in nodes if any(k in p.lower() for k in KEYS))
    print("  display/soc nodes: %d" % len(interesting))
    return nodes


def diff(pa, pb):
    a, na, va = load(pa)
    b, nb, vb = load(pb)
    print("A: %s (%d B, md5 %s)" % (pa, len(a), hashlib.md5(a).hexdigest()))
    print("B: %s (%d B, md5 %s)" % (pb, len(b), hashlib.md5(b).hexdigest()))
    if a == b:
        print("=> BYTE-IDENTICAL, no DTB mismatch")
        return
    oa, ob = sorted(set(na) - set(nb)), sorted(set(nb) - set(na))
    print("=> DIFFERENT: %d nodes only in A, %d nodes only in B" % (len(oa), len(ob)))
    print("--- only in A ---")
    for p in oa[:60]:
        print("  + %s" % p)
    print("--- only in B ---")
    for p in ob[:60]:
        print("  - %s" % p)
    print("--- HDMI-relevant property differences ---")
    for p in sorted(set(na) & set(nb)):
        if not any(k in p.lower() for k in ("vpu", "hdmi", "connector", "cec")):
            continue
        for prop in ("compatible", "status", "remote-endpoint", "ddc-i2c-bus"):
            x, y = na[p].get(prop), nb[p].get(prop)
            if x != y:
                print("  %s %s: A=%r B=%r" % (p, prop, x, y))
    for label, p in (("A", pa), ("B", pb)):
        graph, conn, ep, vpu, tx = hdmi_graph(na if label == "A" else nb)
        print("  %s HDMI graph: %s" % (label, graph))


def extract(args):
    fs = Fat32(args.image)
    try:
        name, data = fs.read("dtb.img")
    finally:
        fs.close()
    if data is None:
        print("ERROR: dtb.img not found in %s" % args.image)
        return 2
    with open(args.out, "wb") as f:
        f.write(data)
    print("extracted %d B md5=%s -> %s" %
          (len(data), hashlib.md5(data).hexdigest(), args.out))
    report(args.out)
    return 0


def replace(args):
    drive = args.drive.strip().rstrip("\\/")
    if len(drive) == 1:
        drive += ":"
    drive += "\\"
    target = os.path.join(drive, "dtb.img")
    if not os.path.isfile(target):
        print("ERROR: %s not found" % target)
        return 2
    new = open(args.dtb, "rb").read()
    if new[:4] != b"\xd0\x0d\xfe\xed":
        print("ERROR: %s is not a FDT/DTB" % args.dtb)
        return 2
    old = open(target, "rb").read()
    print("current: %d B md5=%s" % (len(old), hashlib.md5(old).hexdigest()))
    print("new    : %d B md5=%s" % (len(new), hashlib.md5(new).hexdigest()))
    print("WARNING: this changes the DTB that U-Boot passes to the kernel.")
    print("         Keep the .bak to revert; do NOT use the EE vendor DTB")
    print("         (amhdmitx / compatible 'amlogic, g12b') with mainline 6.x.")
    if not args.yes:
        print("DRY RUN: no file written. Add --yes to apply.")
        return 0
    bak = target + ".bak"
    if not os.path.isfile(bak):
        shutil.copyfile(target, bak)
        print("backup : %s" % bak)
    shutil.copyfile(args.dtb, target)
    print("REPLACED: %s" % target)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="append", default=[], help="inspect a .dtb (repeatable)")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"), help="compare two .dtb files")
    ap.add_argument("--extract", action="store_true", help="extract dtb.img from an image")
    ap.add_argument("--image", help="source .img for --extract")
    ap.add_argument("--out", help="output .dtb for --extract")
    ap.add_argument("--replace", action="store_true", help="replace dtb.img on a drive")
    ap.add_argument("--drive", help="mounted FAT boot volume, e.g. F:")
    ap.add_argument("--dtb", help="replacement .dtb for --replace")
    ap.add_argument("--yes", action="store_true", help="confirm --replace")
    a = ap.parse_args()
    if a.list:
        for p in a.list:
            report(p)
            print()
        return 0
    if a.diff:
        diff(*a.diff)
        return 0
    if a.extract:
        if not a.image or not a.out:
            print("ERROR: --extract needs --image and --out")
            return 2
        return extract(a)
    if a.replace:
        if not a.drive or not a.dtb:
            print("ERROR: --replace needs --drive and --dtb")
            return 2
        return replace(a)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
