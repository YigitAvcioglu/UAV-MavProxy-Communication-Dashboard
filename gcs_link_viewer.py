#!/usr/bin/env python3
"""Minimal read-only link viewer for the 192.168.1.60 GCS computer."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QHeaderView,
    QMainWindow,
    QTableWidget,
    QTableWidgetItem,
)

LINK_ORDER = ["Ubiquiti", "RFD900x", "4G", "CUAV P8", "ELRS"]


class Viewer(QMainWindow):
    def __init__(self, host: str, port: int):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.setblocking(False)

        self.setWindowTitle("GCS Link Status")
        self.resize(760, 330)

        self.table = QTableWidget(len(LINK_ORDER), 4)
        self.table.setHorizontalHeaderLabels(
            ["Device", "Status", "Link Quality", "Active TX"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.setCentralWidget(self.table)

        for row, name in enumerate(LINK_ORDER):
            self.table.setItem(row, 0, self._item(name))
            self.table.setItem(row, 1, self._item("WAITING"))
            self.table.setItem(row, 2, self._item("0.0%"))
            self.table.setItem(row, 3, self._item("NO"))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)

    @staticmethod
    def _item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    def poll(self) -> None:
        newest: dict[str, Any] | None = None
        while True:
            try:
                raw, _sender = self.sock.recvfrom(65535)
            except BlockingIOError:
                break
            except OSError:
                break

            try:
                payload = json.loads(raw.decode("utf-8"))
                if isinstance(payload, dict) and isinstance(payload.get("links"), list):
                    newest = payload
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

        if newest is not None:
            self.render(newest)

    def render(self, payload: dict[str, Any]) -> None:
        rows = {
            str(item.get("name")): item
            for item in payload.get("links", [])
            if isinstance(item, dict)
        }

        for row, name in enumerate(LINK_ORDER):
            link = rows.get(name, {})
            raw_status = str(link.get("status", "DOWN")).upper()
            if raw_status == "UP":
                status = "UP"
            elif raw_status == "NOT_INCLUDED":
                status = "NOT_INCLUDED"
            else:
                status = "DOWN"
            try:
                quality = max(0.0, min(100.0, float(link.get("quality_pct", 0.0) or 0.0)))
            except (TypeError, ValueError):
                quality = 0.0
            active_tx = bool(link.get("active_tx", False))

            self.table.setItem(row, 1, self._item(status))
            self.table.setItem(row, 2, self._item(f"{quality:.1f}%"))
            self.table.setItem(row, 3, self._item("YES" if active_tx else "NO"))

    def closeEvent(self, event) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        event.accept()


def parse_args():
    parser = argparse.ArgumentParser(description="minimal GCS link viewer")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=14660)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv)
    window = Viewer(args.host, args.port)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
