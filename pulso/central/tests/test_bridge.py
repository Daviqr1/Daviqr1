import struct

import numpy as np

from pulso import store
from pulso.bridge import frames as FR
from pulso.bridge.hrm import RrAccumulator, parse_measurement
from pulso.ingest.handler import ingest
from pulso.ingest.protocol import Batch


def test_frames_roundtrip_all_types():
    enc, dec = FR.Encoder(), FR.Decoder()
    t0 = 1_760_630_400_123
    stream = [enc.sync(t0, session=42)]
    rr = [812, 820, 790, 805, 799, 830, 845, 801]
    stream += enc.rr(5_000, rr)
    xyz = [(0, 0, 1000)]
    rng = np.random.default_rng(0)
    for i in range(60):
        p = xyz[-1]
        jump = 400 if i == 30 else 0  # forces a new keyframe
        xyz.append((p[0] + int(rng.integers(-20, 20)) + jump, p[1] + int(rng.integers(-20, 20)), p[2] + int(rng.integers(-20, 20))))
    stream += enc.accel(1_000, 25, xyz)
    stream += enc.feat(2_000, [(12, 30), (5, 8), (300, 120), (0, 1)])
    stream += [enc.hr(3_000, 71), enc.mark(4_000, 1), enc.bat(54, 23)]
    assert all(len(f) <= 20 for f in stream)
    for f in stream:
        dec.feed(f)
    out = dec.take()
    assert out.lost_frames == 0 and out.bad_frames == 0 and out.session == 42
    # RR: values intact, last beat at t0 + 5000
    all_rr = [v for _, chunk in out.rr for v in chunk]
    assert all_rr == rr
    assert out.rr[-1][0] == t0 + 5_000
    # accel reconstructed exactly (deltas + keyframe), timestamps continuous at 25 Hz
    samples = [s for _, _, ss in out.accel for s in ss]
    assert samples == xyz
    assert out.accel[0][0] == t0 + 1_000
    assert out.feat[0][1] == [12.0, 5.0, 300.0] and out.feat[1][2] == [1.0]
    assert out.hr == [(t0 + 3_000, 71)] and out.marks == [(t0 + 4_000, "estresse")]
    assert out.battery == (54, 2.3)


def test_decoder_survives_loss_and_garbage():
    enc, dec = FR.Encoder(), FR.Decoder()
    fs = [enc.sync(1_000_000, 1)] + enc.accel(0, 25, [(0, 0, 1000 + i) for i in range(20)])
    del fs[2]  # lose one delta frame
    for f in fs:
        dec.feed(f)
    dec.feed(b"\x00")          # too short
    dec.feed(bytes(25))        # too long
    dec.feed(bytes((99, 0x7F)) + bytes(4))  # unknown type
    out = dec.take()
    assert out.lost_frames >= 1 and out.bad_frames == 3
    # data before the loss is intact; nothing after it is invented
    samples = [s for _, _, ss in out.accel for s in ss]
    assert samples == [(0, 0, 1000 + i) for i in range(len(samples))]


