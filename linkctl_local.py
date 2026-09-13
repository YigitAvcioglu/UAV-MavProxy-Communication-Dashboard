#!/usr/bin/env python3
"""Local CLI for testing the localhost-only MAVProxy linkcontrol module."""

import argparse
import json
import socket
import sys

HOST = "127.0.0.1"
PORT = 16060
LINKS = ("Ubiquiti", "RFD900x", "4G", "CUAV_P8", "ELRS")


def main() -> int:
    parser = argparse.ArgumentParser(description="Local MAVProxy link control")
    parser.add_argument("action", choices=("status", "on", "off", "toggle", "select"))
    parser.add_argument("link", nargs="?", choices=LINKS)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--timeout", type=float, default=1.0)
    args = parser.parse_args()

    if args.action != "status" and not args.link:
        parser.error("a link name is required for this action")
    if args.action == "status" and args.link:
        parser.error("a link name is not used with the status action")

    payload = {"action": args.action}
    if args.link:
        payload["link"] = args.link

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(args.timeout)
    try:
        sock.sendto(json.dumps(payload).encode("utf-8"), (HOST, args.port))
        raw, _ = sock.recvfrom(65535)
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    finally:
        sock.close()

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
