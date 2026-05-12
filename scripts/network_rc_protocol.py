"""Binary UDP framing for Mini Rex-style RC channel payloads (network bridge)."""

from __future__ import annotations

import struct
from typing import List, Optional

# 16 channels × uint16 PWM-style microseconds-ish (1000–2000), same internal range as minirex_*.py
CHANNEL_PACKET_MAGIC = b"MRX1"
CHANNEL_PAYLOAD_LEN = 4 + 32  # magic + 16×uint16

# Fixed ports for network joystick client ↔ Pi bridge (override via CLI on both sides).
DEFAULT_UDP_CHANNEL_PORT = 50000
DEFAULT_HANDSHAKE_TCP_PORT = 50001

# One-shot TCP handshake after connect: client sends magic + LF, bridge replies OK + optional name + LF.
HANDSHAKE_LINE = CHANNEL_PACKET_MAGIC + b"\n"
HANDSHAKE_OK_LINE = b"OK\n"  # legacy; prefer format_handshake_ok()
_HANDSHAKE_OK_MAX_NAME_BYTES = 128


def format_handshake_ok(bridge_name: str) -> bytes:
    """Build handshake reply: OK <sanitized_name>\\n (UTF-8)."""
    n = (bridge_name or "").replace("\r", "").replace("\n", "").replace("\x00", "").strip()
    if not n:
        n = "bridge"
    encoded = n.encode("utf-8", errors="replace")[:_HANDSHAKE_OK_MAX_NAME_BYTES]
    return b"OK " + encoded + b"\n"


def parse_handshake_response(resp: bytes) -> tuple[bool, str]:
    """If first line is OK or OK <name>, return (True, bridge_name_or_empty). Else (False, '')."""
    if not resp:
        return False, ""
    first = resp.split(b"\n", 1)[0].strip()
    if first == b"OK":
        return True, ""
    if first.startswith(b"OK "):
        return True, first[3:].decode("utf-8", errors="replace")
    return False, ""


def pack_channel_datagram(channels_1000_2000: List[int]) -> bytes:
    if len(channels_1000_2000) != 16:
        raise ValueError("expected 16 channels")
    clipped = tuple(max(1000, min(2000, int(c))) for c in channels_1000_2000)
    return CHANNEL_PACKET_MAGIC + struct.pack("<16H", *clipped)


def unpack_channel_datagram(data: bytes) -> Optional[List[int]]:
    if len(data) != CHANNEL_PAYLOAD_LEN:
        return None
    if data[:4] != CHANNEL_PACKET_MAGIC:
        return None
    return list(struct.unpack("<16H", data[4:]))
