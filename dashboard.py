#!/usr/bin/env python3
"""PyQt6 + HTTP communications control panel.

Runs on the Linux computer that hosts MAVProxy. Features:
- five-link health/status/control
- manual IP ping checks
- persistent device editor
- MAVProxy and mavlink-router process start/stop/log consoles
- editable raw MAVProxy command and form-based master/out command builder
- LAN web interface plus local PyQt6 interface
- read-only five-link JSON forwarding to the GCS computer
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from link_health_payload import build_gcs_forward_payload

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "dashboard_config.json"
PROCESS_NAMES = ("mavproxy", "mavlink_router")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def detect_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        sock.close()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)



def normalize_device(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize an IP-only network-check device."""
    return {
        "id": str(value.get("id") or uuid.uuid4().hex[:10]),
        "name": str(value.get("name", "Device")).strip() or "Device",
        "host": str(value.get("host", "")).strip(),
        "enabled": bool(value.get("enabled", True)),
    }



def normalize_gcs_forward(value: Any) -> dict[str, Any]:
    """Validate the live GCS UDP forwarding destination."""
    value = value if isinstance(value, dict) else {}

    host = str(value.get("host", "192.168.1.60")).strip()
    if not host:
        host = "192.168.1.60"

    try:
        port = int(value.get("port", 14660))
    except (TypeError, ValueError):
        port = 14660
    if not 1 <= port <= 65535:
        port = 14660

    try:
        period_s = float(value.get("period_s", 1.0))
    except (TypeError, ValueError):
        period_s = 1.0
    period_s = max(0.2, min(3600.0, period_s))

    return {
        "enabled": bool(value.get("enabled", True)),
        "host": host,
        "port": port,
        "period_s": round(period_s, 3),
    }



def normalize_tx_selection(value: Any) -> dict[str, Any]:
    """Validate manual/automatic command-TX selection settings."""
    value = value if isinstance(value, dict) else {}

    mode = str(value.get("mode", "manual")).strip().lower()
    if mode not in {"manual", "auto"}:
        mode = "manual"

    def number(name: str, default: float, low: float, high: float) -> float:
        try:
            result = float(value.get(name, default))
        except (TypeError, ValueError):
            result = default
        return max(low, min(high, result))

    return {
        "mode": mode,
        "switch_margin_pct": round(number("switch_margin_pct", 10.0, 0.0, 100.0), 2),
        "hold_s": round(number("hold_s", 3.0, 0.0, 60.0), 2),
        "cooldown_s": round(number("cooldown_s", 8.0, 0.0, 300.0), 2),
        "check_period_s": round(number("check_period_s", 0.5, 0.2, 10.0), 2),
    }


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    config.setdefault("app", {})
    config.setdefault("web", {"host": "0.0.0.0", "port": 8080})
    config.setdefault("stats_udp", {"host": "0.0.0.0", "port": 14660, "stale_after_s": 4.0})
    config["gcs_forward_udp"] = normalize_gcs_forward(
        config.get(
            "gcs_forward_udp",
            {"enabled": True, "host": "192.168.1.60", "port": 14660, "period_s": 1.0},
        )
    )
    config["tx_selection"] = normalize_tx_selection(
        config.get(
            "tx_selection",
            {
                "mode": "manual",
                "switch_margin_pct": 10.0,
                "hold_s": 3.0,
                "cooldown_s": 8.0,
                "check_period_s": 0.5,
            },
        )
    )
    config.setdefault("control_udp", {"host": "127.0.0.1", "port": 16060, "timeout_s": 1.0})
    config.setdefault("links", [])

    network = config.setdefault("network_checks", {})
    network.setdefault("timeout_s", 1.0)
    if "devices" not in network:
        legacy = config.get("ping", {}).get("devices", [])
        network["devices"] = [
            {
                "id": uuid.uuid4().hex[:10],
                "name": item.get("name", "Device"),
                "host": item.get("host", ""),
                "enabled": item.get("enabled", True),
            }
            for item in legacy
        ]
    network["devices"] = [normalize_device(item) for item in network.get("devices", [])]

    processes = config.setdefault("processes", {})
    processes.setdefault(
        "mavproxy",
        {
            "command": "mavproxy.py --aircraft=AIRCRAFT --load-module=linkstats,linkcontrol,linkwatch",
            "cwd": "",
            "auto_start": False,
        },
    )
    processes.setdefault(
        "mavlink_router",
        {
            "command": "mavlink-routerd -c /etc/mavlink-router/main.conf",
            "cwd": "",
            "auto_start": False,
        },
    )

    builder = config.setdefault("mavproxy_builder", {})
    builder.setdefault("aircraft", "AIRCRAFT")
    builder.setdefault("baudrate", 57600)
    builder.setdefault("extra_args", "--load-module=linkstats,linkcontrol,linkwatch")
    builder.setdefault("masters", [])
    builder.setdefault("outs", [])
    for master in builder["masters"]:
        master.setdefault("enabled", True)
        master.setdefault("type", "udp")
        master.setdefault("label", "LINK")
        master.setdefault("host", "0.0.0.0")
        master.setdefault("port", 0)
        master.setdefault("path", "")
        master.setdefault("baud", builder["baudrate"])
    for output in builder["outs"]:
        output.setdefault("enabled", True)
        output.setdefault("host", "127.0.0.1")
        output.setdefault("port", 14550)
    return config


