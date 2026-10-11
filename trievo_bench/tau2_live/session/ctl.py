"""Runtime-side control client for the τ² session service (not agent-visible)."""

from __future__ import annotations

import argparse
import json
import socket
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("op", choices=["finish", "status"])
    parser.add_argument("--socket", default="/run/trievo-tau2/control.sock")
    parser.add_argument("--grace-seconds", type=float, default=420.0)
    args = parser.parse_args(argv)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(args.grace_seconds + 60)
        client.connect(args.socket)
        client.sendall((json.dumps({"op": args.op, "grace_seconds": args.grace_seconds}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    sys.stdout.write(data.decode("utf-8"))
    return 0 if data else 1


if __name__ == "__main__":
    sys.exit(main())
