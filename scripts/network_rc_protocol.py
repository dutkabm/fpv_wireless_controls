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

# One-shot TCP handshake after connect: client sends magic + LF, bridge replies OK + LF.
HANDSHAKE_LINE = CHANNEL_PACKET_MAGIC + b"\n"
HANDSHAKE_OK_LINE = b"OK\n"


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
