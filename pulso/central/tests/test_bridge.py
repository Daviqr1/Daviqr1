import asyncio
import json
import os
import struct
import sys
import tempfile
import types

import numpy as np

from pulso import store
from pulso.bridge import frames as FR
from pulso.bridge.hrm import RrAccumulator, parse_measurement
from pulso.ingest.handler import ingest
from pulso.ingest.protocol import Batch

NOLOG = lambda m: None  # noqa: E731


def _feed(dec: FR.Decoder, frames: list[bytes]) -> None:
    for f in frames:
        dec.feed(f)


def _reseq(frames: list[bytes], start: int = 0) -> list[bytes]:
    """Seq written at transmission time, as the watch does when it interleaves stream and blob."""
    return [bytes(((start + i) & 0xFF,)) + f[1:] for i, f in enumerate(frames)]


def test_frames_roundtrip_all_types():
    enc, dec = FR.Encoder(), FR.Decoder()
    t0 = 1_760_630_400_123
    key = FR.blob_key("pulso_dev_" + "r" * 40)
    nonce = bytes(range(12))
    stream = [enc.hello(key, nonce), enc.sync(t0, session=42)]
    xyz = [(0, 0, 1000)]
    rng = np.random.default_rng(0)
    for i in range(60):
        p = xyz[-1]
        jump = 400 if i == 30 else 0  # forces a new keyframe
        xyz.append((p[0] + int(rng.integers(-20, 20)) + jump, p[1] + int(rng.integers(-20, 20)), p[2] + int(rng.integers(-20, 20))))
    stream += enc.accel(1_000, 25, xyz)
    stream += [enc.bat(54, 23)]
    assert all(len(f) <= 20 for f in stream)
    _feed(dec, stream)
    out = dec.take()
    assert out.lost_frames == 0 and out.bad_frames == 0 and out.session == 42 and out.unsynced == 0
    # accel reconstructed exactly (deltas + keyframe), timestamps continuous at 25 Hz
    samples = [s for _, _, ss in out.accel for s in ss]
    assert samples == xyz
    assert out.accel[0][0] == t0 + 1_000
    assert out.battery == (54, 2.3)
    ev = dec.take_events()
    assert ev == [FR.Hello(FR.key_id(key), nonce)]


def test_decoder_survives_loss_and_garbage():
    enc, dec = FR.Encoder(), FR.Decoder()
    fs = [enc.sync(1_000_000, 1)] + enc.accel(0, 25, [(0, 0, 1000 + i) for i in range(20)])
    del fs[2]  # lose one delta frame
    _feed(dec, fs)
    dec.feed(b"\x00")          # too short
    dec.feed(bytes(25))        # too long
    dec.feed(bytes((99, 0x7F)) + bytes(4))  # unknown type
    out = dec.take()
    assert out.lost_frames >= 1 and out.bad_frames == 3
    # data before the loss is intact; nothing after it is invented
    samples = [s for _, _, ss in out.accel for s in ss]
    assert samples == [(0, 0, 1000 + i) for i in range(len(samples))]


