"""Desk BLE peripheral for the Pulso Captura app (watch = central, this = GATT server).

Advertises one 128-bit service with:
* DATA  (write / write-without-response): 20-byte frames from `frames.py`
* CTRL  (notify): acks `last_seq | accel_hz_hint` so the watch paces itself

Decoded frames are turned into protocol v1 batches every few seconds and handed to a sink
(HTTP POST to the central, or direct insert when the central runs on this machine).
Runs on Linux (BlueZ), macOS and Windows through `bless`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from pulso.bridge.frames import Decoder, to_batch

SERVICE_UUID = "6c1e0001-5b7a-4d6e-9f2a-50554c534f31"   # "PULSO1" tail
DATA_UUID = "6c1e0002-5b7a-4d6e-9f2a-50554c534f31"
CTRL_UUID = "6c1e0003-5b7a-4d6e-9f2a-50554c534f31"


async def serve(on_batch: Callable[[dict], None], device_id: str = "fr255-ble", flush_s: float = 5.0,
                accel_hz_hint: int = 25, log: Callable[[str], None] = print) -> None:
    try:
        from bless import (BlessGATTCharacteristic, BlessServer, GATTAttributePermissions,
                           GATTCharacteristicProperties)
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'pulso[bridge]' para rodar a ponte BLE") from e

    dec = Decoder()
    state = {"seq": int(time.time()), "frames": 0, "t": time.monotonic()}
    server = BlessServer(name="Pulso Ponte")

    def on_write(characteristic: BlessGATTCharacteristic, value, **kwargs):
        if str(characteristic.uuid).lower() != DATA_UUID:
            return
        dec.feed(bytes(value))
        state["frames"] += 1
        # ack every 10 frames: tiny notification, lets the watch detect a dead link
        if state["frames"] % 10 == 0 and dec.last_seq is not None:
            ctrl = server.get_characteristic(CTRL_UUID)
            ctrl.value = bytearray((dec.last_seq, accel_hz_hint))
            server.update_value(SERVICE_UUID, CTRL_UUID)

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
    log(f"ponte BLE anunciando {SERVICE_UUID}; abra o Pulso Captura no relógio e escolha 'Ponte BLE'")
    try:
        while True:
            await asyncio.sleep(flush_s)
            d = dec.take()
            if d.lost_frames or d.bad_frames:
                log(f"frames perdidos: {d.lost_frames}, inválidos: {d.bad_frames}")
            if not d.empty():
                state["seq"] += 1
                on_batch(to_batch(d, device_id, state["seq"], int(time.time())))
            el = time.monotonic() - state["t"]
            if el >= 60:
                log(f"{state['frames'] / el:.1f} frames/s ({state['frames'] * 20 / el:.0f} B/s no máximo)")
                state["frames"], state["t"] = 0, time.monotonic()
    finally:
        await server.stop()


def http_sink(url: str, token: str, log: Callable[[str], None] = print) -> Callable[[dict], None]:
    import httpx

    client = httpx.Client(timeout=15.0)

    def send(batch: dict) -> None:
        try:
            r = client.post(url.rstrip("/") + "/v1/ingest", json=batch, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                log(f"central respondeu {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as e:
            log(f"falha ao enviar lote {batch.get('q')}: {e}")

    return send


def local_sink(db_path: str) -> Callable[[dict], None]:
    from pulso import store
    from pulso.ingest.handler import ingest
    from pulso.ingest.protocol import Batch

    conn = store.connect(db_path)

    def send(batch: dict) -> None:
        ingest(conn, Batch.model_validate(batch))

    return send
