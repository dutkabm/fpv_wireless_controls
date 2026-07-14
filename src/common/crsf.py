"""CRSF framing, RC channel packets, and inbound telemetry parsing (from legacy minirex scripts)."""

from __future__ import annotations

from enum import IntEnum
from typing import Dict, List, MutableMapping, Optional, Union

CRSF_SYNC_BYTE = 0xC8  # Flight controller / common sync used for RC TX
CRSF_ADDRESS_FLIGHT_CONTROLLER = 0xC8
CRSF_ADDRESS_RADIO_TRANSMITTER = 0xEA
CRSF_ADDRESS_CRSF_RECEIVER = 0xEC
CRSF_ADDRESS_CRSF_TRANSMITTER = 0xEE
CRSF_ADDRESS_BROADCAST = 0x00
CRSF_MAX_PACKET_SIZE = 64

# Destination/source addresses that can lead a valid CRSF frame on the wire.
# FC telemetry to a radio/RX often starts with 0xEA (not 0xC8).
CRSF_FRAME_ADDRESSES = frozenset(
    {
        CRSF_ADDRESS_BROADCAST,
        CRSF_ADDRESS_FLIGHT_CONTROLLER,
        CRSF_ADDRESS_RADIO_TRANSMITTER,
        CRSF_ADDRESS_CRSF_RECEIVER,
        CRSF_ADDRESS_CRSF_TRANSMITTER,
    }
)


def is_crsf_frame_address(byte: int) -> bool:
    return (byte & 0xFF) in CRSF_FRAME_ADDRESSES


class CRSFPacketType(IntEnum):
    GPS = 0x02
    BATTERY_SENSOR = 0x08
    LINK_STATISTICS = 0x14
    ATTITUDE = 0x1E
    FLIGHT_MODE = 0x21
    DEVICE_PING = 0x28
    DEVICE_INFO = 0x29
    REQUEST_SETTINGS = 0x2A
    CHANNELS_INFO = 0x2F
    RC_CHANNELS_PACKED = 0x16


def crc8_dvb_s2(data: Union[bytes, bytearray, List[int]]) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0xD5) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def crsf_validate_frame(frame: Union[bytes, bytearray]) -> bool:
    if len(frame) < 4:
        return False
    if not is_crsf_frame_address(frame[0]):
        return False
    length = frame[1]
    if length != len(frame) - 2:
        return False
    return crc8_dvb_s2(frame[2:-1]) == frame[-1]


def build_crsf_packet(dest_addr: int, type_byte: int, payload: bytes = b"") -> bytes:
    """Build ``[dest][len][type][payload…][crc]``."""
    body = bytes([int(type_byte) & 0xFF]) + bytes(payload)
    length = len(body) + 1
    packet = bytearray([int(dest_addr) & 0xFF, length]) + body
    packet.append(crc8_dvb_s2(packet[2:]))
    return bytes(packet)


def build_device_info_packet(
    *,
    dest: int = CRSF_ADDRESS_FLIGHT_CONTROLLER,
    origin: int = CRSF_ADDRESS_CRSF_RECEIVER,
    name: str = "PiBridgeRX",
) -> bytes:
    """Reply used when the FC DEVICE_PINGs the receiver address (0xEC)."""
    name_b = name.encode("ascii", errors="replace")[:14] + b"\x00"
    payload = bytearray()
    payload.append(int(dest) & 0xFF)
    payload.append(int(origin) & 0xFF)
    payload.extend(name_b)
    payload.extend((0x50494252).to_bytes(4, "big"))  # serial 'PIBR'
    payload.extend((0x00000001).to_bytes(4, "big"))  # hardware id
    payload.extend((0x00010000).to_bytes(4, "big"))  # firmware id
    payload.append(0)  # parameter count
    payload.append(1)  # parameter version
    return build_crsf_packet(dest, CRSFPacketType.DEVICE_INFO, bytes(payload))


def device_info_reply_for_ping(packet: Union[bytes, bytearray]) -> Optional[bytes]:
    """If ``packet`` is a DEVICE_PING for us (RX/radio/broadcast), return DEVICE_INFO."""
    if len(packet) < 4 or packet[2] != CRSFPacketType.DEVICE_PING:
        return None
    payload = packet[3:-1]
    queried = int(payload[0]) if payload else CRSF_ADDRESS_BROADCAST
    if queried not in (
        CRSF_ADDRESS_BROADCAST,
        CRSF_ADDRESS_CRSF_RECEIVER,
        CRSF_ADDRESS_RADIO_TRANSMITTER,
    ):
        return None
    origin = CRSF_ADDRESS_CRSF_RECEIVER if queried == CRSF_ADDRESS_BROADCAST else queried
    return build_device_info_packet(dest=CRSF_ADDRESS_FLIGHT_CONTROLLER, origin=origin)


