"""Box enclosure remote panel (``raspberry.box_server``) for embedding in the joystick client."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Callable, Optional

import customtkinter as ctk
import tkinter.messagebox as tk_messagebox

from modules.box_remote import BoxRemoteClient

VIDEO_STREAM_PORT = 8888


class BoxRemotePanel:
    """
    Status poll + LED / servo / camera controls; ffplay UDP viewer.

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
        self._last_status: dict[str, Any] = {}

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(parent, label_text="Box enclosure (raspberry.box_server)")
        scroll.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        panel = scroll

        row = 0
        ctk.CTkLabel(
            panel,
            text="Uses Target IP from the Joystick tab for box HTTP.",
            wraplength=480,
            anchor="w",
            justify="left",
            text_color="gray70",
        ).grid(row=row, column=0, padx=12, pady=(12, 4), sticky="ew")
        row += 1

        ctk.CTkLabel(panel, text="Box HTTP port").grid(row=row, column=0, padx=4, pady=(8, 2), sticky="w")
        row += 1
        self.port_e = ctk.CTkEntry(panel, placeholder_text="50502")
        self.port_e.insert(0, str(getattr(args, "box_http_port", 50502)))
        self.port_e.grid(row=row, column=0, padx=4, pady=2, sticky="ew")
        row += 1

        ctk.CTkLabel(panel, text="Token (optional, BOX_HTTP_TOKEN on Pi)").grid(
            row=row, column=0, padx=4, pady=(8, 2), sticky="w"
        )
        row += 1
        self.token_e = ctk.CTkEntry(panel, placeholder_text="secret", show="*")
        tok = getattr(args, "box_http_token", "") or ""
        if tok:
            self.token_e.insert(0, tok)
        self.token_e.grid(row=row, column=0, padx=4, pady=2, sticky="ew")
        row += 1

        btn_row = ctk.CTkFrame(panel, fg_color="transparent")
        btn_row.grid(row=row, column=0, padx=4, pady=10, sticky="ew")
        btn_row.grid_columnconfigure((0, 1), weight=1)
        self.connect_b = ctk.CTkButton(btn_row, text="Connect box", command=self._on_connect)
        self.connect_b.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.disconnect_b = ctk.CTkButton(btn_row, text="Disconnect", command=self._on_disconnect, state="disabled")
        self.disconnect_b.grid(row=0, column=1, padx=(6, 0), sticky="ew")
        row += 1

        self.conn_l = ctk.CTkLabel(panel, text="Not connected", text_color="gray60")
        self.conn_l.grid(row=row, column=0, padx=4, pady=(0, 6), sticky="w")
        row += 1

        ctk.CTkLabel(panel, text="Controls", font=ctk.CTkFont(weight="bold")).grid(
            row=row, column=0, padx=4, pady=(8, 4), sticky="w"
        )
        row += 1
        ctl = ctk.CTkFrame(panel, fg_color="transparent")
        ctl.grid(row=row, column=0, padx=4, pady=4, sticky="ew")
        ctl.grid_columnconfigure((0, 1, 2), weight=1)
        self.led_toggle_b = ctk.CTkButton(ctl, text="LED: off", command=self._toggle_led, state="disabled", height=36)
        self.led_toggle_b.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.servo_toggle_b = ctk.CTkButton(
            ctl, text="Servo: stop", command=self._toggle_servo, state="disabled", height=36
        )
        self.servo_toggle_b.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self.cam_toggle_b = ctk.CTkButton(ctl, text="Video: off", command=self._toggle_cam, state="disabled", height=36)
        self.cam_toggle_b.grid(row=0, column=2, padx=4, pady=4, sticky="ew")
        row += 1

        ctk.CTkLabel(panel, text="Status", font=ctk.CTkFont(weight="bold")).grid(
            row=row, column=0, padx=4, pady=(12, 4), sticky="w"
        )
        row += 1
        stat = ctk.CTkFrame(panel)
        stat.grid(row=row, column=0, padx=4, pady=4, sticky="ew")
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

        ctk.CTkLabel(panel, text="Video (ffplay, UDP MPEG-TS)").grid(
            row=row, column=0, padx=4, pady=(12, 2), sticky="w"
        )
        row += 1
        self.stream_l = ctk.CTkLabel(
            panel,
            text="Connect and start the camera on the Pi to get a ffplay command.",
            wraplength=520,
            anchor="w",
            justify="left",
        )
        self.stream_l.grid(row=row, column=0, padx=4, pady=2, sticky="ew")
        row += 1
        vid = ctk.CTkFrame(panel, fg_color="transparent")
        vid.grid(row=row, column=0, padx=4, pady=6, sticky="ew")
        vid.grid_columnconfigure((0, 1), weight=1)
        self.copy_url_b = ctk.CTkButton(vid, text="Copy ffplay cmd", command=self._copy_stream_url, state="disabled")
        self.copy_url_b.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.ffplay_b = ctk.CTkButton(vid, text="Try ffplay", command=self._try_ffplay, state="disabled")
        self.ffplay_b.grid(row=0, column=1, padx=4, pady=4, sticky="ew")

        self._last_stream_url = ""
        self._last_ffplay_cmd = ""

    @staticmethod
    def _find_ffplay() -> Optional[str]:
        return shutil.which("ffplay")

    def auto_connect(self, *, quiet: bool = False) -> bool:
        """Connect to box HTTP using Joystick tab Target IP (no dialog if ``quiet``)."""
        if self.client is not None:
            return True
        c = self._make_client()
        if c is None:
            return False
        d = c.get_status()
        if not d.get("ok"):
            if not quiet:
                tk_messagebox.showerror("Box", d.get("error", "Request failed"), parent=self._root)
            return False
        self.client = c
        self.connect_b.configure(state="disabled")
        self.disconnect_b.configure(state="normal")
        self._apply_status(d)
        self._set_controls_enabled(True)
        self._schedule_poll()
        return True

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
        self.auto_connect(quiet=False)

    def disconnect(self) -> None:
        """Stop polling and clear box HTTP session."""
        self._on_disconnect()

    def _on_disconnect(self) -> None:
        if self._poll_after_id is not None:
            try:
                self._root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        self.client = None
        self._last_status = {}
        self.connect_b.configure(state="normal")
        self.disconnect_b.configure(state="disabled")
        self.conn_l.configure(text="Not connected", text_color="gray60")
        self._set_controls_enabled(False)
        self._sync_toggle_buttons({})
        self._last_stream_url = ""
        self.stream_l.configure(text="Connect and start the camera on the Pi to get a ffplay command.")
        self.copy_url_b.configure(state="disabled")
        self.ffplay_b.configure(state="disabled")
        self._last_ffplay_cmd = ""

    def shutdown(self) -> None:
        """Stop polling (e.g. window close)."""
        self.disconnect()

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
            self._set_controls_enabled(True)
            self._poll_after_id = self._root.after(self.poll_ms, tick)

        self._poll_after_id = self._root.after(self.poll_ms, tick)

    def _apply_status(self, d: dict) -> None:
        if d.get("ok"):
            self._last_status = d
        self._sync_toggle_buttons(d)
        if not d.get("ok"):
            self.conn_l.configure(text=d.get("error", "error"), text_color="orange")
            return
        if not d.get("hardware_ok"):
            err = d.get("hardware_error", "box unavailable")
            self.conn_l.configure(text=f"HTTP OK — {err}", text_color="orange")
            return

        if d.get("sensors_ok") is False:
            parts = []
            if d.get("env_error"):
                parts.append(f"env: {d['env_error']}")
            if d.get("battery_error"):
                parts.append(f"ADC: {d['battery_error']}")
            hint = "; ".join(parts) if parts else "sensors unavailable"
            self.conn_l.configure(text=f"Connected — controls OK ({hint})", text_color="#c9a227")
        else:
            self.conn_l.configure(text="Connected", text_color="#2fa572")

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
        cam_on = bool(d.get("camera_streaming"))
        self._update_stream_hint(host, cam_on)

    def _video_play_url(self) -> str:
        return f"udp://@:{VIDEO_STREAM_PORT}"

    def _ffplay_command(self) -> str:
        url = self._video_play_url()
        return f"ffplay -fflags nobuffer -flags low_delay -framedrop -i {url}"

    def _update_stream_hint(
        self,
        host: str,
        cam_on: bool,
    ) -> None:
        host = host.strip()
        if host and self.client is not None:
            url = self._video_play_url()
            cmd = self._ffplay_command()
            self._last_stream_url = url
            self._last_ffplay_cmd = cmd
            ffplay_path = self._find_ffplay()
            cam_line = (
                "UDP stream active — Try ffplay or paste the command in a terminal."
                if cam_on
                else "Turn Video ON, then Try ffplay."
            )
            ffplay_line = (
                "Try ffplay: nobuffer, low_delay, framedrop."
                if ffplay_path
                else "ffplay not on PATH — install ffmpeg, or Copy ffplay cmd."
            )
            self.stream_l.configure(
                text=f"{cmd}\n"
                f"{cam_line}\n{ffplay_line}\n"
                f"MPEG-TS · UDP unicast to this PC · Pi default 640×480 @ 25 fps"
            )
            self.copy_url_b.configure(state="normal")
            self.ffplay_b.configure(state="normal" if ffplay_path else "disabled")
        else:
            self._last_stream_url = ""
            self._last_ffplay_cmd = ""
            self.stream_l.configure(
                text="Connect box, then turn Video ON. Copy / Try ffplay use the Pi Target IP."
            )
            self.copy_url_b.configure(state="disabled")
            self.ffplay_b.configure(state="disabled")

    def _set_controls_enabled(self, on: bool) -> None:
        st = "normal" if on else "disabled"
        for b in (self.led_toggle_b, self.servo_toggle_b, self.cam_toggle_b):
            b.configure(state=st)

    def _toggle_on_color(self) -> tuple[str, str]:
        return "seagreen", "darkgreen"

    def _toggle_off_color(self) -> tuple[str, str]:
        return "gray40", "gray35"

    def _sync_toggle_buttons(self, d: dict) -> None:
        led_on = bool(d.get("led_on"))
        servo_on = bool(d.get("servo_active"))
        cam_on = bool(d.get("camera_streaming"))
        pairs = (
            (self.led_toggle_b, f"LED: {'on' if led_on else 'off'}", led_on),
            (self.servo_toggle_b, f"Servo: {'run' if servo_on else 'stop'}", servo_on),
            (self.cam_toggle_b, f"Video: {'on' if cam_on else 'off'}", cam_on),
        )
        for btn, text, active in pairs:
            fg, hover = self._toggle_on_color() if active else self._toggle_off_color()
            btn.configure(text=text, fg_color=fg, hover_color=hover)

    def _toggle_led(self) -> None:
        if self.client is None:
            return
        on = not bool(self._last_status.get("led_on"))
        self._led(on)

    def _toggle_servo(self) -> None:
        if self.client is None:
            return
        if bool(self._last_status.get("servo_active")):
            self._servo_off()
        else:
            self._servo_on()

    def _toggle_cam(self) -> None:
        if self.client is None:
            return
        on = not bool(self._last_status.get("camera_streaming"))
        self._cam(on)

    def _command_error_text(self, d: dict, fallback: str) -> str:
        parts = [d.get("error"), d.get("hardware_error"), d.get("camera_stream_error")]
        if d.get("led_error"):
            parts.append(f"LED: {d['led_error']}")
        msg = "\n".join(p for p in parts if p)
        return msg or fallback

    def _led(self, on: bool) -> None:
        if self.client is None:
            return
        d = self.client.set_led(on)
        if not d.get("ok"):
            tk_messagebox.showerror(
                "Box",
                self._command_error_text(d, "LED command failed"),
                parent=self._root,
            )
            return
        self._apply_status(d)

    def _servo_on(self) -> None:
        if self.client is None:
            return
        d = self.client.set_servo(True, "neutral")
        if not d.get("ok"):
            tk_messagebox.showerror(
                "Box",
                self._command_error_text(d, "Servo command failed"),
                parent=self._root,
            )
            return
        self._apply_status(d)

    def _servo_off(self) -> None:
        if self.client is None:
            return
        d = self.client.set_servo(False)
        if not d.get("ok"):
            tk_messagebox.showerror(
                "Box",
                self._command_error_text(d, "Servo command failed"),
                parent=self._root,
            )
            return
        self._apply_status(d)

    def _cam(self, streaming: bool) -> None:
        if self.client is None:
            return
        d = self.client.set_camera_streaming(streaming)
        if not d.get("ok"):
            tk_messagebox.showerror(
                "Box",
                self._command_error_text(d, "Camera command failed"),
                parent=self._root,
            )
            return
        self._apply_status(d)

    def _copy_stream_url(self) -> None:
        if not self._last_ffplay_cmd:
            return
        self._root.clipboard_clear()
        self._root.clipboard_append(self._last_ffplay_cmd)
        self._root.update()

    def _try_ffplay(self) -> None:
        if not self._last_stream_url:
            tk_messagebox.showinfo(
                "Box",
                "Connect box and set Target IP on the Joystick tab first.",
                parent=self._root,
            )
            return
        if not self._last_status.get("camera_streaming"):
            tk_messagebox.showinfo(
                "Box",
                "Turn Video ON first so the Pi starts rpicam-vid, then Try ffplay again.",
                parent=self._root,
            )
            return
        ffplay = self._find_ffplay()
        if not ffplay:
            tk_messagebox.showinfo(
                "Box",
                "ffplay not found. Install ffmpeg (ffplay is included).\n"
                "Or use Copy ffplay cmd and run it in a terminal.",
                parent=self._root,
            )
            return
        try:
            subprocess.Popen(
                [
                    ffplay,
                    "-fflags",
                    "nobuffer",
                    "-flags",
                    "low_delay",
                    "-framedrop",
                    "-i",
                    self._last_stream_url,
                ],
                start_new_session=True,
            )
        except OSError as e:
            tk_messagebox.showerror("Box", str(e), parent=self._root)
