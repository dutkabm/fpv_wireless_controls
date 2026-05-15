"""HTTP client for ``raspberry.box_server`` (status + LED / servo / camera commands)."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


API_STATUS = "/api/status"
API_LED = "/api/led"
API_SERVO = "/api/servo"
API_CAMERA = "/api/camera"


class BoxRemoteClient:
    def __init__(
        self,
        host: str,
        port: int = 50502,
        *,
        token: Optional[str] = None,
        timeout: float = 5.0,
    ) -> None:
        self.base = f"http://{host.strip()}:{int(port)}"
        self.token = (token or "").strip() or None
        self.timeout = timeout

    def _headers(self, *, json_body: bool = False) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if json_body:
            h["Content-Type"] = "application/json; charset=utf-8"
        return h

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers=self._headers(json_body=body is not None),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                out = json.loads(raw)
            except json.JSONDecodeError:
                out = {"ok": False, "error": raw or str(e)}
            if isinstance(out, dict) and "error" not in out:
                out["error"] = out.get("hardware_error") or raw or str(e)
            return out
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            return {"ok": False, "error": str(reason) if reason else str(e)}

    def get_status(self) -> Dict[str, Any]:
        return self._request("GET", API_STATUS)

    def set_led(self, on: bool) -> Dict[str, Any]:
        return self._request("POST", API_LED, {"on": on})

    def set_servo(self, active: bool, position: Any = "neutral") -> Dict[str, Any]:
        payload: dict = {"active": active}
        if active and position != "neutral":
            payload["position"] = position
        return self._request("POST", API_SERVO, payload)

    def set_camera_streaming(self, streaming: bool) -> Dict[str, Any]:
        return self._request("POST", API_CAMERA, {"streaming": streaming})