def pack_crsf_to_bytes(channels: List[int]) -> bytes:
    """Pack 16 CRSF 11-bit channel values into a byte stream."""
    if len(channels) != 16:
        raise ValueError("CRSF must have 16 channels")
    result = bytearray()
    bit_buffer = 0
    bits_in_buffer = 0
    for ch in channels:
        bit_buffer |= (int(ch) & 0x7FF) << bits_in_buffer
        bits_in_buffer += 11
        while bits_in_buffer >= 8:
            result.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bits_in_buffer -= 8
    if bits_in_buffer > 0:
        result.append(bit_buffer & 0xFF)
    return bytes(result)


def channels_crsf_to_packet(channels: List[int]) -> bytes:
    """Build a CRSF RC_CHANNELS_PACKED frame from 16 tick values."""
    payload = bytearray([CRSFPacketType.RC_CHANNELS_PACKED])
    payload += pack_crsf_to_bytes(channels)
    length = len(payload) + 1
    packet = bytearray([CRSF_SYNC_BYTE, length]) + payload
    packet.append(crc8_dvb_s2(packet[2:]))
    return bytes(packet)


def map_pwm_us_to_crsf(us_pwm: int) -> int:
    """Map RC PWM µs (1000–2000) to CRSF 0x16 legacy ticks (172–1811)."""
    pwm = max(1000, min(2000, int(us_pwm)))
    ticks = 172.0 + (pwm - 988) * (1811 - 172) / (2012 - 988)
    return max(172, min(1811, int(round(ticks))))


def pwm_channels_to_crsf_packet(channels_1000_2000: List[int]) -> bytes:
    """Clamp 16 PWM µs values and emit one CRSF RC channels packet."""
    capped = [max(1000, min(2000, int(c))) for c in channels_1000_2000]
    crsfs = [map_pwm_us_to_crsf(c) for c in capped]
    return channels_crsf_to_packet(crsfs)


def _packet_type_name(type_byte: int) -> str:
    try:
        return CRSFPacketType(type_byte).name
    except ValueError:
        return f"0x{type_byte:02X}"


def parse_crsf_telemetry(packet: Union[bytes, bytearray], into: MutableMapping[str, object]) -> bool:
    """Merge known CRSF telemetry fields from ``packet`` into ``into``.

    Returns True if a known telemetry type was applied (not just validated).
    """
    if len(packet) < 4 or not is_crsf_frame_address(packet[0]):
        return False
    type_byte = packet[2]
    payload = packet[3:-1]
    if type_byte == CRSFPacketType.LINK_STATISTICS and len(payload) >= 10:
        into["Uplink RSSI 1"] = payload[0]
        into["Uplink RSSI 2"] = payload[1]
        into["Uplink LQ"] = payload[2]
        into["Uplink SNR"] = payload[3]
        into["Active Antenna"] = payload[4]
        into["RF Mode"] = payload[5]
        into["Uplink TX Power"] = payload[6]
        into["Downlink RSSI"] = payload[7]
        into["Downlink LQ"] = payload[8]
        into["Downlink SNR"] = payload[9]
        return True
    if type_byte == CRSFPacketType.BATTERY_SENSOR and len(payload) >= 8:
        voltage = int.from_bytes(payload[0:2], byteorder="little") / 100.0
        current = int.from_bytes(payload[2:4], byteorder="little") / 100.0
        capacity = int.from_bytes(payload[4:7], byteorder="little")
        remaining = payload[7]
        into["Voltage"] = f"{voltage:.2f} V"
        into["Current"] = f"{current:.2f} A"
        into["Capacity"] = f"{capacity} mAh"
        into["Remaining"] = f"{remaining} %"
        return True
    if type_byte == CRSFPacketType.GPS and len(payload) >= 15:
        latitude = int.from_bytes(payload[0:4], byteorder="little", signed=True) / 1e7
        longitude = int.from_bytes(payload[4:8], byteorder="little", signed=True) / 1e7
        ground_speed = int.from_bytes(payload[8:10], byteorder="little")
        heading = int.from_bytes(payload[10:12], byteorder="little") / 100.0
        altitude = int.from_bytes(payload[12:15], byteorder="little", signed=True) / 100.0
        into["Latitude"] = f"{latitude:.7f}"
        into["Longitude"] = f"{longitude:.7f}"
        into["Speed"] = f"{ground_speed} km/h"
        into["Heading"] = f"{heading:.2f}°"
        into["Altitude"] = f"{altitude:.2f} m"
        return True
    if type_byte == CRSFPacketType.ATTITUDE and len(payload) >= 6:
        # int16 radians × 10000
        pitch = int.from_bytes(payload[0:2], byteorder="little", signed=True) / 10000.0
        roll = int.from_bytes(payload[2:4], byteorder="little", signed=True) / 10000.0
        yaw = int.from_bytes(payload[4:6], byteorder="little", signed=True) / 10000.0
        into["Pitch"] = f"{pitch:.3f} rad"
        into["Roll"] = f"{roll:.3f} rad"
        into["Yaw"] = f"{yaw:.3f} rad"
        return True
    if type_byte == CRSFPacketType.FLIGHT_MODE and len(payload) >= 1:
        end = payload.find(0)
        raw = payload if end < 0 else payload[:end]
        mode = raw.decode("utf-8", errors="replace").strip()
        if mode:
            into["Flight Mode"] = mode
            return True
        return False
    return False