def test_decoded_stream_ingests_into_store():
    enc, dec = FR.Encoder(), FR.Decoder()
    t0 = 1_760_630_400_000
    _feed(dec, [enc.sync(t0, 7)] + enc.accel(0, 25, [(1, 2, 1000)] * 10) + [enc.bat(80, 15)])
    batch = FR.to_batch(dec.take(), "fr255-ble", 1, t0 // 1000)
    assert set(batch) == {"v", "d", "q", "src", "t", "sid", "raw", "bat"}
    conn = store.connect(":memory:")
    res = ingest(conn, Batch.model_validate(batch))
    assert res["n"]["raw"] == 1
    row = conn.execute("SELECT source, t0_ms, n FROM motion_chunks").fetchone()
    assert tuple(row) == ("ble:bridge", t0, 10)


def _raw(seq: int, typ: int, payload: bytes) -> bytes:
    return bytes((seq & 0xFF, typ)) + payload


def test_open_stream_never_stores_rr_hr_or_marks():
    # F2/SEC-2: DATA is writable by anyone in range. RR, FEAT, HR and MARK (types the watch never
    # sends on the stream) must not become rr_intervals, hr samples or events.
    t0 = 1_767_225_600_000   # 2026-01-01, a past day an attacker might want to rewrite
    p24 = lambda v: bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))  # noqa: E731
    frames = [
        _raw(0, FR.SYNC, struct.pack("<IHIB", t0 // 1000, 0, 1, 1)),
        _raw(1, FR.RR, p24(5000) + bytes((6,)) + struct.pack("<6H", *[800] * 6)),
        _raw(2, FR.FEAT, p24(1000) + bytes((1,)) + struct.pack("<HH", 30, 40)),
        _raw(3, FR.HR, p24(3000) + bytes((180,))),
        _raw(4, FR.MARK, p24(4000) + bytes((1,))),
        _raw(5, FR.MARK, p24(4100) + bytes((3,))),
    ]
    dec = FR.Decoder()
    _feed(dec, frames)
    out = dec.take()
    assert out.ignored == 5 and out.empty()
    batch = FR.to_batch(out, "fr255-ble", 1, t0 // 1000)
    assert not {"rr", "hr1", "ev", "acc"} & set(batch)
    conn = store.connect(":memory:")
    ingest(conn, Batch.model_validate(batch))
    assert conn.execute("SELECT COUNT(*) FROM rr_intervals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    # (the ingest's own clock_skew_s telemetry is the only sample an empty batch leaves)
    assert conn.execute("SELECT COUNT(*) FROM samples WHERE metric != 'clock_skew_s'").fetchone()[0] == 0


def test_malformed_stream_never_raises():
    # the HR sequence that used to make to_batch raise IndexError (span 1001, index 4000) and
    # deltas that would overflow int16: both are dropped, nothing raises
    t = 1_760_630_400
    p24 = lambda v: bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))  # noqa: E731
    frames = [
        _raw(0, FR.SYNC, struct.pack("<IHIB", t, 0, 1, 1)),
        _raw(1, FR.HR, p24(0) + bytes((70,))),
        _raw(2, FR.HR, p24(4_000_000) + bytes((70,))),
        _raw(3, FR.SYNC, struct.pack("<IHIB", t + 1_000, 0, 1, 2)),
        _raw(4, FR.HR, p24(0) + bytes((70,))),
        _raw(5, FR.ACC_KEY, p24(0) + bytes((25,)) + struct.pack("<hhh", 32767, 0, 0) + bytes((2,))),
        _raw(6, FR.ACC_DELTA, bytes((1, 127, 0, 0))),
        _raw(7, FR.ACC_DELTA, bytes((9, 1, 1, 1))),
        _raw(8, FR.ACC_KEY, bytes(3)),
        _raw(9, FR.HELLO, bytes(5)),
        _raw(10, FR.BLOB_START, bytes(2)),
    ]
    dec = FR.Decoder()
    _feed(dec, frames)
    out = dec.take()
    assert out.ignored == 3 and out.bad_frames == 5
    assert [s for _, _, ss in out.accel for s in ss] == [(32767, 0, 0)]
    b = FR.to_batch(out, "fr255-ble", 1, t)
    Batch.model_validate(b)
    assert dec.take_events() == []


def test_hr_measurement_parsing():
    # flags: u8 HR, contact supported+detected, RR present; 72 bpm; two RR of 1024 and 512 (1/1024 s)
    data = bytes((0x16, 72)) + struct.pack("<HH", 1024, 512)
    m = parse_measurement(data)
    assert m.bpm == 72 and m.contact is True and m.rr_ms == [1000.0, 500.0]
    # u16 HR, energy expended present, no RR (what a watch broadcast usually looks like)
    m2 = parse_measurement(bytes((0x09,)) + struct.pack("<HH", 150, 33))
    assert m2.bpm == 150 and m2.energy_kj == 33 and m2.rr_ms == []


def test_rr_accumulator_batches_validate():
    acc = RrAccumulator("polar-h10", flush_s=0)
    t = 1_760_630_400_000
    for k in range(10):
        acc.add(parse_measurement(bytes((0x10, 70)) + struct.pack("<H", 870)), now_ms=t + k * 850)
    b = acc.batch()
    batch = Batch.model_validate(b)
    assert batch.src == "strap" and batch.rr[0][0] == t + 9 * 850 and len(batch.rr[0][1]) == 10
    assert acc.saw_rr


def _batch_json(q: int) -> bytes:
    b = {"v": 1, "d": "fr255-cap-abc123", "q": q, "src": "cap", "t": 1_760_630_460,
         "rr": [[1_760_630_400_000, [800, 812, 790]]], "ev": [[1_760_630_401, "mark", "café"]]}
    return json.dumps(b, ensure_ascii=False, separators=(",", ":")).encode()


def _db_with_token(d: str, tok: str) -> tuple[str, object]:
    from pulso.ingest import auth

    db = os.path.join(d, "p.sqlite")
    conn = store.connect(db)
    conn.execute("INSERT INTO device_tokens (token_sha256, label, created_ms) VALUES (?, ?, ?)",
                 (auth._digest(tok), "fr255", 0))
    conn.commit()
    return db, conn


def test_blob_roundtrip_interleaved_with_stream_and_ingested():
    from pulso.bridge.peripheral import handle_blob, local_sink
    from pulso.ingest import auth

    enc, dec = FR.Encoder(), FR.Decoder()
    tok = "pulso_dev_" + "x" * 40
    key = FR.blob_key(tok)
    payload = _batch_json(7)
    sync = enc.sync(1_760_630_400_000, 3)
    acc = enc.accel(0, 25, [(0, 0, 1000 + i) for i in range(10)])
    blob = enc.blob(7, payload, key)
    assert all(len(f) <= 20 for f in blob)
    # the watch interleaves stream and blob frames and writes seq at send time
    order = [sync] + [x for pair in zip(blob, acc + [None] * len(blob)) for x in pair if x is not None]
    _feed(dec, _reseq(order))
    ev = dec.take_events()
    assert len(ev) == 1 and isinstance(ev[0], FR.Blob)
    got = ev[0]
    assert got.q == 7 and got.attempt == enc.attempt and got.payload == payload
    out = dec.take()
    assert [s for _, _, ss in out.accel for s in ss] == [(0, 0, 1000 + i) for i in range(10)]

    with tempfile.TemporaryDirectory() as d:
        db, conn = _db_with_token(d, tok)
        sink = local_sink(db)
        q, mac = got.q, got.mac
        assert handle_blob(sink, q, payload, mac, log=NOLOG) == FR.BLOB_OK
        # same batch again (e.g. it also went through the phone): idempotent, still OK
        assert handle_blob(sink, q, payload, mac, log=NOLOG) == FR.BLOB_OK
        n = conn.execute("SELECT COUNT(*) FROM rr_intervals").fetchone()[0]
        assert n == 3
        # wrong key; the genuine blob relabelled under another q (the MAC covers q); signed junk
        assert handle_blob(sink, q, payload, FR.blob_mac(FR.blob_key("pulso_dev_other"), q, payload), log=NOLOG) == FR.BLOB_BAD_MAC
        assert handle_blob(sink, 8, payload, mac, log=NOLOG) == FR.BLOB_BAD_MAC
        junk = b"{not json"
        assert handle_blob(sink, q, junk, FR.blob_mac(key, q, junk), log=NOLOG) == FR.BLOB_REJECTED
        assert handle_blob(sink, 9, junk, FR.blob_mac(key, q, junk), log=NOLOG) == FR.BLOB_BAD_MAC
        # a revoked token no longer signs anything
        auth.revoke(conn, "fr255")
        conn.commit()
        assert handle_blob(sink, q, payload, mac, log=NOLOG) == FR.BLOB_BAD_MAC


def test_blob_lost_frame_aborts_and_asks_resend():
    enc, dec = FR.Encoder(), FR.Decoder()
    key = FR.blob_key("pulso_dev_k")
    fs = enc.blob(9, _batch_json(9), key)
    first = enc.attempt
    del fs[3]  # a data frame lost on the air
    _feed(dec, fs)
    assert dec.take_events() == [FR.BlobFailed(9, first)]
    # the re-send (new attempt) goes through
    _feed(dec, enc.blob(9, _batch_json(9), key))
    ev = dec.take_events()
    assert len(ev) == 1 and isinstance(ev[0], FR.Blob) and ev[0].attempt == (first + 1) & 0xFF


def test_abandoned_attempt_then_retry_succeeds():
    # F1: the watch gives up on an attempt (write timeout, disconnect, app closed, STUCK reset) and
    # resends the same q. The new START must never produce a RETRY: that RETRY used to cancel the
    # new attempt on the watch, forever.
    key = FR.blob_key("pulso_dev_k")
    payload = _batch_json(5) * 3
    enc, dec = FR.Encoder(), FR.Decoder()
    _feed(dec, enc.blob(5, payload, key)[:11])            # START + 10 DATA, then abandoned
    for _ in range(3):
        retry = enc.blob(5, payload, key)
        _feed(dec, retry)
        ev = dec.take_events()
        assert [type(e) for e in ev] == [FR.Blob] and ev[0].q == 5 and ev[0].attempt == enc.attempt
        _feed(dec, retry[:4])                              # and abandon again
    # app restart: the seq starts over, so the gap shows up on the retry's own START
    enc2 = FR.Encoder()
    enc2.seq, enc2.attempt = 200, 77
    _feed(dec, enc2.blob(5, payload, key))
    ev = dec.take_events()
    assert [type(e) for e in ev] == [FR.Blob] and ev[0].attempt == 78


def test_gap_before_delta_never_reuses_stale_base():
    # a lost frame followed by a non-accel frame, then deltas: must not apply to the old base
    enc, dec = FR.Encoder(), FR.Decoder()
    fs = [enc.sync(1_000_000, 1)] + enc.accel(0, 25, [(0, 0, 1000 + i) for i in range(11)])
    bat = enc.bat(50, 10)
    more = enc.accel(400, 25, [(0, 0, 2000 + i) for i in range(11)])
    stream = fs[:2] + [fs[2]] + [bat] + more[1:]   # drop the KEY of the second chunk -> seq gap
    _feed(dec, stream)
    out = dec.take()
    samples = [s for _, _, ss in out.accel for s in ss]
    assert all(s[2] < 2000 for s in samples)   # nothing reconstructed from the orphaned deltas


def test_lost_sync_never_misdates_accel():
    # W3: the 10-s re-SYNC is lost; the next chunk (timed against it) must not be placed on the
    # previous SYNC's base (10 s early, on top of real samples). It is dropped until a SYNC arrives.
    enc, dec = FR.Encoder(), FR.Decoder()
    t0 = 1_760_630_400_000
    a = [enc.sync(t0, 9)] + enc.accel(10_000, 25, [(0, 0, 1000)] * 25)
    lost = enc.sync(t0 + 10_000, 9)
    b = enc.accel(10_000, 25, [(0, 0, 1100)] * 25)        # really at t0 + 20 s
    c = [enc.sync(t0 + 20_000, 9)] + enc.accel(10_000, 25, [(0, 0, 1200)] * 25)
    assert lost
    _feed(dec, a + b + c)                                   # `lost` never reached the bridge
    out = dec.take()
    assert out.lost_frames == 1 and out.unsynced == 1
    chunks = {ss[0][2]: t for t, _, ss in out.accel}
    assert chunks == {1000: t0 + 10_000, 1200: t0 + 30_000}   # 1100 dropped, never misdated
    # the same holds across a reconnection where the new session's SYNC is lost
    dec2 = FR.Decoder()
    _feed(dec2, [enc.sync(t0, 1)] + enc.accel(0, 25, [(0, 0, 1000)] * 5))
    enc.sync(t0 + 3_600_000, 2)                             # lost
    _feed(dec2, enc.accel(10_000, 25, [(0, 0, 1300)] * 5))
    out2 = dec2.take()
    assert [ss[0][2] for _, _, ss in out2.accel] == [1000] and out2.unsynced == 1


def test_app_restart_with_lost_first_sync_never_reuses_old_base():
    # W3: BleLink's SYNC id counter starts over in every app run, so the new run's first SYNC can
    # carry the id of the old run's last one. If that SYNC is lost, its chunk must not be dated
    # against the old run's base (hours earlier, under the old session): HELLO forgets the base.
    t_old = 1_760_630_400_000
    now = [t_old]
    key = FR.blob_key("pulso_dev_k")
    dec = FR.Decoder(now_ms=lambda: now[0])
    run_a = FR.Encoder()
    _feed(dec, [run_a.hello(key, bytes(12)), run_a.sync(t_old, 111)]
          + run_a.accel(10_000, 25, [(0, 0, 1000)] * 25))
    a = dec.take()
    assert [t for t, _, _ in a.accel] == [t_old + 10_000] and a.session == 111
    now[0] = t_old + 3 * 3_600_000
    run_b = FR.Encoder()                                    # app restarted: seq 0, sid counter 0
    _feed(dec, [run_b.hello(key, bytes(range(12)))])
    lost = run_b.sync(now[0], 222)                          # sid 1 again, lost on the air
    assert lost[-1] == 1 == run_a.sid
    _feed(dec, run_b.accel(10_000, 25, [(0, 0, 1300)] * 25))
    b = dec.take()
    assert b.accel == [] and b.unsynced == 1
    # the run's next SYNC restores the stream, under its own session
    _feed(dec, [run_b.sync(now[0] + 10_000, 222)] + run_b.accel(10_000, 25, [(0, 0, 1400)] * 25))
    c = dec.take()
    assert [(t, ss[0]) for t, _, ss in c.accel] == [(now[0] + 20_000, (0, 0, 1400))] and c.session == 222


def test_sync_far_from_wall_clock_is_refused():
    now = 1_760_630_400_000
    enc, dec = FR.Encoder(), FR.Decoder(now_ms=lambda: now)
    _feed(dec, [enc.sync(now - 86_400_000, 1)] + enc.accel(0, 25, [(0, 0, 1000)] * 5))
    out = dec.take()
    assert out.accel == [] and out.bad_frames == 1 and out.unsynced == 1
    _feed(dec, [enc.sync(now - 15_000, 1)] + enc.accel(0, 25, [(0, 0, 1000)] * 5))
    assert len(dec.take().accel) == 1


def test_handshake_and_ack_macs():
    # SEC-1: the watch only trusts a bridge that proves it holds the key, and only acts on an ack
    # signed for its own nonce, batch and attempt.
    key, other = FR.blob_key("pulso_dev_a"), FR.blob_key("pulso_dev_b")
    nonce, nonce2 = bytes(range(12)), bytes(range(1, 13))
    assert FR.match_key([other, key], FR.key_id(key)) == key
    assert FR.match_key([other], FR.key_id(key)) is None
    hello = FR.hello_ack(3, 25, key, nonce)
    assert len(hello) == 19 and hello[2] == FR.HELLO_ACK and hello[3:] == FR.hello_proof(key, nonce)
    assert FR.hello_proof(key, nonce2) != hello[3:] and FR.hello_proof(other, nonce) != hello[3:]
    assert FR.hello_ack(3, 25, None, nonce) == bytes((3, 25, FR.HELLO_ACK))
    # domain separation: a HELLO answer can never serve as an ack MAC and vice versa
    assert FR.hello_proof(key, nonce)[:8] != FR.ack_mac(key, nonce, 0, 0, 0)

    ack = FR.blob_ack(200, 25, 0x01020304, 6, FR.BLOB_OK, key, nonce)
    assert len(ack) == 17 and ack[:9] == bytes((200, 25, 0xB1, 4, 3, 2, 1, 6, 0))
    assert FR.read_ack(ack, key, nonce, 0x01020304, 6) == FR.BLOB_OK
    assert FR.read_ack(ack, key, nonce, 0x01020304, 5) is None          # older attempt
    assert FR.read_ack(ack, key, nonce, 0x01020305, 6) is None          # other batch
    assert FR.read_ack(ack, key, nonce2, 0x01020304, 6) is None         # other connection
    assert FR.read_ack(ack, other, nonce, 0x01020304, 6) is None        # look-alike bridge
    forged = bytearray(ack)
    forged[8] = FR.BLOB_REJECTED                                         # status flipped
    assert FR.read_ack(bytes(forged), key, nonce, 0x01020304, 6) is None
    assert FR.read_ack(ack[:9] + bytes(8), key, nonce, 0x01020304, 6) is None
    assert FR.read_ack(ack[:12], key, nonce, 0x01020304, 6) is None
    # every frame and notification fits the 20-byte limit
    assert len(FR.Encoder().hello(key, nonce)) == 18 and len(FR.Encoder().sync(0, 1)) == 13


def test_handle_blob_retries_everything_but_content_errors():
    # F6/W1: only errors about the batch itself make the watch delete it
    from pulso.bridge.peripheral import Sink, central_base, handle_blob

    key = FR.blob_key("pulso_dev_k")
    payload = _batch_json(3)
    mac = FR.blob_mac(key, 3, payload)

    def status(code):
        def send(_b):
            if isinstance(code, Exception):
                raise code
            return code
        return handle_blob(Sink(send=send, keys=lambda: [key]), 3, payload, mac, log=NOLOG)

    for code in (0, 401, 403, 404, 405, 408, 409, 410, 429, 500, 502, 503, 530, RuntimeError("db locked")):
        assert status(code) == FR.BLOB_RETRY, code
    for code in (400, 413, 422):
        assert status(code) == FR.BLOB_REJECTED, code
    assert status(200) == FR.BLOB_OK
    for url in ("https://h.dev", "https://h.dev/", "https://h.dev/v1", "https://h.dev/v1/ingest/ "):
        assert central_base(url) == "https://h.dev"


# ---- serve() end to end, with an in-memory stand-in for bless ---------------------------------


class _FakeServer:
    instances: list["_FakeServer"] = []

    def __init__(self, name: str = ""):
        self.chars: dict[str, types.SimpleNamespace] = {}
        self.notes: list[bytes] = []
        self.write_request_func = None
        _FakeServer.instances.append(self)

    async def add_new_service(self, uuid):
        pass

    async def add_new_characteristic(self, svc, uuid, props, value, perms):
        self.chars[uuid] = types.SimpleNamespace(uuid=uuid, value=value)

    async def start(self):
        pass

    async def stop(self):
        pass

    def get_characteristic(self, uuid):
        return self.chars[uuid]

    def update_value(self, svc, uuid):
        self.notes.append(bytes(self.chars[uuid].value))

    def write(self, frame: bytes):
        from pulso.bridge.peripheral import DATA_UUID

        self.write_request_func(types.SimpleNamespace(uuid=DATA_UUID), bytearray(frame))


def _fake_bless(monkeypatch):
    mod = types.ModuleType("bless")
    mod.BlessServer = _FakeServer
    mod.BlessGATTCharacteristic = object
    mod.GATTAttributePermissions = types.SimpleNamespace(writeable=1, readable=2)
    mod.GATTCharacteristicProperties = types.SimpleNamespace(write=1, write_without_response=2, notify=4, read=8)
    monkeypatch.setitem(sys.modules, "bless", mod)
    _FakeServer.instances.clear()


def test_serve_handshake_acks_and_survives_bad_input(monkeypatch):
    from pulso.bridge.peripheral import Sink, serve

    _fake_bless(monkeypatch)
    key = FR.blob_key("pulso_dev_" + "s" * 40)
    nonce = bytes(range(100, 112))
    stored: list[dict] = []
    codes = iter([404, 200])

    def send(b: dict) -> int:
        if b.get("src") == "ble":
            raise RuntimeError("disk full")       # the stream flush fails: must only be logged
        stored.append(b)
        return next(codes)

    logs: list[str] = []
    sink = Sink(send=send, keys=lambda: [key])
    enc = FR.Encoder()
    payload = _batch_json(21)
    now = int(__import__("time").time() * 1000)

    async def run():
        task = asyncio.create_task(serve(sink, flush_s=0.3, log=logs.append))
        await asyncio.sleep(0.05)
        srv = _FakeServer.instances[0]
        # a blob before any HELLO is never processed (and never acked)
        for f in enc.blob(20, _batch_json(20), key):
            srv.write(f)
        srv.write(FR.Encoder().hello(FR.blob_key("pulso_dev_stranger"), bytes(12)))   # unknown token
        srv.write(enc.hello(key, nonce))
        await asyncio.sleep(0.35)
        srv.write(enc.sync(now, 1))
        for f in enc.accel(0, 25, [(0, 0, 1000)] * 5):
            srv.write(f)
        # attempt 1 is abandoned midway, attempt 2 reaches the central's 404, attempt 3 is stored
        first = enc.blob(21, payload, key)
        for f in first[:5]:
            srv.write(f)
        for _ in range(2):
            for f in enc.blob(21, payload, key):
                srv.write(f)
            await asyncio.sleep(0.35)
        srv.write(b"\x01")                         # garbage on the open characteristic
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return srv.notes

    notes = asyncio.run(run())
    hellos = [n for n in notes if len(n) >= 3 and n[2] == FR.HELLO_ACK]
    assert hellos[0] == bytes((hellos[0][0], 25, FR.HELLO_ACK))            # stranger: no proof
    assert hellos[1][3:] == FR.hello_proof(key, nonce)
    acks = [n for n in notes if len(n) >= 3 and n[2] == FR.BLOB_ACK]
    # no RETRY for the abandoned attempt; 404 -> RETRY for attempt 2; OK for attempt 3
    att = enc.attempt
    assert [FR.read_ack(a, key, nonce, 21, a[7]) for a in acks] == [FR.BLOB_RETRY, FR.BLOB_OK]
    assert [a[7] for a in acks] == [(att - 1) & 0xFF, att]
    assert [b["q"] for b in stored] == [21, 21]                # q 20 (before HELLO) never stored
    assert any("disk full" in m for m in logs) and any("antes do HELLO" in m for m in logs)


def _serve(monkeypatch, sink, script):
    """Run serve() against the fake bless; `script(srv)` is an async function that writes frames."""
    from pulso.bridge.peripheral import serve

    _fake_bless(monkeypatch)

    async def run():
        task = asyncio.create_task(serve(sink, flush_s=0.3, log=NOLOG))
        await asyncio.sleep(0.05)
        srv = _FakeServer.instances[0]
        await script(srv)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return srv.notes

    return asyncio.run(run())


def test_relabelled_blob_never_earns_a_signed_reject(monkeypatch):
    # SEC-1: DATA is open. While the watch (proven bridge, nonce N) has batch 8 in flight, a second
    # central in range writes START(8, a) + the bytes of genuine batch 7 sniffed earlier (valid MAC)
    # + END. The bridge must not sign "rejected" for (8, a) — the watch would delete batch 8, which
    # was never stored — and must not store anything for it.
    from pulso.bridge.peripheral import Sink

    key = FR.blob_key("pulso_dev_" + "v" * 40)
    stored: list[int] = []
    sink = Sink(send=lambda b: (stored.append(b["q"]), 200)[1], keys=lambda: [key])
    watch, nonce = FR.Encoder(), bytes(range(40, 52))
    p7 = _batch_json(7)
    state = {}

    async def script(srv):
        srv.write(watch.hello(key, nonce))
        await asyncio.sleep(0.35)
        for f in watch.blob(7, p7, key):                   # batch 7 goes through (and is sniffed)
            srv.write(f)
        await asyncio.sleep(0.35)
        sniffed = FR.Encoder().blob(7, p7, key)[1:]        # its DATA frames and END (same MAC)
        start8 = watch.blob(8, _batch_json(8), key)[0]     # the watch's START(8, a) is on the air
        a = state["attempt"] = watch.attempt
        seq = start8[0] + 1
        srv.write(start8)
        for f in [bytes((0, FR.BLOB_START)) + struct.pack("<IB", 8, a)] + sniffed:
            srv.write(bytes((seq & 0xFF,)) + f[1:])        # no seq gap: nothing looks lost
            seq += 1
        await asyncio.sleep(0.35)

    notes = _serve(monkeypatch, sink, script)
    acks = [n for n in notes if len(n) >= 3 and n[2] == FR.BLOB_ACK]
    a = state["attempt"]
    for_8 = [FR.read_ack(n, key, nonce, 8, a) for n in acks if n[3:7] == struct.pack("<I", 8)]
    assert for_8 == [FR.BLOB_BAD_MAC]                       # never REJECTED (nor OK)
    assert stored == [7]
    # the same through handle_blob directly: relabelled -> bad MAC; signed for 8 but naming 7 -> retry
    from pulso.bridge.peripheral import handle_blob
    assert handle_blob(sink, 8, p7, FR.blob_mac(key, 7, p7), log=NOLOG) == FR.BLOB_BAD_MAC
    assert handle_blob(sink, 8, p7, FR.blob_mac(key, 8, p7), log=NOLOG) == FR.BLOB_RETRY
    assert stored == [7]
    # a blob MAC is never a HELLO proof or an ack MAC (distinct labels), whatever q and payload
    assert FR.blob_mac(key, 0x736C7570, b"o-hello" + nonce) != FR.hello_proof(key, nonce)


def test_painel_token_cannot_sign_over_ble(monkeypatch):
    # a painel token is read-only (it lives in a browser): the local bridge must neither prove
    # itself to it nor store a batch signed with it
    from pulso.bridge.peripheral import handle_blob, local_sink
    from pulso.ingest import auth

    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "p.sqlite")
        conn = store.connect(db)
        dev = auth.create_token(conn, "fr255")
        pan = auth.create_token(conn, "navegador", scope=auth.PAINEL)
        conn.commit()
        sink = local_sink(db)
        kdev, kpan = FR.blob_key(dev), FR.blob_key(pan)
        assert sink.keys() == [kdev]
        payload = _batch_json(4)
        assert handle_blob(sink, 4, payload, FR.blob_mac(kpan, 4, payload), log=NOLOG) == FR.BLOB_BAD_MAC
        assert conn.execute("SELECT COUNT(*) FROM rr_intervals").fetchone()[0] == 0
        nonce = bytes(range(12))

        async def script(srv):
            srv.write(FR.Encoder().hello(kpan, nonce))
            await asyncio.sleep(0.35)
            enc = FR.Encoder()
            enc.seq = 1
            srv.write(enc.hello(kdev, nonce))
            await asyncio.sleep(0.35)

        notes = _serve(monkeypatch, sink, script)
        hellos = [n for n in notes if len(n) >= 3 and n[2] == FR.HELLO_ACK]
        assert hellos[0] == bytes((0, 25, FR.HELLO_ACK))                  # bare: unknown token
        assert hellos[1][3:] == FR.hello_proof(kdev, nonce)
