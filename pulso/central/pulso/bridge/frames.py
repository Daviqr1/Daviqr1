"""BLE bridge frame codec (watch -> desk peripheral).

Connect IQ BLE writes are capped at 20 bytes, one outstanding at a time, with no MTU exchange,
so the capture app streams tiny self-describing frames. Layout of every frame:

    byte 0      seq   u8, increments per frame, wraps (peripheral acks the last one it got)
    byte 1      type  u8
    bytes 2..   payload (<= 18 bytes), little-endian

Types the watch sends (dt = ms since the SYNC named by `sid`, u24 -> up to 4.6 h; the watch
re-syncs every 10 s):

    0x00 SYNC       epoch_s u32 | ms u16 | session u32 | sid u8                 (13 B)
    0x02 ACC_KEY    dt u24 | hz u8 | x i16 | y i16 | z i16 (milli-g) | sid u8   (13 B)
    0x03 ACC_DELTA  k u8 | (dx i8, dy i8, dz i8) * k   (k <= 5, vs previous)    (<= 17 B)
    0x07 BAT        pct u8 | drain_x10 u8 (%/h * 10)
    0x08 BLOB_START q u32 | attempt u8          (a protocol-v1 JSON batch follows)
    0x09 BLOB_DATA  up to 18 bytes of the JSON
    0x0A BLOB_END   total u16 | mac[16]         (HMAC-SHA256(key, "pulso-blob" | q u32 | json)[:16])
    0x0B HELLO      key_id[4] | nonce[12]       (key_id = SHA-256(key)[:4])

key = SHA-256(device token). 0x01, 0x04, 0x05 and 0x06 (RR, FEAT, HR, MARK in early drafts) are
reserved: the DATA characteristic is open to anyone in range, so the bridge drops them instead of
turning unauthenticated beats, heart rate or marks into stored data. RR, HR and marks only arrive
inside signed BLOBs.

Peripheral -> watch notifications (CTRL characteristic), all starting with last_seq u8 | hint u8
(the accelerometer rate the bridge wants; 0 = no raw stream):

    plain       last_seq | hint                                           every 10 frames
    0xB2        last_seq | hint | 0xB2 | proof[16]                        answer to HELLO
                proof = HMAC-SHA256(key, "pulso-hello" | nonce)[:16]; no proof = unknown token
    0xB1        last_seq | hint | 0xB1 | q u32 | attempt u8 | status u8 | mac[8]   blob result
                status 0 stored, 1 rejected, 2 bad MAC, 3 retry;
                mac = HMAC-SHA256(key, "pulso-ack" | nonce | q u32 | attempt u8 | status u8)[:8]

The watch writes nothing but HELLO until the proof checks, and ignores any 0xB1 whose attempt or
mac does not match the batch in flight, so a look-alike peripheral neither receives batches nor
makes the watch delete them. The blob MAC covers q: anyone can write to DATA, and a genuine blob
replayed under the q of another batch must fail the MAC (never earn a signed "rejected" for it).
The three MACs use distinct labels, so none of them can stand in for another.

A delta beyond ±127 mG forces a new ACC_KEY, so decoding never accumulates error. The watch writes
the seq at transmission time, so a gap means frames lost on the air: the decoder then waits for the
next ACC_KEY and aborts a blob in progress (the watch re-sends the whole batch, same q, new
attempt). An ACC_KEY is only timed against the SYNC whose `sid` it carries, so a lost SYNC never
shifts later samples onto an older time base; HELLO (a new connection, maybe a new app run whose sid
counter started over) forgets the time base too, and the watch re-SYNCs after every handshake.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

SYNC, RR, ACC_KEY, ACC_DELTA, FEAT, HR, MARK, BAT, BLOB_START, BLOB_DATA, BLOB_END, HELLO = range(12)
RESERVED = frozenset((RR, FEAT, HR, MARK))   # never accepted from the open stream
MAX_FRAME = 20
MAC_LEN = 16
NONCE_LEN = 12
ACK_MAC_LEN = 8
BLOB_ACK = 0xB1
HELLO_ACK = 0xB2
BLOB_OK, BLOB_REJECTED, BLOB_BAD_MAC, BLOB_RETRY = range(4)
MAX_BLOB = 65535


def blob_key(token: str) -> bytes:
    """MAC key shared by the watch and the bridge: SHA-256 of the device token. The central
    stores exactly this digest (hex) for every token, so a local bridge needs no secret of its own."""
    return hashlib.sha256(token.strip().encode()).digest()


def blob_mac(key: bytes, q: int, payload: bytes) -> bytes:
    """MAC of batch q's JSON; q is inside so the open BLOB_START header cannot relabel a blob."""
    msg = b"pulso-blob" + struct.pack("<I", q & 0xFFFFFFFF) + payload
    return hmac.new(key, msg, hashlib.sha256).digest()[:MAC_LEN]


