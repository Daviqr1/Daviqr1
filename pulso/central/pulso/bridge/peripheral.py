"""Desk BLE peripheral for the Pulso Captura app (watch = central, this = GATT server).

Advertises one 128-bit service with:
* DATA  (write / write-without-response): 20-byte frames from `frames.py`
* CTRL  (notify): acks `last_seq | accel_hz_hint`, the HELLO answer `0xB2 | proof` and per JSON
  batch `0xB1 | q | attempt | status | mac`

Every connection starts with HELLO: the watch names its key (SHA-256 of the device token) and
sends a nonce; the bridge answers with HMAC(key, nonce). The watch writes nothing else until that
proof checks, and every batch result is signed with the same key and nonce, so a look-alike
peripheral can neither collect the batches nor make the watch delete them. A "stored"/"rejected"
result also signs the MAC of the blob it judged, which the watch compares with its own: a genuine
blob of another app life (the q space restarts with Storage) replayed under the q in flight never
clears the batch the watch actually sent.

Two kinds of traffic then arrive on DATA:
* the live stream (raw accelerometer, battery): decoded and turned into protocol v1 batches every
  few seconds — best effort, a lost frame is a gap, never an invented sample. DATA is open to
  anyone in range, so only these frame types are accepted from it (no RR, HR or marks);
* whole protocol v1 batches from the watch's upload queue (BLOB frames), when the router on the
  watch sends them here instead of through the phone. Each is checked (HMAC with the device token
  over q and the JSON, JSON schema), stored through the same sink and acknowledged, so the watch
  only deletes a batch whose exact bytes the central really has (or will never accept).

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
    """Where batches go. `send` returns an HTTP-like status (200 stored or duplicate, 400/413/422
    the batch is bad, anything else transient); `keys` are the accepted HELLO/BLOB MAC keys."""

    send: Callable[[dict], int]
    keys: Callable[[], list[bytes]]

    def __call__(self, batch: dict) -> int:   # usable wherever a plain on_batch callable is expected
        return self.send(batch)


def handle_blob(sink: Sink, q: int, payload: bytes, mac: bytes, log: Callable[[str], None] = print) -> int:
    """Verify, validate and store one batch that came over BLE. Returns a BLOB_* status.

    The MAC covers q and the JSON, so a valid one means the watch itself sent this content as
    batch q: DATA is open, and a genuine blob replayed under the q of the batch in flight fails
    here instead of earning a signed REJECTED for it. REJECTED (the watch deletes the batch) only
    when that content is bad: local validation, or the central answering 400/413/422. Anything
    else — a payload that names another q, 404 from a proxy or a wrong URL, 401/403 of the
    bridge's own token, 408/429, 5xx, no connection, a local error — is RETRY: the batch stays on
    the watch."""
    from pydantic import ValidationError

    from pulso.ingest.protocol import Batch

    if not any(hmac.compare_digest(FR.blob_mac(k, q, payload), mac) for k in sink.keys()):
        log(f"lote {q} pela ponte com assinatura inválida (token do relógio ≠ token da ponte, ou injetado)")
        return FR.BLOB_BAD_MAC
    try:
        batch = json.loads(payload.decode("utf-8"))
    except ValueError as e:   # UnicodeDecodeError and JSONDecodeError are both ValueErrors
        log(f"lote {q} pela ponte rejeitado: {str(e)[:200]}")
        return FR.BLOB_REJECTED
    bq = batch.get("q") if isinstance(batch, dict) else None
    if type(bq) is int and bq != q:
        # signed for q but claims another batch: never let it decide q's fate (nor store it)
        log(f"lote pela ponte com q {bq} ≠ {q} do cabeçalho: ignorado")
        return FR.BLOB_RETRY
    try:
        b = Batch.model_validate(batch)
    except ValidationError as e:
        log(f"lote {q} pela ponte rejeitado: {str(e)[:200]}")
        return FR.BLOB_REJECTED
    if b.q != q:   # q given in another form (e.g. "7" or 7.0) that validation coerced
        log(f"lote pela ponte com q {b.q} ≠ {q} do cabeçalho: ignorado")
        return FR.BLOB_RETRY
    try:
        code = sink.send(batch)
    except Exception as e:  # noqa: BLE001 - a storage error is transient for the watch
        log(f"lote {q} pela ponte não foi gravado: {e!r}")
        return FR.BLOB_RETRY
    if code == 200:
        return FR.BLOB_OK
    return FR.BLOB_REJECTED if code in (400, 413, 422) else FR.BLOB_RETRY


async def serve(sink: Sink | Callable[[dict], object], device_id: str = "fr255-ble", flush_s: float = 5.0,
                accel_hz_hint: int = 25, log: Callable[[str], None] = print) -> None:
    try:
        from bless import (BlessGATTCharacteristic, BlessServer, GATTAttributePermissions,
                           GATTCharacteristicProperties)
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'pulso[bridge]' para rodar a ponte BLE") from e

    if not isinstance(sink, Sink):
        sink = Sink(send=lambda b, f=sink: int(f(b) or 200), keys=lambda: [])
    dec = Decoder(now_ms=lambda: int(time.time() * 1000))
    lock = threading.Lock()   # bless may call on_write from its own thread (macOS)
    state = {"seq": int(time.time()), "frames": 0, "t": time.monotonic()}
    link: dict[str, bytes | None] = {"key": None, "nonce": None}   # from the last good HELLO
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

    async def on_event(ev: FR.Hello | FR.Blob | FR.BlobFailed, last: int) -> None:
        if isinstance(ev, FR.Hello):
            key = FR.match_key(await asyncio.to_thread(sink.keys), ev.key_id)
            if key is None:
                # an unknown token must not end the session of the watch that is already in
                notify(FR.hello_ack(last, accel_hz_hint, None, ev.nonce))
                log("relógio na ponte com token desconhecido (crie com `pulso token create fr255` ou confira --token)")
                return
            link["key"], link["nonce"] = key, ev.nonce
            notify(FR.hello_ack(last, accel_hz_hint, key, ev.nonce))
            log("relógio conectado à ponte (token conferido)")
            return
        key, nonce = link["key"], link["nonce"]
        if key is None or nonce is None:
            log(f"lote {ev.q} pela ponte antes do HELLO: ignorado (o relógio reenvia)")
            return
        if isinstance(ev, FR.BlobFailed):
            notify(FR.blob_ack(last, accel_hz_hint, ev.q, ev.attempt, FR.BLOB_RETRY, key, nonce))
            return
        st = await asyncio.to_thread(handle_blob, sink, ev.q, ev.payload, ev.mac, log)
        # a verdict names the blob MAC handle_blob just verified: the watch acts on it only when
        # that is the MAC it computed for its own batch (blob_ack signs zeros for the other statuses)
        notify(FR.blob_ack(last, accel_hz_hint, ev.q, ev.attempt, st, key, nonce, ev.mac))
        if st == FR.BLOB_OK:
            log(f"lote {ev.q} recebido pela ponte ({len(ev.payload)} B)")

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
        log("aviso: nenhum token para conferir o relógio — a ponte recusa a conexão até existir um")
    log(f"ponte BLE anunciando {SERVICE_UUID}; no relógio: Pulso Captura → menu → Ponte BLE")
    next_flush = time.monotonic() + flush_s
    try:
        while True:
            await asyncio.sleep(0.25)
            with lock:
                events = dec.take_events()
                last = dec.last_seq or 0
            for ev in events:
                # one malformed frame or failing store must never stop the bridge (and with it
                # `pulso notebook --ble`)
                try:
                    await on_event(ev, last)
                except Exception as e:  # noqa: BLE001
                    log(f"erro na ponte ao tratar {type(ev).__name__}: {e!r}")
            if time.monotonic() < next_flush:
                continue
            next_flush = time.monotonic() + flush_s
            with lock:
                d = dec.take()
            if d.lost_frames or d.bad_frames or d.ignored or d.unsynced:
                log(f"frames perdidos: {d.lost_frames}, inválidos: {d.bad_frames}, recusados: {d.ignored}, "
                    f"sem SYNC: {d.unsynced}")
            if not d.empty():
                state["seq"] += 1
                try:
                    await asyncio.to_thread(sink.send, to_batch(d, device_id, state["seq"], int(time.time())))
                except Exception as e:  # noqa: BLE001
                    log(f"erro ao gravar o fluxo da ponte: {e!r}")
            el = time.monotonic() - state["t"]
            if el >= 60:
                log(f"{state['frames'] / el:.1f} frames/s ({state['frames'] * 20 / el:.0f} B/s no máximo)")
                state["frames"], state["t"] = 0, time.monotonic()
    finally:
        await server.stop()


def central_base(url: str) -> str:
    """Base URL of the central: tolerates a trailing slash, `/v1` or the full `/v1/ingest`."""
    u = url.strip().rstrip("/")
    for tail in ("/v1/ingest", "/v1"):
        if u.endswith(tail):
            u = u[: -len(tail)].rstrip("/")
            break
    return u


def http_sink(url: str, token: str, log: Callable[[str], None] = print) -> Sink:
    import httpx

    client = httpx.Client(timeout=15.0)
    endpoint = central_base(url) + "/v1/ingest"

    def send(batch: dict) -> int:
        try:
            r = client.post(endpoint, json=batch, headers={"Authorization": f"Bearer {token}"})
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
    from pulso.ingest import auth
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
        # live *device* tokens only: a painel token is read-only and must not sign uploads here
        return auth.mac_keys(conn)

    return Sink(send=lambda b: pool.submit(_send, b).result(), keys=lambda: pool.submit(_keys).result())