class CrsfSerialReader:
    """Accumulate bytes from a CRSF serial port and dispatch validated telemetry frames."""

    def __init__(self, telemetry: Optional[MutableMapping[str, object]] = None) -> None:
        self._buffer = bytearray()
        self.telemetry: Dict[str, object] = telemetry if telemetry is not None else {}
        self.bytes_fed = 0
        self.frames_ok = 0
        self.frames_bad_crc = 0
        self.frames_telem = 0
        self.sync_skips = 0
        self.last_type_name = ""
        self.last_types: Dict[str, int] = {}
        self.addr_hits: Dict[str, int] = {}
        self.pending_replies: List[bytes] = []
        self._recent_raw = bytearray()
        self._recent_raw_max = 64

    @property
    def buffer_len(self) -> int:
        return len(self._buffer)

    def reset_stats(self) -> None:
        self.bytes_fed = 0
        self.frames_ok = 0
        self.frames_bad_crc = 0
        self.frames_telem = 0
        self.sync_skips = 0
        self.last_type_name = ""
        self.last_types.clear()
        self.addr_hits.clear()
        self.pending_replies.clear()
        self._recent_raw.clear()

    def take_replies(self) -> List[bytes]:
        out = list(self.pending_replies)
        self.pending_replies.clear()
        return out

    def recent_raw_hex(self) -> str:
        return bytes(self._recent_raw).hex(" ") if self._recent_raw else ""

    def feed(self, data: bytes) -> int:
        """Feed UART bytes. Returns count of validated CRSF frames in this chunk."""
        if not data:
            return 0
        self.bytes_fed += len(data)
        self._recent_raw.extend(data)
        if len(self._recent_raw) > self._recent_raw_max:
            del self._recent_raw[: len(self._recent_raw) - self._recent_raw_max]
        self._buffer.extend(data)
        frames = 0
        while len(self._buffer) >= 4:
            addr = self._buffer[0]
            if not is_crsf_frame_address(addr):
                self._buffer.pop(0)
                self.sync_skips += 1
                continue
            self.addr_hits[f"0x{addr:02X}"] = self.addr_hits.get(f"0x{addr:02X}", 0) + 1
            length = self._buffer[1]
            if length > CRSF_MAX_PACKET_SIZE or length < 2:
                self._buffer.pop(0)
                self.sync_skips += 1
                continue
            if len(self._buffer) < length + 2:
                break
            packet = bytes(self._buffer[: length + 2])
            if crsf_validate_frame(packet):
                frames += 1
                self.frames_ok += 1
                type_byte = packet[2]
                tname = _packet_type_name(type_byte)
                self.last_type_name = tname
                self.last_types[tname] = self.last_types.get(tname, 0) + 1
                if parse_crsf_telemetry(packet, self.telemetry):
                    self.frames_telem += 1
                reply = device_info_reply_for_ping(packet)
                if reply is not None:
                    self.pending_replies.append(reply)
                    self.telemetry["CRSF Device"] = "ping → DEVICE_INFO"
                del self._buffer[: length + 2]
            else:
                self.frames_bad_crc += 1
                self._buffer.pop(0)
        return frames
