#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""A311D front-end / GPU stack consistency check (read-only, stdlib only).

Why this exists
---------------
The 2026-11-09 bring-up on the CAINIAO CNIoT-CORE (Amlogic A311D) could
not start the front-end. Three independent faults were found:

  1. KERNEL (6.18.49) and SYSTEM (modules built for 6.18.21) came from
     different builds. /lib/modules/$(uname -r)/modules.dep did not exist,
     so modprobe could not find panfrost.ko or mali_kbase.ko and neither
     /dev/mali* nor /dev/dri/card1 was ever created.
  2. The SYSTEM default config had no gpu.driver line, so gpudriver fell
     back to libmali. The verified chain-boot DTB only has the mainline
     gpu@ffe40000 (arm,mali-bifrost) node, so the vendor DDK reports
     "No mali devices found" and ES cannot initialise EGL.
  3. No platform quirk set UI_SERVICE, so autostart called
     "systemctl start" with no unit name ("Too few arguments").

This tool checks a built SYSTEM image (offline) and/or a live box probe
dump against those faults. It never writes to the image or the device.

Usage
-----
  python a311d_gpu_stack_check.py --list-checks
  python a311d_gpu_stack_check.py --probe-command
  python a311d_gpu_stack_check.py --root /path/to/unpacked/SYSTEM
  python a311d_gpu_stack_check.py --live /tmp/a311d_probe.txt
  python a311d_gpu_stack_check.py --root DIR --live FILE --json report.json

Generate the live dump on the box with --probe-command, then copy the
resulting file back to the host and pass it to --live.

Exit codes: 0 = no FAIL, 1 = at least one FAIL, 2 = usage/IO error.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

UI_VALUE = "sway.service essway.service"
GPU_DEFAULT = "panfrost"
MODULE_ROOT = os.path.join("usr", "lib", "kernel-overlays", "base", "lib", "modules")
VERMAGIC_RE = re.compile(rb"vermagic=([0-9][0-9A-Za-z._+\-]*)")
PLACEHOLDER_RE = re.compile(r"@(PAN|GPU_OPTIONS|DTB_OVERLAY_LOAD|DTB_OVERLAY_UNLOAD)@")

CHECKS = [
    ("TC-SYS-01", "system", "SYSTEM profile.d sets UI_SERVICE", UI_VALUE),
    ("TC-SYS-02", "system", "SYSTEM default gpu.driver", GPU_DEFAULT),
    ("TC-SYS-03", "system", "A311D platform quirk 090-ui_service", UI_VALUE),
    ("TC-SYS-04", "system", "gpudriver has no unresolved placeholders", "none"),
    ("TC-SYS-05", "system", "exactly one kernel module directory", "1 directory"),
    ("TC-SYS-06", "system", "panfrost.ko packaged", "present"),
    ("TC-SYS-07", "system", "mali_kbase.ko packaged (fallback stack)", "present"),
    ("TC-SYS-08", "system", "libmali userspace libraries packaged", "libEGL.so present"),
    ("TC-SYS-09", "system", "modules.dep lists both GPU modules", "both entries"),
    ("TC-SYS-10", "system", "module vermagic matches module dir", "same kernel release"),
    ("TC-LIVE-01", "live", "uname -r == module directory", "same kernel release"),
    ("TC-LIVE-02", "live", "/lib/modules/$(uname -r)/modules.dep exists", "present"),
    ("TC-LIVE-03", "live", "panfrost kernel module loaded", "lsmod hit"),
    ("TC-LIVE-04", "live", "GPU render node present", "card1 or renderD*"),
    ("TC-LIVE-05", "live", "effective gpu.driver", GPU_DEFAULT),
    ("TC-LIVE-06", "live", "effective UI_SERVICE", UI_VALUE),
    ("TC-LIVE-07", "live", "sway.service / essway.service active", "both active"),
    ("TC-LIVE-08", "live", "config.ini gpu_dtb target exists on /flash", "no dangling dtb"),
]

PROBE_COMMAND = r'''# Run on the A311D box over SSH, then copy /tmp/a311d_probe.txt to the host.
{
  echo "=== uname";        uname -r
  echo "=== modules_dir";   ls -1 /usr/lib/kernel-overlays/base/lib/modules/ 2>/dev/null
  echo "=== modules_dep";   ls -l /lib/modules/$(uname -r)/modules.dep 2>&1
  echo "=== lsmod";         lsmod | grep -Ei "panfrost|mali_kbase" 2>/dev/null
  echo "=== dri";           ls -1 /dev/dri /dev/mali* 2>&1
  echo "=== gpu_driver";    bash -c '. /etc/profile.d/001-functions; get_setting gpu.driver'
  echo "=== ui_service";    bash -c '. /etc/profile; echo "$UI_SERVICE"'
  echo "=== services";      for s in sway.service essway.service emustation.service; do systemctl show "$s" --property=Id,ActiveState,SubState,NRestarts --no-pager; done
  echo "=== gpu_dtb";       grep -n '^gpu_dtb=' /flash/config.ini 2>/dev/null
  echo "=== dtb_files";     ls -1 /flash/dtb*.img 2>/dev/null
  echo "=== dtb_md5";       md5sum /flash/dtb.img 2>/dev/null
} > /tmp/a311d_probe.txt
'''


