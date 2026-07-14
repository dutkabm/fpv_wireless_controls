"""Drone CRSF telemetry panel (link / battery / GPS) for the ground station."""

from __future__ import annotations

from typing import Any, Callable, Optional

import customtkinter as ctk

from modules.box_remote import BOX_HTTP_PORT, BoxRemoteClient

TAB_POLL_MS = 500  # Telemetry tab visible: GET /api/status interval
BADGE_POLL_MS = 1500  # While Connected: refresh CRSF badge on Joystick tab


_LINK_KEYS = (
    ("Uplink LQ", "Uplink LQ"),
    ("Uplink RSSI 1", "Uplink RSSI 1"),
    ("Uplink RSSI 2", "Uplink RSSI 2"),
    ("Uplink SNR", "Uplink SNR"),
    ("Active Antenna", "Active Antenna"),
    ("RF Mode", "RF Mode"),
    ("Uplink TX Power", "Uplink TX Power"),
    ("Downlink LQ", "Downlink LQ"),
    ("Downlink RSSI", "Downlink RSSI"),
    ("Downlink SNR", "Downlink SNR"),
)
_BATT_KEYS = (
    ("Voltage", "Voltage"),
    ("Current", "Current"),
    ("Capacity", "Capacity"),
    ("Remaining", "Remaining"),
)
_GPS_KEYS = (
    ("Latitude", "Latitude"),
    ("Longitude", "Longitude"),
    ("Altitude", "Altitude"),
    ("Speed", "Speed"),
    ("Heading", "Heading"),
)
_ATT_KEYS = (
    ("Pitch", "Pitch"),
    ("Roll", "Roll"),
    ("Yaw", "Yaw"),
    ("Flight Mode", "Flight Mode"),
)


def format_crsf_status(d: dict) -> tuple[str, str]:
    """Return (label text, color key: off|serial|link) from a status dict."""
    if not d.get("ok"):
        return "CRSF: —", "off"
    if not d.get("crsf_serial_open"):
        return "CRSF: serial closed", "off"
    path = (d.get("crsf_serial_path") or "").strip()
    mode = (d.get("crsf_output") or "").strip()
    suffix = path or mode or "open"
    if d.get("crsf_link_ok"):
        telem = d.get("crsf_telemetry") or {}
        lq = telem.get("Uplink LQ", "?")
        return f"CRSF: link OK · LQ {lq} · {suffix}", "link"
    return f"CRSF: serial open · {suffix}", "serial"