def test_decoded_frames_ingest_into_store():
    enc, dec = FR.Encoder(), FR.Decoder()
    t0 = 1_760_630_400_000
    for f in [enc.sync(t0, 7)] + enc.rr(2_000, [800, 810, 805]) + enc.accel(0, 25, [(1, 2, 1000)] * 10) + [enc.hr(1_500, 70)]:
        dec.feed(f)
    batch = FR.to_batch(dec.take(), "fr255-ble", 1, t0 // 1000)
    conn = store.connect(":memory:")
    res = ingest(conn, Batch.model_validate(batch))
    assert res["n"]["rr"] == 3 and res["n"]["raw"] == 1
    ts, rr = store.rr(conn, 0, 2**62, source="ble:bridge")
    assert rr.tolist() == [800, 810, 805] and ts[-1] == t0 + 2_000


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
    import json

    b = {"v": 1, "d": "fr255-cap-abc123", "q": q, "src": "cap", "t": 1_760_630_460,
         "rr": [[1_760_630_400_000, [800, 812, 790]]], "ev": [[1_760_630_401, "mark", "café"]]}
    return json.dumps(b, ensure_ascii=False, separators=(",", ":")).encode()


def test_blob_roundtrip_interleaved_with_stream_and_ingested():
    import os
    import tempfile

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
    for i, f in enumerate(order):
        dec.feed(bytes((i & 0xFF,)) + f[1:])
    done, failed = dec.take_blobs()
    assert failed == [] and len(done) == 1
    q, got, mac, total = done[0]
    assert q == 7 and got == payload and total == len(payload)
    out = dec.take()
    assert [s for _, _, ss in out.accel for s in ss] == [(0, 0, 1000 + i) for i in range(10)]

    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "p.sqlite")
        conn = store.connect(db)
        conn.execute("INSERT INTO device_tokens (token_sha256, label, created_ms) VALUES (?, ?, ?)",
                     (auth._digest(tok), "fr255", 0))
        conn.commit()
        sink = local_sink(db)
        assert handle_blob(sink, q, got, mac, log=lambda m: None) == FR.BLOB_OK
        # same batch again (e.g. it also went through the phone): idempotent, still OK
        assert handle_blob(sink, q, got, mac, log=lambda m: None) == FR.BLOB_OK
        n = conn.execute("SELECT COUNT(*) FROM rr_intervals").fetchone()[0]
        assert n == 3
        # wrong key, wrong q, garbage
        assert handle_blob(sink, q, got, FR.blob_mac(FR.blob_key("pulso_dev_other"), got), log=lambda m: None) == FR.BLOB_BAD_MAC
        assert handle_blob(sink, 8, got, mac, log=lambda m: None) == FR.BLOB_REJECTED
        junk = b"{not json"
        assert handle_blob(sink, q, junk, FR.blob_mac(key, junk), log=lambda m: None) == FR.BLOB_REJECTED
        # a revoked token no longer signs anything
        auth.revoke(conn, "fr255")
        conn.commit()
        assert handle_blob(sink, q, got, mac, log=lambda m: None) == FR.BLOB_BAD_MAC


def test_blob_lost_frame_aborts_and_asks_resend():
    enc, dec = FR.Encoder(), FR.Decoder()
    key = FR.blob_key("pulso_dev_k")
    fs = enc.blob(9, _batch_json(9), key)
    del fs[3]  # a data frame lost on the air
    for f in fs:
        dec.feed(f)
    done, failed = dec.take_blobs()
    assert done == [] and failed == [9]
    # the re-send goes through
    for f in enc.blob(9, _batch_json(9), key):
        dec.feed(f)
    done, failed = dec.take_blobs()
    assert len(done) == 1 and failed == []


def test_gap_before_delta_never_reuses_stale_base():
    # a lost frame followed by a non-accel frame, then deltas: must not apply to the old base
    enc, dec = FR.Encoder(), FR.Decoder()
    fs = [enc.sync(1_000_000, 1)] + enc.accel(0, 25, [(0, 0, 1000 + i) for i in range(11)])
    hr = enc.hr(500, 70)
    more = enc.accel(400, 25, [(0, 0, 2000 + i) for i in range(11)])
    stream = fs[:2] + [fs[2]] + [hr] + more[1:]   # drop the KEY of the second chunk -> seq gap
    for f in stream:
        dec.feed(f)
    out = dec.take()
    samples = [s for _, _, ss in out.accel for s in ss]
    assert all(s[2] < 2000 for s in samples)   # nothing reconstructed from the orphaned deltas


def test_blob_ack_layout():
    a = FR.blob_ack(200, 25, 0x01020304, FR.BLOB_OK)
    assert a == bytes((200, 25, 0xB1, 4, 3, 2, 1, 0)) and len(a) <= 20