def key_id(key: bytes) -> bytes:
    """Names the key in HELLO without revealing it."""
    return hashlib.sha256(key).digest()[:4]


def match_key(keys: Iterable[bytes], kid: bytes) -> bytes | None:
    return next((k for k in keys if hmac.compare_digest(key_id(k), kid)), None)


def hello_proof(key: bytes, nonce: bytes) -> bytes:
    return hmac.new(key, b"pulso-hello" + nonce, hashlib.sha256).digest()[:MAC_LEN]


def ack_mac(key: bytes, nonce: bytes, q: int, attempt: int, status: int) -> bytes:
    msg = b"pulso-ack" + nonce + struct.pack("<IBB", q & 0xFFFFFFFF, attempt & 0xFF, status & 0xFF)
    return hmac.new(key, msg, hashlib.sha256).digest()[:ACK_MAC_LEN]


def hello_ack(last_seq: int, hint: int, key: bytes | None, nonce: bytes) -> bytes:
    """Answer to HELLO: the proof when the bridge knows the key, nothing after 0xB2 otherwise."""
    head = bytes((last_seq & 0xFF, hint & 0xFF, HELLO_ACK))
    return head + (hello_proof(key, nonce) if key is not None else b"")


def blob_ack(last_seq: int, hint: int, q: int, attempt: int, status: int, key: bytes, nonce: bytes) -> bytes:
    return (bytes((last_seq & 0xFF, hint & 0xFF, BLOB_ACK))
            + struct.pack("<IBB", q & 0xFFFFFFFF, attempt & 0xFF, status)
            + ack_mac(key, nonce, q, attempt, status))


