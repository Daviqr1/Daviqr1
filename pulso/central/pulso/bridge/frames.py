"""BLE bridge frame codec (watch -> desk peripheral).

Connect IQ BLE writes are capped at 20 bytes, one outstanding at a time, with no MTU exchange,
so the capture app streams tiny self-describing frames. Layout of every frame:

    byte 0      seq   u8, increments per frame, wraps (peripheral acks the last one it got)
    byte 1      type  u8
    bytes 2..   payload (<= 18 bytes), little-endian

Types (dt = ms since the last SYNC, u24 -> up to 4.6 h; the watch re-syncs every 30 s):

    0x00 SYNC       epoch_s u32 | ms u16 | session u32                        (10 B)
    0x01 RR         dt u24 (last beat) | n u8 | rr u16 * n   (n <= 6)         (<= 16 B)
    0x02 ACC_KEY    dt u24 | hz u8 | x i16 | y i16 | z i16   (milli-g)        (10 B)
    0x03 ACC_DELTA  k u8 | (dx i8, dy i8, dz i8) * k   (k <= 5, vs previous)  (<= 16 B)
    0x04 FEAT       dt u24 | n u8 | (enmo u16, mad u16) * n  (n <= 3, 1-s)    (<= 16 B)
    0x05 HR         dt u24 | bpm u8                                            (4 B)
    0x06 MARK       dt u24 | code u8   (1 estresse, 2 café, 3 reunião começou, 4 reunião acabou)
    0x07 BAT        pct u8 | drain_x10 u8 (%/h * 10)

Peripheral -> watch notification (ctrl characteristic): last_seq u8 | accel_hz_hint u8.
A delta beyond ±127 mG forces a new ACC_KEY, so decoding never accumulates error.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

SYNC, RR, ACC_KEY, ACC_DELTA, FEAT, HR, MARK, BAT = range(8)
MAX_FRAME = 20
MARK_LABELS = {1: "estresse", 2: "café", 3: "reunião começou", 4: "reunião acabou"}


def _u24(b: bytes) -> int:
    return b[0] | (b[1] << 8) | (b[2] << 16)


def _p24(v: int) -> bytes:
    if not 0 <= v < 1 << 24:
        raise ValueError("dt out of u24 range; re-SYNC more often")
    return bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))


# ------------------------------------------------------------------------------ encoding
# (the watch implements the same in Monkey C; the Python encoder exists for tests and replay)


class Encoder:
    def __init__(self) -> None:
        self.seq = 0
        self.prev: tuple[int, int, int] | None = None

    def _frame(self, typ: int, payload: bytes) -> bytes:
        f = bytes((self.seq & 0xFF, typ)) + payload
        if len(f) > MAX_FRAME:
            raise ValueError(f"frame too long: {len(f)}")
        self.seq = (self.seq + 1) & 0xFF
        return f

    def sync(self, epoch_ms: int, session: int) -> bytes:
        return self._frame(SYNC, struct.pack("<IHI", epoch_ms // 1000, epoch_ms % 1000, session))

    def rr(self, dt_last: int, rr: list[int]) -> list[bytes]:
        out = []
        # chunks of 6; each chunk's dt is the time of its own last beat
        cum = 0
        tail = [sum(rr[i + 1 :]) for i in range(len(rr))]
        for k in range(0, len(rr), 6):
            part = rr[k : k + 6]
            last_idx = k + len(part) - 1
            dt = dt_last - tail[last_idx]
            out.append(self._frame(RR, _p24(dt) + bytes((len(part),)) + struct.pack(f"<{len(part)}H", *part)))
            cum += len(part)
        return out

    def accel(self, dt0: int, hz: int, xyz: list[tuple[int, int, int]]) -> list[bytes]:
        out: list[bytes] = []
        pending: list[tuple[int, int, int]] = []

        def flush():
            if pending:
                body = b"".join(struct.pack("<bbb", *d) for d in pending)
                out.append(self._frame(ACC_DELTA, bytes((len(pending),)) + body))
                pending.clear()

        prev = None
        for i, s in enumerate(xyz):
            if prev is not None:
                d = (s[0] - prev[0], s[1] - prev[1], s[2] - prev[2])
                if all(-127 <= v <= 127 for v in d):
                    pending.append(d)
                    if len(pending) == 5:
                        flush()
                    prev = s
                    continue
            flush()
            dt = dt0 + int(round(i * 1000 / hz))
            out.append(self._frame(ACC_KEY, _p24(dt) + bytes((hz,)) + struct.pack("<hhh", *s)))
            prev = s
        flush()
        return out

    def feat(self, dt0: int, pairs: list[tuple[int, int]]) -> list[bytes]:
        out = []
        for k in range(0, len(pairs), 3):
            part = pairs[k : k + 3]
            body = b"".join(struct.pack("<HH", min(e, 65535), min(m, 65535)) for e, m in part)
            out.append(self._frame(FEAT, _p24(dt0 + k * 1000) + bytes((len(part),)) + body))
        return out

    def hr(self, dt: int, bpm: int) -> bytes:
        return self._frame(HR, _p24(dt) + bytes((max(0, min(255, bpm)),)))

    def mark(self, dt: int, code: int) -> bytes:
        return self._frame(MARK, _p24(dt) + bytes((code,)))

    def bat(self, pct: int, drain_x10: int) -> bytes:
        return self._frame(BAT, bytes((max(0, min(100, pct)), max(0, min(255, drain_x10)))))


# ------------------------------------------------------------------------------ decoding


@dataclass
class Decoded:
    rr: list[tuple[int, list[int]]] = field(default_factory=list)        # (t_last_ms, [rr])
    accel: list[tuple[int, float, list[tuple[int, int, int]]]] = field(default_factory=list)  # (t0_ms, hz, samples)
    feat: list[tuple[int, list[float], list[float]]] = field(default_factory=list)            # (t0_ms, enmo, mad)
    hr: list[tuple[int, int]] = field(default_factory=list)
    marks: list[tuple[int, str]] = field(default_factory=list)
    battery: tuple[int, float] | None = None
    lost_frames: int = 0
    bad_frames: int = 0
    session: int | None = None

    def empty(self) -> bool:
        return not (self.rr or self.accel or self.feat or self.hr or self.marks or self.battery)


class Decoder:
    """Stateful: keeps the time base, the last accel sample and sequence tracking across frames."""

    def __init__(self) -> None:
        self.base_ms: int | None = None
        self.session: int | None = None
        self.last_seq: int | None = None
        self.acc_prev: tuple[int, int, int] | None = None
        self.acc_t: float | None = None     # ms timestamp of acc_prev
        self.acc_hz: float = 25.0
        self.out = Decoded()

    def take(self) -> Decoded:
        d, self.out = self.out, Decoded(session=self.session)
        return d

    def feed(self, frame: bytes) -> None:
        if len(frame) < 2 or len(frame) > MAX_FRAME:
            self.out.bad_frames += 1
            return
        seq, typ, p = frame[0], frame[1], frame[2:]
        if self.last_seq is not None:
            gap = (seq - self.last_seq - 1) & 0xFF
            if gap:
                self.out.lost_frames += gap
                if typ == ACC_DELTA:   # cannot apply deltas after a loss: wait for the next key
                    self.acc_prev = None
        self.last_seq = seq
        try:
            self._dispatch(typ, p)
        except (struct.error, IndexError, ValueError):
            self.out.bad_frames += 1

    def _t(self, dt: int) -> int:
        if self.base_ms is None:
            raise ValueError("no SYNC yet")
        return self.base_ms + dt

    def _dispatch(self, typ: int, p: bytes) -> None:
        if typ == SYNC:
            s, ms, sess = struct.unpack("<IHI", p[:10])
            self.base_ms = s * 1000 + ms
            if self.session is not None and sess != self.session:
                self.acc_prev = None
            self.session = self.out.session = sess
        elif typ == RR:
            t = self._t(_u24(p[0:3]))
            n = p[3]
            if n == 0 or n > 6:
                raise ValueError("bad rr count")
            rr = list(struct.unpack(f"<{n}H", p[4 : 4 + 2 * n]))
            self.out.rr.append((t, rr))
        elif typ == ACC_KEY:
            t = self._t(_u24(p[0:3]))
            hz = p[3] or 25
            x, y, z = struct.unpack("<hhh", p[4:10])
            self.acc_prev, self.acc_t, self.acc_hz = (x, y, z), float(t), float(hz)
            self._acc_append(t, hz, (x, y, z))
        elif typ == ACC_DELTA:
            k = p[0]
            if self.acc_prev is None or self.acc_t is None:
                return
            for i in range(k):
                dx, dy, dz = struct.unpack("<bbb", p[1 + 3 * i : 4 + 3 * i])
                s = (self.acc_prev[0] + dx, self.acc_prev[1] + dy, self.acc_prev[2] + dz)
                self.acc_t += 1000.0 / self.acc_hz
                self.acc_prev = s
                self._acc_append(int(round(self.acc_t)), self.acc_hz, s)
        elif typ == FEAT:
            t = self._t(_u24(p[0:3]))
            n = p[3]
            vals = [struct.unpack("<HH", p[4 + 4 * i : 8 + 4 * i]) for i in range(n)]
            self.out.feat.append((t, [float(v[0]) for v in vals], [float(v[1]) for v in vals]))
        elif typ == HR:
            self.out.hr.append((self._t(_u24(p[0:3])), p[3]))
        elif typ == MARK:
            self.out.marks.append((self._t(_u24(p[0:3])), MARK_LABELS.get(p[3], f"marca_{p[3]}")))
        elif typ == BAT:
            self.out.battery = (p[0], p[1] / 10.0)
        else:
            raise ValueError(f"unknown type {typ}")

    def _acc_append(self, t: int, hz: float, s: tuple[int, int, int]) -> None:
        chunks = self.out.accel
        if chunks:
            t0, chz, samples = chunks[-1]
            expected = t0 + len(samples) * 1000.0 / chz
            if chz == hz and abs(t - expected) <= 1000.0 / hz:
                samples.append(s)
                return
        chunks.append((t, float(hz), [s]))


def to_batch(dec: Decoded, device: str, seq: int, now_s: int) -> dict:
    """Decoded frames -> protocol v1 batch (src 'ble')."""
    import base64

    import numpy as np

    b: dict = {"v": 1, "d": device, "q": seq, "src": "ble", "t": now_s,
               "sid": f"ble-{dec.session}" if dec.session is not None else None}
    if dec.rr:
        b["rr"] = [[t, rr] for t, rr in dec.rr]
    if dec.feat:
        b["acc"] = [[t, t // 1000, e, m] for t, e, m in dec.feat]
    if dec.accel:
        raw = []
        for t0, hz, samples in dec.accel:
            a = np.asarray(samples, dtype="<i2")
            raw.append([t0, hz] + [base64.b64encode(a[:, i].tobytes()).decode() for i in range(3)])
        b["raw"] = raw
    if dec.hr:
        t0 = dec.hr[0][0] // 1000
        span = dec.hr[-1][0] // 1000 - t0 + 1
        if 0 < span <= 3600:
            vals: list[int | None] = [None] * span
            for t, v in dec.hr:
                vals[t // 1000 - t0] = v
            b["hr1"] = [t0, vals]
    if dec.marks:
        b["ev"] = [[t // 1000, "mark", label] for t, label in dec.marks]
    if dec.battery:
        b["bat"] = [now_s, float(dec.battery[0]), None]
    return {k: v for k, v in b.items() if v is not None}
