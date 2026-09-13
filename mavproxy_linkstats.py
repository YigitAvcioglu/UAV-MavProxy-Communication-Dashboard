#!/usr/bin/env python3
"""Five-link MAVProxy health publisher for the dashboard.

Publishes one JSON datagram per second to the local dashboard collector.
The packet contains:
- five-link health metrics for every currently enabled MAVProxy master
- active command TX link
- links currently receiving fresh telemetry

Environment:
    LINK_DASHBOARD_CONFIG=/path/to/dashboard_config.json
"""

from __future__ import annotations

import json
import os
import socket
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

from MAVProxy.modules.lib import mp_module

DEFAULT_CONFIG = Path.home() / ".config" / "link-dashboard" / "dashboard_config.json"
PERIOD = 1.0
WINDOW = 5.0
STALE_DOWN_S = 3.0
RF_STALE_S = 3.0


class LinkStatsModule(mp_module.MPModule):
    def __init__(self, mpstate):
        super().__init__(mpstate, "linkstats", "five-link health JSON publisher")
        self.config_path = Path(os.environ.get("LINK_DASHBOARD_CONFIG", DEFAULT_CONFIG))
        self.config = self._load_config()
        stats_cfg = self.config.get("stats_udp", {})
        # Dashboard is on the same computer. Pollers may also send to the dashboard separately.
        self.dest = ("127.0.0.1", int(stats_cfg.get("port", 14660)))
        self.link_items = list(self.config.get("links", []))
        self.alias_map = self._build_alias_map()

        self.last_pub = 0.0
        self.hist: dict[str, deque] = {}
        self.prev_count: dict[str, int] = {}
        self.last_change: dict[str, float] = {}
        self.object_ids: dict[str, int] = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._err_count = 0
        print(
            "linkstats v9: %s:%d, %.1fs period, %.0fs window, 5-link dashboard"
            % (self.dest[0], self.dest[1], PERIOD, WINDOW)
        )

    def _load_config(self) -> dict[str, Any]:
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            raise RuntimeError("linkstats config could not be read: %s (%s)" % (self.config_path, exc))

    def _build_alias_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in self.link_items:
            canonical = str(item.get("id", item.get("display_name", "UNKNOWN")))
            values = {
                canonical,
                str(item.get("display_name", "")),
                str(item.get("mavproxy_label", "")),
            }
            values.update(str(x) for x in item.get("aliases", []))
            for value in values:
                if value:
                    result[value.upper()] = canonical
        return result

    def _canonical_name(self, master) -> str:
        label = str(getattr(master, "label", "") or "")
        address = str(getattr(master, "address", "") or "")

        for candidate in (label, address):
            upper = candidate.upper()
            if upper in self.alias_map:
                return self.alias_map[upper]
            for alias, canonical in self.alias_map.items():
                if alias and alias in upper:
                    return canonical

        return label or address or "LINK%d" % (int(getattr(master, "linknum", 0)) + 1)

    @staticmethod
    def _msg_timestamp(msg, fallback=0.0) -> float:
        try:
            ts = getattr(msg, "_timestamp", None)
            return float(ts) if ts is not None else fallback
        except Exception:
            return fallback

    @staticmethod
    def _latest_message(master, msg_type):
        try:
            return (getattr(master, "messages", {}) or {}).get(msg_type)
        except Exception:
            return None

    @staticmethod
    def _sik_raw_to_dbm(value):
        try:
            value = float(value)
            if value < 0 or value >= 255:
                return None
            return round(value / 1.9 - 127.0, 1)
        except Exception:
            return None

    def _radio_status_fields(self, master, now):
        radio = self._latest_message(master, "RADIO_STATUS")
        if radio is None:
            return {}
        ts = self._msg_timestamp(radio, fallback=0.0)
        if ts <= 0 or now - ts > RF_STALE_S:
            return {}

        mapping = {
            "rssi_dbm": "rssi",
            "remote_rssi_dbm": "remrssi",
            "noise_dbm": "noise",
            "remote_noise_dbm": "remnoise",
        }
        result = {}
        for out_name, mav_name in mapping.items():
            converted = self._sik_raw_to_dbm(getattr(radio, mav_name, 255))
            if converted is not None:
                result[out_name] = converted
        return result

    def _rc_rssi_fields(self, master, now):
        rc = self._latest_message(master, "RC_CHANNELS")
        if rc is None:
            return {}
        ts = self._msg_timestamp(rc, fallback=0.0)
        if ts <= 0 or now - ts > RF_STALE_S:
            return {}
        try:
            raw = float(getattr(rc, "rssi", 255))
            if 0 <= raw < 255:
                return {"rssi_pct": round(100.0 * raw / 254.0, 1)}
        except Exception:
            pass
        return {}

    def _reset_history_if_readded(self, name: str, master, now: float):
        current_id = id(master)
        if self.object_ids.get(name) == current_id:
            return
        self.object_ids[name] = current_id
        self.hist[name] = deque()
        self.prev_count[name] = int(getattr(master, "mav_count", 0) or 0)
        self.last_change[name] = now

    def _collect_one(self, master, now: float) -> dict[str, Any]:
        name = self._canonical_name(master)
        self._reset_history_if_readded(name, master, now)

        count = int(getattr(master, "mav_count", 0) or 0)
        lost = int(getattr(master, "mav_loss", 0) or 0)
        history = self.hist.setdefault(name, deque())
        history.append((now, count, lost))
        while history and now - history[0][0] > WINDOW:
            history.popleft()

        t0, c0, l0 = history[0]
        dcount = max(0, count - c0)
        dlost = max(0, lost - l0)
        denom = dcount + dlost
        loss_pct = 100.0 * dlost / denom if denom > 0 else 0.0
        elapsed = max(PERIOD, now - t0)
        pkt_rate = dcount / elapsed

        previous = self.prev_count.get(name, count)
        if count != previous or name not in self.last_change:
            self.last_change[name] = now
        self.prev_count[name] = count

        last_seen = self.last_change.get(name, now)
        stale = max(0.0, now - last_seen)
        linkerror = bool(getattr(master, "linkerror", False))
        delayed = bool(getattr(master, "link_delayed", False))
        up = not linkerror and stale <= STALE_DOWN_S

        entry: dict[str, Any] = {
            "id": name,
            "name": name,
            "mavproxy_label": str(getattr(master, "label", name)),
            "link": int(getattr(master, "linknum", -1)) + 1,
            "enabled": True,
            "up": up,
            "delayed": delayed,
            "loss_pct": round(loss_pct, 1),
            "pkt_rate": round(pkt_rate, 1),
            "stale_s": round(stale, 2),
            "address": str(getattr(master, "address", "")),
            "_last_seen": last_seen,
        }

        if name in ("RFD900x", "CUAV_P8"):
            entry.update(self._radio_status_fields(master, now))
        elif name == "ELRS":
            entry.update(self._rc_rssi_fields(master, now))

        return entry

    def _active_command_tx(self):
        masters = list(getattr(self.mpstate, "mav_master", []) or [])
        try:
            fwd = int(getattr(self.mpstate.settings, "mavfwd_link", -1))
        except Exception:
            fwd = -1
        if 1 <= fwd <= len(masters):
            conn = masters[fwd - 1]
            return self._canonical_name(conn), "mavfwd_link"
        try:
            target_sysid = int(getattr(self.mpstate.settings, "target_system", -1))
            if target_sysid > 0:
                conn = self.mpstate.master(target_sysid)
                source = "freshest_heartbeat"
            else:
                conn = self.mpstate.master()
                source = "primary"
        except Exception:
            conn = None
            source = "none"
        return (self._canonical_name(conn), source) if conn is not None else (None, "none")

    def idle_task(self):
        now = time.time()
        if now - self.last_pub < PERIOD:
            return
        self.last_pub = now

        entries = []
        masters = list(getattr(self.mpstate, "mav_master", []) or [])
        for master in masters:
            try:
                entries.append(self._collect_one(master, now))
            except Exception:
                self._err_count += 1
                if self._err_count <= 5:
                    print("LINKSTATS ERROR:")
                    traceback.print_exc()

        freshest = max(
            [entry.get("_last_seen", 0.0) for entry in entries if entry.get("up")]
            or [0.0]
        )
        max_rate = max([float(entry.get("pkt_rate", 0.0)) for entry in entries] or [0.0])
        rx_used = []

        for entry in entries:
            last_seen = float(entry.pop("_last_seen", 0.0) or 0.0)
            if entry.get("up") and freshest > 0 and last_seen > 0:
                entry["delay_ms"] = int(round(max(0.0, freshest - last_seen) * 1000.0))
                rx_used.append(entry["id"])
            if max_rate > 0:
                entry["rate_pct"] = round(100.0 * float(entry.get("pkt_rate", 0.0)) / max_rate, 1)

        active_tx, source = self._active_command_tx()
        payload = {
            "t": round(now, 3),
            "active_tx": active_tx,
            "active_tx_source": source,
            "rx_used": rx_used,
            "links": entries,
        }

        try:
            self.sock.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"), self.dest)
        except Exception:
            self._err_count += 1
            if self._err_count <= 5:
                print("LINKSTATS UDP ERROR:")
                traceback.print_exc()


def init(mpstate):
    return LinkStatsModule(mpstate)