class TelemetryPanel:
    """Polls bridge ``GET /api/status`` for CRSF serial + drone telemetry fields."""

    def __init__(
        self,
        parent: Any,
        *,
        root: ctk.CTk,
        args: Any,
        get_target_ip: Callable[[], str],
        on_status: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._root = root
        self.args = args
        self._get_target_ip = get_target_ip
        self._on_status = on_status
        self.client: BoxRemoteClient | None = None
        self._tab_visible = False
        self._poll_after_id: Optional[str] = None
        self._badge_after_id: Optional[str] = None
        self._box_token: Optional[str] = None

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(parent, label_text="Drone telemetry (CRSF)")
        scroll.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        panel = scroll

        row = 0
        ctk.CTkLabel(panel, text="CRSF connection", font=ctk.CTkFont(weight="bold")).grid(
            row=row, column=0, padx=4, pady=(8, 4), sticky="w"
        )
        row += 1
        conn = ctk.CTkFrame(panel)
        conn.grid(row=row, column=0, padx=4, pady=4, sticky="ew")
        conn.grid_columnconfigure(1, weight=1)
        row += 1
        self._conn_labels: dict[str, ctk.CTkLabel] = {}
        for i, (title, key) in enumerate(
            (
                ("Serial", "crsf_serial_open"),
                ("Path", "crsf_serial_path"),
                ("Output", "crsf_output"),
                ("Link", "crsf_link_ok"),
                ("Telemetry age", "crsf_telemetry_age_s"),
            )
        ):
            ctk.CTkLabel(conn, text=title + ":").grid(row=i, column=0, padx=8, pady=2, sticky="w")
            lab = ctk.CTkLabel(conn, text="—", anchor="w")
            lab.grid(row=i, column=1, padx=8, pady=2, sticky="ew")
            self._conn_labels[key] = lab

        self._field_labels: dict[str, ctk.CTkLabel] = {}
        for title, keys in (
            ("Link statistics", _LINK_KEYS),
            ("Battery", _BATT_KEYS),
            ("GPS", _GPS_KEYS),
            ("Attitude / mode", _ATT_KEYS),
        ):
            ctk.CTkLabel(panel, text=title, font=ctk.CTkFont(weight="bold")).grid(
                row=row, column=0, padx=4, pady=(12, 4), sticky="w"
            )
            row += 1
            grid = ctk.CTkFrame(panel)
            grid.grid(row=row, column=0, padx=4, pady=4, sticky="ew")
            grid.grid_columnconfigure(1, weight=1)
            row += 1
            for i, (disp, key) in enumerate(keys):
                ctk.CTkLabel(grid, text=disp + ":").grid(row=i, column=0, padx=8, pady=2, sticky="w")
                lab = ctk.CTkLabel(grid, text="—", anchor="w")
                lab.grid(row=i, column=1, padx=8, pady=2, sticky="ew")
                self._field_labels[key] = lab

    def _timeout(self) -> float:
        return float(getattr(self.args, "box_http_timeout", 5.0))

    def _make_client(self) -> Optional[BoxRemoteClient]:
        host = self._get_target_ip().strip()
        if not host:
            return None
        return BoxRemoteClient(host, BOX_HTTP_PORT, token=self._box_token, timeout=self._timeout())

    def connect_with_token(self, token: str, *, quiet: bool = False) -> bool:
        tok = (token or "").strip()
        self._box_token = tok or None
        if self.client is not None:
            self.client.token = tok or None
        else:
            c = self._make_client()
            if c is None:
                return False
            self.client = c
        d = self.client.get_status()
        if not d.get("ok"):
            if not quiet:
                return False
            self.client = None
            return False
        self._apply_status(d)
        self._start_badge_poll()
        if self._tab_visible:
            self._cancel_tab_poll()
            self._poll_after_id = self._root.after(TAB_POLL_MS, self._tab_poll_tick)
        return True

    def disconnect(self) -> None:
        self._cancel_tab_poll()
        self._cancel_badge_poll()
        self.client = None
        self._box_token = None
        self._clear_labels()
        if self._on_status is not None:
            self._on_status({"ok": False})

    def shutdown(self) -> None:
        self.disconnect()

    def set_tab_visible(self, visible: bool) -> None:
        self._tab_visible = bool(visible)
        if not self._tab_visible:
            self._cancel_tab_poll()
            return
        if self.client is None:
            return
        self._cancel_tab_poll()
        self._poll_after_id = self._root.after(0, self._tab_poll_tick)

    def _start_badge_poll(self) -> None:
        self._cancel_badge_poll()
        if self.client is None:
            return
        self._badge_after_id = self._root.after(BADGE_POLL_MS, self._badge_poll_tick)

    def _cancel_tab_poll(self) -> None:
        if self._poll_after_id is not None:
            try:
                self._root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None

    def _cancel_badge_poll(self) -> None:
        if self._badge_after_id is not None:
            try:
                self._root.after_cancel(self._badge_after_id)
            except Exception:
                pass
            self._badge_after_id = None

    def _tab_poll_tick(self) -> None:
        self._poll_after_id = None
        if not self._tab_visible or self.client is None:
            return
        d = self.client.get_status()
        if not d.get("ok"):
            self.disconnect()
            return
        self._apply_status(d)
        self._poll_after_id = self._root.after(TAB_POLL_MS, self._tab_poll_tick)

    def _badge_poll_tick(self) -> None:
        self._badge_after_id = None
        if self.client is None:
            return
        # Skip duplicate GET when the telemetry tab is already polling.
        if not self._tab_visible:
            d = self.client.get_status()
            if not d.get("ok"):
                self.disconnect()
                return
            self._apply_status(d)
        self._badge_after_id = self._root.after(BADGE_POLL_MS, self._badge_poll_tick)

    def _clear_labels(self) -> None:
        for lab in self._conn_labels.values():
            lab.configure(text="—")
        for lab in self._field_labels.values():
            lab.configure(text="—")

    def _apply_status(self, d: dict) -> None:
        if self._on_status is not None:
            self._on_status(d)
        if not d.get("ok"):
            self._clear_labels()
            return

        def yes_no(v: Any) -> str:
            return "yes" if v else "no"

        open_ = bool(d.get("crsf_serial_open"))
        self._conn_labels["crsf_serial_open"].configure(text=yes_no(open_))
        self._conn_labels["crsf_serial_path"].configure(text=d.get("crsf_serial_path") or "—")
        self._conn_labels["crsf_output"].configure(text=d.get("crsf_output") or "—")
        self._conn_labels["crsf_link_ok"].configure(text=yes_no(d.get("crsf_link_ok")))
        age = d.get("crsf_telemetry_age_s")
        self._conn_labels["crsf_telemetry_age_s"].configure(
            text="—" if age is None else f"{age:.2f} s"
        )

        telem = d.get("crsf_telemetry") or {}
        if not isinstance(telem, dict):
            telem = {}
        for key, lab in self._field_labels.items():
            v = telem.get(key)
            if v is None:
                lab.configure(text="—")
            else:
                lab.configure(text=str(v))
