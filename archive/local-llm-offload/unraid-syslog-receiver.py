#!/usr/bin/env python3
"""Minimal off-box UDP syslog receiver for Unraid (storagedemon).
Listens on 0.0.0.0:5514 and appends each datagram to a dated logfile so a
host wedge/crash on Unraid leaves its pre-crash log safely on Studio.
Stdlib only. Runs under a LaunchAgent (KeepAlive)."""
import datetime as dt
import os
import socket

PORT = int(os.environ.get("SYSLOG_PORT", "5514"))
LOGDIR = os.path.expanduser("~/unraid-syslog")
os.makedirs(LOGDIR, exist_ok=True)

def logpath():
    return os.path.join(LOGDIR, "storagedemon-%s.log" % dt.date.today().isoformat())

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    while True:
        data, addr = s.recvfrom(65535)
        ts = dt.datetime.now().isoformat(timespec="seconds")
        line = "%s %s %s\n" % (ts, addr[0], data.decode("utf-8", "replace").rstrip("\n"))
        with open(logpath(), "a") as f:
            f.write(line)

if __name__ == "__main__":
    main()
