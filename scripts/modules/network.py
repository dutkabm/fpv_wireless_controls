"""TCP bridge handshake, IPv4 validation, and UDP channel datagram helpers for the joystick client."""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import List, Optional, Tuple

from network_rc_protocol import HANDSHAKE_LINE, pack_channel_datagram, parse_handshake_response

_LOG = logging.getLogger(__name__)


def validate_ipv4_text(text: str) -> Tuple[Optional[str], str]:
    """Return (normalized_ipv4, "") if valid, else (None, error_message)."""
    t = text.strip()
    if not t:
        return None, "Set target IP"
    try:
        return str(ipaddress.IPv4Address(t)), ""
    except ipaddress.AddressValueError:
        return None, "Invalid IPv4 address"


def tcp_handshake(host: str, tcp_port: int, timeout: float = 5.0) -> Tuple[bool, str, str]:
    """Connect to the bridge TCP port, send HANDSHAKE_LINE, expect OK response (optional bridge name)."""
    _LOG.info("TCP handshake: connecting to %s:%s", host, tcp_port)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, tcp_port))
        s.sendall(HANDSHAKE_LINE)
        resp = s.recv(64)
        ok, bridge_name = parse_handshake_response(resp)
        if not ok:
            _LOG.warning(
                "TCP handshake: unexpected reply from %s:%s: %r",
                host,
                tcp_port,
                resp,
            )
            return False, "Handshake failed (unexpected reply)", ""
        if bridge_name:
            _LOG.info("TCP handshake: OK from %s:%s (bridge %r)", host, tcp_port, bridge_name)
        else:
            _LOG.info("TCP handshake: OK from %s:%s", host, tcp_port)
        return True, "", bridge_name
    except OSError as e:
        _LOG.warning("TCP handshake: failed %s:%s: %s", host, tcp_port, e)
        return False, str(e), ""
    finally:
        try:
            s.close()
        except OSError:
            pass


def try_open_udp_socket() -> Tuple[Optional[socket.socket], Optional[str]]:
    """Create an IPv4 UDP socket for channel frames, or return (None, error message)."""
    try:
        return socket.socket(socket.AF_INET, socket.SOCK_DGRAM), None
    except OSError as e:
        return None, str(e)


def send_pwm_datagram(sock: socket.socket, host: str, port: int, pwm: List[int]) -> int:
    """Pack 16 PWM values and send one UDP frame to the bridge. Returns payload length in bytes."""
    pkt = pack_channel_datagram(pwm)
    sock.sendto(pkt, (host, port))
    return len(pkt)
