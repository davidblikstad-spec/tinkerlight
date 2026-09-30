"""DMX output drivers.

Every driver takes a 512-byte universe via send(). Network drivers are pure
stdlib; the serial drivers talk to the tty directly through termios/ioctl so
no pyserial is needed on the board.
"""
from __future__ import annotations

import fcntl
import logging
import os
import socket
import struct
import termios
import time
import uuid

log = logging.getLogger(__name__)


class NullOutput:
    kind = "none"

    def send(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return "No output (preview only)"


# --------------------------------------------------------------- Art-Net ----
def artnet_packet(data: bytes, seq: int, net: int, subnet: int, universe: int) -> bytes:
    length = len(data) + (len(data) & 1)
    return (b"Art-Net\x00"
            + struct.pack("<H", 0x5000)          # OpDmx
            + struct.pack(">H", 14)              # protocol version
            + bytes([seq & 0xFF, 0])             # sequence, physical
            + bytes([((subnet & 0x0F) << 4) | (universe & 0x0F), net & 0x7F])
            + struct.pack(">H", length)
            + data.ljust(length, b"\x00"))


class ArtNetOutput:
    kind = "artnet"

    def __init__(self, host: str = "255.255.255.255", net: int = 0,
                 subnet: int = 0, universe: int = 0, port: int = 6454):
        self.addr = (host, port)
        self.net, self.subnet, self.universe = net, subnet, universe
        self.seq = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def send(self, data: bytes) -> None:
        self.seq = self.seq % 255 + 1            # 0 disables sequencing
        pkt = artnet_packet(data, self.seq, self.net, self.subnet, self.universe)
        self.sock.sendto(pkt, self.addr)

    def close(self) -> None:
        self.sock.close()

    def describe(self) -> str:
        return "Art-Net %d:%d:%d -> %s" % (self.net, self.subnet, self.universe, self.addr[0])


# ------------------------------------------------------------ sACN E1.31 ----
ACN_ID = b"ASC-E1.17\x00\x00\x00"


def sacn_packet(data: bytes, seq: int, universe: int, cid: bytes,
                source: str = "tinkerlight", priority: int = 100) -> bytes:
    dmx = b"\x00" + data                         # start code + slots
    n = len(dmx)
    dmp = (struct.pack(">H", 0x7000 | (10 + n))  # flags+length
           + b"\x02\xa1"                         # vector, addr/data type
           + struct.pack(">HHH", 0, 1, n)        # first addr, increment, count
           + dmx)
    framing = (struct.pack(">HI", 0x7000 | (77 + len(dmp)), 0x00000002)
               + source.encode("utf-8")[:63].ljust(64, b"\x00")
               + bytes([priority])
               + struct.pack(">H", 0)            # sync address
               + bytes([seq & 0xFF, 0])          # sequence, options
               + struct.pack(">H", universe)
               + dmp)
    root = (struct.pack(">HH", 0x0010, 0x0000) + ACN_ID
            + struct.pack(">HI", 0x7000 | (22 + len(framing)), 0x00000004)
            + cid + framing)
    return root


class SacnOutput:
    kind = "sacn"

    def __init__(self, universe: int = 1, host: str = "", priority: int = 100):
        self.universe = max(1, min(63999, universe))
        self.priority = priority
        # Unicast when a host is given, otherwise the standard multicast group.
        self.host = host or "239.255.%d.%d" % (self.universe >> 8, self.universe & 0xFF)
        self.cid = uuid.uuid4().bytes
        self.seq = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)

    def send(self, data: bytes) -> None:
        self.seq = (self.seq + 1) & 0xFF
        pkt = sacn_packet(data, self.seq, self.universe, self.cid, priority=self.priority)
        self.sock.sendto(pkt, (self.host, 5568))

    def close(self) -> None:
        self.sock.close()

    def describe(self) -> str:
        return "sACN universe %d -> %s" % (self.universe, self.host)


# ---------------------------------------------------------------- serial ----
# struct termios2 (asm-generic): 4 x tcflag_t, c_line, c_cc[19], ispeed, ospeed
TCGETS2 = 0x802C542A
TCSETS2 = 0x402C542B
BOTHER = 0o010000
CBAUD = 0o010017
TIOCSBRK = 0x5427
TIOCCBRK = 0x5428
_T2 = "4IB19BII"


def _open_raw(path: str, baud: int, two_stop: bool) -> int:
    """Open a tty raw at an arbitrary baud rate (termios2/BOTHER)."""
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    try:
        buf = bytearray(struct.calcsize(_T2))
        fcntl.ioctl(fd, TCGETS2, buf)
        f = list(struct.unpack(_T2, buf))
        iflag, oflag, cflag, lflag = f[0:4]
        iflag = 0
        oflag = 0
        lflag = 0
        cflag &= ~(CBAUD | termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
        cflag |= BOTHER | termios.CS8 | termios.CLOCAL | termios.CREAD
        if two_stop:
            cflag |= termios.CSTOPB
        f[0:4] = [iflag, oflag, cflag, lflag]
        f[-2] = f[-1] = baud
        fcntl.ioctl(fd, TCSETS2, struct.pack(_T2, *f))
    except Exception:
        os.close(fd)
        raise
    return fd


class EnttecProOutput:
    """Enttec DMX USB Pro (and compatibles, e.g. DMXking ultraDMX Pro)."""
    kind = "enttec_pro"

    def __init__(self, port: str = "/dev/ttyUSB0"):
        self.port = port
        self.fd = _open_raw(port, 57600, False)

    def send(self, data: bytes) -> None:
        payload = b"\x00" + data
        pkt = (b"\x7e\x06" + struct.pack("<H", len(payload)) + payload + b"\xe7")
        os.write(self.fd, pkt)

    def close(self) -> None:
        os.close(self.fd)

    def describe(self) -> str:
        return "Enttec USB Pro on %s" % self.port


class UartDmxOutput:
    """Raw DMX on a UART: FTDI 'Open DMX' dongles, or the Tinker Board's own
    UART pins wired to an RS-485 transceiver (MAX485 etc., DE/RE tied high)."""
    kind = "uart"

    def __init__(self, port: str = "/dev/ttyUSB0"):
        self.port = port
        self.fd = _open_raw(port, 250000, True)

    def send(self, data: bytes) -> None:
        termios.tcdrain(self.fd)                 # previous frame fully out
        fcntl.ioctl(self.fd, TIOCSBRK)
        time.sleep(0.00015)                      # break >= 88 us
        fcntl.ioctl(self.fd, TIOCCBRK)
        time.sleep(0.00002)                      # mark-after-break >= 8 us
        os.write(self.fd, b"\x00" + data)

    def close(self) -> None:
        os.close(self.fd)

    def describe(self) -> str:
        return "Raw DMX UART on %s" % self.port


def make_output(cfg: dict):
    kind = cfg.get("type", "artnet")
    if kind == "artnet":
        return ArtNetOutput(cfg.get("host") or "255.255.255.255",
                            int(cfg.get("net", 0)), int(cfg.get("subnet", 0)),
                            int(cfg.get("universe", 0)))
    if kind == "sacn":
        return SacnOutput(int(cfg.get("sacn_universe", 1)), cfg.get("sacn_host", ""),
                          int(cfg.get("priority", 100)))
    if kind == "enttec_pro":
        return EnttecProOutput(cfg.get("serial_port", "/dev/ttyUSB0"))
    if kind == "uart":
        return UartDmxOutput(cfg.get("serial_port", "/dev/ttyUSB0"))
    return NullOutput()
