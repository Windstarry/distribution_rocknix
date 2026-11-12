#!/usr/bin/env python3
"""Read-only command runner for the A311D test device.

Usage:
    python tools/a311d_ssh_exec.py <host> <user> <password> < cmdlist.txt

Each input line is executed as one command over SSH; blank lines and lines
starting with '#' are skipped. Output is printed with a banner per command.
No file is written on the device; the runner only reads.
"""
import sys
import time

import paramiko

DEFAULT_TIMEOUT = 60
CONNECT_RETRIES = 5
RETRY_SLEEP = 15


def connect(host, user, password):
    """Connect with backoff. The device refuses new sessions with
    'Not allowed at this time' while stale unauthenticated sockets are
    pending (sshd MaxStartups / LoginGraceTime), so a retry loop matters."""
    last = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                host,
                username=user,
                password=password,
                timeout=10,
                banner_timeout=20,
                auth_timeout=20,
                allow_agent=False,
                look_for_keys=False,
            )
            client.get_transport().set_keepalive(15)
            return client
        except Exception as exc:  # noqa: BLE001 - retry on any transport error
            last = exc
            print("[connect %d/%d failed: %s]" % (attempt, CONNECT_RETRIES, exc))
            try:
                client.close()
            except Exception:
                pass
            if attempt < CONNECT_RETRIES:
                time.sleep(RETRY_SLEEP)
    raise SystemExit("cannot connect to %s: %s" % (host, last))


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    host, user, password = sys.argv[1:4]
    cmds = [
        line.rstrip("\n")
        for line in sys.stdin.read().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    client = connect(host, user, password)
    rc_all = 0
    try:
        for cmd in cmds:
            print("=" * 70)
            print("$ " + cmd)
            print("-" * 70)
            body = errs = ""
            rc = 0
            for attempt in (1, 2, 3):
                try:
                    _in, out, err = client.exec_command(cmd, timeout=DEFAULT_TIMEOUT)
                    body = out.read().decode("utf-8", "replace")
                    errs = err.read().decode("utf-8", "replace")
                    rc = out.channel.recv_exit_status()
                    break
                except Exception as exc:  # noqa: BLE001 - reconnect then retry
                    print("[channel error on attempt %d: %s]" % (attempt, exc))
                    if attempt == 3:
                        body, rc = "", 255
                        break
                    try:
                        client.close()
                    except Exception:
                        pass
                    time.sleep(RETRY_SLEEP)
                    client = connect(host, user, password)
            sys.stdout.write(body)
            if not body.endswith("\n") and body:
                print()
            if errs.strip():
                print("[stderr] " + errs.strip())
            if rc != 0:
                print("[rc=%d]" % rc)
                rc_all = rc
    finally:
        client.close()
    return 0 if rc_all == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
