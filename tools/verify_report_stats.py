#!/usr/bin/env python3
"""A311D / AURKNIX: verify the rocknix-report-stats hardening.

Why this exists
---------------
On the CAINIAO CNIoT-CORE box the boot console printed
"[FAILED] Failed to start rocknix-report-stats.service" during the first
upgrade. The upstream script gates on "ping -c 1 8.8.8.8", posts to a
third-party DDNS endpoint, retries 5 x 60s and exits 1. On a CN uplink
ICMP to 8.8.8.8 and that endpoint are normally unreachable, so every
upgrade ends with a failed systemd unit and up to five minutes of a live
oneshot job. post-update deletes the "already reported" marker on every
upgrade, so it is guaranteed to recur.

The fix makes reporting opt-in and strictly best effort. This tool verifies
the fix both statically and by actually running the POSIX script against a
local HTTP endpoint. Standard library only, no external dependencies.

Usage
-----
  python tools/verify_report_stats.py
  python tools/verify_report_stats.py --json E:/A311d/tools/report_stats_verify.json
  python tools/verify_report_stats.py --repo-root D:/ProgramData/distribution_rocknix

Exit code 0 = all checks passed, 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[1]
PKG = Path("projects/ROCKNIX/packages/rocknix")
SCRIPT_REL = PKG / "sources/scripts/rocknix-report-stats"
UNIT_REL = PKG / "system.d/rocknix-report-stats.service"
TIMER_REL = PKG / "system.d/rocknix-report-stats.timer"
CFG_REL = PKG / "config/system/configs/system.cfg"
MK_REL = PKG / "package.mk"
POST_UPDATE_REL = PKG / "sources/post-update"

MACHINE_ID = "abcdefghijklmnop"
UID12 = MACHINE_ID[-12:]  # efghijklmnop


class Handler(http.server.BaseHTTPRequestHandler):
    requests: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        Handler.requests.append(self.rfile.read(n))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        pass


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, cid: str, name: str, ok: bool | None, detail: str) -> None:
        state = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.items.append({"id": cid, "name": name, "state": state, "detail": detail})
        print(f"[{state:4}] {cid:12} {name}: {detail}")

    @property
    def failed(self) -> int:
        return sum(1 for i in self.items if i["state"] == "FAIL")

    @property
    def passed(self) -> int:
        return sum(1 for i in self.items if i["state"] == "PASS")

    @property
    def skipped(self) -> int:
        return sum(1 for i in self.items if i["state"] == "SKIP")


def static_checks(root: Path, rep: Report) -> str | None:
    script = root / SCRIPT_REL
    unit = root / UNIT_REL
    cfg = root / CFG_REL
    mk = root / MK_REL
    post_update = root / POST_UPDATE_REL

    for label, path in (("script", script), ("unit", unit), ("system.cfg", cfg)):
        if not path.is_file():
            rep.add("TC-STA-00", f"{label} exists", False, f"missing {path}")
            return None
    src = script.read_text(encoding="utf-8", errors="replace")
    # Only inspect code lines: the header comment quotes the old broken
    # implementation on purpose, and must not trigger the static checks.
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    unit_txt = unit.read_text(encoding="utf-8", errors="replace")
    cfg_txt = cfg.read_text(encoding="utf-8", errors="replace")
    mk_txt = mk.read_text(encoding="utf-8", errors="replace") if mk.is_file() else ""
    pu_txt = post_update.read_text(encoding="utf-8", errors="replace") if post_update.is_file() else ""

    sh = shutil.which("sh") or shutil.which("bash")
    if sh:
        r = subprocess.run([sh, "-n", str(script)], capture_output=True, text=True, timeout=20)
        rep.add("TC-STA-01", "POSIX sh syntax (sh -n)", r.returncode == 0,
                "syntax OK" if r.returncode == 0 else (r.stderr.strip() or "syntax error"))
    else:
        rep.add("TC-STA-01", "POSIX sh syntax (sh -n)", None, "no sh found, skipped")

    rep.add("TC-STA-02", "no 8.8.8.8 ICMP gate", "8.8.8.8" not in code and "ping -c" not in code,
            "removed" if "8.8.8.8" not in code else "still gates on ping 8.8.8.8")
    rep.add("TC-STA-03", "no bash-only substring", "${USERID:(-12)}" not in code,
            "POSIX tail -c used" if "${USERID:(-12)}" not in code else "still uses ${VAR:(-N)}")
    rep.add("TC-STA-04", "opt-in gate present", "stats.enabled" in code,
            "stats.enabled gate found" if "stats.enabled" in code else "no opt-in gate")
    rep.add("TC-STA-05", "curl bounded timeouts",
            "--connect-timeout" in code and "--max-time" in code and "--fail" in code,
            "connect/max timeouts + --fail (HTTP errors are failures)"
            if "--connect-timeout" in code and "--max-time" in code and "--fail" in code
            else "missing timeout or --fail option")
    bad_exits = [m for m in re.findall(r"(?m)^\s*exit\s+(\S+)", code) if m.strip() != "0"]
    rep.add("TC-STA-06", "all explicit exits are 0", not bad_exits,
            "no non-zero exit" if not bad_exits else f"found exit {bad_exits}")
    need_unit = ["ConditionPathExists=/storage/.cache/systemd-machine-id",
                 "TimeoutStartSec=90s", "SuccessExitStatus=1"]
    miss = [x for x in need_unit if x not in unit_txt]
    rep.add("TC-STA-07", "unit hardening", not miss,
            "machine-id condition + timeout + success status" if not miss else f"missing {miss}")
    rep.add("TC-STA-08", "default stats.enabled=0",
            bool(re.search(r"(?m)^stats\.enabled=0\s*$", cfg_txt)),
            "shipped default is off" if "stats.enabled=0" in cfg_txt else "no default (script still defaults off)")
    no_direct_enable = "enable_service rocknix-report-stats.service" not in mk_txt
    rep.add("TC-STA-09", "service not on boot target", no_direct_enable,
            "only the 5min timer starts it; failure cannot block boot"
            if no_direct_enable else "service is enabled on a boot target")
    rep.add("TC-STA-10", "upgrade still clears marker",
            "rocknix-stats-reported" in pu_txt,
            "post-update deletes marker; script must stay non-fatal"
            if "rocknix-stats-reported" in pu_txt else "marker cleanup missing")
    lf_only = b"\r\n" not in script.read_bytes() and b"\r\n" not in unit.read_bytes()
    rep.add("TC-STA-11", "LF line endings", lf_only,
            "pure LF" if lf_only else "CRLF detected (git autocrlf must stay false)")
    return sh


def run_case(sh: str, script: Path, cfg_value: str, want_id: bool,
             endpoint: str, timeout: int = 20) -> dict:
    with tempfile.TemporaryDirectory(prefix="rnstats_") as td:
        t = Path(td)
        cache = t / ".cache"
        cache.mkdir()
        cfg = t / "system.cfg"
        cfg.write_text(f"root.password=rocknix\nstats.enabled={cfg_value}\n", encoding="utf-8")
        marker = cache / "rocknix-stats-reported"
        mid = cache / "systemd-machine-id"
        if want_id:
            mid.write_text(MACHINE_ID + "\n", encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "ROCKNIX_SKIP_PROFILE": "1",
            "ROCKNIX_STORAGE_ROOT": t.as_posix(),
            "ROCKNIX_STATS_SYSCFG": cfg.as_posix(),
            "ROCKNIX_STATS_MARKER": marker.as_posix(),
            "ROCKNIX_STATS_MACHINE_ID": mid.as_posix(),
            "ROCKNIX_STATS_ENDPOINT": endpoint,
            "ROCKNIX_STATS_RETRIES": "1",
            "ROCKNIX_STATS_RETRY_DELAY": "0",
            "ROCKNIX_STATS_CONNECT_TIMEOUT": "2",
            "ROCKNIX_STATS_MAX_TIME": "3",
            # The WorkBuddy sandbox injects an HTTP proxy; bypass it so the
            # local endpoint / closed port behave deterministically.
            "NO_PROXY": "*", "no_proxy": "*",
            "HTTP_PROXY": "", "http_proxy": "",
            "HTTPS_PROXY": "", "https_proxy": "",
        })
        t0 = time.monotonic()
        try:
            r = subprocess.run([sh, str(script)], env=env, capture_output=True,
                               text=True, timeout=timeout)
            rc, out = r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            rc, out = -99, f"TIMEOUT after {timeout}s"
        return {"rc": rc, "out": out, "marker": marker.exists(),
                "elapsed": round(time.monotonic() - t0, 2)}


def behaviour_checks(sh: str | None, script: Path, rep: Report) -> None:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    endpoint = f"http://127.0.0.1:{port}/"
    try:
        if not sh:
            for cid in ("TC-BEH-01", "TC-BEH-02", "TC-BEH-03", "TC-BEH-04"):
                rep.add(cid, "behaviour", None, "no sh found, skipped")
            return

        # TC-BEH-01: default off -> zero network traffic, instant success
        Handler.requests.clear()
        r = run_case(sh, script, "0", True, endpoint)
        ok = (r["rc"] == 0 and "disabled" in r["out"].lower()
              and not Handler.requests and not r["marker"] and r["elapsed"] < 5)
        rep.add("TC-BEH-01", "opt-out: no network, exit 0", ok,
                f"rc={r['rc']} http={len(Handler.requests)} marker={r['marker']} "
                f"{r['elapsed']}s msg={r['out'][:60]!r}")

        # TC-BEH-02: enabled but no machine id -> skip without network
        Handler.requests.clear()
        r = run_case(sh, script, "1", False, endpoint)
        ok = (r["rc"] == 0 and "machine id" in r["out"].lower()
              and not Handler.requests and not r["marker"])
        rep.add("TC-BEH-02", "missing machine id: skip", ok,
                f"rc={r['rc']} http={len(Handler.requests)} msg={r['out'][:60]!r}")

        # TC-BEH-03: endpoint unreachable -> must NOT fail the unit
        Handler.requests.clear()
        r = run_case(sh, script, "1", True, "http://127.0.0.1:1/")
        ok = (r["rc"] == 0 and "non-fatal" in r["out"] and not r["marker"])
        rep.add("TC-BEH-03", "endpoint down: non-fatal exit 0", ok,
                f"rc={r['rc']} marker={r['marker']} {r['elapsed']}s msg={r['out'][:60]!r}")

        # TC-BEH-04: reachable endpoint -> marker written, correct payload
        Handler.requests.clear()
        r = run_case(sh, script, "1", True, endpoint)
        body = Handler.requests[0] if Handler.requests else b""
        payload_ok = (b"aurknix" in body and b"ROCKNIX_UID" in body
                      and UID12.encode() in body and b"ROCKNIX_BUILD" in body)
        ok = (r["rc"] == 0 and r["marker"] and len(Handler.requests) == 1 and payload_ok)
        rep.add("TC-BEH-04", "endpoint up: report + marker", ok,
                f"rc={r['rc']} http={len(Handler.requests)} marker={r['marker']} "
                f"uid12={UID12} payload_ok={payload_ok} msg={r['out'][:50]!r}")
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=str(REPO_ROOT_DEFAULT))
    ap.add_argument("--json", dest="json_out", default=None, help="write JSON report here")
    ap.add_argument("--list-checks", action="store_true")
    args = ap.parse_args()

    if args.list_checks:
        ids = [f"TC-STA-{i:02d}" for i in range(0, 12)] + [f"TC-BEH-{i:02d}" for i in range(1, 5)]
        print("\n".join(ids))
        return 0

    root = Path(args.repo_root)
    print(f"A311D report-stats verification  repo={root}\n")
    rep = Report()
    sh = static_checks(root, rep)
    behaviour_checks(sh, root / SCRIPT_REL, rep)

    print(f"\nRESULT: {rep.passed} PASS / {rep.failed} FAIL / {rep.skipped} SKIP")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "repo_root": str(root), "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {"pass": rep.passed, "fail": rep.failed, "skip": rep.skipped},
            "checks": rep.items,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"report: {out}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
