import base64

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pulso import config, store
from pulso import metrics as M
from pulso.ingest import auth
from pulso.ingest.app import create_app


@pytest.fixture()
def client_and_token():
    conn = store.connect(":memory:")
    tok = auth.create_token(conn, "fr255")
    app = create_app(config.Settings(), conn=conn)
    return TestClient(app), tok, conn


def test_rejects_without_token(client_and_token):
    c, _, _ = client_and_token
    r = c.post("/v1/ingest", json={"v": 1, "d": "fr255-x", "q": 1, "src": "face"})
    assert r.status_code == 401
    r = c.post("/v1/ingest", json={"v": 1, "d": "fr255-x", "q": 1, "src": "face"},
               headers={"Authorization": "Bearer pulso_dev_wrong"})
    assert r.status_code == 401


def test_face_batch_roundtrip_and_idempotency(client_and_token):
    c, tok, conn = client_and_token
    t = 1_760_630_400
    body = {
        "v": 1, "d": "fr255-x", "q": 7, "src": "face", "t": t,
        "h": {"hr": [[t - 120, 61], [t - 60, 63], [t, 300]], "stress": [[t - 180, 35]], "bb": [[t - 180, 72]]},
        "m": {"t": t, "stress": 41, "resp": 14.5, "steps": 5120},
        "ev": [[t - 30, "mark", "estressado"]],
        "bat": [t, 54.5, 6.2],
    }
    h = {"Authorization": f"Bearer {tok}"}
    r = c.post("/v1/ingest", json=body, headers=h)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] == 1 and j["ack"] == 7 and j["dup"] == 0 and isinstance(j["face"], dict)
    ts, hr = store.series(conn, M.HR, 0, 2**62)
    assert hr.tolist() == [61, 63]  # 300 bpm rejected by the physiological gate
    assert store.series(conn, "stress_now", 0, 2**62)[1].tolist() == [41]
    assert store.events(conn, 0, 2**62, kinds=["note"])[0].title == "estressado"
    r2 = c.post("/v1/ingest", json=body, headers=h)
    assert r2.json()["dup"] == 1
    assert store.series(conn, M.HR, 0, 2**62)[1].size == 2


def test_capture_batch_rr_and_raw(client_and_token):
    c, tok, conn = client_and_token
    rr = [800, 810, 790, 805]
    t_last = 1_760_630_400_000
    x = np.array([1000, 1010, 990], dtype="<i2")
    b64 = base64.b64encode(x.tobytes()).decode()
    body = {
        "v": 1, "d": "fr255-x", "q": 8, "src": "cap", "sid": "c-1",
        "rr": [[t_last, rr]],
        "hr1": [1_760_630_390, [70, 71, None, 72]],
        "acc": [[t_last - 3000, 1_760_630_397, [5.0, 12.5, 30.0], [8.0, 9.0, 40.0]]],
        "raw": [[t_last - 3000, 25, b64, b64, b64]],
        "bar": [1_760_630_390, 1, [101_325.0, 101_324.5]],
    }
    r = c.post("/v1/ingest", json=body, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    ts, vals = store.rr(conn, 0, 2**62)
    assert vals.tolist() == rr
    assert ts[-1] == t_last and ts[0] == t_last - (810 + 790 + 805)
    assert store.series(conn, M.HR, 0, 2**62)[1].tolist() == [70, 71, 72]
    assert store.series(conn, M.ENMO, 0, 2**62)[1].tolist() == [5.0, 12.5, 30.0]
    chunks = list(store.motion(conn, "accel", t_last - 10_000, t_last + 10_000))
    assert chunks and chunks[0][2][:, 0].tolist() == [1000, 1010, 990]
    assert store.series(conn, M.PRESSURE_PA, 0, 2**62)[1].size == 2


def test_bad_payloads(client_and_token):
    c, tok, _ = client_and_token
    h = {"Authorization": f"Bearer {tok}"}
    assert c.post("/v1/ingest", json={"v": 2, "d": "fr255-x", "q": 1, "src": "face"}, headers=h).status_code == 422
    assert c.post("/v1/ingest", json={"v": 1, "d": "fr255-x", "q": 1, "src": "evil"}, headers=h).status_code == 422
    big = {"v": 1, "d": "fr255-x", "q": 1, "src": "cap", "rr": [[1, [800] * 601]]}
    assert c.post("/v1/ingest", json=big, headers=h).status_code == 422


def test_face_endpoint(client_and_token):
    c, tok, _ = client_and_token
    r = c.get("/v1/face", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and "t" in r.json()
