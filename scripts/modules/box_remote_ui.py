"""Box enclosure remote panel (``raspberry.box_server``) for embedding in the joystick client."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Callable, Optional

import customtkinter as ctk
import tkinter.messagebox as tk_messagebox

from modules.box_remote import BoxRemoteClient


class BoxRemotePanel:
    """
    Status poll + LED / servo / camera controls; video URL for VLC.

    ``get_target_ip`` should return the same IPv4 as the joystick bridge Target IP field.
    """

    def __init__(
        self,
        parent: Any,
        *,
        root: ctk.CTk,
        args: Any,
        get_target_ip: Callable[[], str],
    ) -> None:
        self._root = root
        self.args = args
        self._get_target_ip = get_target_ip
        self.client: BoxRemoteClient | None = None
        self.poll_ms = max(500, int(getattr(args, "box_poll_ms", 1500)))
        self._poll_after_id: Optional[str] = None

        parent.grid_columnconfigure(0, weight=1)

        row = 0
        ctk.CTkLabel(
            parent,
            text="Uses Target IP from the Joystick tab for box HTTP (raspberry.box_server).",
            wraplength=480,
            anchor="w",
            justify="left",
            text_color="gray70",
        ).grid(row=row, column=0, padx=12, pady=(12, 4), sticky="ew")
        row += 1

        ctk.CTkLabel(parent, text="Box HTTP port").grid(row=row, column=0, padx=12, pady=(8, 2), sticky="w")
        row += 1
        self.port_e = ctk.CTkEntry(parent, placeholder_text="50502")
        self.port_e.insert(0, str(getattr(args, "box_http_port", 50502)))
        self.port_e.grid(row=row, column=0, padx=12, pady=2, sticky="ew")
        row += 1

        ctk.CTkLabel(parent, text="Token (optional, BOX_HTTP_TOKEN on Pi)").grid(
            row=row, column=0, padx=12, pady=(8, 2), sticky="w"
        )
        row += 1
        self.token_e = ctk.CTkEntry(parent, placeholder_text="secret", show="*")
        tok = getattr(args, "box_http_token", "") or ""
        if tok:
            self.token_e.insert(0, tok)
        self.token_e.grid(row=row, column=0, padx=12, pady=2, sticky="ew")
        row += 1

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.grid(row=row, column=0, padx=12, pady=10, sticky="ew")
        btn_row.grid_columnconfigure((0, 1), weight=1)
        self.connect_b = ctk.CTkButton(btn_row, text="Connect box", command=self._on_connect)
        self.connect_b.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.disconnect_b = ctk.CTkButton(btn_row, text="Disconnect", command=self._on_disconnect, state="disabled")
        self.disconnect_b.grid(row=0, column=1, padx=(6, 0), sticky="ew")
        row += 1

        self.conn_l = ctk.CTkLabel(parent, text="Not connected", text_color="gray60")
        self.conn_l.grid(row=row, column=0, padx=12, pady=(0, 6), sticky="w")
        row += 1

        stat = ctk.CTkFrame(parent)
        stat.grid(row=row, column=0, padx=12, pady=4, sticky="ew")
        stat.grid_columnconfigure(1, weight=1)
        row += 1
        self._status_labels: dict[str, ctk.CTkLabel] = {}
        labels = [
            ("Sensor", "sensor_kind"),
            ("Temp °C", "temperature_c"),
            ("RH %", "humidity_percent"),
            ("Pressure hPa", "pressure_hpa"),
            ("Box V", "box_battery_v"),
            ("Drone V", "drone_battery_v"),
            ("LED", "led_on"),
            ("Servo", "servo_active"),
            ("Drone power", "drone_power_on"),
            ("Camera", "camera_streaming"),
            ("Cam error", "camera_stream_error"),
        ]
        for i, (title, key) in enumerate(labels):
            ctk.CTkLabel(stat, text=title + ":").grid(row=i, column=0, padx=8, pady=2, sticky="w")
            lab = ctk.CTkLabel(stat, text="—", anchor="w")
            lab.grid(row=i, column=1, padx=8, pady=2, sticky="ew")
            self._status_labels[key] = lab

        row += 1
        ctk.CTkLabel(parent, text="Controls (Pi hardware must be OK)").grid(
            row=row, column=0, padx=12, pady=(14, 4), sticky="w"
        )
        row += 1
        ctl = ctk.CTkFrame(parent, fg_color="transparent")
        ctl.grid(row=row, column=0, padx=12, pady=4, sticky="ew")
        ctl.grid_columnconfigure((0, 1), weight=1)
        self.led_on_b = ctk.CTkButton(ctl, text="LED on", command=lambda: self._led(True), state="disabled")
        self.led_on_b.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.led_off_b = ctk.CTkButton(ctl, text="LED off", command=lambda: self._led(False), state="disabled")
        self.led_off_b.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        row += 1

        ctl2 = ctk.CTkFrame(parent, fg_color="transparent")
        ctl2.grid(row=row, column=0, padx=12, pady=4, sticky="ew")
        ctl2.grid_columnconfigure((0, 1), weight=1)
        self.servo_on_b = ctk.CTkButton(ctl2, text="Servo start", command=self._servo_on, state="disabled")
        self.servo_on_b.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.servo_off_b = ctk.CTkButton(ctl2, text="Servo stop", command=self._servo_off, state="disabled")
        self.servo_off_b.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        row += 1

        ctl3 = ctk.CTkFrame(parent, fg_color="transparent")
        ctl3.grid(row=row, column=0, padx=12, pady=4, sticky="ew")
        ctl3.grid_columnconfigure((0, 1), weight=1)
        self.cam_on_b = ctk.CTkButton(ctl3, text="Camera start (Pi)", command=lambda: self._cam(True), state="disabled")
        self.cam_on_b.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.cam_off_b = ctk.CTkButton(ctl3, text="Camera stop (Pi)", command=lambda: self._cam(False), state="disabled")
        self.cam_off_b.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        row += 1

        ctk.CTkLabel(parent, text="Video (VLC → Open Network Stream)").grid(
            row=row, column=0, padx=12, pady=(12, 2), sticky="w"
        )
        row += 1
        self.stream_l = ctk.CTkLabel(
            parent,
            text="Connect and start the camera on the Pi to get a tcp:// URL.",
            wraplength=520,
            anchor="w",
            justify="left",
        )
        self.stream_l.grid(row=row, column=0, padx=12, pady=2, sticky="ew")
        row += 1
        vid = ctk.CTkFrame(parent, fg_color="transparent")
        vid.grid(row=row, column=0, padx=12, pady=6, sticky="ew")
        vid.grid_columnconfigure((0, 1), weight=1)
        self.copy_url_b = ctk.CTkButton(vid, text="Copy stream URL", command=self._copy_stream_url, state="disabled")
        self.copy_url_b.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.vlc_b = ctk.CTkButton(vid, text="Try VLC", command=self._try_vlc, state="disabled")
        self.vlc_b.grid(row=0, column=1, padx=4, pady=4, sticky="ew")

        self._last_stream_url = ""

    def _parse_port(self) -> int:
        try:
            return int(self.port_e.get().strip() or "50502")
        except ValueError:
            return 50502

    def _timeout(self) -> float:
        return float(getattr(self.args, "box_http_timeout", 5.0))

    def _make_client(self) -> Optional[BoxRemoteClient]:
        host = self._get_target_ip().strip()
        if not host:
            tk_messagebox.showerror(
                "Box",
                "Set Target IP on the Joystick tab first.",
                parent=self._root,
            )
            return None
        tok = self.token_e.get().strip() or None
        return BoxRemoteClient(host, self._parse_port(), token=tok, timeout=self._timeout())

    def _on_connect(self) -> None:
        c = self._make_client()
        if c is None:
            return
        d = c.get_status()
        if not d.get("ok"):
            tk_messagebox.showerror("Box", d.get("error", "Request failed"), parent=self._root)
            return
        self.client = c
        self.connect_b.configure(state="disabled")
        self.disconnect_b.configure(state="normal")
        self._apply_status(d)
        self._set_controls_enabled(d.get("hardware_ok") is True)
        self._schedule_poll()

    def _on_disconnect(self) -> None:
        if self._poll_after_id is not None:
            try:
                self._root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        self.client = None
        self.connect_b.configure(state="normal")
        self.disconnect_b.configure(state="disabled")
        self.conn_l.configure(text="Not connected", text_color="gray60")
        self._set_controls_enabled(False)
        self._last_stream_url = ""
        self.stream_l.configure(text="Connect and start the camera on the Pi to get a tcp:// URL.")
        self.copy_url_b.configure(state="disabled")
        self.vlc_b.configure(state="disabled")

    def shutdown(self) -> None:
        """Stop polling (e.g. window close)."""
        self._on_disconnect()

    def _schedule_poll(self) -> None:
        if self.client is None:
            return

        def tick() -> None:
            self._poll_after_id = None
            if self.client is None:
                return
            d = self.client.get_status()
            if not d.get("ok"):
                self.conn_l.configure(text=f"Lost: {d.get('error', '?')}", text_color="orange")
                self._on_disconnect()
                return
            self._apply_status(d)
            self._set_controls_enabled(d.get("hardware_ok") is True)
            self._poll_after_id = self._root.after(self.poll_ms, tick)

        self._poll_after_id = self._root.after(self.poll_ms, tick)

    def _apply_status(self, d: dict) -> None:
        if not d.get("ok"):
            self.conn_l.configure(text=d.get("error", "error"), text_color="orange")
            return
        if not d.get("hardware_ok"):
            err = d.get("hardware_error", "hardware unavailable")
            self.conn_l.configure(text=f"HTTP OK — hardware: {err}", text_color="orange")
            for key, lab in self._status_labels.items():
                lab.configure(text="—")
            self._update_stream_hint("", 8888, False)
            return

        self.conn_l.configure(text="Connected — hardware OK", text_color="#2fa572")

        def fmt_val(key: str, v) -> str:
            if v is None:
                return "n/a"
            if isinstance(v, bool):
                return "yes" if v else "no"
            if isinstance(v, float):
                if key in ("temperature_c", "humidity_percent", "pressure_hpa"):
                    return f"{v:.1f}"
                return f"{v:.2f}"
            return str(v)

        for key, lab in self._status_labels.items():
            if key not in d:
                lab.configure(text="—")
                continue
            lab.configure(text=fmt_val(key, d.get(key)))

        host = self._get_target_ip().strip()
        port = int(d.get("video_tcp_port") or 8888)
        cam_on = bool(d.get("camera_streaming"))
        self._update_stream_hint(host, port, cam_on)

    def _update_stream_hint(self, host: str, video_port: int, cam_on: bool) -> None:
        if cam_on and host:
            url = f"tcp://{host}:{video_port}"
            self._last_stream_url = url
            self.stream_l.configure(
                text=f"Stream URL (open in VLC): {url}\n"
                "One client connects; Pi runs rpicam-vid with --listen on this port."
            )
            self.copy_url_b.configure(state="normal")
            self.vlc_b.configure(state="normal" if shutil.which("vlc") else "disabled")
        else:
            self._last_stream_url = ""
            self.stream_l.configure(
                text="Start the camera on the Pi, then use Copy / VLC with the tcp:// URL."
            )
            self.copy_url_b.configure(state="disabled")
            self.vlc_b.configure(state="disabled")

    def _set_controls_enabled(self, on: bool) -> None:
        st = "normal" if on else "disabled"
        for b in (
            self.led_on_b,
            self.led_off_b,
            self.servo_on_b,
            self.servo_off_b,
            self.cam_on_b,
            self.cam_off_b,
        ):
            b.configure(state=st)

    def _led(self, on: bool) -> None:
        if self.client is None:
            return
        d = self.client.set_led(on)
        if not d.get("ok"):
            tk_messagebox.showerror("Box", d.get("error", "LED command failed"), parent=self._root)
            return
        self._apply_status(d)

    def _servo_on(self) -> None:
        if self.client is None:
            return
        d = self.client.set_servo(True, "neutral")
        if not d.get("ok"):
            tk_messagebox.showerror("Box", d.get("error", "Servo command failed"), parent=self._root)
            return
        self._apply_status(d)

    def _servo_off(self) -> None:
        if self.client is None:
            return
        d = self.client.set_servo(False)
        if not d.get("ok"):
            tk_messagebox.showerror("Box", d.get("error", "Servo command failed"), parent=self._root)
            return
        self._apply_status(d)

    def _cam(self, streaming: bool) -> None:
        if self.client is None:
            return
        d = self.client.set_camera_streaming(streaming)
        if not d.get("ok"):
            tk_messagebox.showerror("Box", d.get("error", "Camera command failed"), parent=self._root)
            return
        self._apply_status(d)

    def _copy_stream_url(self) -> None:
        if not self._last_stream_url:
            return
        self._root.clipboard_clear()
        self._root.clipboard_append(self._last_stream_url)
        self._root.update()

    def _try_vlc(self) -> None:
        if not self._last_stream_url:
            return
        vlc = shutil.which("vlc")
        if not vlc:
            tk_messagebox.showinfo(
                "Box",
                "VLC not found in PATH. Install VLC or paste the URL manually.",
                parent=self._root,
            )
            return
        try:
            subprocess.Popen([vlc, self._last_stream_url], start_new_session=True)
        except OSError as e:
            tk_messagebox.showerror("Box", str(e), parent=self._root)