class ConfigManager:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        with path.open("r", encoding="utf-8") as handle:
            self.config = normalize_config(json.load(handle))
        self.save()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.config)

    def save(self) -> None:
        with self.lock:
            atomic_write_json(self.path, self.config)

    def update_network_devices(self, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = [normalize_device(item) for item in devices]
        with self.lock:
            self.config["network_checks"]["devices"] = normalized
            self.save()
        return copy.deepcopy(normalized)

    def update_gcs_forward(self, value: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_gcs_forward(value)
        with self.lock:
            self.config["gcs_forward_udp"] = normalized
            self.save()
        return copy.deepcopy(normalized)

    def update_tx_selection(self, value: dict[str, Any]) -> dict[str, Any]:
        current = self.snapshot().get("tx_selection", {})
        merged = {**current, **(value if isinstance(value, dict) else {})}
        normalized = normalize_tx_selection(merged)
        with self.lock:
            self.config["tx_selection"] = normalized
            self.save()
        return copy.deepcopy(normalized)

    def update_process(self, name: str, command: str, cwd: str = "") -> dict[str, Any]:
        if name not in PROCESS_NAMES:
            raise ValueError("unknown process")
        with self.lock:
            entry = self.config["processes"].setdefault(name, {})
            entry["command"] = str(command).strip()
            entry["cwd"] = str(cwd).strip()
            entry.setdefault("auto_start", False)
            self.save()
            return copy.deepcopy(entry)

    def update_builder(self, builder: dict[str, Any]) -> dict[str, Any]:
        cleaned = {
            "aircraft": str(builder.get("aircraft", "AIRCRAFT")).strip() or "AIRCRAFT",
            "baudrate": int(builder.get("baudrate", 57600)),
            "extra_args": str(builder.get("extra_args", "")).strip(),
            "masters": [],
            "outs": [],
        }
        for raw in builder.get("masters", []):
            kind = str(raw.get("type", "udp")).lower()
            if kind not in {"udp", "serial"}:
                kind = "udp"
            item = {
                "enabled": bool(raw.get("enabled", True)),
                "type": kind,
                "label": str(raw.get("label", "LINK")).strip() or "LINK",
                "host": str(raw.get("host", "0.0.0.0")).strip() or "0.0.0.0",
                "port": int(raw.get("port", 0) or 0),
                "path": str(raw.get("path", "")).strip(),
                "baud": int(raw.get("baud", cleaned["baudrate"]) or cleaned["baudrate"]),
            }
            cleaned["masters"].append(item)
        for raw in builder.get("outs", []):
            cleaned["outs"].append(
                {
                    "enabled": bool(raw.get("enabled", True)),
                    "host": str(raw.get("host", "127.0.0.1")).strip() or "127.0.0.1",
                    "port": int(raw.get("port", 14550) or 14550),
                }
            )

        with self.lock:
            self.config["mavproxy_builder"] = cleaned
            self._sync_links_from_builder(cleaned)
            self.save()
        return copy.deepcopy(cleaned)

    def _sync_links_from_builder(self, builder: dict[str, Any]) -> None:
        by_key: dict[str, dict[str, Any]] = {}
        for item in self.config.get("links", []):
            for key in (item.get("id"), item.get("mavproxy_label"), item.get("display_name")):
                if key:
                    by_key[str(key).upper()] = item
            for alias in item.get("aliases", []):
                by_key[str(alias).upper()] = item

        for master in builder.get("masters", []):
            label = str(master.get("label", "")).strip()
            target = by_key.get(label.upper())
            if target is None:
                continue
            target["mavproxy_label"] = label
            target["descriptor"] = master_descriptor(master)
            if master.get("type") == "serial":
                target["baud"] = int(master.get("baud", builder.get("baudrate", 57600)))
            else:
                target["baud"] = None


def master_descriptor(master: dict[str, Any]) -> str:
    label_json = json.dumps({"label": str(master.get("label", "LINK"))}, ensure_ascii=False, separators=(",", ":"))
    if str(master.get("type", "udp")).lower() == "serial":
        return "%s:%s" % (str(master.get("path", "")).strip(), label_json)
    return "udp:%s:%d:%s" % (
        str(master.get("host", "0.0.0.0")).strip() or "0.0.0.0",
        int(master.get("port", 0) or 0),
        label_json,
    )


def build_mavproxy_command(builder: dict[str, Any]) -> str:
    parts = ["mavproxy.py"]
    serial_present = False
    for master in builder.get("masters", []):
        if not master.get("enabled", True):
            continue
        descriptor = master_descriptor(master)
        if not descriptor or "REPLACE_WITH" in descriptor:
            continue
        parts.append("--master=%s" % shlex.quote(descriptor))
        serial_present = serial_present or str(master.get("type", "udp")).lower() == "serial"
    if serial_present:
        parts.append("--baudrate=%d" % int(builder.get("baudrate", 57600)))
    for output in builder.get("outs", []):
        if not output.get("enabled", True):
            continue
        parts.append("--out=%s" % shlex.quote("udp:%s:%d" % (output["host"], int(output["port"]))))
    aircraft = str(builder.get("aircraft", "")).strip()
    if aircraft:
        parts.append("--aircraft=%s" % shlex.quote(aircraft))
    extra = str(builder.get("extra_args", "")).strip()
    if extra:
        parts.append(extra)
    return " \\\n  ".join(parts)


class StateStore:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        config = config_manager.snapshot()
        self.lock = threading.RLock()
        self.links: dict[str, dict[str, Any]] = {}
        self.network_results: dict[str, dict[str, Any]] = {}
        self.active_tx: str | None = None
        self.active_tx_source = "unknown"
        self.rx_used: list[str] = []
        self.last_stats_at = 0.0
        self.last_control_at = 0.0
        self.control_error: str | None = None
        self.last_action: dict[str, Any] | None = None
        self.network_scan_running = False
        self.network_last_scan_at = 0.0
        self.alias_map = self._build_alias_map(config)
        for item in config.get("links", []):
            link_id = str(item["id"])
            self.links[link_id] = {
                "id": link_id,
                "display_name": item.get("display_name", link_id),
                "mavproxy_label": item.get("mavproxy_label", link_id),
                "metrics": {},
                "control": {},
                "received_at": 0.0,
            }

    @staticmethod
    def _build_alias_map(config: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in config.get("links", []):
            link_id = str(item["id"])
            values = {link_id, str(item.get("display_name", "")), str(item.get("mavproxy_label", ""))}
            values.update(str(x) for x in item.get("aliases", []))
            for value in values:
                if value:
                    result[value.upper()] = link_id
        return result

    def reload_aliases(self) -> None:
        with self.lock:
            self.alias_map = self._build_alias_map(self.config_manager.snapshot())

    def canonical_link(self, value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        upper = text.upper()
        with self.lock:
            if upper in self.alias_map:
                return self.alias_map[upper]
            for alias, link_id in self.alias_map.items():
                if alias and alias in upper:
                    return link_id
        return None

    def merge_stats(self, payload: dict[str, Any]) -> None:
        now = time.time()
        with self.lock:
            self.last_stats_at = now
            active = self.canonical_link(payload.get("active_tx"))
            if active:
                self.active_tx = active
            if payload.get("active_tx_source"):
                self.active_tx_source = str(payload["active_tx_source"])
            if isinstance(payload.get("rx_used"), list):
                self.rx_used = [x for value in payload["rx_used"] if (x := self.canonical_link(value))]
            rows = payload.get("links", []) if isinstance(payload.get("links"), list) else [payload]
            for row in rows:
                link_id = self.canonical_link(row.get("id") or row.get("name") or row.get("label"))
                if link_id is None or link_id not in self.links:
                    continue
                cleaned = dict(row)
                cleaned.pop("_t", None)
                self.links[link_id]["metrics"].update(cleaned)
                self.links[link_id]["received_at"] = now

    def merge_control(self, payload: dict[str, Any]) -> None:
        now = time.time()
        with self.lock:
            self.last_control_at = now
            self.control_error = None if payload.get("ok", False) else str(payload.get("error", "control error"))
            active = self.canonical_link(payload.get("active_tx"))
            if active:
                self.active_tx = active
            if payload.get("active_tx_source"):
                self.active_tx_source = str(payload["active_tx_source"])
            for row in payload.get("links", []) if isinstance(payload.get("links"), list) else []:
                link_id = self.canonical_link(row.get("id") or row.get("name") or row.get("label"))
                if link_id and link_id in self.links:
                    self.links[link_id]["control"].update(dict(row))
            if payload.get("message") or payload.get("error"):
                self.last_action = {
                    "ok": bool(payload.get("ok")),
                    "message": payload.get("message") or payload.get("error"),
                    "time": now,
                }

    def set_control_error(self, message: str) -> None:
        with self.lock:
            self.control_error = message

    def set_network_scan_running(self, running: bool) -> None:
        with self.lock:
            self.network_scan_running = running
            if not running:
                self.network_last_scan_at = time.time()

    def update_network_result(self, device_id: str, result: dict[str, Any]) -> None:
        with self.lock:
            self.network_results[device_id] = result

    def reset_network_results(self) -> None:
        valid_ids = {x["id"] for x in self.config_manager.snapshot()["network_checks"]["devices"]}
        with self.lock:
            self.network_results = {key: value for key, value in self.network_results.items() if key in valid_ids}

    def _signal_score(self, metrics: dict[str, Any], config: dict[str, Any]) -> float | None:
        for key in ("lq_pct", "rssi_pct"):
            if metrics.get(key) is not None:
                try:
                    return clamp(float(metrics[key]))
                except Exception:
                    pass
        values = []
        for key in ("rssi_dbm", "remote_rssi_dbm"):
            if metrics.get(key) is not None:
                try:
                    values.append(float(metrics[key]))
                except Exception:
                    pass
        if not values:
            return None
        limits = config["quality"]["limits"]
        good = float(limits.get("rssi_good_dbm", -55.0))
        bad = float(limits.get("rssi_bad_dbm", -100.0))
        if good <= bad:
            return None
        return clamp(100.0 * (min(values) - bad) / (good - bad))

    def _quality(self, enabled: bool, up: bool, metrics: dict[str, Any], age: float, config: dict[str, Any]) -> tuple[float, dict[str, float]]:
        if not enabled or not up:
            return 0.0, {}
        weights = config.get("quality", {}).get("weights", {})
        limits = config.get("quality", {}).get("limits", {})
        components: dict[str, float] = {}
        try:
            loss = float(metrics.get("loss_pct", 0.0))
            components["loss"] = clamp(100.0 * (1.0 - loss / max(0.1, float(limits.get("loss_bad_pct", 25.0)))))
        except Exception:
            pass
        try:
            stale = max(age, float(metrics.get("stale_s", 0.0) or 0.0))
            components["freshness"] = clamp(100.0 * (1.0 - stale / max(0.1, float(limits.get("stale_bad_s", 3.0)))))
        except Exception:
            pass
        if metrics.get("delay_ms") is not None:
            try:
                delay = max(0.0, float(metrics["delay_ms"]))
                components["delay"] = clamp(100.0 * (1.0 - delay / max(1.0, float(limits.get("delay_bad_ms", 1000.0)))))
            except Exception:
                pass
        if metrics.get("rate_pct") is not None:
            try:
                components["rate"] = clamp(float(metrics["rate_pct"]))
            except Exception:
                pass
        signal_score = self._signal_score(metrics, config)
        if signal_score is not None:
            components["signal"] = signal_score
        numerator = denominator = 0.0
        for key, value in components.items():
            weight = max(0.0, float(weights.get(key, 0.0)))
            numerator += weight * value
            denominator += weight
        return (round(clamp(numerator / denominator), 1), components) if denominator else (100.0, components)

    @staticmethod
    def _signal_text(metrics: dict[str, Any]) -> str:
        if metrics.get("lq_pct") is not None:
            return "LQ %.0f%%" % float(metrics["lq_pct"])
        if metrics.get("rssi_pct") is not None:
            return "RSSI %.0f%%" % float(metrics["rssi_pct"])
        if metrics.get("rssi_dbm") is not None or metrics.get("remote_rssi_dbm") is not None:
            return "L:%s R:%s dBm" % (metrics.get("rssi_dbm", "-"), metrics.get("remote_rssi_dbm", "-"))
        if metrics.get("snr_db") is not None:
            return "SNR %s dB" % metrics["snr_db"]
        return "-"

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        config = self.config_manager.snapshot()
        with self.lock:
            links_copy = copy.deepcopy(self.links)
            network_results = copy.deepcopy(self.network_results)
            active_tx = self.active_tx
            active_source = self.active_tx_source
            rx_used = list(self.rx_used)
            control_error = self.control_error
            last_action = copy.deepcopy(self.last_action)
            last_stats_at = self.last_stats_at
            last_control_at = self.last_control_at
            scan_running = self.network_scan_running
            scan_at = self.network_last_scan_at

        stale_after = float(config.get("stats_udp", {}).get("stale_after_s", 4.0))
        colors = config.get("quality", {}).get("colors", {})
        green_min = float(colors.get("green_min", 75.0))
        yellow_min = float(colors.get("yellow_min", 45.0))
        rows = []
        for item in config.get("links", []):
            link_id = str(item["id"])
            record = links_copy.get(link_id, {"metrics": {}, "control": {}, "received_at": 0.0})
            metrics = record.get("metrics", {})
            control = record.get("control", {})
            received_at = float(record.get("received_at", 0.0) or 0.0)
            age = now - received_at if received_at else float("inf")
            enabled = bool(control["enabled"]) if "enabled" in control else bool(metrics)
            if not enabled:
                state, up = "OFF", False
            else:
                up = bool(metrics.get("up", False)) and age <= stale_after
                state = "UP" if up else ("DELAYED" if control.get("state") == "DELAYED" or metrics.get("delayed") else "DOWN")
            quality, components = self._quality(enabled, up, metrics, age, config)
            if not enabled:
                color_name, color_hex = "off", colors.get("off", "#64748b")
            elif not up:
                color_name, color_hex = "red", colors.get("red", "#dc2626")
            elif quality >= green_min:
                color_name, color_hex = "green", colors.get("green", "#16a34a")
            elif quality >= yellow_min:
                color_name, color_hex = "yellow", colors.get("yellow", "#f59e0b")
            else:
                color_name, color_hex = "red", colors.get("red", "#dc2626")
            rows.append(
                {
                    "id": link_id,
                    "name": item.get("display_name", link_id),
                    "label": item.get("mavproxy_label", link_id),
                    "enabled": enabled,
                    "up": up,
                    "state": state,
                    "quality_pct": quality,
                    "quality_components": components,
                    "color": color_name,
                    "color_hex": color_hex,
                    "active_tx": link_id == active_tx,
                    "rx_used": link_id in rx_used,
                    "loss_pct": metrics.get("loss_pct"),
                    "pkt_rate": metrics.get("pkt_rate"),
                    "rate_pct": metrics.get("rate_pct"),
                    "delay_ms": metrics.get("delay_ms"),
                    "stale_s": metrics.get("stale_s"),
                    "signal": self._signal_text(metrics),
                    "address": metrics.get("address") or control.get("address"),
                    "data_age_s": None if age == float("inf") else round(age, 2),
                }
            )

        devices = []
        for device in config["network_checks"]["devices"]:
            result = network_results.get(device["id"], {})
            checked_at = float(result.get("checked_at", 0.0) or 0.0)
            devices.append(
                {
                    **copy.deepcopy(device),
                    "online": result.get("online"),
                    "latency_ms": result.get("latency_ms"),
                    "error": result.get("error"),
                    "checked_at": checked_at,
                    "age_s": None if not checked_at else round(now - checked_at, 1),
                }
            )

        return {
            "ok": True,
            "timestamp": now,
            "active_tx": active_tx,
            "active_tx_name": next((x["name"] for x in rows if x["id"] == active_tx), None),
            "active_tx_source": active_source,
            "rx_used": rx_used,
            "links": rows,
            "devices": devices,
            "network_scan_running": scan_running,
            "network_last_scan_age_s": None if not scan_at else round(now - scan_at, 1),
            "control_error": control_error,
            "last_action": last_action,
            "stats_age_s": None if not last_stats_at else round(now - last_stats_at, 2),
            "control_age_s": None if not last_control_at else round(now - last_control_at, 2),
        }


class GCSLinkForwarder(threading.Thread):
    def __init__(self, store: StateStore, config_manager: ConfigManager, stop_event: threading.Event):
        super().__init__(name="gcs-link-forwarder", daemon=True)
        self.store = store
        self.config_manager = config_manager
        self.stop_event = stop_event
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lock = threading.RLock()
        self.last_sent_at = 0.0
        self.last_error = ""
        self.last_target = ""
        self.last_bytes = 0

    def status(self) -> dict[str, Any]:
        cfg = normalize_gcs_forward(
            self.config_manager.snapshot().get("gcs_forward_udp", {})
        )
        with self.lock:
            return {
                "ok": not bool(self.last_error),
                "enabled": cfg["enabled"],
                "host": cfg["host"],
                "port": cfg["port"],
                "period_s": cfg["period_s"],
                "target": "%s:%d" % (cfg["host"], cfg["port"]),
                "last_sent_at": self.last_sent_at or None,
                "last_sent_age_s": (
                    None
                    if not self.last_sent_at
                    else round(max(0.0, time.time() - self.last_sent_at), 2)
                ),
                "last_error": self.last_error or None,
                "last_target": self.last_target or None,
                "last_bytes": self.last_bytes,
            }

    def send_once(self, force: bool = False) -> dict[str, Any]:
        cfg = normalize_gcs_forward(
            self.config_manager.snapshot().get("gcs_forward_udp", {})
        )
        if not cfg["enabled"] and not force:
            return {
                "ok": False,
                "error": "GCS JSON forwarding is disabled",
                "target": "%s:%d" % (cfg["host"], cfg["port"]),
            }

        target = (cfg["host"], cfg["port"])
        try:
            payload = build_gcs_forward_payload(self.store.snapshot())
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            sent = self.sock.sendto(raw, target)
            now = time.time()
            with self.lock:
                self.last_sent_at = now
                self.last_error = ""
                self.last_target = "%s:%d" % target
                self.last_bytes = int(sent)
            return {
                "ok": True,
                "message": "JSON sent",
                "target": "%s:%d" % target,
                "bytes": int(sent),
                "payload": payload,
            }
        except Exception as exc:
            message = str(exc)
            with self.lock:
                self.last_error = message
                self.last_target = "%s:%d" % target
            print("GCS JSON forwarding error: %s" % message)
            return {
                "ok": False,
                "error": message,
                "target": "%s:%d" % target,
            }

    def run(self) -> None:
        while not self.stop_event.is_set():
            cfg = normalize_gcs_forward(
                self.config_manager.snapshot().get("gcs_forward_udp", {})
            )
            if cfg["enabled"]:
                self.send_once()
            self.stop_event.wait(cfg["period_s"])


class StatsListener(threading.Thread):
    def __init__(self, store: StateStore, config_manager: ConfigManager, stop_event: threading.Event):
        super().__init__(name="stats-listener", daemon=True)
        self.store = store
        self.config_manager = config_manager
        self.stop_event = stop_event
        self.sock: socket.socket | None = None

    def run(self) -> None:
        cfg = self.config_manager.snapshot().get("stats_udp", {})
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((str(cfg.get("host", "0.0.0.0")), int(cfg.get("port", 14660))))
        self.sock.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                raw, _ = self.sock.recvfrom(65535)
                payload = json.loads(raw.decode("utf-8"))
                if isinstance(payload, dict):
                    self.store.merge_stats(payload)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as exc:
                self.store.set_control_error("stats UDP: %s" % exc)


class LocalControlClient:
    def __init__(self, config_manager: ConfigManager):
        cfg = config_manager.snapshot().get("control_udp", {})
        self.host = "127.0.0.1"
        self.port = int(cfg.get("port", 16060))
        self.timeout = float(cfg.get("timeout_s", 1.0))

    def request(self, action: str, link: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action}
        if link:
            payload["link"] = link
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(json.dumps(payload).encode("utf-8"), (self.host, self.port))
            raw, _ = sock.recvfrom(65535)
            result = json.loads(raw.decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("invalid linkcontrol response")
            return result
        finally:
            sock.close()


class ControlPoller(threading.Thread):
    def __init__(self, store: StateStore, client: LocalControlClient, stop_event: threading.Event):
        super().__init__(name="control-poller", daemon=True)
        self.store = store
        self.client = client
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.store.merge_control(self.client.request("status"))
            except Exception as exc:
                self.store.set_control_error("MAVProxy linkcontrol is unreachable: %s" % exc)
            self.stop_event.wait(1.0)


class AutoTxSelector(threading.Thread):
    """Selects the best UP link from dashboard quality scores in AUTO mode."""

    def __init__(
        self,
        store: StateStore,
        config_manager: ConfigManager,
        client: LocalControlClient,
        stop_event: threading.Event,
    ):
        super().__init__(name="auto-tx-selector", daemon=True)
        self.store = store
        self.config_manager = config_manager
        self.client = client
        self.stop_event = stop_event
        self.lock = threading.RLock()
        self.wake_event = threading.Event()
        self.candidate_id: str | None = None
        self.candidate_since = 0.0
        self.last_switch_at = 0.0
        self.last_switch_from: str | None = None
        self.last_switch_to: str | None = None
        self.last_reason = ""
        self.last_error = ""

    def reset(self) -> None:
        with self.lock:
            self.candidate_id = None
            self.candidate_since = 0.0
            self.last_error = ""
        self.wake_event.set()

    def status(self) -> dict[str, Any]:
        cfg = normalize_tx_selection(
            self.config_manager.snapshot().get("tx_selection", {})
        )
        with self.lock:
            return {
                **cfg,
                "candidate": self.candidate_id,
                "candidate_age_s": (
                    None
                    if not self.candidate_since
                    else round(max(0.0, time.time() - self.candidate_since), 2)
                ),
                "last_switch_at": self.last_switch_at or None,
                "last_switch_age_s": (
                    None
                    if not self.last_switch_at
                    else round(max(0.0, time.time() - self.last_switch_at), 2)
                ),
                "last_switch_from": self.last_switch_from,
                "last_switch_to": self.last_switch_to,
                "last_reason": self.last_reason or None,
                "last_error": self.last_error or None,
            }

    def _clear_candidate(self) -> None:
        with self.lock:
            self.candidate_id = None
            self.candidate_since = 0.0

    def _select(self, link_id: str, current_id: str | None, reason: str) -> bool:
        try:
            result = self.client.request("select", link_id)
            self.store.merge_control(result)
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or result.get("message") or "TX selection failed")
            with self.lock:
                self.last_switch_at = time.time()
                self.last_switch_from = current_id
                self.last_switch_to = link_id
                self.last_reason = reason
                self.last_error = ""
                self.candidate_id = None
                self.candidate_since = 0.0
            print("[auto-tx] %s -> %s (%s)" % (current_id or "NONE", link_id, reason))
            return True
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
            return False

    def evaluate(self) -> None:
        cfg = normalize_tx_selection(
            self.config_manager.snapshot().get("tx_selection", {})
        )
        if cfg["mode"] != "auto":
            self._clear_candidate()
            return

        state = self.store.snapshot()
        usable = [
            link for link in state.get("links", [])
            if bool(link.get("enabled")) and bool(link.get("up"))
        ]
        if not usable:
            self._clear_candidate()
            return

        current_id = state.get("active_tx")
        current = next((link for link in usable if link.get("id") == current_id), None)

        # Keep current link on exact ties; otherwise deterministic config order wins.
        best = max(
            usable,
            key=lambda link: (
                float(link.get("quality_pct", 0.0) or 0.0),
                1 if link.get("id") == current_id else 0,
            ),
        )

        if current is None:
            self._select(
                str(best["id"]),
                str(current_id) if current_id else None,
                "active TX is unavailable; best UP link %.1f%%" %
                float(best.get("quality_pct", 0.0) or 0.0),
            )
            return

        if best.get("id") == current.get("id"):
            self._clear_candidate()
            return

        current_quality = float(current.get("quality_pct", 0.0) or 0.0)
        best_quality = float(best.get("quality_pct", 0.0) or 0.0)
        improvement = best_quality - current_quality
        if improvement < float(cfg["switch_margin_pct"]):
            self._clear_candidate()
            return

        now = time.time()
        with self.lock:
            if self.candidate_id != str(best["id"]):
                self.candidate_id = str(best["id"])
                self.candidate_since = now
                return
            candidate_age = now - self.candidate_since
            cooldown_age = now - self.last_switch_at if self.last_switch_at else float("inf")

        if candidate_age < float(cfg["hold_s"]):
            return
        if cooldown_age < float(cfg["cooldown_s"]):
            return

        self._select(
            str(best["id"]),
            str(current["id"]),
            "%.1f%% > %.1f%%, margin %.1f points" %
            (best_quality, current_quality, improvement),
        )

    def run(self) -> None:
        while not self.stop_event.is_set():
            cfg = normalize_tx_selection(
                self.config_manager.snapshot().get("tx_selection", {})
            )
            try:
                self.evaluate()
            except Exception as exc:
                with self.lock:
                    self.last_error = str(exc)
            self.wake_event.wait(float(cfg["check_period_s"]))
            self.wake_event.clear()


class NetworkScanner:
    TIME_PATTERN = re.compile(r"time[=<]\s*([0-9.]+)\s*ms", re.IGNORECASE)

    def __init__(self, store: StateStore, config_manager: ConfigManager):
        self.store = store
        self.config_manager = config_manager
        self.lock = threading.Lock()

    def start(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        self.store.set_network_scan_running(True)
        threading.Thread(target=self._run, name="manual-network-scan", daemon=True).start()
        return True

    def _ping(self, host: str, timeout_s: float) -> tuple[bool, float | None, str | None]:
        started = time.perf_counter()
        if platform.system().lower().startswith("win"):
            command = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), host]
        else:
            command = ["ping", "-c", "1", "-W", str(max(1, int(round(timeout_s)))), host]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s + 1.0, check=False)
            online = result.returncode == 0
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            match = self.TIME_PATTERN.search(output)
            latency = float(match.group(1)) if match else ((time.perf_counter() - started) * 1000 if online else None)
            return online, round(latency, 1) if latency is not None else None, None if online else "no ping response"
        except Exception as exc:
            return False, None, str(exc)


    def _check_device(self, device: dict[str, Any], timeout_s: float) -> tuple[str, dict[str, Any]]:
        # The network check only sends ICMP ping to the device IP address.
        # No TCP/UDP port check is performed.
        online, latency, error = self._ping(device["host"], timeout_s)
        return device["id"], {
            "online": online,
            "latency_ms": latency,
            "error": error,
            "checked_at": time.time(),
        }


    def _run(self) -> None:
        try:
            config = self.config_manager.snapshot()
            timeout_s = float(config["network_checks"].get("timeout_s", 1.0))
            devices = [item for item in config["network_checks"]["devices"] if item.get("enabled", True) and item.get("host")]
            if devices:
                with ThreadPoolExecutor(max_workers=min(16, len(devices))) as pool:
                    futures = [pool.submit(self._check_device, item, timeout_s) for item in devices]
                    for future in as_completed(futures):
                        device_id, result = future.result()
                        self.store.update_network_result(device_id, result)
        finally:
            self.store.set_network_scan_running(False)
            self.lock.release()


class ManagedProcess:
    def __init__(self, name: str, display_name: str, config_manager: ConfigManager):
        self.name = name
        self.display_name = display_name
        self.config_manager = config_manager
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.output: deque[str] = deque(maxlen=3000)
        self.started_at = 0.0
        self.exit_code: int | None = None
        self.last_error: str | None = None

    def _append(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.output.append("[%s] %s" % (stamp, text.rstrip("\n")))

    def start(self, command: str | None = None, cwd: str | None = None) -> dict[str, Any]:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return {"ok": False, "error": "%s is already running" % self.display_name}
            cfg = self.config_manager.snapshot()["processes"][self.name]
            command = str(command if command is not None else cfg.get("command", "")).strip()
            cwd = str(cwd if cwd is not None else cfg.get("cwd", "")).strip()
            if not command:
                return {"ok": False, "error": "command is empty"}
            if cwd and not Path(cwd).expanduser().is_dir():
                return {"ok": False, "error": "working directory not found: %s" % cwd}
            env = os.environ.copy()
            env["LINK_DASHBOARD_CONFIG"] = str(self.config_manager.path)
            self.output.clear()
            self.exit_code = None
            self.last_error = None
            self._append("STARTING: %s" % command)
            try:
                self.process = subprocess.Popen(
                    ["/bin/bash", "-lc", "exec " + command],
                    cwd=str(Path(cwd).expanduser()) if cwd else None,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                self.started_at = time.time()
                threading.Thread(target=self._reader, name="%s-output" % self.name, daemon=True).start()
                return {"ok": True, "message": "%s started" % self.display_name, "pid": self.process.pid}
            except Exception as exc:
                self.process = None
                self.last_error = str(exc)
                self._append("START ERROR: %s" % exc)
                return {"ok": False, "error": str(exc)}

    def _reader(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in iter(process.stdout.readline, ""):
                if not line and process.poll() is not None:
                    break
                if line:
                    self._append(line)
        except Exception as exc:
            self._append("OUTPUT READ ERROR: %s" % exc)
        finally:
            code = process.wait()
            with self.lock:
                self.exit_code = code
                self._append("PROCESS ENDED, exit code=%s" % code)

    def stop(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                return {"ok": True, "message": "%s is not running" % self.display_name}
            pid = process.pid
        try:
            os.killpg(pid, signal.SIGINT)
            try:
                process.wait(timeout=4.0)
            except subprocess.TimeoutExpired:
                os.killpg(pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    os.killpg(pid, signal.SIGKILL)
            return {"ok": True, "message": "%s stopped" % self.display_name}
        except Exception as exc:
            self._append("STOP ERROR: %s" % exc)
            return {"ok": False, "error": str(exc)}

    def send_input(self, text: str) -> dict[str, Any]:
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                return {"ok": False, "error": "%s is not running" % self.display_name}
            try:
                process.stdin.write(text.rstrip("\n") + "\n")
                process.stdin.flush()
                self._append(">>> %s" % text)
                return {"ok": True}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def snapshot(self, tail: int = 400) -> dict[str, Any]:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            cfg = self.config_manager.snapshot()["processes"][self.name]
            lines = list(self.output)[-max(1, min(2000, int(tail))):]
            return {
                "name": self.name,
                "display_name": self.display_name,
                "running": running,
                "pid": self.process.pid if running and self.process else None,
                "exit_code": self.exit_code,
                "started_at": self.started_at,
                "uptime_s": round(time.time() - self.started_at, 1) if running else None,
                "command": cfg.get("command", ""),
                "cwd": cfg.get("cwd", ""),
                "last_error": self.last_error,
                "output": "\n".join(lines),
            }


class ProcessManager:
    def __init__(self, config_manager: ConfigManager):
        self.processes = {
            "mavproxy": ManagedProcess("mavproxy", "MAVProxy", config_manager),
            "mavlink_router": ManagedProcess("mavlink_router", "mavlink-router", config_manager),
        }

    def get(self, name: str) -> ManagedProcess:
        if name not in self.processes:
            raise ValueError("unknown process")
        return self.processes[name]

    def snapshot(self, tail: int = 400) -> dict[str, Any]:
        return {name: process.snapshot(tail) for name, process in self.processes.items()}

    def stop_all(self) -> None:
        for process in self.processes.values():
            process.stop()


class WebServerThread(threading.Thread):
    def __init__(self, flask_app: Flask, host: str, port: int):
        super().__init__(name="web-server", daemon=True)
        self.server = make_server(host, port, flask_app, threaded=True)

    def run(self) -> None:
        self.server.serve_forever()

    def stop(self) -> None:
        self.server.shutdown()


class DashboardService:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.store = StateStore(config_manager)
        self.stop_event = threading.Event()
        self.control_client = LocalControlClient(config_manager)
        self.stats_listener = StatsListener(self.store, config_manager, self.stop_event)
        self.control_poller = ControlPoller(self.store, self.control_client, self.stop_event)
        self.auto_tx_selector = AutoTxSelector(
            self.store,
            config_manager,
            self.control_client,
            self.stop_event,
        )
        self.gcs_forwarder = GCSLinkForwarder(self.store, config_manager, self.stop_event)
        self.network_scanner = NetworkScanner(self.store, config_manager)
        self.process_manager = ProcessManager(config_manager)
        self.web_server: WebServerThread | None = None

    def start_workers(self) -> None:
        self.stats_listener.start()
        self.control_poller.start()
        self.auto_tx_selector.start()
        self.gcs_forwarder.start()
        config = self.config_manager.snapshot()
        for name in PROCESS_NAMES:
            if bool(config["processes"][name].get("auto_start", False)):
                self.process_manager.get(name).start()

    def start_web(self, app: Flask) -> None:
        cfg = self.config_manager.snapshot().get("web", {})
        self.web_server = WebServerThread(app, str(cfg.get("host", "0.0.0.0")), int(cfg.get("port", 8080)))
        self.web_server.start()

    def control(self, action: str, link: str) -> dict[str, Any]:
        if action == "select":
            mode = normalize_tx_selection(
                self.config_manager.snapshot().get("tx_selection", {})
            )["mode"]
            if mode != "manual":
                return {
                    "ok": False,
                    "error": "To select the command TX link manually, switch to Manual Selection Mode first",
                }
        try:
            result = self.control_client.request(action, link)
            self.store.merge_control(result)
            return result
        except Exception as exc:
            message = "linkcontrol command failed: %s" % exc
            self.store.set_control_error(message)
            return {"ok": False, "error": message}

    def update_devices(self, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = self.config_manager.update_network_devices(devices)
        self.store.reset_network_results()
        return result

    def update_tx_selection(self, value: dict[str, Any]) -> dict[str, Any]:
        result = self.config_manager.update_tx_selection(value)
        self.auto_tx_selector.reset()
        return result

    def tx_selection_status(self) -> dict[str, Any]:
        return self.auto_tx_selector.status()

    def update_gcs_forward(self, value: dict[str, Any]) -> dict[str, Any]:
        return self.config_manager.update_gcs_forward(value)

    def test_gcs_forward(self) -> dict[str, Any]:
        return self.gcs_forwarder.send_once(force=True)

    def gcs_forward_status(self) -> dict[str, Any]:
        return self.gcs_forwarder.status()

    def update_builder(self, builder: dict[str, Any]) -> dict[str, Any]:
        result = self.config_manager.update_builder(builder)
        self.store.reload_aliases()
        return result

    def stop(self) -> None:
        self.stop_event.set()
        self.process_manager.stop_all()
        if self.web_server:
            self.web_server.stop()


def create_web_app(service: DashboardService) -> Flask:
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(BASE_DIR / "static"))

    @app.get("/")
    def index():
        title = service.config_manager.snapshot().get("app", {}).get("title", "Communications Dashboard")
        return render_template("index.html", title=title)

    @app.get("/api/state")
    def api_state():
        state = service.store.snapshot()
        state["tx_selection"] = service.tx_selection_status()
        state["processes"] = {name: {k: v for k, v in row.items() if k != "output"} for name, row in service.process_manager.snapshot(1).items()}
        return jsonify(state)

    @app.get("/api/config")
    def api_config():
        return jsonify(service.config_manager.snapshot())

    @app.get("/api/gcs-forward")
    def api_gcs_forward_get():
        return jsonify(
            {
                "ok": True,
                "config": service.config_manager.snapshot().get(
                    "gcs_forward_udp", {}
                ),
                "status": service.gcs_forward_status(),
            }
        )

    @app.post("/api/gcs-forward")
    def api_gcs_forward_save():
        payload = request.get_json(silent=True) or {}
        try:
            saved = service.update_gcs_forward(payload)
            return jsonify(
                {
                    "ok": True,
                    "message": "GCS JSON target saved and applied immediately",
                    "config": saved,
                    "status": service.gcs_forward_status(),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/gcs-forward/test")
    def api_gcs_forward_test():
        result = service.test_gcs_forward()
        return jsonify(result), (200 if result.get("ok") else 400)

    @app.get("/api/tx-selection")
    def api_tx_selection_get():
        return jsonify(
            {
                "ok": True,
                "config": service.config_manager.snapshot().get("tx_selection", {}),
                "status": service.tx_selection_status(),
            }
        )

    @app.post("/api/tx-selection")
    def api_tx_selection_save():
        payload = request.get_json(silent=True) or {}
        try:
            saved = service.update_tx_selection(payload)
            return jsonify(
                {
                    "ok": True,
                    "message": (
                        "Automatic switching mode enabled"
                        if saved["mode"] == "auto"
                        else "Manual selection mode enabled"
                    ),
                    "config": saved,
                    "status": service.tx_selection_status(),
                }
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/link/<path:link_id>/<action>")
    def api_link(link_id: str, action: str):
        canonical = service.store.canonical_link(link_id)
        if canonical is None:
            return jsonify({"ok": False, "error": "unknown link"}), 404
        if action not in {"on", "off", "toggle", "select"}:
            return jsonify({"ok": False, "error": "unknown action"}), 400
        result = service.control(action, canonical)
        return jsonify(result), (200 if result.get("ok") else 409)

    @app.post("/api/network/scan")
    def api_network_scan():
        started = service.network_scanner.start()
        return jsonify({"ok": started, "message": "check started" if started else "check is already running"}), (200 if started else 409)

    @app.post("/api/network/devices")
    def api_network_devices():
        payload = request.get_json(silent=True) or {}
        devices = payload.get("devices")
        if not isinstance(devices, list):
            return jsonify({"ok": False, "error": "devices list is required"}), 400
        try:
            saved = service.update_devices(devices)
            return jsonify({"ok": True, "devices": saved})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/process/<name>")
    def api_process(name: str):
        try:
            return jsonify({"ok": True, "process": service.process_manager.get(name).snapshot(int(request.args.get("tail", 400)))})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post("/api/process/<name>/save")
    def api_process_save(name: str):
        payload = request.get_json(silent=True) or {}
        try:
            saved = service.config_manager.update_process(name, str(payload.get("command", "")), str(payload.get("cwd", "")))
            return jsonify({"ok": True, "process": saved})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/process/<name>/start")
    def api_process_start(name: str):
        payload = request.get_json(silent=True) or {}
        try:
            if "command" in payload:
                service.config_manager.update_process(name, str(payload.get("command", "")), str(payload.get("cwd", "")))
            result = service.process_manager.get(name).start()
            return jsonify(result), (200 if result.get("ok") else 409)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/process/<name>/stop")
    def api_process_stop(name: str):
        try:
            result = service.process_manager.get(name).stop()
            return jsonify(result), (200 if result.get("ok") else 409)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/process/<name>/input")
    def api_process_input(name: str):
        payload = request.get_json(silent=True) or {}
        try:
            result = service.process_manager.get(name).send_input(str(payload.get("text", "")))
            return jsonify(result), (200 if result.get("ok") else 409)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/mavproxy/builder")
    def api_builder_save():
        payload = request.get_json(silent=True) or {}
        try:
            saved = service.update_builder(payload)
            command = build_mavproxy_command(saved)
            if bool(payload.get("save_command", True)):
                service.config_manager.update_process("mavproxy", command, service.config_manager.snapshot()["processes"]["mavproxy"].get("cwd", ""))
            return jsonify({"ok": True, "builder": saved, "command": command})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    return app



class DeviceDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, device: dict[str, Any] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Network Device")
        device = device or {}

        layout = QFormLayout(self)

        self.name_edit = QLineEdit(str(device.get("name", "")))
        self.host_edit = QLineEdit(str(device.get("host", "")))
        self.enabled_box = QCheckBox("Include in ping check")
        self.enabled_box.setChecked(bool(device.get("enabled", True)))

        layout.addRow("Device name", self.name_edit)
        layout.addRow("IP / Host", self.host_edit)
        layout.addRow("", self.enabled_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self.device_id = str(device.get("id") or uuid.uuid4().hex[:10])

    def value(self) -> dict[str, Any]:
        return normalize_device(
            {
                "id": self.device_id,
                "name": self.name_edit.text(),
                "host": self.host_edit.text(),
                "enabled": self.enabled_box.isChecked(),
            }
        )


class MasterDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, value: dict[str, Any] | None = None):
        super().__init__(parent)
        self.setWindowTitle("MAVProxy Master")
        value = value or {}
        layout = QFormLayout(self)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(bool(value.get("enabled", True)))
        self.kind = QComboBox()
        self.kind.addItems(["udp", "serial"])
        self.kind.setCurrentText(str(value.get("type", "udp")))
        self.label = QLineEdit(str(value.get("label", "")))
        self.host = QLineEdit(str(value.get("host", "0.0.0.0")))
        self.port = QSpinBox(); self.port.setRange(0, 65535); self.port.setValue(int(value.get("port", 0) or 0))
        self.path = QLineEdit(str(value.get("path", "")))
        self.path.setPlaceholderText("/dev/serial/by-id/...")
        self.baud = QSpinBox(); self.baud.setRange(1200, 2000000); self.baud.setValue(int(value.get("baud", 57600) or 57600))
        layout.addRow("", self.enabled)
        layout.addRow("Type", self.kind)
        layout.addRow("Label", self.label)
        layout.addRow("UDP Bind IP", self.host)
        layout.addRow("UDP port", self.port)
        layout.addRow("Serial device path", self.path)
        layout.addRow("Baud", self.baud)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def value(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled.isChecked(), "type": self.kind.currentText(), "label": self.label.text().strip(),
            "host": self.host.text().strip(), "port": self.port.value(), "path": self.path.text().strip(), "baud": self.baud.value(),
        }


class OutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, value: dict[str, Any] | None = None):
        super().__init__(parent)
        self.setWindowTitle("MAVProxy OUT")
        value = value or {}
        layout = QFormLayout(self)
        self.enabled = QCheckBox("Enabled"); self.enabled.setChecked(bool(value.get("enabled", True)))
        self.host = QLineEdit(str(value.get("host", "127.0.0.1")))
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(int(value.get("port", 14550) or 14550))
        layout.addRow("", self.enabled); layout.addRow("Target IP", self.host); layout.addRow("Port", self.port)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addRow(buttons)

    def value(self) -> dict[str, Any]:
        return {"enabled": self.enabled.isChecked(), "host": self.host.text().strip(), "port": self.port.value()}


class MainWindow(QMainWindow):
    LINK_COLUMNS = ["Link", "ON/OFF", "UP/DOWN", "Quality", "Loss", "Latency", "Telemetry", "Signal", "Command TX", "Control"]

    def __init__(self, service: DashboardService, web_url: str):
        super().__init__()
        self.service = service
        self.web_url = web_url
        self.config = service.config_manager.snapshot()
        self.row_by_id: dict[str, int] = {}
        self.quality_bars: dict[str, QProgressBar] = {}
        self.action_buttons: dict[str, dict[str, QPushButton]] = {}
        self.master_rows = copy.deepcopy(self.config["mavproxy_builder"]["masters"])
        self.out_rows = copy.deepcopy(self.config["mavproxy_builder"]["outs"])
        self.setWindowTitle(self.config.get("app", {}).get("title", "Communications Dashboard"))
        self.resize(1500, 950)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_links_tab(), "Links")
        self.tabs.addTab(self._build_network_tab(), "Network Check")
        self.tabs.addTab(self._build_mavproxy_tab(), "MAVProxy")
        self.tabs.addTab(self._build_router_tab(), "mavlink-router")
        self.setCentralWidget(self.tabs)
        self.setStyleSheet(self._stylesheet())
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh_ui); self.timer.start(int(self.config.get("app", {}).get("refresh_ms", 750)))
        self.refresh_ui()

    @staticmethod
    def _stylesheet() -> str:
        return """
        QMainWindow, QWidget { background:#0f172a; color:#e2e8f0; font-size:13px; }
        QLabel#title { font-size:22px; font-weight:700; } QLabel#section { font-size:16px; font-weight:700; margin-top:8px; }
        QTableWidget, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox { background:#111827; color:#e2e8f0; border:1px solid #334155; }
        QTableWidget { gridline-color:#334155; alternate-background-color:#172033; } QHeaderView::section { background:#1e293b; padding:7px; }
        QPushButton { background:#334155; border:1px solid #475569; border-radius:5px; padding:7px 10px; } QPushButton:hover { background:#475569; }
        QPushButton:disabled { color:#64748b; } QProgressBar { border:1px solid #475569; border-radius:4px; text-align:center; background:#0f172a; }
        QTabBar::tab { background:#1e293b; padding:10px 18px; margin-right:2px; } QTabBar::tab:selected { background:#2563eb; }
        QGroupBox { border:1px solid #334155; border-radius:7px; margin-top:10px; padding-top:10px; font-weight:700; }
        """

    def _title_row(self, title_text: str) -> QHBoxLayout:
        row = QHBoxLayout(); title = QLabel(title_text); title.setObjectName("title"); row.addWidget(title); row.addStretch(1)
        open_web = QPushButton("Open Web Interface"); open_web.clicked.connect(lambda: webbrowser.open(self.web_url)); row.addWidget(open_web)
        return row

    def _build_links_tab(self) -> QWidget:
        root = QWidget(); layout = QVBoxLayout(root); layout.addLayout(self._title_row("Communication Links"))
        self.active_label = QLabel("Command TX: -"); self.status_label = QLabel("Starting..."); layout.addWidget(self.active_label); layout.addWidget(self.status_label)

        tx_cfg = normalize_tx_selection(self.config.get("tx_selection", {}))
        tx_box = QGroupBox("Command TX Selection Mode")
        tx_layout = QHBoxLayout(tx_box)
        self.manual_tx_mode_button = QPushButton("Manual Selection Mode")
        self.manual_tx_mode_button.setCheckable(True)
        self.manual_tx_mode_button.clicked.connect(lambda: self.set_tx_mode("manual"))
        self.auto_tx_mode_button = QPushButton("Automatic Switching Mode")
        self.auto_tx_mode_button.setCheckable(True)
        self.auto_tx_mode_button.clicked.connect(lambda: self.set_tx_mode("auto"))
        self.tx_mode_status_label = QLabel("")
        tx_layout.addWidget(self.manual_tx_mode_button)
        tx_layout.addWidget(self.auto_tx_mode_button)
        tx_layout.addWidget(self.tx_mode_status_label, 1)
        layout.addWidget(tx_box)
        self.manual_tx_mode_button.setChecked(tx_cfg["mode"] == "manual")
        self.auto_tx_mode_button.setChecked(tx_cfg["mode"] == "auto")

        gcs_cfg = normalize_gcs_forward(self.config.get("gcs_forward_udp", {}))
        gcs_box = QGroupBox("GCS Link JSON Forwarding")
        gcs_layout = QHBoxLayout(gcs_box)
        self.gcs_forward_enabled = QCheckBox("Forwarding enabled")
        self.gcs_forward_enabled.setChecked(bool(gcs_cfg["enabled"]))
        self.gcs_forward_host = QLineEdit(str(gcs_cfg["host"]))
        self.gcs_forward_host.setPlaceholderText("192.168.1.60")
        self.gcs_forward_port = QSpinBox()
        self.gcs_forward_port.setRange(1, 65535)
        self.gcs_forward_port.setValue(int(gcs_cfg["port"]))
        self.gcs_forward_period = QDoubleSpinBox()
        self.gcs_forward_period.setRange(0.2, 3600.0)
        self.gcs_forward_period.setDecimals(1)
        self.gcs_forward_period.setSingleStep(0.1)
        self.gcs_forward_period.setSuffix(" sn")
        self.gcs_forward_period.setValue(float(gcs_cfg["period_s"]))
        save_gcs = QPushButton("Save and Apply")
        save_gcs.clicked.connect(self.save_gcs_forward)
        test_gcs = QPushButton("Send Test JSON")
        test_gcs.clicked.connect(self.test_gcs_forward)
        self.gcs_forward_status_label = QLabel("Target: %s:%d" % (gcs_cfg["host"], gcs_cfg["port"]))
        gcs_layout.addWidget(self.gcs_forward_enabled)
        gcs_layout.addWidget(QLabel("Target IP"))
        gcs_layout.addWidget(self.gcs_forward_host, 2)
        gcs_layout.addWidget(QLabel("Port"))
        gcs_layout.addWidget(self.gcs_forward_port)
        gcs_layout.addWidget(QLabel("Period"))
        gcs_layout.addWidget(self.gcs_forward_period)
        gcs_layout.addWidget(save_gcs)
        gcs_layout.addWidget(test_gcs)
        gcs_layout.addWidget(self.gcs_forward_status_label, 2)
        layout.addWidget(gcs_box)

        self.link_table = QTableWidget(len(self.config.get("links", [])), len(self.LINK_COLUMNS)); self.link_table.setHorizontalHeaderLabels(self.LINK_COLUMNS)
        self.link_table.verticalHeader().setVisible(False); self.link_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); self.link_table.setAlternatingRowColors(True)
        self.link_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents); self.link_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for row, item in enumerate(self.config.get("links", [])):
            link_id = str(item["id"]); self.row_by_id[link_id] = row; self.link_table.setItem(row, 0, QTableWidgetItem(item.get("display_name", link_id)))
            for col in (1,2,4,5,6,7,8): self.link_table.setItem(row, col, QTableWidgetItem("-"))
            bar = QProgressBar(); bar.setRange(0,100); bar.setFormat("%p%"); self.link_table.setCellWidget(row,3,bar); self.quality_bars[link_id]=bar
            widget=QWidget(); actions=QHBoxLayout(widget); actions.setContentsMargins(0,0,0,0); buttons={}
            for action,text in (("on","ON"),("off","OFF"),("select","Select TX")):
                button=QPushButton(text); button.clicked.connect(lambda _=False,a=action,l=link_id:self.run_control(a,l)); actions.addWidget(button); buttons[action]=button
            self.action_buttons[link_id]=buttons; self.link_table.setCellWidget(row,9,widget)
        layout.addWidget(self.link_table)
        return root

    def _build_network_tab(self) -> QWidget:
        root=QWidget(); layout=QVBoxLayout(root); layout.addLayout(self._title_row("Network Devices and Ping Check"))
        controls=QHBoxLayout(); self.scan_button=QPushButton("Run Ping Check Now"); self.scan_button.clicked.connect(self.start_network_scan)
        add=QPushButton("Add IP / Device"); add.clicked.connect(self.add_device); edit=QPushButton("Edit Selected"); edit.clicked.connect(self.edit_device); delete=QPushButton("Delete Selected"); delete.clicked.connect(self.delete_device)
        controls.addWidget(self.scan_button); controls.addWidget(add); controls.addWidget(edit); controls.addWidget(delete); controls.addStretch(1); layout.addLayout(controls)
        self.network_status=QLabel("Only IP addresses are pinged; no port checks are performed."); layout.addWidget(self.network_status)
        self.device_table=QTableWidget(0,5); self.device_table.setHorizontalHeaderLabels(["Device","IP / Host","Ping","Latency","Last Check"])
        self.device_table.verticalHeader().setVisible(False); self.device_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); self.device_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents); self.device_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch); self.device_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        self.device_row_device_ids={}
        layout.addWidget(self.device_table); return root

    def _process_controls(self, process_name: str, command_edit: QPlainTextEdit, cwd_edit: QLineEdit, log_edit: QPlainTextEdit, input_edit: QLineEdit | None = None) -> QHBoxLayout:
        row=QHBoxLayout(); save=QPushButton("Save Command"); start=QPushButton("Start"); stop=QPushButton("Stop")
        save.clicked.connect(lambda:self.save_process_command(process_name,command_edit,cwd_edit)); start.clicked.connect(lambda:self.start_process(process_name,command_edit,cwd_edit)); stop.clicked.connect(lambda:self.stop_process(process_name))
        row.addWidget(save); row.addWidget(start); row.addWidget(stop)
        if input_edit is not None:
            send=QPushButton("Send to Console"); send.clicked.connect(lambda:self.send_process_input(process_name,input_edit)); row.addWidget(input_edit); row.addWidget(send)
        row.addStretch(1); return row

    def _build_mavproxy_tab(self) -> QWidget:
        root=QWidget(); layout=QVBoxLayout(root); layout.addLayout(self._title_row("MAVProxy Control"))
        raw=QGroupBox("1. Raw MAVProxy Command"); raw_layout=QVBoxLayout(raw); self.mavproxy_command=QPlainTextEdit(self.config["processes"]["mavproxy"].get("command","")); self.mavproxy_command.setMaximumHeight(125)
        self.mavproxy_cwd=QLineEdit(self.config["processes"]["mavproxy"].get("cwd","")); self.mavproxy_cwd.setPlaceholderText("Working directory, may be left blank")
        raw_layout.addWidget(self.mavproxy_command); raw_layout.addWidget(self.mavproxy_cwd); self.mavproxy_input=QLineEdit(); self.mavproxy_input.setPlaceholderText("MAVProxy console command: link, mode, set link 2...")
        raw_layout.addLayout(self._process_controls("mavproxy",self.mavproxy_command,self.mavproxy_cwd,None,self.mavproxy_input)); layout.addWidget(raw)
        builder=QGroupBox("2. Easy MAVProxy Master / OUT Builder"); b=QVBoxLayout(builder)
        general=QHBoxLayout(); self.aircraft_edit=QLineEdit(str(self.config["mavproxy_builder"].get("aircraft","AIRCRAFT"))); self.baud_spin=QSpinBox(); self.baud_spin.setRange(1200,2000000); self.baud_spin.setValue(int(self.config["mavproxy_builder"].get("baudrate",57600))); self.extra_edit=QLineEdit(str(self.config["mavproxy_builder"].get("extra_args","")))
        general.addWidget(QLabel("Aircraft")); general.addWidget(self.aircraft_edit); general.addWidget(QLabel("Serial baud")); general.addWidget(self.baud_spin); general.addWidget(QLabel("Extra arguments")); general.addWidget(self.extra_edit,2); b.addLayout(general)
        self.master_table=QTableWidget(0,7); self.master_table.setHorizontalHeaderLabels(["Enabled","Type","Label","IP","Port","/dev/serial/by-id/...","Baud"]); self.master_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); self.master_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        mb=QHBoxLayout(); ma=QPushButton("Add Master"); me=QPushButton("Edit Master"); md=QPushButton("Delete Master"); ma.clicked.connect(self.add_master); me.clicked.connect(self.edit_master); md.clicked.connect(self.delete_master); mb.addWidget(ma); mb.addWidget(me); mb.addWidget(md); mb.addStretch(1)
        b.addWidget(self.master_table); b.addLayout(mb)
        self.out_table=QTableWidget(0,3); self.out_table.setHorizontalHeaderLabels(["Enabled","Target IP","Port"]); self.out_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); self.out_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        ob=QHBoxLayout(); oa=QPushButton("Add OUT"); oe=QPushButton("Edit OUT"); od=QPushButton("Delete OUT"); oa.clicked.connect(self.add_out); oe.clicked.connect(self.edit_out); od.clicked.connect(self.delete_out); ob.addWidget(oa); ob.addWidget(oe); ob.addWidget(od); ob.addStretch(1)
        b.addWidget(self.out_table); b.addLayout(ob); generate=QPushButton("Generate Command from Form, Save, and Copy to Raw Field"); generate.clicked.connect(self.generate_command_from_form); b.addWidget(generate); layout.addWidget(builder)
        console=QGroupBox("MAVProxy Live Output"); cl=QVBoxLayout(console); self.mavproxy_status=QLabel("-"); self.mavproxy_log=QPlainTextEdit(); self.mavproxy_log.setReadOnly(True); self.mavproxy_log.setFont(QFont("Monospace")); cl.addWidget(self.mavproxy_status); cl.addWidget(self.mavproxy_log); layout.addWidget(console,2)
        self.refresh_builder_tables(); return root

    def _build_router_tab(self) -> QWidget:
        root=QWidget(); layout=QVBoxLayout(root); layout.addLayout(self._title_row("mavlink-router Control"))
        self.router_command=QPlainTextEdit(self.config["processes"]["mavlink_router"].get("command","")); self.router_command.setMaximumHeight(130)
        self.router_cwd=QLineEdit(self.config["processes"]["mavlink_router"].get("cwd","")); self.router_cwd.setPlaceholderText("Working directory, may be left blank")
        layout.addWidget(QLabel("The mavlink-router command you run in the terminal")); layout.addWidget(self.router_command); layout.addWidget(self.router_cwd); layout.addLayout(self._process_controls("mavlink_router",self.router_command,self.router_cwd,None))
        self.router_status=QLabel("-"); self.router_log=QPlainTextEdit(); self.router_log.setReadOnly(True); self.router_log.setFont(QFont("Monospace")); layout.addWidget(self.router_status); layout.addWidget(self.router_log); return root

    @staticmethod
    def _set_item(table:QTableWidget,row:int,col:int,text:str,background:str|None=None):
        item=table.item(row,col) or QTableWidgetItem(); table.setItem(row,col,item); item.setText(text)
        if background: item.setBackground(QColor(background)); item.setForeground(QColor("#ffffff"))
        else: item.setBackground(QColor("transparent"))

    def gcs_forward_value(self) -> dict[str, Any]:
        return {
            "enabled": self.gcs_forward_enabled.isChecked(),
            "host": self.gcs_forward_host.text().strip(),
            "port": self.gcs_forward_port.value(),
            "period_s": self.gcs_forward_period.value(),
        }

    def save_gcs_forward(self) -> None:
        try:
            saved = self.service.update_gcs_forward(self.gcs_forward_value())
            self.gcs_forward_status_label.setText(
                "Applied: %s:%d · %.1f s"
                % (saved["host"], saved["port"], saved["period_s"])
            )
            QMessageBox.information(
                self,
                "GCS JSON target",
                "Target saved and applied immediately.\n%s:%d"
                % (saved["host"], saved["port"]),
            )
        except Exception as exc:
            QMessageBox.critical(self, "GCS JSON target", str(exc))

    def test_gcs_forward(self) -> None:
        try:
            self.service.update_gcs_forward(self.gcs_forward_value())
            result = self.service.test_gcs_forward()
            if result.get("ok"):
                self.gcs_forward_status_label.setText(
                    "Test sent: %s · %d bytes"
                    % (result.get("target", "-"), result.get("bytes", 0))
                )
                QMessageBox.information(
                    self,
                    "Test successful",
                    "JSON sent:\n%s" % result.get("target", "-"),
                )
            else:
                QMessageBox.warning(
                    self,
                    "Test failed",
                    result.get("error", "Could not send"),
                )
        except Exception as exc:
            QMessageBox.critical(self, "Test failed", str(exc))

    def set_tx_mode(self, mode: str) -> None:
        try:
            saved = self.service.update_tx_selection({"mode": mode})
            self.manual_tx_mode_button.setChecked(saved["mode"] == "manual")
            self.auto_tx_mode_button.setChecked(saved["mode"] == "auto")
        except Exception as exc:
            QMessageBox.critical(self, "TX selection mode", str(exc))

    def run_control(self, action:str, link_id:str):
        threading.Thread(target=lambda:self.service.control(action,link_id),daemon=True).start()

    def start_network_scan(self):
        if not self.service.network_scanner.start(): QMessageBox.information(self,"Network Check","A check is already running.")

    def current_devices(self)->list[dict[str,Any]]:
        return self.service.config_manager.snapshot()["network_checks"]["devices"]

    def add_device(self):
        dialog=DeviceDialog(self)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.service.update_devices(self.current_devices()+[dialog.value()])

    def selected_device_id(self) -> str | None:
        row = self.device_table.currentRow()
        if row < 0:
            return None
        return self.device_row_device_ids.get(row)

    def edit_device(self):
        device_id = self.selected_device_id()
        devices = self.current_devices()
        index = next((i for i, item in enumerate(devices) if item["id"] == device_id), -1)
        if index < 0:
            QMessageBox.information(self, "Edit", "Select a device first.")
            return
        dialog = DeviceDialog(self, devices[index])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            devices[index] = dialog.value()
            self.service.update_devices(devices)

    def delete_device(self):
        device_id = self.selected_device_id()
        devices = self.current_devices()
        index = next((i for i, item in enumerate(devices) if item["id"] == device_id), -1)
        if index < 0:
            return
        if QMessageBox.question(
            self,
            "Delete",
            f"{devices[index]['name']} silinsin mi?"
        ) == QMessageBox.StandardButton.Yes:
            devices.pop(index)
            self.service.update_devices(devices)


    def save_process_command(self,name,command_edit,cwd_edit):
        try: self.service.config_manager.update_process(name,command_edit.toPlainText(),cwd_edit.text()); QMessageBox.information(self,"Saved","Command saved.")
        except Exception as exc: QMessageBox.critical(self,"Error",str(exc))

    def start_process(self,name,command_edit,cwd_edit):
        self.service.config_manager.update_process(name,command_edit.toPlainText(),cwd_edit.text())
        result=self.service.process_manager.get(name).start()
        if not result.get("ok"): QMessageBox.warning(self,"Could not start",result.get("error","Error"))

    def stop_process(self,name):
        result=self.service.process_manager.get(name).stop()
        if not result.get("ok"): QMessageBox.warning(self,"Could not stop",result.get("error","Error"))

    def send_process_input(self,name,input_edit):
        text=input_edit.text().strip()
        if text:
            result=self.service.process_manager.get(name).send_input(text)
            if result.get("ok"): input_edit.clear()
            else: QMessageBox.warning(self,"Could not send",result.get("error","Error"))

    def builder_value(self)->dict[str,Any]:
        return {"aircraft":self.aircraft_edit.text(),"baudrate":self.baud_spin.value(),"extra_args":self.extra_edit.text(),"masters":copy.deepcopy(self.master_rows),"outs":copy.deepcopy(self.out_rows)}

    def refresh_builder_tables(self):
        self.master_table.setRowCount(len(self.master_rows))
        for row,m in enumerate(self.master_rows):
            vals=["Yes" if m.get("enabled",True) else "No",m.get("type","udp"),m.get("label",""),m.get("host","") if m.get("type")=="udp" else "-",str(m.get("port",0)) if m.get("type")=="udp" else "-",m.get("path","") if m.get("type")=="serial" else "-",str(m.get("baud",self.baud_spin.value()))]
            for col,val in enumerate(vals): self.master_table.setItem(row,col,QTableWidgetItem(str(val)))
        self.out_table.setRowCount(len(self.out_rows))
        for row,o in enumerate(self.out_rows):
            for col,val in enumerate(["Yes" if o.get("enabled",True) else "No",o.get("host",""),o.get("port","")]): self.out_table.setItem(row,col,QTableWidgetItem(str(val)))

    def add_master(self):
        d=MasterDialog(self,{"baud":self.baud_spin.value()})
        if d.exec()==QDialog.DialogCode.Accepted: self.master_rows.append(d.value()); self.refresh_builder_tables()
    def edit_master(self):
        row=self.master_table.currentRow()
        if row<0:return
        d=MasterDialog(self,self.master_rows[row])
        if d.exec()==QDialog.DialogCode.Accepted:self.master_rows[row]=d.value();self.refresh_builder_tables()
    def delete_master(self):
        row=self.master_table.currentRow()
        if row>=0:self.master_rows.pop(row);self.refresh_builder_tables()
    def add_out(self):
        d=OutDialog(self)
        if d.exec()==QDialog.DialogCode.Accepted:self.out_rows.append(d.value());self.refresh_builder_tables()
    def edit_out(self):
        row=self.out_table.currentRow()
        if row<0:return
        d=OutDialog(self,self.out_rows[row])
        if d.exec()==QDialog.DialogCode.Accepted:self.out_rows[row]=d.value();self.refresh_builder_tables()
    def delete_out(self):
        row=self.out_table.currentRow()
        if row>=0:self.out_rows.pop(row);self.refresh_builder_tables()

    def generate_command_from_form(self):
        try:
            saved=self.service.update_builder(self.builder_value()); command=build_mavproxy_command(saved); self.mavproxy_command.setPlainText(command)
            self.service.config_manager.update_process("mavproxy",command,self.mavproxy_cwd.text()); QMessageBox.information(self,"Command generated","The MAVProxy command was generated and saved. Restart MAVProxy so linkcontrol can read the new link descriptor settings.")
        except Exception as exc: QMessageBox.critical(self,"Error",str(exc))

    def refresh_ui(self):
        state=self.service.store.snapshot(); self.active_label.setText("Command TX: %s (%s)"%(state.get("active_tx_name") or "NONE",state.get("active_tx_source") or "-"))
        tx_status = self.service.tx_selection_status()
        manual_mode = tx_status.get("mode") == "manual"
        self.manual_tx_mode_button.setChecked(manual_mode)
        self.auto_tx_mode_button.setChecked(not manual_mode)
        if manual_mode:
            self.tx_mode_status_label.setText("MANUAL · Select TX buttons enabled")
        elif tx_status.get("candidate"):
            self.tx_mode_status_label.setText(
                "AUTOMATIC · candidate %s · %.1f s"
                % (
                    tx_status.get("candidate"),
                    float(tx_status.get("candidate_age_s") or 0.0),
                )
            )
        else:
            self.tx_mode_status_label.setText("AUTOMATIC · monitoring the highest-quality UP link")
        forward_status = self.service.gcs_forward_status()
        if forward_status.get("last_error"):
            self.gcs_forward_status_label.setText(
                "Error · %s · %s"
                % (forward_status.get("target", "-"), forward_status["last_error"])
            )
            self.gcs_forward_status_label.setStyleSheet("color:#fca5a5")
        elif forward_status.get("last_sent_age_s") is not None:
            self.gcs_forward_status_label.setText(
                "%s · last sent %.1f s ago"
                % (forward_status.get("target", "-"), forward_status["last_sent_age_s"])
            )
            self.gcs_forward_status_label.setStyleSheet("color:#86efac")
        else:
            self.gcs_forward_status_label.setText(
                "%s · %s"
                % (
                    forward_status.get("target", "-"),
                    "enabled, waiting for first send"
                    if forward_status.get("enabled")
                    else "forwarding disabled",
                )
            )
            self.gcs_forward_status_label.setStyleSheet("color:#fbbf24")
        age=state.get("stats_age_s")
        if state.get("control_error"): self.status_label.setText("UYARI: "+state["control_error"]); self.status_label.setStyleSheet("color:#fca5a5")
        elif age is None or age>3:self.status_label.setText("Waiting for link telemetry.");self.status_label.setStyleSheet("color:#fbbf24")
        else:self.status_label.setText("RX: %s | linkstats %.1f s ago"%(", ".join(state.get("rx_used",[])) or "NONE",age));self.status_label.setStyleSheet("color:#86efac")
        enabled_count=sum(1 for x in state["links"] if x["enabled"])
        for link in state["links"]:
            row=self.row_by_id[link["id"]]; color=link["color_hex"]; self._set_item(self.link_table,row,1,"ON" if link["enabled"] else "OFF",color if not link["enabled"] else None); self._set_item(self.link_table,row,2,link["state"],color)
            self._set_item(self.link_table,row,4,"-" if link["loss_pct"] is None else "%.1f%%"%float(link["loss_pct"])); self._set_item(self.link_table,row,5,"-" if link["delay_ms"] is None else "%d ms"%int(link["delay_ms"])); self._set_item(self.link_table,row,6,"-" if link["pkt_rate"] is None else "%.1f pkt/s"%float(link["pkt_rate"])); self._set_item(self.link_table,row,7,str(link["signal"])); self._set_item(self.link_table,row,8,"ACTIVE" if link["active_tx"] else "-",color if link["active_tx"] else None)
            bar=self.quality_bars[link["id"]];bar.setValue(int(round(link["quality_pct"])));bar.setStyleSheet("QProgressBar::chunk{background:%s}"%color)
            self.action_buttons[link["id"]]["on"].setEnabled(not link["enabled"]);self.action_buttons[link["id"]]["off"].setEnabled(link["enabled"] and enabled_count>1);self.action_buttons[link["id"]]["select"].setEnabled(manual_mode and link["enabled"] and link["up"] and not link["active_tx"])
        self.scan_button.setEnabled(not state["network_scan_running"]); self.network_status.setText("Check in progress..." if state["network_scan_running"] else ("Last manual check: %.1f s ago"%state["network_last_scan_age_s"] if state["network_last_scan_age_s"] is not None else "No check has been run yet; continuous ping is disabled."))
        self.device_table.setRowCount(len(state["devices"]))
        self.device_row_device_ids = {}

        for row, device in enumerate(state["devices"]):
            self.device_row_device_ids[row] = device["id"]
            online = device.get("online")
            ping_status = "VAR" if online is True else "YOK" if online is False else "WAITING"
            ping_color = "#16a34a" if online is True else "#dc2626" if online is False else "#64748b"

            self._set_item(self.device_table, row, 0, device["name"])
            self._set_item(self.device_table, row, 1, device["host"])
            self._set_item(self.device_table, row, 2, ping_status, ping_color)
            self._set_item(
                self.device_table,
                row,
                3,
                "-" if device.get("latency_ms") is None else "%.1f ms" % device["latency_ms"],
            )
            self._set_item(
                self.device_table,
                row,
                4,
                "-" if device.get("age_s") is None else "%.1f s ago" % device["age_s"],
            )
        processes=self.service.process_manager.snapshot(600)
        for name,status_label,log_widget in (("mavproxy",self.mavproxy_status,self.mavproxy_log),("mavlink_router",self.router_status,self.router_log)):
            p=processes[name];status_label.setText(("RUNNING · PID %s · %.1f s"%(p["pid"],p["uptime_s"])) if p["running"] else "STOPPED · exit code %s"%p["exit_code"]); current=log_widget.toPlainText()
            if current!=p["output"]:log_widget.setPlainText(p["output"]);log_widget.verticalScrollBar().setValue(log_widget.verticalScrollBar().maximum())

    def closeEvent(self,event):
        self.service.stop();event.accept()


def parse_args():
    parser=argparse.ArgumentParser(description="communications dashboard");parser.add_argument("--config",default=os.environ.get("LINK_DASHBOARD_CONFIG",str(DEFAULT_CONFIG)));parser.add_argument("--no-gui",action="store_true");return parser.parse_args()


def main()->int:
    args=parse_args();config_path=Path(args.config).expanduser().resolve();manager=ConfigManager(config_path);os.environ["LINK_DASHBOARD_CONFIG"]=str(config_path)
    service=DashboardService(manager);service.start_workers();app=create_web_app(service);service.start_web(app);port=int(manager.snapshot()["web"].get("port",8080));url="http://%s:%d"%(detect_lan_ip(),port);print("communications dashboard: %s"%url)
    if args.no_gui:
        try:
            while True:time.sleep(1)
        except KeyboardInterrupt:service.stop();return 0
    qt=QApplication(sys.argv);window=MainWindow(service,url);window.show();code=qt.exec();service.stop();return int(code)


if __name__=="__main__":raise SystemExit(main())
