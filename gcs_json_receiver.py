#!/usr/bin/env python3
"""Print link-health JSON received on UDP 14660."""
import argparse
import json
import socket

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--port", type=int, default=14660)
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((args.host, args.port))
print("Listening for UDP JSON: %s:%d" % (args.host, args.port))
while True:
    raw, sender = sock.recvfrom(65535)
    try:
        payload = json.loads(raw.decode("utf-8"))
        print("%s:%d" % sender)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:
        print("invalid packet:", exc)