class Report(object):
    def __init__(self):
        self.items = []

    def add(self, cid, status, actual, detail=""):
        self.items.append({"id": cid, "status": status,
                           "actual": str(actual), "detail": detail})

    def counts(self):
        out = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for item in self.items:
            out[item["status"]] = out.get(item["status"], 0) + 1
        return out


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (OSError, IOError):
        return None


def find_modules(root):
    """Return (module_dir_path, release, list_of_ko_paths)."""
    base = os.path.join(root, MODULE_ROOT)
    dirs = [d for d in sorted(glob.glob(os.path.join(base, "*")))
            if os.path.isdir(d)]
    if not dirs:
        return None, None, []
    moddir = dirs[0]
    kos = []
    for dirpath, _dirnames, filenames in os.walk(moddir):
        for name in filenames:
            if re.match(r".*\.ko(\.xz|\.zst|\.gz)?$", name):
                kos.append(os.path.join(dirpath, name))
    return moddir, os.path.basename(moddir), kos


def find_ko(kos, name):
    hits = [p for p in kos
            if os.path.basename(p).startswith(name + ".ko")]
    return hits[0] if hits else None


def check_system(root, report):
    """Offline checks against an unpacked SYSTEM squashfs root."""
    # TC-SYS-01 -- /etc/profile.d UI_SERVICE
    profile_files = sorted(glob.glob(os.path.join(root, "etc", "profile.d", "*")))
    value = None
    for path in profile_files:
        if not os.path.isfile(path):
            continue
        text = read_text(path) or ""
        for line in text.splitlines():
            if re.match(r"\s*UI_SERVICE\s*=", line):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
    ok = bool(value) and "sway.service" in value and "essway.service" in value
    report.add("TC-SYS-01", "PASS" if ok else "FAIL",
               value or "not set",
               "scanned %d files in etc/profile.d" % len(profile_files))

    # TC-SYS-02 -- SYSTEM default gpu.driver
    cfg_path = os.path.join(root, "usr", "config", "system", "configs", "system.cfg")
    text = read_text(cfg_path)
    driver = None
    if text is not None:
        for line in text.splitlines():
            if line.startswith("gpu.driver="):
                driver = line.split("=", 1)[1].strip()
    if text is None:
        report.add("TC-SYS-02", "SKIP", "system.cfg missing", cfg_path)
    else:
        ok = driver == GPU_DEFAULT
        report.add("TC-SYS-02", "PASS" if ok else "FAIL", driver or "not set",
                   "gpu.driver must be %s, gpudriver defaults to libmali" % GPU_DEFAULT)

    # TC-SYS-03 -- platform quirk
    quirk = os.path.join(root, "usr", "lib", "autostart", "quirks",
                         "platforms", "A311D", "090-ui_service")
    qtext = read_text(quirk)
    ok = bool(qtext) and "sway.service essway.service" in qtext
    report.add("TC-SYS-03", "PASS" if ok else "FAIL",
               "present" if qtext else "missing",
               "platforms/%s is selected by HW_DEVICE" % "A311D")

    # TC-SYS-04 -- gpudriver placeholders
    gp = os.path.join(root, "usr", "bin", "gpudriver")
    gtext = read_text(gp)
    if gtext is None:
        report.add("TC-SYS-04", "SKIP", "gpudriver missing", gp)
    else:
        left = sorted(set(PLACEHOLDER_RE.findall(gtext)))
        report.add("TC-SYS-04", "PASS" if not left else "FAIL",
                   "none" if not left else ", ".join(left),
                   "post_makeinstall_target() must substitute all tokens")

    # TC-SYS-05 -- exactly one module directory
    base = os.path.join(root, MODULE_ROOT)
    dirs = [d for d in sorted(glob.glob(os.path.join(base, "*")))
            if os.path.isdir(d)]
    moddir, release, kos = find_modules(root)
    ok = len(dirs) == 1
    report.add("TC-SYS-05", "PASS" if ok else "FAIL",
               ", ".join(os.path.basename(d) for d in dirs) or "none",
               "multiple module directories indicate a mixed KERNEL/SYSTEM build")

    # TC-SYS-06 / 07 -- both GPU kernel modules
    pan = find_ko(kos, "panfrost")
    mali = find_ko(kos, "mali_kbase")
    report.add("TC-SYS-06", "PASS" if pan else "FAIL",
               os.path.relpath(pan, root) if pan else "missing")
    report.add("TC-SYS-07", "PASS" if mali else "FAIL",
               os.path.relpath(mali, root) if mali else "missing",
               "libmali stays packaged as a fallback stack")

    # TC-SYS-08 -- libmali userspace
    malidir = os.path.join(root, "usr", "lib", "mali")
    libs = sorted(glob.glob(os.path.join(malidir, "*")))
    egl = [p for p in libs if os.path.basename(p).startswith("libEGL.so")]
    report.add("TC-SYS-08", "PASS" if egl else "FAIL",
               "%d files, libEGL.so %s" % (len(libs), "yes" if egl else "no"),
               malidir)

    # TC-SYS-09 -- modules.dep
    dep = os.path.join(moddir, "modules.dep") if moddir else None
    deptext = read_text(dep) if dep else None
    if deptext is None:
        report.add("TC-SYS-09", "SKIP", "modules.dep missing",
                   "scripts/image runs depmod before squashfs packing")
    else:
        has_pan = "panfrost" in deptext
        has_mali = "mali_kbase" in deptext
        report.add("TC-SYS-09", "PASS" if (has_pan and has_mali) else "FAIL",
                   "panfrost=%s mali_kbase=%s" % (has_pan, has_mali))

    # TC-SYS-10 -- vermagic vs module dir
    target = pan or mali
    if target is None or target.endswith((".xz", ".zst", ".gz")) or not release:
        report.add("TC-SYS-10", "SKIP", "no readable uncompressed .ko", "")
    else:
        try:
            with open(target, "rb") as handle:
                blob = handle.read()
        except OSError:
            blob = b""
        match = VERMAGIC_RE.search(blob)
        vm = match.group(1).decode("ascii", "replace") if match else None
        ok = bool(vm) and vm == release
        report.add("TC-SYS-10", "PASS" if ok else "FAIL",
                   "vermagic=%s dir=%s" % (vm or "unknown", release),
                   "a mismatch means KERNEL and SYSTEM are from different builds")


