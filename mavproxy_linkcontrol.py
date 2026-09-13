#!/usr/bin/env python3
"""Local-only MAVProxy link ON/OFF controller.

The module listens only on 127.0.0.1. It is intended to be controlled by the
PyQt/web dashboard running on the same MAVProxy computer. There is no token,
remote IP allow-list, or LAN-facing control socket.

Environment:
    LINK_DASHBOARD_CONFIG=/path/to/dashboard_config.json

MAVProxy console:
    linkcontrol status
    linkcontrol on RFD900x
    linkcontrol off 4G
    linkcontrol toggle ELRS
    linkcontrol select Ubiquiti
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from MAVProxy.modules.lib import mp_module

DEFAULT_CONFIG = Path.home() / ".config" / "link-dashboard" / "dashboard_config.json"


class LinkControlModule(mp_module.MPModule):
    def __init__(self, mpstate):
        super().__init__(
            mpstate,
            "linkcontrol",
            "localhost-only link enable/disable controller",
            public=True,
        )
        self.config_path = Path(os.environ.get("LINK_DASHBOARD_CONFIG", DEFAULT_CONFIG))
        self.config = self._load_config()
        udp_cfg = self.config.get("control_udp", {})
        self.bind_ip = "127.0.0.1"  # intentionally fixed; never expose to LAN
        self.port = int(udp_cfg.get("port", 16060))
        self.links = {
            str(item["id"]): item
            for item in self.config.get("links", [])
            if item.get("id") and item.get("mavproxy_label")
        }

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.bind_ip, self.port))
        self.sock.setblocking(False)

        self.add_command(
            "linkcontrol",
            self.cmd_linkcontrol,
            "local link control",
            ["<status|on|off|toggle|select> [LINK]"],
        )
        print(
            "[linkcontrol] local UDP %s:%d, links=%s"
            % (self.bind_ip, self.port, ",".join(self.links))
        )

    def unload(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _load_config(self) -> dict[str, Any]:
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            raise RuntimeError("linkcontrol config could not be read: %s (%s)" % (self.config_path, exc))

    @staticmethod
    def _conn_label(conn) -> str:
        return str(getattr(conn, "label", getattr(conn, "address", "UNKNOWN")))

    def _find_connection_by_label(self, label: str):
        wanted = label.upper()
        for conn in list(self.mpstate.mav_master):
            if self._conn_label(conn).upper() == wanted:
                return conn
        return None

    def _item_for_name(self, name: str) -> dict[str, Any] | None:
        wanted = name.strip().upper()
        for item in self.links.values():
            candidates = {
                str(item.get("id", "")).upper(),
                str(item.get("display_name", "")).upper(),
                str(item.get("mavproxy_label", "")).upper(),
            }
            candidates.update(str(x).upper() for x in item.get("aliases", []))
            if wanted in candidates:
                return item
        return None

    def _link_module(self):
        module = self.module("link")
        if module is None:
            raise RuntimeError("MAVProxy link module is not loaded")
        return module

    def _command_tx_connection(self):
        masters = list(self.mpstate.mav_master)
        try:
            fwd = int(getattr(self.mpstate.settings, "mavfwd_link", -1))
        except Exception:
            fwd = -1
        if 1 <= fwd <= len(masters):
            return masters[fwd - 1], "mavfwd_link"
        try:
            target_sysid = int(getattr(self.mpstate.settings, "target_system", -1))
            if target_sysid > 0:
                return self.mpstate.master(target_sysid), "freshest_heartbeat"
            return self.mpstate.master(), "primary"
        except Exception:
            return None, "none"

    def _status_payload(self) -> dict[str, Any]:
        active_conn, source = self._command_tx_connection()
        active_label = self._conn_label(active_conn) if active_conn is not None else None
        now = time.time()
        rows = []

        for item in self.links.values():
            label = str(item["mavproxy_label"])
            conn = self._find_connection_by_label(label)
            if conn is None:
                rows.append(
                    {
                        "id": item["id"],
                        "name": item.get("display_name", item["id"]),
                        "label": label,
                        "enabled": False,
                        "state": "OFF",
                        "active_tx": False,
                    }
                )
                continue

            last_rx = max(
                float(getattr(conn, "last_message", 0.0) or 0.0),
                float(getattr(conn, "last_heartbeat", 0.0) or 0.0),
            )
            age = None if last_rx <= 0 else round(max(0.0, now - last_rx), 3)
            down = bool(getattr(conn, "linkerror", False))
            delayed = bool(getattr(conn, "link_delayed", False))
            state = "DOWN" if down else ("DELAYED" if delayed else "UP")

            rows.append(
                {
                    "id": item["id"],
                    "name": item.get("display_name", item["id"]),
                    "label": label,
                    "enabled": True,
                    "state": state,
                    "index": int(getattr(conn, "linknum", -1)) + 1,
                    "address": str(getattr(conn, "address", "")),
                    "active_tx": conn is active_conn,
                    "last_message_age_s": age,
                }
            )

        return {
            "ok": True,
            "active_tx": active_label,
            "active_tx_source": source,
            "links": rows,
            "timestamp": now,
        }

    def _pin_connection(self, conn):
        number = int(conn.linknum) + 1
        self.mpstate.settings.link = number
        if hasattr(self.mpstate.settings, "mavfwd_link"):
            self.mpstate.settings.mavfwd_link = number
        return number

    def _disable(self, item: dict[str, Any]):
        label = str(item["mavproxy_label"])
        target = self._find_connection_by_label(label)
        if target is None:
            return True, "%s is already OFF" % item["display_name"]
        if len(self.mpstate.mav_master) <= 1:
            return False, "the last MAVProxy link cannot be disabled"

        active_before, _source = self._command_tx_connection()
        active_label_before = self._conn_label(active_before) if active_before is not None else None
        try:
            pinned_before = int(getattr(self.mpstate.settings, "mavfwd_link", -1)) > 0
        except Exception:
            pinned_before = False

        self._link_module().cmd_link_remove([label])
        if self._find_connection_by_label(label) is not None:
            return False, "%s could not be disabled" % item["display_name"]

        # Link indices change after removal. Preserve an explicitly pinned TX link.
        remaining_active = self._find_connection_by_label(active_label_before) if active_label_before else None
        if pinned_before and remaining_active is not None:
            self._pin_connection(remaining_active)
        elif active_before is target:
            candidates = [m for m in self.mpstate.mav_master if not bool(getattr(m, "linkerror", False))]
            if not candidates:
                candidates = list(self.mpstate.mav_master)
            if candidates:
                self._pin_connection(candidates[0])

        return True, "%s OFF" % item["display_name"]

    def _enable(self, item: dict[str, Any]):
        label = str(item["mavproxy_label"])
        if self._find_connection_by_label(label) is not None:
            return True, "%s is already ON" % item["display_name"]

        descriptor = str(item.get("descriptor", "")).strip()
        if not descriptor or "REPLACE_WITH" in descriptor:
            return False, "%s descriptor is not configured in the JSON file" % item["display_name"]

        link_module = self._link_module()
        old_baud = getattr(link_module.settings, "baudrate", None)
        try:
            baud = item.get("baud")
            if baud is not None:
                link_module.settings.baudrate = int(baud)
            success = bool(link_module.link_add(descriptor))
        finally:
            if old_baud is not None:
                link_module.settings.baudrate = old_baud

        if not success or self._find_connection_by_label(label) is None:
            return False, "%s could not be enabled" % item["display_name"]
        return True, "%s ON" % item["display_name"]

    def _select(self, item: dict[str, Any]):
        conn = self._find_connection_by_label(str(item["mavproxy_label"]))
        if conn is None:
            return False, "%s is OFF; enable it first" % item["display_name"]
        self._pin_connection(conn)
        return True, "command TX link %s" % item["display_name"]

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action", "status")).strip().lower()
        if action == "status":
            return self._status_payload()

        item = self._item_for_name(str(request.get("link", "")))
        if item is None:
            return {"ok": False, "error": "unknown link", "allowed": list(self.links)}

        if action == "on":
            ok, message = self._enable(item)
        elif action == "off":
            ok, message = self._disable(item)
        elif action == "toggle":
            if self._find_connection_by_label(str(item["mavproxy_label"])) is None:
                ok, message = self._enable(item)
            else:
                ok, message = self._disable(item)
        elif action == "select":
            ok, message = self._select(item)
        else:
            return {"ok": False, "error": "action must be status/on/off/toggle/select"}

        result = self._status_payload()
        result["ok"] = ok
        result["message"] = message
        return result

    def _send(self, addr, payload):
        try:
            self.sock.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), addr)
        except Exception as exc:
            print("[linkcontrol] response error: %s" % exc)

    def idle_task(self):
        for _ in range(20):
            try:
                raw, addr = self.sock.recvfrom(65535)
            except BlockingIOError:
                break
            except OSError:
                break
            try:
                # Socket is loopback-only, but verify defensively.
                if addr[0] not in ("127.0.0.1", "::1"):
                    self._send(addr, {"ok": False, "error": "localhost only"})
                    continue
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("JSON object expected")
                response = self._handle(request)
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            self._send(addr, response)

    def cmd_linkcontrol(self, args):
        action = args[0].lower() if args else "status"
        request = {"action": action}
        if len(args) > 1:
            request["link"] = " ".join(args[1:])
        print(json.dumps(self._handle(request), indent=2, ensure_ascii=False))


def init(mpstate):
    return LinkControlModule(mpstate)
