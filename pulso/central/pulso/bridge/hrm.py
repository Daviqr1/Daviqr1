"""Standard Bluetooth Heart Rate Service client (0x180D / measurement 0x2A37).

Two uses:
* a chest strap (Polar H10, Garmin HRM-Pro in BLE mode, Wahoo TICKR) read directly by the
  laptop: ECG-grade RR intervals, independent of every Garmin limit — the reference signal for
  stress/HRV and for calibrating the watch's optical RR;
* a 2-minute probe of the FR255's own "Broadcast Heart Rate": does it set the RR-present flag?
  (community reports say no — this settles it on your watch).

Batches go to the central as protocol v1 with src "strap" (or are stored directly).
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"


@dataclass
class Measurement:
    bpm: int
    rr_ms: list[float]
    contact: bool | None
    energy_kj: int | None


def parse_measurement(data: bytes) -> Measurement:
    """Heart Rate Measurement characteristic (Bluetooth SIG HRS 1.0, section 3.1)."""
    flags = data[0]
    i = 1
    if flags & 0x01:
        bpm = struct.unpack_from("<H", data, i)[0]
        i += 2
    else:
        bpm = data[i]
        i += 1
    contact = bool(flags & 0x02) if flags & 0x04 else None
    energy = None
    if flags & 0x08:
        energy = struct.unpack_from("<H", data, i)[0]
        i += 2
    rr = []
    if flags & 0x10:
        while i + 1 < len(data):
            rr.append(struct.unpack_from("<H", data, i)[0] * 1000.0 / 1024.0)  # 1/1024 s units
            i += 2
    return Measurement(bpm, rr, contact, energy)


class RrAccumulator:
    """Collects notifications into protocol batches every `flush_s` seconds."""

    def __init__(self, device: str, flush_s: float = 30.0, src: str = "strap"):
        self.device = device
        self.flush_s = flush_s
        self.src = src
        self.rr: list[int] = []
        self.last_beat_ms: int | None = None
        self.hr: dict[int, int] = {}
        self.t_flush = time.monotonic()
        self.seq = int(time.time())
        self.saw_rr = False
        self.n_notifications = 0

    def add(self, m: Measurement, now_ms: int | None = None) -> None:
        now_ms = now_ms or int(time.time() * 1000)
        self.n_notifications += 1
        self.hr[now_ms // 1000] = m.bpm
        if m.rr_ms:
            self.saw_rr = True
            for v in m.rr_ms:
                self.rr.append(int(round(v)))
            # notifications arrive right after the last beat they report
            self.last_beat_ms = now_ms

    def due(self) -> bool:
        return time.monotonic() - self.t_flush >= self.flush_s

    def batch(self) -> dict | None:
        self.t_flush = time.monotonic()
        if not self.rr and not self.hr:
            return None
        self.seq += 1
        b: dict = {"v": 1, "d": self.device, "q": self.seq, "src": self.src, "t": int(time.time())}
        if self.rr and self.last_beat_ms:
            b["rr"] = [[self.last_beat_ms, self.rr[i : i + 600]] for i in range(0, len(self.rr), 600)]
            # only the final chunk ends at last_beat_ms; earlier chunks end before it
            if len(b["rr"]) > 1:
                tails, t = [], self.last_beat_ms
                for chunk in reversed(b["rr"]):
                    tails.append([t, chunk[1]])
                    t -= sum(chunk[1])
                b["rr"] = list(reversed(tails))
        if self.hr:
            t0 = min(self.hr)
            span = max(self.hr) - t0 + 1
            b["hr1"] = [t0, [self.hr.get(t0 + i) for i in range(span)]]
        self.rr, self.hr = [], {}
        return b


async def stream(name_contains: str, on_batch: Callable[[dict], None], device_id: str,
                 seconds: float | None = None, flush_s: float = 30.0, src: str = "strap",
                 log: Callable[[str], None] = print) -> RrAccumulator:
    """Connect to the first HR-service device whose name contains `name_contains` and stream."""
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'pulso[bridge]' (bleak/bless) para usar BLE") from e

    log(f"procurando '{name_contains}' com serviço de FC...")
    dev = await BleakScanner.find_device_by_filter(
        lambda d, ad: name_contains.lower() in (d.name or ad.local_name or "").lower(), timeout=20.0)
    if dev is None:
        raise SystemExit(f"nenhum dispositivo com '{name_contains}' no nome (o broadcast/cinta está ligado?)")
    acc = RrAccumulator(device_id, flush_s=flush_s, src=src)

    def handler(_char, data: bytearray):
        acc.add(parse_measurement(bytes(data)))
        if acc.due():
            b = acc.batch()
            if b:
                on_batch(b)

    async with BleakClient(dev) as client:
        log(f"conectado a {dev.name} ({dev.address})")
        await client.start_notify(HR_MEASUREMENT, handler)
        t0 = time.monotonic()
        while seconds is None or time.monotonic() - t0 < seconds:
            await asyncio.sleep(1.0)
        await client.stop_notify(HR_MEASUREMENT)
    b = acc.batch()
    if b:
        on_batch(b)
    log(f"{acc.n_notifications} notificações; RR presente: {'sim' if acc.saw_rr else 'NÃO'}")
    return acc
