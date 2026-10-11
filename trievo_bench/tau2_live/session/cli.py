#!/usr/bin/env python3
"""Talk to the live tau2-bench conversation of this task.

You are the customer service agent. The customer (a simulated user) and the
domain tools are live; every tool call changes the real environment state, and
the conversation is graded by the official tau2-bench evaluator when it ends.

Commands:
  python3 /workspace/.tau2/tau2.py policy            agent instructions and domain policy
  python3 /workspace/.tau2/tau2.py tools             tool schemas (JSON)
  python3 /workspace/.tau2/tau2.py observe           whose turn it is and the message waiting for you
  python3 /workspace/.tau2/tau2.py say "TEXT"        send TEXT to the customer; prints the reply
  python3 /workspace/.tau2/tau2.py call TOOL 'JSON'  call TOOL with a JSON object of arguments; prints the result

Each turn you either send one message or make one tool call. The customer
ends the conversation. If a reply takes longer than the wait (default 60 s,
--wait N), the command prints turn=pending; run `observe` to collect it.
"""

import json
import os
import socket
import sys

SOCKET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.sock")


def request(payload, wait):
    payload["wait_seconds"] = wait
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(wait + 30)
        client.connect(SOCKET)
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8"))


def main(argv):
    wait = 60.0
    if "--wait" in argv:
        index = argv.index("--wait")
        wait = float(argv[index + 1])
        argv = argv[:index] + argv[index + 2:]
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    if command in {"policy", "tools", "observe"} and not rest:
        payload = {"op": command}
    elif command == "say" and len(rest) == 1:
        payload = {"op": "say", "content": rest[0]}
    elif command == "call" and len(rest) in {1, 2}:
        try:
            arguments = json.loads(rest[1]) if len(rest) == 2 else {}
        except json.JSONDecodeError as exc:
            print(json.dumps({"error": f"arguments are not valid JSON: {exc}"}))
            return 2
        payload = {"op": "call", "name": rest[0], "arguments": arguments}
    else:
        print(__doc__)
        return 2
    if not os.path.exists(SOCKET):
        print(json.dumps({"error": "the conversation has ended or is not running"}))
        return 1
    try:
        response = request(payload, wait)
    except OSError as exc:
        print(json.dumps({"error": f"session unavailable: {exc}"}))
        return 1
    if command == "policy" and "policy" in response:
        print(response["policy"])
    else:
        print(json.dumps(response, ensure_ascii=False, indent=2))
    return 1 if "error" in response else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