def read_ack(value: bytes, key: bytes, nonce: bytes, q: int, attempt: int) -> int | None:
    """Watch side (reference for BleLink.onCharacteristicChanged): the status of a 0xB1 ack for
    batch q / attempt, or None when it is for another attempt or its MAC does not check."""
    if len(value) < 17 or value[2] != BLOB_ACK:
        return None
    aq, aa, st = struct.unpack("<IBB", value[3:9])
    if aq != q & 0xFFFFFFFF or aa != attempt & 0xFF:
        return None
    return st if hmac.compare_digest(value[9:17], ack_mac(key, nonce, aq, aa, st)) else None


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
        self.sid = 0
        self.attempt = 0

    def _frame(self, typ: int, payload: bytes) -> bytes:
        f = bytes((self.seq & 0xFF, typ)) + payload
        if len(f) > MAX_FRAME:
            raise ValueError(f"frame too long: {len(f)}")
        self.seq = (self.seq + 1) & 0xFF
        return f

    def hello(self, key: bytes, nonce: bytes) -> bytes:
        if len(nonce) != NONCE_LEN:
            raise ValueError("nonce must be 12 bytes")
        return self._frame(HELLO, key_id(key) + nonce)

    def sync(self, epoch_ms: int, session: int) -> bytes:
        self.sid = (self.sid + 1) & 0xFF
        return self._frame(SYNC, struct.pack("<IHIB", epoch_ms // 1000, epoch_ms % 1000, session, self.sid))

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
            out.append(self._frame(ACC_KEY, _p24(dt) + bytes((hz,)) + struct.pack("<hhh", *s) + bytes((self.sid,))))
            prev = s
        flush()
        return out

    def bat(self, pct: int, drain_x10: int) -> bytes:
        return self._frame(BAT, bytes((max(0, min(100, pct)), max(0, min(255, drain_x10)))))

    def blob(self, q: int, payload: bytes, key: bytes) -> list[bytes]:
        """One attempt at sending a batch (each call is a new attempt id, like BleLink.sendBlob)."""
        if len(payload) > MAX_BLOB:
            raise ValueError("blob too large")
        self.attempt = (self.attempt + 1) & 0xFF
        out = [self._frame(BLOB_START, struct.pack("<IB", q, self.attempt))]
        out += [self._frame(BLOB_DATA, payload[i : i + 18]) for i in range(0, len(payload), 18)]
        out.append(self._frame(BLOB_END, struct.pack("<H", len(payload)) + blob_mac(key, q, payload)))
        return out


# ------------------------------------------------------------------------------ decoding


@dataclass
class Hello:
    key_id: bytes
    nonce: bytes


@dataclass
class Blob:
    q: int
    attempt: int
    payload: bytes
    mac: bytes


@dataclass
class BlobFailed:
    """The current attempt broke off in the middle (lost frame, size): the watch should resend."""
    q: int
    attempt: int


@dataclass
class Decoded:
    accel: list[tuple[int, float, list[tuple[int, int, int]]]] = field(default_factory=list)  # (t0_ms, hz, samples)
    battery: tuple[int, float] | None = None
    lost_frames: int = 0
    bad_frames: int = 0
    ignored: int = 0          # reserved types (RR/FEAT/HR/MARK): never accepted from the open stream
    unsynced: int = 0         # ACC_KEY whose SYNC never arrived (or was out of range)
    session: int | None = None

    def empty(self) -> bool:
        return not (self.accel or self.battery)


class Decoder:
    """Stateful: keeps the time base, the last accel sample and sequence tracking across frames.

    `now_ms` (optional) bounds SYNC epochs to the wall clock ± `max_skew_ms`, so the open stream
    can only ever add motion around the present, never at an arbitrary past time."""

    def __init__(self, now_ms: Callable[[], int] | None = None, max_skew_ms: int = 600_000) -> None:
        self.base_ms: int | None = None
        self.sid: int | None = None
        self.session: int | None = None
        self.last_seq: int | None = None
        self.acc_prev: tuple[int, int, int] | None = None
        self.acc_t: float | None = None     # ms timestamp of acc_prev
        self.acc_hz: float = 25.0
        self.blob_q: int | None = None
        self.blob_attempt = 0
        self.blob_buf = bytearray()
        self.now_ms = now_ms
        self.max_skew_ms = max_skew_ms
        self.out = Decoded()
        self.events: list[Hello | Blob | BlobFailed] = []

    def take(self) -> Decoded:
        """The stream part accumulated so far (events stay for take_events)."""
        d, self.out = self.out, Decoded(session=self.session)
        return d

    def take_events(self) -> list[Hello | Blob | BlobFailed]:
        """HELLOs, completed blobs and broken-off blob attempts, in arrival order."""
        ev, self.events = self.events, []
        return ev

    def _abort_blob(self, report: bool) -> None:
        # Only an attempt that breaks off in the middle is reported (-> RETRY). A new BLOB_START or
        # HELLO means the watch already gave up on the previous attempt: answering it would only
        # cancel the new one.
        if self.blob_q is not None and report:
            self.events.append(BlobFailed(self.blob_q, self.blob_attempt))
        self.blob_q = None
        self.blob_buf = bytearray()

    def feed(self, frame: bytes) -> None:
        if len(frame) < 2 or len(frame) > MAX_FRAME:
            self.out.bad_frames += 1
            return
        seq, typ, p = frame[0], frame[1], frame[2:]
        if self.last_seq is not None:
            gap = (seq - self.last_seq - 1) & 0xFF
            if gap:
                self.out.lost_frames += gap
                # whatever was lost may have been a delta or part of a blob: never guess
                self.acc_prev = None
                self._abort_blob(report=typ not in (BLOB_START, HELLO))
        self.last_seq = seq
        try:
            self._dispatch(typ, p)
        except (struct.error, IndexError, ValueError):
            self.out.bad_frames += 1

    def _dispatch(self, typ: int, p: bytes) -> None:
        if typ == SYNC:
            s, ms, sess, sid = struct.unpack("<IHIB", p[:11])
            base = s * 1000 + ms
            if self.now_ms is not None and abs(base - self.now_ms()) > self.max_skew_ms:
                self.base_ms = self.sid = None
                self.acc_prev = None
                raise ValueError("SYNC far from the wall clock")
            self.base_ms, self.sid = base, sid
            if self.session is not None and sess != self.session:
                self.acc_prev = None
            self.session = self.out.session = sess
        elif typ == ACC_KEY:
            dt = _u24(p[0:3])
            hz = p[3] or 25
            x, y, z = struct.unpack("<hhh", p[4:10])
            if self.base_ms is None or p[10] != self.sid:
                # timed against a SYNC this side never got: drop it rather than misdate it
                self.acc_prev = None
                self.out.unsynced += 1
                return
            t = self.base_ms + dt
            self.acc_prev, self.acc_t, self.acc_hz = (x, y, z), float(t), float(hz)
            self._acc_append(t, hz, (x, y, z))
        elif typ == ACC_DELTA:
            k = p[0]
            if not 1 <= k <= 5:
                raise ValueError("bad delta count")
            if self.acc_prev is None or self.acc_t is None:
                return
            for i in range(k):
                dx, dy, dz = struct.unpack("<bbb", p[1 + 3 * i : 4 + 3 * i])
                s = (self.acc_prev[0] + dx, self.acc_prev[1] + dy, self.acc_prev[2] + dz)
                if not all(-32768 <= v <= 32767 for v in s):
                    self.acc_prev = None   # the watch clamps to int16 before taking deltas
                    raise ValueError("delta leaves int16")
                self.acc_t += 1000.0 / self.acc_hz
                self.acc_prev = s
                self._acc_append(int(round(self.acc_t)), self.acc_hz, s)
        elif typ == BAT:
            self.out.battery = (p[0], p[1] / 10.0)
        elif typ == HELLO:
            if len(p) < 4 + NONCE_LEN:
                raise ValueError("short HELLO")
            # a (re)connection: whatever blob was in flight was abandoned by the watch, and the
            # time base may belong to another app run (whose sid counter also started at 1); the
            # watch sends a fresh SYNC after the handshake
            self._abort_blob(report=False)
            self.acc_prev = None
            self.base_ms = self.sid = self.session = None
            self.events.append(Hello(bytes(p[:4]), bytes(p[4 : 4 + NONCE_LEN])))
        elif typ == BLOB_START:
            q, attempt = struct.unpack("<IB", p[:5])
            self._abort_blob(report=False)
            self.blob_q, self.blob_attempt = q, attempt
        elif typ == BLOB_DATA:
            if self.blob_q is not None:
                self.blob_buf += p
                if len(self.blob_buf) > MAX_BLOB:
                    self._abort_blob(report=True)
        elif typ == BLOB_END:
            if self.blob_q is None:
                return
            (total,) = struct.unpack("<H", p[:2])
            mac = bytes(p[2 : 2 + MAC_LEN])
            if len(mac) != MAC_LEN or total != len(self.blob_buf):
                self._abort_blob(report=True)
                return
            self.events.append(Blob(self.blob_q, self.blob_attempt, bytes(self.blob_buf), mac))
            self.blob_q = None
            self.blob_buf = bytearray()
        elif typ in RESERVED:
            self.out.ignored += 1
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
    """Decoded stream -> protocol v1 batch (src 'ble'): raw accelerometer and battery only."""
    import base64

    import numpy as np

    b: dict = {"v": 1, "d": device, "q": seq, "src": "ble", "t": now_s,
               "sid": f"ble-{dec.session}" if dec.session is not None else None}
    raw = []
    for t0, hz, samples in dec.accel:
        a = np.clip(np.asarray(samples, dtype=np.int64).reshape(-1, 3), -32768, 32767).astype("<i2")
        raw.append([t0, hz] + [base64.b64encode(a[:, i].tobytes()).decode() for i in range(3)])
    if raw:
        b["raw"] = raw
    if dec.battery:
        b["bat"] = [now_s, float(dec.battery[0]), None]
    return {k: v for k, v in b.items() if v is not None}
