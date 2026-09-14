"""Box enclosure remote panel (``drone_control.box_server``) for embedding in the joystick client."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any, Callable, Optional

import customtkinter as ctk
import tkinter.messagebox as tk_messagebox

from modules.box_remote import BOX_HTTP_PORT, BoxRemoteClient

VIDEO_STREAM_PORT = 5004
TAB_POLL_MS = 3000  # Box tab visible: GET /api/status interval


class BoxRemotePanel:
    """
    Status poll + LED / servo / camera controls; GStreamer RTP viewer (H264 MIPI or JPEG USB).

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
        self._box_tab_visible = False
        self._poll_after_id: Optional[str] = None
        self._last_status: dict[str, Any] = {}
        self._controls_enabled = False

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(parent, label_text="Box enclosure (drone_control.box_server)")
        scroll.grid(row=0, column=0, padx=8, pady=8, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        panel = scroll

        self._box_token: Optional[str] = None

        row = 0
        ctk.CTkLabel(panel, text="Controls", font=ctk.CTkFont(weight="bold")).grid(
            row=row, column=0, padx=4, pady=(12, 4), sticky="w"
        )
        row += 1
        ctl = ctk.CTkFrame(panel, fg_color="transparent")
        ctl.grid(row=row, column=0, padx=4, pady=4, sticky="ew")
        ctl.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self.cam_toggle_b = ctk.CTkButton(ctl, text="Video: off", command=self._toggle_cam, state="disabled", height=36)
        self.cam_toggle_b.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self.led_toggle_b = ctk.CTkButton(ctl, text="LED: off", command=self._toggle_led, state="disabled", height=36)
        self.led_toggle_b.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self.servo_toggle_b = ctk.CTkButton(
            ctl, text="Servo: stop", command=self._toggle_servo, state="disabled", height=36
        )
        self.servo_toggle_b.grid(row=0, column=2, padx=4, pady=4, sticky="ew")
        self.drone_power_toggle_b = ctk.CTkButton(
            ctl, text="Drone power: off", command=self._toggle_drone_power, state="disabled", height=36
        )
        self.drone_power_toggle_b.grid(row=0, column=3, padx=4, pady=4, sticky="ew")
        row += 1
        self.usb_cam_var = ctk.BooleanVar(value=False)  # unchecked = MIPI (default)
        self.usb_cam_cb = ctk.CTkCheckBox(
            panel,
            text="USB camera (unchecked = MIPI)",
            variable=self.usb_cam_var,
            state="disabled",
        )
        self.usb_cam_cb.grid(row=row, column=0, padx=8, pady=(0, 4), sticky="w")
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
            ("Box I/O", "box_io_enabled"),
            ("Sensor", "sensor_kind"),
            ("Temp °C", "temperature_c"),
            ("RH %", "humidity_percent"),
            ("Pressure hPa", "pressure_hpa"),
            ("Box V", "box_battery_v"),
            ("Drone V", "drone_battery_v"),
            ("Env error", "env_error"),
            ("Batt error", "battery_error"),
            ("Camera", "camera_streaming"),
            ("Cam source", "camera_source"),
            ("LED", "led_on"),
            ("Servo", "servo_active"),
            ("Drone power", "drone_power_on"),
            ("Cam error", "camera_stream_error"),
        ]
        for i, (title, key) in enumerate(labels):
            ctk.CTkLabel(stat, text=title + ":").grid(row=i, column=0, padx=8, pady=2, sticky="w")
            lab = ctk.CTkLabel(stat, text="—", anchor="w")
            lab.grid(row=i, column=1, padx=8, pady=2, sticky="ew")
            self._status_labels[key] = lab

        self._video_proc: Optional[subprocess.Popen] = None
        self._video_source: str = "mipi"

    @staticmethod
    def _find_gst_launch() -> Optional[str]:
        return shutil.which("gst-launch-1.0")

    def _find_video_player(self) -> Optional[str]:
        """Return ``gst-launch-1.0`` path if available."""
        return self._find_gst_launch()

    def connect_with_token(self, token: str, *, quiet: bool = False) -> bool:
        """Start box HTTP session after joystick bridge Connect (token from handshake)."""
        tok = (token or "").strip()
        if not tok:
            if not quiet:
                tk_messagebox.showerror(
                    "Box",
                    "No box HTTP token from bridge handshake.",
                    parent=self._root,
                )
            return False
        self._box_token = tok
        if self.client is not None:
            self.client.token = tok
            return True
        c = self._make_client()
        if c is None:
            return False
        d = c.get_status()
        if not d.get("ok"):
            if not quiet:
                tk_messagebox.showerror("Box", d.get("error", "Request failed"), parent=self._root)
            self.client = None
            return False
        self.client = c
        self._apply_status(d)
        self._set_controls_enabled(True)
        if self._box_tab_visible:
            self._cancel_poll_timer()
            self._poll_after_id = self._root.after(TAB_POLL_MS, self._poll_tick)
        return True

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
        return BoxRemoteClient(host, BOX_HTTP_PORT, token=self._box_token, timeout=self._timeout())

    def set_box_tab_visible(self, visible: bool) -> None:
        """Start/stop status polling every ``TAB_POLL_MS`` while Box tab is selected."""
        self._box_tab_visible = bool(visible)
        if not self._box_tab_visible:
            self._cancel_poll_timer()
            return
        if self.client is None:
            return
        self._cancel_poll_timer()
        self._poll_after_id = self._root.after(0, self._poll_tick)

    def _cancel_poll_timer(self) -> None:
        if self._poll_after_id is not None:
            try:
                self._root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None

    def disconnect(self) -> None:
        """Stop polling and clear box HTTP session (e.g. joystick Disconnect)."""
        self._on_disconnect()

    def _on_disconnect(self) -> None:
        self._cancel_poll_timer()
        self.client = None
        self._last_status = {}
        self._set_controls_enabled(False)
        self._sync_toggle_buttons({})
        self.usb_cam_var.set(False)
        self._video_source = "mipi"
        self._stop_video_player()

    def shutdown(self) -> None:
        """Stop polling (e.g. window close)."""
        self._stop_video_player()
        self.disconnect()

    def _poll_tick(self) -> None:
        self._poll_after_id = None
        if not self._box_tab_visible or self.client is None:
            return
        d = self.client.get_status()
        if not d.get("ok"):
            self._on_disconnect()
            return
        self._apply_status(d)
        self._set_controls_enabled(True)
        self._poll_after_id = self._root.after(TAB_POLL_MS, self._poll_tick)

    def _apply_status(self, d: dict) -> None:
        prev_cam = bool(self._last_status.get("camera_streaming"))
        if d.get("ok"):
            self._last_status = d
        self._sync_toggle_buttons(d)
        if not d.get("ok"):
            return
        if not d.get("hardware_ok") and d.get("box_io_enabled", True):
            return

        io_on = bool(d.get("box_io_enabled", True))
        io_keys = {
            "sensor_kind",
            "temperature_c",
            "humidity_percent",
            "pressure_hpa",
            "box_battery_v",
            "drone_battery_v",
            "env_error",
            "battery_error",
            "led_on",
            "servo_active",
            "drone_power_on",
        }

        def fmt_val(key: str, v) -> str:
            if key == "box_io_enabled":
                return "on" if v else "off (uart CRSF)"
            if not io_on and key in io_keys:
                return "—"
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

        cam_on = bool(d.get("camera_streaming"))
        if prev_cam and not cam_on:
            self._stop_video_player()

    def _selected_camera_source(self) -> str:
        return "usb" if bool(self.usb_cam_var.get()) else "mipi"

    def _play_argv(self, gst_bin: str, source: str = "mipi") -> list[str]:
        """RTP UDP viewer: H264 for MIPI, JPEG for USB."""
        src = "usb" if (source or "").strip().lower() == "usb" else "mipi"
        if sys.platform == "darwin":
            if src == "usb":
                sink = ["videoconvert", "!", "osxvideosink", "sync=false"]
            else:
                sink = [
                    "videoconvert",
                    "!",
                    "video/x-raw,format=UYVY",
                    "!",
                    "osxvideosink",
                    "sync=false",
                ]
        else:
            sink = ["videoconvert", "!", "autovideosink", "sync=false"]
        if src == "usb":
            return [
                gst_bin,
                "udpsrc",
                f"port={VIDEO_STREAM_PORT}",
                'caps=application/x-rtp,encoding-name=JPEG,payload=26',
                "!",
                "rtpjpegdepay",
                "!",
                "jpegdec",
                "!",
                *sink,
            ]
        return [
            gst_bin,
            "udpsrc",
            f"port={VIDEO_STREAM_PORT}",
            'caps=application/x-rtp,payload=96,encoding-name=H264',
            "!",
            "rtph264depay",
            "!",
            "h264parse",
            "!",
            "avdec_h264",
            "!",
            *sink,
        ]

    def _stop_video_player(self) -> None:
        proc = self._video_proc
        self._video_proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

    def _set_controls_enabled(self, on: bool) -> None:
        self._controls_enabled = bool(on)
        st = "normal" if on else "disabled"
        self.cam_toggle_b.configure(state=st)
        io = on and bool(self._last_status.get("box_io_enabled", True))
        io_st = "normal" if io else "disabled"
        for b in (self.led_toggle_b, self.servo_toggle_b, self.drone_power_toggle_b):
            b.configure(state=io_st)
        self._sync_usb_cam_checkbox_state()

    def _sync_usb_cam_checkbox_state(self) -> None:
        cam_on = bool(self._last_status.get("camera_streaming"))
        if not self._controls_enabled or cam_on:
            self.usb_cam_cb.configure(state="disabled")
        else:
            self.usb_cam_cb.configure(state="normal")

    def _toggle_on_color(self) -> tuple[str, str]:
        return "seagreen", "darkgreen"

    def _toggle_off_color(self) -> tuple[str, str]:
        return "gray40", "gray35"

    def _sync_toggle_buttons(self, d: dict) -> None:
        cam_on = bool(d.get("camera_streaming"))
        led_on = bool(d.get("led_on"))
        servo_on = bool(d.get("servo_active"))
        drone_on = bool(d.get("drone_power_on"))
        pairs = (
            (self.cam_toggle_b, f"Video: {'on' if cam_on else 'off'}", cam_on),
            (self.led_toggle_b, f"LED: {'on' if led_on else 'off'}", led_on),
            (self.servo_toggle_b, f"Servo: {'run' if servo_on else 'stop'}", servo_on),
            (self.drone_power_toggle_b, f"Drone power: {'on' if drone_on else 'off'}", drone_on),
        )
        for btn, text, active in pairs:
            fg, hover = self._toggle_on_color() if active else self._toggle_off_color()
            btn.configure(text=text, fg_color=fg, hover_color=hover)
        self._sync_usb_cam_checkbox_state()

    def _box_io_on(self) -> bool:
        return bool(self._last_status.get("box_io_enabled", True))

    def _toggle_led(self) -> None:
        if self.client is None or not self._box_io_on():
            return
        on = not bool(self._last_status.get("led_on"))
        self._led(on)

    def _toggle_servo(self) -> None:
        if self.client is None or not self._box_io_on():
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

    def _toggle_drone_power(self) -> None:
        if self.client is None or not self._box_io_on():
            return
        on = not bool(self._last_status.get("drone_power_on"))
        self._drone_power(on)

    def _command_error_text(self, d: dict, fallback: str) -> str:
        parts = [d.get("error"), d.get("hardware_error"), d.get("camera_stream_error")]
        if d.get("led_error"):
            parts.append(f"LED: {d['led_error']}")
        if d.get("servo_error"):
            parts.append(f"Servo: {d['servo_error']}")
        if d.get("drone_power_error"):
            parts.append(f"Drone power: {d['drone_power_error']}")
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

    def _drone_power(self, on: bool) -> None:
        if self.client is None:
            return
        d = self.client.set_drone_power(on)
        if not d.get("ok"):
            tk_messagebox.showerror(
                "Box",
                self._command_error_text(d, "Drone power command failed"),
                parent=self._root,
            )
            return
        self._apply_status(d)

    def _start_video_player(self, source: str = "mipi") -> bool:
        gst_bin = self._find_video_player()
        if not gst_bin:
            tk_messagebox.showinfo(
                "Box",
                "gst-launch-1.0 not found.\n"
                "Install GStreamer: brew install gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav",
                parent=self._root,
            )
            return False
        self._stop_video_player()
        self._video_source = "usb" if source == "usb" else "mipi"
        try:
            self._video_proc = subprocess.Popen(
                self._play_argv(gst_bin, self._video_source),
                start_new_session=True,
            )
        except OSError as e:
            self._video_proc = None
            tk_messagebox.showerror("Box", str(e), parent=self._root)
            return False
        return True

    def _cam(self, streaming: bool) -> None:
        if self.client is None:
            return
        source = self._selected_camera_source()
        if streaming:
            if not self._find_video_player():
                tk_messagebox.showinfo(
                    "Box",
                    "gst-launch-1.0 not found.\n"
                    "Install GStreamer: brew install gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-libav",
                    parent=self._root,
                )
                return
        else:
            self._stop_video_player()
        d = self.client.set_camera_streaming(streaming, source=source)
        if not d.get("ok"):
            tk_messagebox.showerror(
                "Box",
                self._command_error_text(d, "Camera command failed"),
                parent=self._root,
            )
            if streaming:
                self._stop_video_player()
            return
        self._apply_status(d)
        if streaming and bool(d.get("camera_streaming")):
            active = (d.get("camera_source") or source or "mipi").strip().lower()
            self._start_video_player(active)
        elif not streaming:
            self._stop_video_player()
