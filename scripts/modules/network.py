"""TCP bridge handshake, IPv4 validation, binary UDP RC channel framing, and datagram helpers."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import socket
import struct
from typing import List, Optional, Tuple

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


def tcp_handshake(
    host: str,
    tcp_port: int,
    timeout: float = 5.0,
    *,
    quiet: bool = False,
) -> Tuple[bool, str, str]:
    """Connect to the bridge TCP port, send HANDSHAKE_LINE, expect OK response (optional bridge name)."""
    if not quiet:
        _LOG.info("TCP handshake: connecting to %s:%s", host, tcp_port)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, tcp_port))
        s.sendall(HANDSHAKE_LINE)
        resp = s.recv(64)
        ok, bridge_name = parse_handshake_response(resp)
        if not ok:
            if quiet:
                _LOG.debug("TCP handshake: unexpected reply from %s:%s: %r", host, tcp_port, resp)
            else:
                _LOG.warning(
                    "TCP handshake: unexpected reply from %s:%s: %r",
                    host,
                    tcp_port,
                    resp,
                )
            return False, "Handshake failed (unexpected reply)", ""
        if bridge_name:
            if not quiet:
                _LOG.info("TCP handshake: OK from %s:%s (bridge %r)", host, tcp_port, bridge_name)
        else:
            if not quiet:
                _LOG.info("TCP handshake: OK from %s:%s", host, tcp_port)
        return True, "", bridge_name
    except OSError as e:
        if quiet:
            _LOG.debug("TCP handshake: failed %s:%s: %s", host, tcp_port, e)
        else:
            _LOG.warning("TCP handshake: failed %s:%s: %s", host, tcp_port, e)
        return False, str(e), ""
    finally:
        try:
            s.close()
        except OSError:
            pass


def _ip_with_netmask(ip: str, netmask: str) -> str:
    """Build ``host/prefix`` for ip_network (supports dotted mask or ``/n`` CIDR)."""
    t = netmask.strip()
    if t.startswith("/"):
        return f"{ip}{t}"
    return f"{ip}/{t}"


def validate_ipv4_netmask(text: str) -> Tuple[Optional[str], str]:
    """Return (normalized input string, "") if usable with ip_network(host+mask), else (None, err)."""
    t = text.strip()
    if not t:
        return None, "Set netmask"
    try:
        ipaddress.ip_network(_ip_with_netmask("0.0.0.0", t), strict=False)
    except ValueError as e:
        return None, str(e) or "Invalid netmask"
    return t, ""


def _local_non_loopback_ipv4() -> List[str]:
    """Best-effort list of this host's IPv4 addresses (no 127.0.0.0/8)."""
    seen: set[str] = set()
    out: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.2)
        s.connect(("192.0.2.1", 1))
        ip = s.getsockname()[0]
        if not ip.startswith("127.") and ip not in seen:
            seen.add(ip)
            out.append(ip)
    except OSError:
        pass
    finally:
        s.close()
    return out


def _scan_network_hosts(netmask: str) -> Tuple[List[str], str]:
    """Build ordered unique host IPs to probe from local IPv4 + netmask (one subnet per local interface)."""
    nm, err = validate_ipv4_netmask(netmask)
    if nm is None:
        return [], err
    locals_ = _local_non_loopback_ipv4()
    if not locals_:
        return [], "No local IPv4 found (except loopback); connect to LAN or enter IP manually"
    nets: List[ipaddress.IPv4Network] = []
    seen_net: set[str] = set()
    for ip in locals_:
        try:
            net = ipaddress.ip_network(_ip_with_netmask(ip, nm), strict=False)
        except ValueError:
            continue
        k = str(net)
        if k not in seen_net:
            seen_net.add(k)
            nets.append(net)
    if not nets:
        return [], "Could not derive subnet from local IP and netmask"
    hosts: List[str] = []
    seen_h: set[str] = set()
    for net in nets:
        for h in net.hosts():
            hs = str(h)
            if hs not in seen_h:
                seen_h.add(hs)
                hosts.append(hs)
    return hosts, ""


def scan_tx_bridges(
    handshake_port: int,
    netmask: str,
    *,
    probe_timeout: float = 0.22,
    max_workers: int = 48,
    max_hosts: int = 1024,
) -> Tuple[List[Tuple[str, str]], str]:
    """TCP-probe each host on derived subnet(s) for a bridge handshake.

    Returns ``(sorted [(ip, bridge_name), ...], "")`` on success, or ``([], error)``.
    ``bridge_name`` may be empty if the bridge replied with bare ``OK``.
    """
    hosts, err = _scan_network_hosts(netmask)
    if err:
        return [], err
    if not hosts:
        return [], "No host addresses in subnet to scan (check netmask)"
    if len(hosts) > max_hosts:
        return [], f"Too many hosts to scan ({len(hosts)} > {max_hosts}); use a narrower netmask"

    def probe(ip: str) -> Optional[Tuple[str, str]]:
        ok, _, name = tcp_handshake(ip, handshake_port, timeout=probe_timeout, quiet=True)
        if ok:
            return (ip, name)
        return None

    found: List[Tuple[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(probe, ip) for ip in hosts]
        for fut in concurrent.futures.as_completed(futures):
            try:
                r = fut.result()
                if r is not None:
                    found.append(r)
            except Exception:
                pass
    found.sort(key=lambda t: ipaddress.ip_address(t[0]))
    return found, ""


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
