#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Observe the A311D bring-up over the LAN (no serial cable needed).

The box exposes several distinguishable states over Ethernet:
  * eMMC Android   : TCP/5555 (network ADB) open
  * Linux booting  : TCP/445 (Samba) up while TCP/22 is not yet / briefly down
                     (first-boot fs-resize calls reboot -f, so ports flap)
  * Linux booted   : TCP/22 (SSH) open, usually a new IP
  * reboot window  : all ports gone for one or two samples -> REBOOTING

A black HDMI screen alone cannot tell "kernel died" from "display not
initialised". This tool resolves that by watching TCP state over time.

IMPORTANT - baseline and device IP
----------------------------------
Home LANs already contain SSH/Samba hosts (measured: 192.168.31.3:22/445,
192.168.31.55:445). Judging "any 22 open == the box booted" is WRONG.
Take a baseline while the box is OFF, then name the box's IP explicitly:

  # 1. with the box OFF, before power-on:
  python a311d_bringup_probe.py --once --save-baseline baseline.json

  # 2. power on the flashed USB, then (192.168.31.7 = box IP, user-confirmed):
  python a311d_bringup_probe.py --watch --device-ip 192.168.31.7 \
         --baseline baseline.json --duration 300 --interval 10 \
         --out bringup_log.jsonl

Without --device-ip the tool still scans the /24 and reports all hosts, but
verdicts are based on the whole subnet and are less precise.

Stdlib only. adb is optional and used only when --adb is given explicitly.

Verdicts
--------
  BOOTED             : device SSH (22) is up and new vs baseline
  BOOTED_UNCONFIRMED : SSH seen but no baseline supplied
  BASELINE_ONLY      : SSH seen only on pre-existing hosts
  BOOTING            : device Samba (445/139) up, SSH not up yet -> still starting
  REBOOTING          : device was online, now silent (first-boot resize reboot)
  ANDROID_FALLBACK   : device 5555 up -> booted eMMC Android, USB chain ignored
  NO_LINK            : device fully silent -> early kernel failure / PHY down
