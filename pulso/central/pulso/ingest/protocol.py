"""Wire protocol v1 between the watch (face background service, capture app, BLE bridge) and
the central. Designed around the Connect IQ link: JSON bodies only, ~400-800 B/s over BLE via
Garmin Connect Mobile, requests of 4-8 KB, one request in flight, tiny responses.

Keys are short on purpose (every byte crosses Bluetooth). All times are UNIX epoch; `_s`
fields are seconds (Time.now()), `_ms` fields milliseconds (Time.now() anchored + getTimer()).

    {
      "v": 1,                      protocol version
      "d": "fr255-a1b2",           device id (random, generated on the watch at install)
      "q": 1842,                   per-device sequence number -> idempotency key (d, q)
      "src": "face"|"cap"|"ble",   which component sent it
      "sid": "c-1739...",          capture session id (cap/ble only)
      "t": 1760630400,             send time, epoch s (used to estimate clock skew)
      "h": {"hr": [[t_s, v], ...], "stress": [...], "bb": [...], "spo2": [...],
            "temp": [...], "press": [...], "elev": [...]},     SensorHistory points
      "m": {"t": t_s, "stress": 31, "resp": 14.5, "steps": 5120, "rhr": 52},  ActivityMonitor snapshot
      "rr": [[t_last_ms, [812, 820, ...]], ...],               RR chunks; last beat at t_last_ms
      "hr1": [t0_s, [bpm, bpm, ...]],                           1 Hz HR from Sensor.Info (capture)
      "acc": [[t0_ms, epoch_s, [enmo...], [mad...]], ...],      on-watch 1-s movement features (mg)
      "raw": [[t0_ms, hz, "b64x", "b64y", "b64z"], ...],        raw int16 LE accel, base64 (bridge only)
      "bar": [t0_s, step_s, [pa, ...]],                         raw ambient pressure series
      "ev":  [[t_s, "kind", "label"], ...],                     user markers from the watch buttons
      "bat": [t_s, pct, days]                                   battery telemetry for the governor
    }
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

HISTORY_KEYS = {
    "hr": "hr",
    "stress": "stress_garmin",
    "bb": "body_battery",
    "spo2": "spo2",
    "temp": "temp_c",
    "press": "pressure_pa",
    "elev": "altitude_m",
    "resp": "resp_rate",
}

MAX_RR_PER_CHUNK = 600


class Batch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    d: str = Field(min_length=3, max_length=64)
    q: int = Field(ge=0)
    src: str = Field(pattern=r"^(face|cap|ble)$")
    sid: str | None = Field(default=None, max_length=64)
    t: int | None = None
    h: dict[str, list[tuple[int, float | None]]] = Field(default_factory=dict)
    m: dict[str, float | int | None] | None = None
    rr: list[tuple[int, list[int]]] = Field(default_factory=list)
    hr1: tuple[int, list[int | None]] | None = None
    acc: list[tuple[int, int, list[float], list[float]]] = Field(default_factory=list)
    raw: list[tuple[int, float, str, str, str]] = Field(default_factory=list)
    bar: tuple[int, int, list[float | None]] | None = None
    ev: list[tuple[int, str, str]] = Field(default_factory=list)
    bat: tuple[int, float, float | None] | None = None

    @field_validator("v")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported protocol version {v}")
        return v

    @field_validator("rr")
    @classmethod
    def _rr(cls, v: list) -> list:
        for _, arr in v:
            if len(arr) > MAX_RR_PER_CHUNK:
                raise ValueError("rr chunk too long")
        return v


class Ack(BaseModel):
    ok: int = 1
    ack: int
    dup: int = 0
    face: dict | None = None
    cfg: dict | None = None     # remote configuration for the watch (capture tier, upload cadence)
