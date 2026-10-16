-- Pulso central store (SQLite, WAL).
-- Conventions:
--   * every timestamp is UTC epoch milliseconds (INTEGER, column suffix _ms)
--   * `source` says where a number came from, so we can always ask "how was this measured?"
--     e.g. 'ciq:face', 'ciq:capture', 'ble:bridge', 'fit:monitor', 'gc:api', 'derived:v1'
--   * raw data is append-only and idempotent (INSERT OR IGNORE on natural keys)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_ms  INTEGER NOT NULL
);

-- Low/medium-rate scalar series in long format (HR, Garmin stress, body battery, SpO2,
-- respiration, temperature, pressure, steps, ENMO per second, derived features...).
CREATE TABLE IF NOT EXISTS samples (
    metric  TEXT    NOT NULL,
    ts_ms   INTEGER NOT NULL,
    source  TEXT    NOT NULL,
    value   REAL    NOT NULL,
    PRIMARY KEY (metric, source, ts_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts_ms);

-- Beat-to-beat intervals. ts_ms is the timestamp of the beat that CLOSES the interval.
-- flag: 0 = as received, 1 = corrected/interpolated by the cleaner, 2 = rejected.
CREATE TABLE IF NOT EXISTS rr_intervals (
    source      TEXT    NOT NULL,
    ts_ms       INTEGER NOT NULL,
    rr_ms       INTEGER NOT NULL,
    session_id  TEXT,
    PRIMARY KEY (source, ts_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS rr_ts ON rr_intervals (ts_ms);

-- High-rate multi-axis chunks (accelerometer / gyro / magnetometer). One row per chunk;
-- axes are little-endian int16 arrays (milli-g for accel), rate_hz samples per second.
CREATE TABLE IF NOT EXISTS motion_chunks (
    source      TEXT    NOT NULL,
    sensor      TEXT    NOT NULL,          -- 'accel' | 'gyro' | 'mag'
    t0_ms       INTEGER NOT NULL,          -- timestamp of the first sample
    rate_hz     REAL    NOT NULL,
    n           INTEGER NOT NULL,
    x           BLOB    NOT NULL,
    y           BLOB    NOT NULL,
    z           BLOB    NOT NULL,
    session_id  TEXT,
    PRIMARY KEY (source, sensor, t0_ms)
) WITHOUT ROWID;

-- Anything with a start/end: meetings, calendar entries, commits (end = start), workouts,
-- capture sessions, sleep windows, manual notes ("café", "discussão com X").
CREATE TABLE IF NOT EXISTS events (
    id          TEXT    PRIMARY KEY,
    kind        TEXT    NOT NULL,          -- 'meeting' | 'calendar' | 'commit' | 'workout' | 'session' | 'sleep' | 'note'
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    title       TEXT,
    source      TEXT    NOT NULL,
    attrs       TEXT    NOT NULL DEFAULT '{}'   -- JSON
);
CREATE INDEX IF NOT EXISTS events_time ON events (start_ms, end_ms);
CREATE INDEX IF NOT EXISTS events_kind ON events (kind, start_ms);

-- Transcript segments aligned to absolute time. tags is JSON filled by the AI tagger.
CREATE TABLE IF NOT EXISTS transcript_segments (
    event_id    TEXT    NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    speaker     TEXT,
    text        TEXT    NOT NULL,
    tags        TEXT,
    PRIMARY KEY (event_id, idx)
);
CREATE INDEX IF NOT EXISTS segments_time ON transcript_segments (start_ms);

-- Sleep staging epochs (Garmin's own and ours). stage: 'deep'|'light'|'rem'|'awake'|'unmeasurable'
CREATE TABLE IF NOT EXISTS sleep_epochs (
    source      TEXT    NOT NULL,
    ts_ms       INTEGER NOT NULL,
    duration_s  INTEGER NOT NULL,
    stage       TEXT    NOT NULL,
    PRIMARY KEY (source, ts_ms)
) WITHOUT ROWID;

-- Device tokens: only the SHA-256 of the bearer token is stored.
-- scope: 'device' (watch/bridge: upload + face state) | 'painel' (dashboard: read-only history).
-- Databases created before v2 get the column from store.db._migrate.
CREATE TABLE IF NOT EXISTS device_tokens (
    token_sha256  TEXT    PRIMARY KEY,
    label         TEXT    NOT NULL,
    created_ms    INTEGER NOT NULL,
    revoked_ms    INTEGER,
    scope         TEXT    NOT NULL DEFAULT 'device'
);

-- Idempotency for uploads coming from the watch / bridge (retries must not double-count).
CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id     TEXT    PRIMARY KEY,
    device       TEXT    NOT NULL,
    received_ms  INTEGER NOT NULL,
    n_items      INTEGER NOT NULL
);

-- Generated artefacts: daily/weekly reports, model fits, face state snapshots.
CREATE TABLE IF NOT EXISTS artefacts (
    kind        TEXT    NOT NULL,          -- 'report:daily' | 'report:weekly' | 'face_state' | 'model:*'
    key         TEXT    NOT NULL,          -- e.g. '2026-10-16'
    created_ms  INTEGER NOT NULL,
    body        TEXT    NOT NULL,          -- JSON
    PRIMARY KEY (kind, key)
);