"""
import argparse
import concurrent.futures
import json
import socket
import subprocess
import sys
import time
from datetime import datetime

DEFAULT_SUBNET = "192.168.31"
DEFAULT_PORTS = (22, 445, 5555, 80)


def tcp_open(ip, port, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def probe_adb(adb, ip):
    """Optional: confirm Android identity on a 5555 host. Never required."""
    try:
        out = subprocess.run([adb, "-s", "%s:5555" % ip, "shell",
                              "getprop ro.product.model; getprop ro.board.platform"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=6)
        return out.stdout.decode("utf-8", "replace").strip().replace("\n", " / ")
    except Exception as exc:
        return "adb failed: %s" % exc


def arp_table(subnet):
    """Best-effort neighbour list inside the target subnet (INFO only)."""
    try:
        out = subprocess.run(["arp", "-a"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=8)
        text = out.stdout.decode("utf-8", "replace")
        found = []
        for line in text.splitlines():
            for tok in line.split():
                parts = tok.split(".")
                if len(parts) == 4 and tok.startswith(subnet + ".") and \
                        all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    found.append(tok)
                    break
        return sorted(set(found))
    except Exception:
        return []


def collect(subnet, ports, timeout, adb, target):
    hosts = [target] if target else ["%s.%d" % (subnet, i) for i in range(1, 255)]
    jobs = [(ip, p) for ip in hosts for p in ports]
    opened = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=96) as pool:
        fut = {pool.submit(tcp_open, ip, p, timeout): (ip, p) for ip, p in jobs}
        for f in concurrent.futures.as_completed(fut):
            ip, p = fut[f]
            try:
                if f.result():
                    opened.setdefault(ip, []).append(p)
            except Exception:
                pass
    identities = {}
    adb_hosts = sorted(ip for ip, ps in opened.items() if 5555 in ps)
    if adb and adb_hosts:
        for ip in adb_hosts:
            identities[ip] = probe_adb(adb, ip)
    return {"hosts": len(hosts), "open": {ip: sorted(ps) for ip, ps in sorted(opened.items())},
            "ssh_hosts": sorted(ip for ip, ps in opened.items() if 22 in ps),
            "samba_hosts": sorted(ip for ip, ps in opened.items() if 445 in ps or 139 in ps),
            "adb_hosts": adb_hosts, "adb_identity": identities}


def verdict_of(snap, baseline, device_ip, prev_online):
    opened = snap["open"]
    known_ssh = ({ip for ip, ps in baseline.get("open", {}).items() if 22 in ps}
                 if baseline is not None else set())

    def ssh_verdict(ip):
        if baseline is None:
            return "BOOTED_UNCONFIRMED"
        return "BOOTED" if ip not in known_ssh else "BASELINE_ONLY"

    if device_ip:
        dev = opened.get(device_ip, [])
        if 22 in dev:
            return ssh_verdict(device_ip), ([device_ip] if device_ip not in known_ssh else [])
        if 5555 in dev:
            return "ANDROID_FALLBACK", []
        if 445 in dev or 139 in dev:
            return "BOOTING", []
        if prev_online:
            return "REBOOTING", []
        return "NO_LINK", []

    ssh = snap["ssh_hosts"]
    if baseline is None:
        if ssh:
            return "BOOTED_UNCONFIRMED", []
    else:
        new = [ip for ip in ssh if ip not in known_ssh]
        if new:
            return "BOOTED", new
        if ssh:
            return "BASELINE_ONLY", []
    if snap["adb_hosts"]:
        return "ANDROID_FALLBACK", []
    if snap["samba_hosts"]:
        return "BOOTING", []
    if prev_online:
        return "REBOOTING", []
    return "NO_LINK", []


def snapshot(subnet, ports, timeout, adb, target, baseline, device_ip, prev_online):
    snap = collect(subnet, ports, timeout, adb, target)
    verdict, new = verdict_of(snap, baseline, device_ip, prev_online)
    dev_ports = snap["open"].get(device_ip, []) if device_ip else []
    return {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target": target or "%s.0/24" % subnet,
        "device_ip": device_ip,
        "device_ports": dev_ports,
        "device_online": bool(dev_ports),
        "hosts_scanned": snap["hosts"],
        "open": snap["open"],
        "ssh_hosts": snap["ssh_hosts"],
        "samba_hosts": snap["samba_hosts"],
        "adb_hosts": snap["adb_hosts"],
        "new_ssh_hosts": new,
        "baseline_used": baseline is not None,
        "arp": arp_table(subnet) if not target else [],
        "adb_identity": snap["adb_identity"],
        "verdict": verdict,
    }


def human(r, t0):
    rel = int(time.monotonic() - t0)
    print("[t+%3ds] %-18s device=%s:%s open=%s" %
          (rel, r["verdict"], r["device_ip"] or "-",
           "/".join(map(str, r["device_ports"])) or "none",
           ", ".join("%s:%s" % (ip, "/".join(map(str, ps))) for ip, ps in r["open"].items()) or "none"))
    if r["ssh_hosts"]:
        print("          SSH: %s | new vs baseline: %s" % (r["ssh_hosts"], r["new_ssh_hosts"] or "none"))
    if r["adb_hosts"]:
        print("          ADB: %s -> eMMC Android, USB boot did NOT take" % r["adb_hosts"])
    for ip, ident in r["adb_identity"].items():
        print("          adb %s: %s" % (ip, ident))
    if r["arp"]:
        print("          arp: %s" % ", ".join(r["arp"][:20]))


def guidance(verdict):
    return {
        "BOOTED": "PASS: device SSH is up and new vs baseline -> Linux init reached. "
                  "Wait for the first-boot resize reboot, then do post-boot checks.",
        "BOOTED_UNCONFIRMED": "INCONCLUSIVE: SSH seen but no baseline. "
                              "Re-run with --baseline baseline.json before judging.",
        "BASELINE_ONLY": "NOT YET: SSH only on pre-existing hosts. "
                         "Wait for the resize reboot or check UART.",
        "BOOTING": "IN PROGRESS: Samba up, SSH not ready (or briefly down). "
                   "Normal during first-boot fs-resize; keep power on, do NOT retry passwords.",
        "REBOOTING": "IN PROGRESS: device went silent after being online - the "
                     "resize script called reboot -f. Keep power on and keep watching.",
        "ANDROID_FALLBACK": "FAIL: U-Boot ignored the USB chain. Check USB port, "
                            "aml_autoscript execution, SD slot, or UART log.",
        "NO_LINK": "FAIL: device silent. Kernel did not reach init or the "
                   "Ethernet PHY is down. Use UART ttyAML0,115200n8.",
    }[verdict]


def load_baseline(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "open" not in data:
        raise ValueError("baseline file has no 'open' mapping")
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subnet", default=DEFAULT_SUBNET, help="first 3 octets (default: %(default)s)")
    ap.add_argument("--target", help="probe a single IP instead of the /24")
    ap.add_argument("--device-ip", help="IP of the box; makes verdicts device-specific")
    ap.add_argument("--ports", default=",".join(map(str, DEFAULT_PORTS)),
                    help="comma separated TCP ports (default: %(default)s)")
    ap.add_argument("--timeout", type=float, default=0.4, help="per-connect timeout, seconds")
    ap.add_argument("--once", action="store_true", help="single snapshot and exit")
    ap.add_argument("--watch", action="store_true", help="repeat until --duration elapses")
    ap.add_argument("--duration", type=int, default=300, help="watch duration, seconds")
    ap.add_argument("--interval", type=float, default=10, help="watch interval, seconds")
    ap.add_argument("--adb", help="optional path to adb.exe; only used on 5555 hosts")
    ap.add_argument("--out", help="append each snapshot as JSONL to this file")
    ap.add_argument("--save-baseline", help="record the pre-power-on LAN state and exit")
    ap.add_argument("--baseline", help="baseline JSON produced by --save-baseline")
    args = ap.parse_args()

    try:
        ports = tuple(int(p) for p in args.ports.split(",") if p.strip())
        if args.adb and not args.adb.lower().endswith(".exe"):
            ap.error("--adb must point to adb.exe")
        baseline = load_baseline(args.baseline) if args.baseline else None
    except ValueError as exc:
        ap.error(str(exc))
    except OSError as exc:
        ap.error("--baseline: %s" % exc)

    print("A311D bring-up probe | target=%s device=%s ports=%s baseline=%s" %
          (args.target or (args.subnet + ".0/24"), args.device_ip or "auto", ports,
           args.baseline or "NONE (verdicts will be unconfirmed)"))

    if args.save_baseline:
        rec = snapshot(args.subnet, ports, args.timeout, args.adb, args.target,
                       None, args.device_ip, False)
        with open(args.save_baseline, "w", encoding="utf-8") as f:
            json.dump({"ts": rec["ts"], "open": rec["open"],
                       "ssh_hosts": rec["ssh_hosts"], "samba_hosts": rec["samba_hosts"],
                       "adb_hosts": rec["adb_hosts"]}, f, indent=2)
        print("BASELINE SAVED: %s" % args.save_baseline)
        print("  pre-existing open ports: %s" % (rec["open"] or "none"))
        print("  Take it BEFORE powering the box, then use --baseline on every later run.")
        return 0

    print("Start the box NOW if running --watch. Ctrl+C stops early.\n")
    t0 = time.monotonic()
    last, prev_online = None, False
    fout = open(args.out, "a", encoding="utf-8") if args.out else None
    try:
        while True:
            r = snapshot(args.subnet, ports, args.timeout, args.adb, args.target,
                         baseline, args.device_ip, prev_online)
            human(r, t0)
            if fout:
                fout.write(json.dumps(r, ensure_ascii=False) + "\n")
                fout.flush()
            last = r
            prev_online = r["device_online"] if args.device_ip else bool(r["open"])
            if args.once or not args.watch:
                break
            if time.monotonic() - t0 >= args.duration:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        if fout:
            fout.close()

    print("")
    print("FINAL VERDICT: %s" % (last["verdict"] if last else "NO_DATA"))
    print(guidance(last["verdict"]) if last else "no snapshot collected")
    if args.out:
        print("JSONL record: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
