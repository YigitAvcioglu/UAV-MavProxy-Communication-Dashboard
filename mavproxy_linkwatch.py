#!/usr/bin/env python3
"""Optional MAVProxy console watcher for five redundant links.

The dashboard does not require this module; linkstats already publishes the same
state. Keep it loaded only when console messages are useful.
"""

import time
from MAVProxy.modules.lib import mp_module


class LinkWatchModule(mp_module.MPModule):
    def __init__(self, mpstate):
        super().__init__(mpstate, "linkwatch", "five-link status console watcher")
        self.add_command(
            "linkwatch",
            self.cmd_linkwatch,
            "show/watch redundant MAVLink links",
            ["<status|on|off>"],
        )
        self.watch_enabled = True
        self.last_check = 0.0
        self.last_signature = None

    @staticmethod
    def _label(conn):
        return str(getattr(conn, "label", "LINK%d" % (int(conn.linknum) + 1)))

    def _active_command_tx(self):
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

    def _snapshot(self):
        now = time.time()
        active, source = self._active_command_tx()
        tx_label = self._label(active) if active is not None else "NONE"
        rows = []
        rx_used = []
        checkdelay = bool(getattr(self.mpstate.settings, "checkdelay", False))

        for conn in list(self.mpstate.mav_master):
            label = self._label(conn)
            last_rx = max(
                float(getattr(conn, "last_message", 0.0) or 0.0),
                float(getattr(conn, "last_heartbeat", 0.0) or 0.0),
            )
            age = now - last_rx if last_rx > 0 else float("inf")
            down = bool(getattr(conn, "linkerror", False))
            delayed = bool(getattr(conn, "link_delayed", False))
            accepted = not down and age < 3.0 and not (checkdelay and delayed)
            if accepted:
                rx_used.append(label)
            try:
                rate = int(self.mpstate.status.bytecounters["MasterIn"][conn.linknum].rate())
            except Exception:
                rate = 0
            try:
                loss = float(conn.packet_loss())
            except Exception:
                loss = 0.0
            rows.append((label, accepted, down, delayed, age, rate, loss))
        return tx_label, source, rx_used, rows

    def _print_status(self):
        tx_label, source, rx_used, rows = self._snapshot()
        print(
            "COMMAND_TX=%s (%s) | RX_USED=%s"
            % (tx_label, source, ",".join(rx_used) if rx_used else "NONE")
        )
        for label, accepted, down, delayed, age, rate, loss in rows:
            age_text = "never" if age == float("inf") else "%.1fs" % age
            print(
                "  %-12s rx=%-5s down=%-5s delayed=%-5s age=%-7s rate=%6d B/s loss=%5.1f%%"
                % (label, accepted, down, delayed, age_text, rate, loss)
            )

    def cmd_linkwatch(self, args):
        action = args[0].lower() if args else "status"
        if action == "status":
            self._print_status()
        elif action == "on":
            self.watch_enabled = True
            self.last_signature = None
            print("linkwatch automatic reporting ON")
        elif action == "off":
            self.watch_enabled = False
            print("linkwatch automatic reporting OFF")
        else:
            print("usage: linkwatch <status|on|off>")

    def idle_task(self):
        if not self.watch_enabled:
            return
        now = time.time()
        if now - self.last_check < 1.0:
            return
        self.last_check = now
        tx_label, source, rx_used, rows = self._snapshot()
        signature = (
            tx_label,
            source,
            tuple(rx_used),
            tuple((row[0], row[1], row[2], row[3]) for row in rows),
        )
        if signature != self.last_signature:
            self.last_signature = signature
            print(
                "[LINK] COMMAND_TX=%s (%s) | RX_USED=%s"
                % (tx_label, source, ",".join(rx_used) if rx_used else "NONE")
            )


def init(mpstate):
    return LinkWatchModule(mpstate)
