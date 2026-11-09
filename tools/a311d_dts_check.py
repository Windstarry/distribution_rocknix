#!/usr/bin/env python3
"""Static checks for the A311D board DTS + DTC stub generator.

The full kernel include tree (6.18 dtsi + dt-bindings) is not available on
this Windows host, so this tool does two things:

1. Runs structural / semantic checks that do not need dtc:
   - brace balance
   - label definitions vs references (external labels are listed)
   - pinctrl-names vs pinctrl-N cardinality
   - fixed-regulator enable gpio count (binding maxItems: 1)
2. Writes a `stub.dts` that defines every external label and every ALL-CAPS
   macro, so a real `dtc` can parse and compile the board DTS offline.
   ALL-CAPS tokens followed by '-' (e.g. VCC-supply, AVDD-supply) are NOT
   stubbed: they are property names, not macros.

Real compile (from Git Bash, Docker Desktop running):

  MSYS_NO_PATHCONV=1 docker run --rm \
    -v "E:/A311d/dts_compare/dtc_stub:/work" \
    docker.m.daocloud.io/library/ubuntu:22.04 sh -c \
    "apt-get update -qq && DEBIAN_FRONTEND=noninteractive \
     apt-get install -y -qq device-tree-compiler cpp && \
     cpp -nostdinc -undef -x assembler-with-cpp /work/stub.dts -o /work/stub.pre.dts && \
     dtc -I dts -O dtb -o /work/out.dtb /work/stub.pre.dts && \
     dtc -I dtb -O dts /work/out.dtb -o /work/out.dts && echo DTC_OK"

Stub-only warnings about missing #gpio-cells / #sound-dai-cells on the
synthetic stub nodes are expected and harmless.

Python 3 standard library only. Read-only except for the --stub-dir output.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

DEF_DTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "projects", "ROCKNIX", "devices", "A311D", "linux", "dts", "amlogic",
    "meson-g12b-a311d-cainiao-cniot-core.dts",
)
DEF_STUB = r"E:\A311d\dts_compare\dtc_stub"

# Labels confirmed present in the 6.18 kernel dtsi but not always visible in
# locally cached samples (they are informational, not a FAIL condition).
VERIFIED_EXTERNAL = {
    "cpu_opp_table_0", "cpub_opp_table_1",
    "usb2_phy0", "usb2_phy1", "usb3_pcie_phy",
    "arb", "clkc_audio", "frddr_a", "frddr_b", "frddr_c",
    "tdmif_a", "tdmif_b", "tdmout_a", "tdmout_b", "toacodec", "tohdmitx",
}


class Report:
    def __init__(self):
        self.fails = 0

    def check(self, cid, ok, detail):
        print("%-12s %-5s %s" % (cid, "PASS" if ok else "FAIL", detail))
        if not ok:
            self.fails += 1

    def info(self, cid, detail):
        print("%-12s %-5s %s" % (cid, "INFO", detail))


def strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dts", default=DEF_DTS, help="board DTS to check")
    ap.add_argument("--stub-dir", default=DEF_STUB, help="output dir for stub.dts")
    ap.add_argument("--samples", default=r"E:\A311d\dts_compare",
                    help="dir with cached 6.18 dtsi samples for label audit")
    ap.add_argument("--no-stub", action="store_true", help="checks only")
    args = ap.parse_args()

    if not os.path.isfile(args.dts):
        print("ERROR: DTS not found: %s" % args.dts)
        return 2
    raw = open(args.dts, encoding="utf-8").read()
    clean = strip_comments(raw)
    rep = Report()

    rep.check("TC-DTS-01", clean.count("{") == clean.count("}"),
              "braces %d open / %d close" % (clean.count("{"), clean.count("}")))

    defs = set(re.findall(r"(?m)^\s*([A-Za-z_][\w-]*)\s*:\s*[\w@,.-]+\s*\{", clean))
    refs = set(re.findall(r"&([A-Za-z_][\w-]*)", clean))
    external = sorted(refs - defs)
    rep.check("TC-DTS-02", bool(defs) and bool(external),
              "%d labels defined, %d external references" % (len(defs), len(external)))

    # pinctrl-names must not declare more states than pinctrl-N properties
    bad_pinctrl = []
    for m in re.finditer(r"pinctrl-names\s*=\s*([^;]+);", clean):
        names = re.findall(r'"([^"]+)"', m.group(1))
        window = clean[max(0, m.start() - 500):m.end()]
        states = set(re.findall(r"pinctrl-(\d+)\s*=", window))
        if len(names) > len(states):
            bad_pinctrl.append((names, sorted(states)))
    rep.check("TC-DTS-03", not bad_pinctrl,
              "pinctrl-names/pinctrl-N consistent" if not bad_pinctrl
              else "orphan states: %s" % bad_pinctrl)

    # fixed-regulator.yaml: gpio/gpios maxItems = 1
    bad_gpio = []
    for m in re.finditer(r"\b(gpios?)\s*=\s*([^;]+);", clean):
        n = len(re.findall(r"<", m.group(2)))
        if n > 1:
            bad_gpio.append((m.group(1), n))
    rep.check("TC-DTS-04", not bad_gpio,
              "every enable gpio has one phandle" if not bad_gpio
              else "regulator with >1 gpio: %s" % bad_gpio)

    # cached dtsi samples for an informational label audit
    known = set(VERIFIED_EXTERNAL)
    if os.path.isdir(args.samples):
        for fn in ("meson-g12-common.dtsi", "meson-g12b.dtsi"):
            p = os.path.join(args.samples, fn)
            if os.path.isfile(p):
                t = open(p, encoding="utf-8", errors="replace").read()
                known |= set(re.findall(
                    r"(?m)^\s*([A-Za-z_][\w-]*)\s*:\s*[\w@,.-]+\s*\{", t))
    unknown = [x for x in external if x not in known and x not in VERIFIED_EXTERNAL]
    rep.info("TC-DTS-05", "%d external labels not in cached samples: %s"
             % (len(unknown), ", ".join(unknown[:20]) or "none"))

    if not args.no_stub:
        # exclude ALL-CAPS tokens that are part of a hyphenated property name
        caps = sorted(set(re.findall(
            r"(?<![\w-])([A-Z][A-Z0-9_]{2,})(?![\w-])", clean)))
        rep.check("TC-DTS-06", not ({"VCC", "AVDD"} & set(caps)),
                  "%d macros stubbed, property-name prefixes excluded" % len(caps))
        body = re.sub(r"(?m)^\s*#include[^\n]*\n", "", clean)
        body = re.sub(r"/dts-v1/\s*;", "", body, count=1)
        lines = ["/dts-v1/;"]
        lines += ["#define %s 0" % c for c in caps]
        lines.append("/ {")
        lines += ["\t%s: stub@%x { };" % (l, i) for i, l in enumerate(external)]
        lines += ["};", body]
        os.makedirs(args.stub_dir, exist_ok=True)
        out = os.path.join(args.stub_dir, "stub.dts")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        rep.info("TC-DTS-07", "stub written: %s (%d B)" % (out, os.path.getsize(out)))

    print("\nRESULT: %s (FAIL %d)" % ("FAIL" if rep.fails else "PASS", rep.fails))
    return 1 if rep.fails else 0


if __name__ == "__main__":
    sys.exit(main())