def parse_live(path):
    """Parse a '=== key' sectioned probe dump into an ordered mapping."""
    sections = {}
    current = None
    text = read_text(path)
    if text is None:
        raise IOError("cannot read live probe file: %s" % path)
    for line in text.splitlines():
        marker = re.match(r"^===\s*([A-Za-z0-9_]+)\s*$", line.strip())
        if marker:
            current = marker.group(1)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def section_text(sections, key):
    return "\n".join(sections.get(key, []))


def check_live(sections, report):
    # TC-LIVE-01 -- kernel release vs module directory
    uname = section_text(sections, "uname").strip().splitlines()
    moddirs = [x.strip() for x in section_text(sections, "modules_dir").splitlines()
               if x.strip()]
    release = uname[0].strip() if uname else ""
    ok = bool(release) and moddirs == [release]
    report.add("TC-LIVE-01", "PASS" if ok else "FAIL",
               "uname=%s modules=%s" % (release or "?", ",".join(moddirs) or "?"),
               "KERNEL and SYSTEM must come from the same build")

    # TC-LIVE-02 -- modules.dep for the running kernel
    dep = section_text(sections, "modules_dep")
    ok = "modules.dep" in dep and not re.search(r"No such file|cannot access", dep, re.I)
    report.add("TC-LIVE-02", "PASS" if ok else "FAIL",
               dep.strip().splitlines()[0].strip() if dep.strip() else "no data")

    # TC-LIVE-03 -- panfrost loaded
    lsmod = section_text(sections, "lsmod")
    ok = "panfrost" in lsmod
    report.add("TC-LIVE-03", "PASS" if ok else "FAIL",
               lsmod.strip().splitlines()[0].strip() if lsmod.strip() else "no GPU module loaded")

    # TC-LIVE-04 -- render node
    dri = section_text(sections, "dri")
    ok = bool(re.search(r"card1|renderD\d+", dri))
    report.add("TC-LIVE-04", "PASS" if ok else "FAIL",
               " ".join(dri.split())[:100] or "no data",
               "card0 alone is the VPU; panfrost creates card1/renderD128")

    # TC-LIVE-05 -- effective gpu.driver
    drv = section_text(sections, "gpu_driver").strip().splitlines()
    value = drv[-1].strip() if drv else ""
    report.add("TC-LIVE-05", "PASS" if value == GPU_DEFAULT else "FAIL",
               value or "not set", "SYSTEM default is ignored if /storage keeps gpu.driver=libmali")

    # TC-LIVE-06 -- effective UI_SERVICE
    ui = section_text(sections, "ui_service").strip()
    ok = "sway.service" in ui and "essway.service" in ui
    report.add("TC-LIVE-06", "PASS" if ok else "FAIL", ui or "not set",
               "a stale /storage/.config/profile.d/999-ui-service overrides SYSTEM")

    # TC-LIVE-07 -- UI units active and stable
    services = section_text(sections, "services")
    def state(unit):
        block = re.search(r"Id=" + re.escape(unit) + r"(.*?)(?=Id=|$)", services, re.S)
        if not block:
            return None, None
        chunk = block.group(0)
        active = re.search(r"ActiveState=(\S+)", chunk)
        restarts = re.search(r"NRestarts=(\d+)", chunk)
        return (active.group(1) if active else None,
                int(restarts.group(1)) if restarts else None)
    sway, sway_r = state("sway.service")
    es, es_r = state("essway.service")
    ok = sway == "active" and es == "active" and (sway_r or 0) < 3 and (es_r or 0) < 3
    report.add("TC-LIVE-07", "PASS" if ok else "FAIL",
               "sway=%s(%s) essway=%s(%s)" % (sway, sway_r, es, es_r))

    # TC-LIVE-08 -- no dangling gpu_dtb
    dtbkey = section_text(sections, "gpu_dtb")
    dtbfiles = section_text(sections, "dtb_files")
    m = re.search(r"gpu_dtb=(\S+)", dtbkey)
    if not m:
        report.add("TC-LIVE-08", "PASS", "gpu_dtb unset (uses dtb.img)")
    else:
        want = m.group(1).strip()
        ok = want in dtbfiles
        report.add("TC-LIVE-08", "PASS" if ok else "FAIL",
                   "gpu_dtb=%s present=%s" % (want, ok),
                   "a missing dtb-panfrost.img makes cfgload fail on next boot")


