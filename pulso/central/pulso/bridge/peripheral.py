"""Desk BLE peripheral for the Pulso Captura app (watch = central, this = GATT server).

Advertises one 128-bit service with:
* DATA  (write / write-without-response): 20-byte frames from `frames.py`
* CTRL  (notify): acks `last_seq | accel_hz_hint`, and per JSON batch `0xB1 | q | status`

Two kinds of traffic arrive on DATA:
* the live stream (raw accelerometer, battery): decoded and turned into protocol v1 batches every
  few seconds — best effort, a lost frame is a gap, never an invented sample;
* whole protocol v1 batches from the watch's upload queue (BLOB frames), when the router on the
  watch sends them here instead of through the phone. Each is checked (HMAC with the device token,
  JSON schema, q), stored through the same sink and acknowledged, so the watch only deletes a
  batch the central really has.

Runs on Linux (BlueZ), macOS and Windows through `bless`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from pulso.bridge import frames as FR
from pulso.bridge.frames import Decoder, to_batch

SERVICE_UUID = "6c1e0001-5b7a-4d6e-9f2a-50554c534f31"   # "PULSO1" tail
DATA_UUID = "6c1e0002-5b7a-4d6e-9f2a-50554c534f31"
CTRL_UUID = "6c1e0003-5b7a-4d6e-9f2a-50554c534f31"


@dataclass
class Sink:
    """Where batches go. `send` returns an HTTP-like status (200 stored or duplicate, 4xx the
    batch is bad, anything else transient); `keys` are the accepted BLOB MAC keys."""

    send: Callable[[dict], int]
    keys: Callable[[], list[bytes]]

    def __call__(self, batch: dict) -> int:   # usable wherever a plain on_batch callable is expected
        return self.send(batch)


def handle_blob(sink: Sink, q: int, payload: bytes, mac: bytes, log: Callable[[str], None] = print) -> int:
    """Verify, validate and store one batch that came over BLE. Returns a BLOB_* status."""
    from pydantic import ValidationError

    from pulso.ingest.protocol import Batch

    if not any(hmac.compare_digest(FR.blob_mac(k, payload), mac) for k in sink.keys()):
        log(f"lote {q} pela ponte com assinatura inválida (token do relógio ≠ token da ponte?)")
        return FR.BLOB_BAD_MAC
    try:
        batch = json.loads(payload.decode("utf-8"))
        b = Batch.model_validate(batch)
    except (UnicodeDecodeError, ValueError, ValidationError) as e:
        log(f"lote {q} pela ponte rejeitado: {str(e)[:200]}")
        return FR.BLOB_REJECTED
    if b.q != q:
        log(f"lote pela ponte com q {b.q} ≠ {q} do cabeçalho")
        return FR.BLOB_REJECTED
    code = sink.send(batch)
    if code == 200:
        return FR.BLOB_OK
    return FR.BLOB_REJECTED if 400 <= code < 500 and code not in (401, 403, 408, 429) else FR.BLOB_RETRY


async def serve(sink: Sink | Callable[[dict], object], device_id: str = "fr255-ble", flush_s: float = 5.0,
                accel_hz_hint: int = 25, log: Callable[[str], None] = print) -> None:
    try:
        from bless import (BlessGATTCharacteristic, BlessServer, GATTAttributePermissions,
                           GATTCharacteristicProperties)
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'pulso[bridge]' para rodar a ponte BLE") from e

    if not isinstance(sink, Sink):
        sink = Sink(send=lambda b, f=sink: int(f(b) or 200), keys=lambda: [])
    dec = Decoder()
    lock = threading.Lock()   # bless may call on_write from its own thread (macOS)
    state = {"seq": int(time.time()), "frames": 0, "t": time.monotonic()}
    server = BlessServer(name="Pulso Ponte")

    def notify(value: bytes) -> None:
        ctrl = server.get_characteristic(CTRL_UUID)
        ctrl.value = bytearray(value)
        server.update_value(SERVICE_UUID, CTRL_UUID)

    def on_write(characteristic: BlessGATTCharacteristic, value, **kwargs):
        if str(characteristic.uuid).lower() != DATA_UUID:
            return
        with lock:
            dec.feed(bytes(value))
            state["frames"] += 1
            last = dec.last_seq
        # ack every 10 frames: tiny notification, lets the watch detect a dead link
        if state["frames"] % 10 == 0 and last is not None:
            notify(bytes((last, accel_hz_hint)))

    server.write_request_func = on_write
    await server.add_new_service(SERVICE_UUID)
    await server.add_new_characteristic(
        SERVICE_UUID, DATA_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response,
        None, GATTAttributePermissions.writeable)
    await server.add_new_characteristic(
        SERVICE_UUID, CTRL_UUID, GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read,
        bytearray(2), GATTAttributePermissions.readable)
    await server.start()
    if not sink.keys():
        log("aviso: nenhum token para conferir lotes pela ponte — só o fluxo ao vivo será aceito")
    log(f"ponte BLE anunciando {SERVICE_UUID}; no relógio: Pulso Captura → menu → Ponte BLE")
    next_flush = time.monotonic() + flush_s
    try:
        while True:
            await asyncio.sleep(0.25)
            with lock:
                done, failed = dec.take_blobs()
                last = dec.last_seq or 0
            for q in failed:
                notify(FR.blob_ack(last, accel_hz_hint, q, FR.BLOB_RETRY))
            for q, payload, mac, _ in done:
                st = await asyncio.to_thread(handle_blob, sink, q, payload, mac, log)
                notify(FR.blob_ack(last, accel_hz_hint, q, st))
                if st == FR.BLOB_OK:
                    log(f"lote {q} recebido pela ponte ({len(payload)} B)")
            if time.monotonic() < next_flush:
                continue
            next_flush = time.monotonic() + flush_s
            with lock:
                d = dec.take()
            if d.lost_frames or d.bad_frames:
                log(f"frames perdidos: {d.lost_frames}, inválidos: {d.bad_frames}")
            if not d.empty():
                state["seq"] += 1
                await asyncio.to_thread(sink.send, to_batch(d, device_id, state["seq"], int(time.time())))
            el = time.monotonic() - state["t"]
            if el >= 60:
                log(f"{state['frames'] / el:.1f} frames/s ({state['frames'] * 20 / el:.0f} B/s no máximo)")
                state["frames"], state["t"] = 0, time.monotonic()
    finally:
        await server.stop()


def http_sink(url: str, token: str, log: Callable[[str], None] = print) -> Sink:
    import httpx

    client = httpx.Client(timeout=15.0)

    def send(batch: dict) -> int:
        try:
            r = client.post(url.rstrip("/") + "/v1/ingest", json=batch, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            log(f"falha ao enviar lote {batch.get('q')}: {e}")
            return 0
        if r.status_code != 200:
            log(f"central respondeu {r.status_code}: {r.text[:200]}")
        return r.status_code

    return Sink(send=send, keys=lambda: [FR.blob_key(token)] if token else [])


def local_sink(db_path: str) -> Sink:
    """Write straight into the local database. Every SQLite call runs on one worker thread (the
    connection is created there), whatever thread the BLE loop hands batches from."""
    from concurrent.futures import ThreadPoolExecutor

    from pydantic import ValidationError

    from pulso import store
    from pulso.ingest.handler import ingest
    from pulso.ingest.protocol import Batch

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pulso-ble-db")
    conn = pool.submit(store.connect, db_path).result()

    def _send(batch: dict) -> int:
        try:
            ingest(conn, Batch.model_validate(batch))
        except ValidationError:
            return 422
        return 200

    def _keys() -> list[bytes]:
        rows = conn.execute("SELECT token_sha256 FROM device_tokens WHERE revoked_ms IS NULL").fetchall()
        return [bytes.fromhex(r[0]) for r in rows]

    return Sink(send=lambda b: pool.submit(_send, b).result(), keys=lambda: pool.submit(_keys).result())
