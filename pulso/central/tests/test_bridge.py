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