def render(report, root, live):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("=" * 80)
    lines.append("A311D front-end / GPU stack consistency check")
    lines.append("time      : %s (Beijing, GMT+8)" % now)
    lines.append("SYSTEM    : %s" % (root or "not supplied"))
    lines.append("live dump : %s" % (live or "not supplied"))
    lines.append("=" * 80)
    expected = {c[0]: (c[2], c[3]) for c in CHECKS}
    for item in report.items:
        title, want = expected.get(item["id"], ("", ""))
        lines.append("[%-4s] %-11s %-46s actual: %s" %
                     (item["status"], item["id"], title, item["actual"]))
        if item["detail"]:
            lines.append("             note: %s" % item["detail"])
        if item["status"] == "FAIL":
            lines.append("             want: %s" % want)
    lines.append("-" * 80)
    counts = report.counts()
    lines.append("SUMMARY: PASS %d  FAIL %d  SKIP %d  TOTAL %d" %
                 (counts["PASS"], counts["FAIL"], counts["SKIP"], len(report.items)))
    if counts["FAIL"]:
        lines.append("VERDICT: FAIL - do not flash / do not accept this build.")
    else:
        lines.append("VERDICT: PASS - no blocking fault found (SKIP items are optional).")
    lines.append("=" * 80)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="A311D front-end / GPU stack consistency check (read-only)")
    parser.add_argument("--root", help="unpacked SYSTEM squashfs root (offline checks)")
    parser.add_argument("--live", help="live probe dump produced by --probe-command")
    parser.add_argument("--json", dest="json_path", help="write machine-readable report")
    parser.add_argument("--list-checks", action="store_true", help="list checks and exit")
    parser.add_argument("--probe-command", action="store_true",
                        help="print the shell probe to run on the box and exit")
    args = parser.parse_args(argv)

    if args.list_checks:
        for cid, phase, title, want in CHECKS:
            print("%-11s %-7s %-48s expected: %s" % (cid, phase, title, want))
        return 0
    if args.probe_command:
        sys.stdout.write(PROBE_COMMAND)
        return 0
    if not args.root and not args.live:
        parser.error("supply --root and/or --live (see --probe-command / --list-checks)")
    if args.root and not os.path.isdir(args.root):
        parser.error("--root is not a directory: %s" % args.root)

    report = Report()
    if args.root:
        check_system(os.path.abspath(args.root), report)
    else:
        for cid, phase, _title, _want in CHECKS:
            if phase == "system":
                report.add(cid, "SKIP", "no --root supplied")
    if args.live:
        try:
            sections = parse_live(args.live)
        except IOError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 2
        check_live(sections, report)
    else:
        for cid, phase, _title, _want in CHECKS:
            if phase == "live":
                report.add(cid, "SKIP", "no --live supplied")

    output = render(report, args.root, args.live)
    print(output)
    if args.json_path:
        payload = {"time": datetime.datetime.now().isoformat(),
                   "root": args.root, "live": args.live,
                   "summary": report.counts(), "items": report.items}
        try:
            with open(args.json_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
        except OSError as exc:
            print("ERROR: cannot write json: %s" % exc, file=sys.stderr)
            return 2
    return 1 if report.counts()["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
