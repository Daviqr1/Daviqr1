import Toybox.BluetoothLowEnergy;
import Toybox.Communications;
import Toybox.Cryptography;
import Toybox.Lang;
import Toybox.System;
import Toybox.Time;

module BleSt {
    const OFF = 0;       // bridge switched off
    const REG = 1;       // registering the GATT profile
    const SCAN = 2;      // looking for the "Pulso Ponte" service
    const CONN = 3;      // paired, waiting for the (re)connection
    const READY = 4;     // notifications on, bridge proven, writing frames
    const WAIT = 5;      // nothing found / error: retry later
    const HELLO = 6;     // connected, waiting for the bridge to prove it holds the token
    const NAMES = ["off", "reg", "busca", "conect", "ok", "espera", "confere"];
}

//! Direct BLE link to the Pulso bridge on the laptop (or an ESP32): the watch is the GATT central
//! and writes 20-byte frames to DATA (docs/ponte-ble.md, central/pulso/bridge/frames.py).
//!
//! Two kinds of traffic share the link, one write in flight at a time, alternating:
//!   - the live stream (raw accelerometer + battery), too big for the phone route. It is queued
//!     in "units" (SYNC, BAT, or one ACC_KEY with its deltas); when the link cannot keep up the
//!     oldest unit is dropped whole, so the bridge never sees an orphaned delta;
//!   - one upload-queue batch at a time as a BLOB (JSON, HMAC-SHA256 with SHA-256(token) as key
//!     over "pulso-blob" | q | JSON, so the open START header cannot relabel it), acknowledged by
//!     the bridge on CTRL only once it is stored.
//! Nothing but HELLO is written until the peripheral proves it holds the key: HELLO carries the
//! key id and a fresh nonce, and the bridge must answer HMAC(key, "pulso-hello" | nonce). Every blob
//! result is signed with the same key and nonce and names the attempt, so a look-alike peripheral
//! advertising the (public) service UUID neither receives batches nor makes the watch delete them.
//! The frame seq is written at transmission time, so a gap on the bridge means frames lost on air.
class BleLink extends BluetoothLowEnergy.BleDelegate {
    const MAX_UNITS = 40;            // ~40 s of 25-Hz accelerometer (~6 frames each)
    const WRITE_TIMEOUT_MS = 3000;
    const SCAN_S = 60;
    const RETRY_S = 120;
    const LOST_S = 20;
    const SYNC_S = 10;               // a lost SYNC costs at most this much raw accelerometer
    const HELLO_S = 5;               // per HELLO try
    const HELLO_TRIES = 3;
    const BAT_S = 60;
    const ACK_STALL_S = 20;
    const BLOB_ACK_S = 60;

    const T_SYNC = 0x00;
    const T_ACC_KEY = 0x02;
    const T_ACC_DELTA = 0x03;
    const T_BAT = 0x07;
    const T_BLOB_START = 0x08;
    const T_BLOB_DATA = 0x09;
    const T_BLOB_END = 0x0A;
    const T_HELLO = 0x0B;
    const BLOB_ACK = 0xB1;
    const HELLO_ACK = 0xB2;

    var state as Number = BleSt.OFF;
    var hint as Number = 25;          // accel rate the bridge wants (0 = no raw accelerometer)
    var ackSeq as Number = -1;
    var written as Number = 0;
    var dropped as Number = 0;        // stream units dropped because the link was too slow
    var fails as Number = 0;          // write errors / timeouts
    var blobsOk as Number = 0;
    var frameRate as Float = 0.0;     // frames/s written, last 10 s
    var authFail as Boolean = false;  // no token here, or the bridge said it does not know it

    private var _clock as Clock;
    private var _svcU as BluetoothLowEnergy.Uuid;
    private var _dataU as BluetoothLowEnergy.Uuid;
    private var _ctrlU as BluetoothLowEnergy.Uuid;
    private var _registered as Boolean = false;
    private var _dev as BluetoothLowEnergy.Device or Null = null;
    private var _data as BluetoothLowEnergy.Characteristic or Null = null;
    private var _withResp as Boolean = false;
    private var _devScan as BluetoothLowEnergy.ScanResult or Null = null;
    private var _rejected as BluetoothLowEnergy.ScanResult or Null = null;   // failed the proof

    private var _tok as String = "";             // token of this connection
    private var _key as ByteArray or Null = null; // SHA-256(token)
    private var _nonce as ByteArray = []b;        // this connection's HELLO nonce (acks are bound to it)
    private var _proof as ByteArray = []b;        // what the bridge must answer to HELLO
    private var _helloTries as Number = 0;
    private var _helloAtS as Number = 0;

    private var _units as Array<Array<ByteArray>> = [] as Array<Array<ByteArray>>;
    private var _uIdx as Number = 0;              // next frame of _units[0]
    private var _seq as Number = 0;
    private var _busy as Boolean = false;
    private var _busyAt as Number = 0;
    private var _busyBlob as Boolean = false;     // the write in flight is a blob frame
    private var _late as Boolean = false;         // ... and passed WRITE_TIMEOUT_MS: callback still owed
    private var _turn as Boolean = false;
    private var _timeouts as Number = 0;
    private var _stateAtS as Number = 0;
    private var _ackAtS as Number = 0;
    private var _writtenAtAck as Number = 0;
    private var _rateAtS as Number = 0;
    private var _rateN as Number = 0;

    private var _session as Number = 0;           // capture session (0 = not streaming)
    private var _syncMs as Long = 0l;             // time base of the last SYNC queued
    private var _syncId as Number = 0;            // its id, repeated in every ACC_KEY timed against it
    private var _syncAtS as Number = 0;
    private var _batAtS as Number = 0;

    private var _blob as ByteArray or Null = null;
    private var _blobQ as Number = 0;
    private var _blobAttempt as Number = 0;       // echoed by the bridge's ack
    private var _blobOff as Number = 0;
    private var _blobPhase as Number = 0;         // 0 START, 1 DATA, 2 END, 3 waiting for the ack
    private var _blobMac as ByteArray = []b;
    private var _blobCb as SendCallback or Null = null;
    private var _blobAtS as Number = 0;

    function initialize(clock as Clock) {
        BleDelegate.initialize();
        _clock = clock;
        _svcU = BluetoothLowEnergy.stringToUuid("6c1e0001-5b7a-4d6e-9f2a-50554c534f31");
        _dataU = BluetoothLowEnergy.stringToUuid("6c1e0002-5b7a-4d6e-9f2a-50554c534f31");
        _ctrlU = BluetoothLowEnergy.stringToUuid("6c1e0003-5b7a-4d6e-9f2a-50554c534f31");
    }

    function ready() as Boolean {
        return state == BleSt.READY && _data != null;
    }

    function streaming() as Boolean {
        return ready() && _session != 0 && hint > 0;
    }

    function stateName() as String {
        return BleSt.NAMES[state] as String;
    }

    function queuedUnits() as Number {
        return _units.size();
    }

    // ---- on / off ---------------------------------------------------------------------------

    //! Also called on every settings change while the bridge is on: the write type applies to the
    //! next frame, and a new token restarts the connection (the bridge must prove the new key).
    function enable() as Void {
        _withResp = Cfg.bool("bleWithResponse", false);
        if (state != BleSt.OFF) {
            if ((state == BleSt.HELLO || state == BleSt.READY) && !Cfg.token().equals(_tok)) {
                restart();
            }
            return;
        }
        if (_registered) {
            startScan();
            return;
        }
        try {
            BluetoothLowEnergy.setDelegate(self);
            BluetoothLowEnergy.registerProfile({
                :uuid => _svcU,
                :characteristics => [
                    {:uuid => _dataU},
                    {:uuid => _ctrlU, :descriptors => [BluetoothLowEnergy.cccdUuid()]}
                ]
            });
            setState(BleSt.REG);
        } catch (e) {
            setState(BleSt.WAIT);
        }
    }

    function disable() as Void {
        if (state == BleSt.OFF) {
            return;
        }
        try {
            BluetoothLowEnergy.setScanState(BluetoothLowEnergy.SCAN_STATE_OFF);
        } catch (e) {
        }
        drop();
        authFail = false;
        _rejected = null;
        setState(BleSt.OFF);
    }

    //! Capture session that the stream belongs to (0 = capture stopped: no stream).
    function setSession(s as Number) as Void {
        if (s != _session) {
            _session = s;
            _syncMs = 0l;   // the next stream frame starts with a fresh SYNC
        }
    }

    // ---- stream -----------------------------------------------------------------------------

    //! One sensor callback worth of accelerometer (mG): ACC_KEY + deltas (a delta beyond ±127 mG
    //! starts a new key). t0Ms = time of the first sample, n samples over `period` seconds. Above
    //! the rate the bridge asks for (hint, 25 Hz by default) groups of k samples are averaged
    //! (k = ceil(rate / hint)): 100 Hz in MAX would need ~4x the frames the link can carry.
    function pushAccel(t0Ms as Long, x as Array<Numeric>, y as Array<Numeric>, z as Array<Numeric>, n as Number,
            period as Number) as Void {
        if (!streaming() || n <= 0) {
            return;
        }
        var rate = (n + period / 2) / period;
        var k = (rate > hint) ? (rate + hint - 1) / hint : 1;
        var nOut = (n + k - 1) / k;
        var hz = (nOut + period / 2) / period;
        hz = (hz < 1) ? 1 : ((hz > 255) ? 255 : hz);
        var dt = syncFor(t0Ms);
        var unit = [] as Array<ByteArray>;
        var px = 0;
        var py = 0;
        var pz = 0;
        var d = null as ByteArray or Null;
        var m = 0;
        for (var j = 0; j < nOut; j++) {
            var sx;
            var sy;
            var sz;
            if (k == 1) {
                sx = i16(x[j]);
                sy = i16(y[j]);
                sz = i16(z[j]);
            } else {
                var a = j * k;
                var b = (a + k > n) ? n : a + k;
                var ax = 0;
                var ay = 0;
                var az = 0;
                for (var i = a; i < b; i++) {
                    ax += x[i].toNumber();
                    ay += y[i].toNumber();
                    az += z[i].toNumber();
                }
                sx = i16(ax / (b - a));
                sy = i16(ay / (b - a));
                sz = i16(az / (b - a));
            }
            var dx = sx - px;
            var dy = sy - py;
            var dz = sz - pz;
            if (j > 0 && dx >= -127 && dx <= 127 && dy >= -127 && dy <= 127 && dz >= -127 && dz <= 127) {
                if (d == null) {
                    d = new [18]b;
                    d[1] = T_ACC_DELTA;
                    m = 0;
                }
                var o = 3 + 3 * m;
                d[o] = dx & 0xFF;
                d[o + 1] = dy & 0xFF;
                d[o + 2] = dz & 0xFF;
                m++;
                if (m == 5) {
                    d[2] = 5;
                    unit.add(d);
                    d = null;
                }
            } else {
                if (d != null) {
                    d[2] = m;
                    unit.add(d.slice(0, 3 + 3 * m));
                    d = null;
                }
                var f = new [13]b;
                f[1] = T_ACC_KEY;
                put24(f, 2, dt + (j * 1000) / hz);
                f[5] = hz;
                put16(f, 6, sx);
                put16(f, 8, sy);
                put16(f, 10, sz);
                f[12] = _syncId;   // the bridge drops it unless it has this very SYNC
                unit.add(f);
            }
            px = sx;
            py = sy;
            pz = sz;
        }
        if (d != null) {
            d[2] = m;
            unit.add(d.slice(0, 3 + 3 * m));
        }
        addUnit(unit);
        pump();
    }

    //! ms of t since the current SYNC; queues a new SYNC (based 10 s back, so samples a few
    //! seconds old still have dt >= 0) when there is none, it is old, or t falls outside it.
    //! Each SYNC gets a new id that the ACC_KEYs after it repeat.
    private function syncFor(tMs as Long) as Number {
        var nowS = _clock.nowS();
        var dt = (_syncMs == 0l) ? -1l : tMs - _syncMs;
        if (_syncMs == 0l || nowS - _syncAtS >= SYNC_S || dt < 0l || dt > 0xFFFFFFl) {
            _syncMs = tMs - 10000l;
            _syncAtS = nowS;
            _syncId = (_syncId + 1) & 0xFF;
            var f = new [13]b;
            f[1] = T_SYNC;
            var s = _syncMs / 1000l;
            put32(f, 2, s.toNumber());
            put16(f, 6, (_syncMs - s * 1000l).toNumber());
            put32(f, 8, _session);
            f[12] = _syncId;
            // a SYNC with no data after it is useless: replace it instead of stacking
            var last = _units.size() - 1;
            if (last >= 1 || (last == 0 && _uIdx == 0)) {
                var lu = _units[last];
                if (lu.size() == 1 && lu[0][1] == T_SYNC) {
                    _units[last] = [f] as Array<ByteArray>;
                    return (tMs - _syncMs).toNumber();
                }
            }
            addUnit([f] as Array<ByteArray>);
            dt = tMs - _syncMs;
        }
        return dt.toNumber();
    }

    private function addUnit(u as Array<ByteArray>) as Void {
        _units.add(u);
        while (_units.size() > MAX_UNITS) {
            dropOldestUnit();
        }
    }

    //! Drop the oldest unit that is not a SYNC (later frames are timed against it) and not
    //! half-written; whole units only, so the stream never resumes on a delta.
    private function dropOldestUnit() as Void {
        var start = (_uIdx > 0) ? 1 : 0;
        for (var i = start; i < _units.size(); i++) {
            if (_units[i][0][1] != T_SYNC) {
                _units = cut(_units, i);
                dropped++;
                return;
            }
        }
        // only SYNCs left (nothing worth keeping): keep the newest
        var lastU = _units[_units.size() - 1];
        _units = (start == 1) ? [_units[0], lastU] as Array<Array<ByteArray>> : [lastU] as Array<Array<ByteArray>>;
    }

    private function cut(a as Array<Array<ByteArray>>, i as Number) as Array<Array<ByteArray>> {
        var out = a.slice(0, i) as Array<Array<ByteArray>>;
        out.addAll(a.slice(i + 1, null));
        return out;
    }

    // ---- blobs (upload-queue batches) --------------------------------------------------------

    //! Send one protocol-v1 batch; cb gets 200 once the bridge stored it, 422 (rejected), 401
    //! (signature: watch and bridge tokens differ), 503 (retry) or a link error. Each call is a new
    //! attempt: the bridge silently drops an unfinished earlier one and its ack names the attempt.
    function sendBlob(q as Number, json as ByteArray, cb as SendCallback) as Void {
        if (!ready()) {
            cb.invoke(Communications.BLE_CONNECTION_UNAVAILABLE, null);
            return;
        }
        if (_blobCb != null) {
            finishBlob(Communications.BLE_HOST_TIMEOUT);   // the uploader gave up on the previous one
        }
        // q is under the MAC: a genuine blob replayed by someone else under the q of the batch in
        // flight fails the bridge's check instead of earning a signed "rejected" for it
        var q4 = new [4]b;
        put32(q4, 0, q);
        var mac = (_key != null) ? hmac(_key as ByteArray, Json.utf8("pulso-blob"), q4, json, 16) : null;
        if (mac == null) {
            cb.invoke(401, null);
            return;
        }
        _blob = json;
        _blobQ = q;
        _blobAttempt = (_blobAttempt + 1) & 0xFF;
        _blobOff = 0;
        _blobPhase = 0;
        _blobMac = mac;
        _blobCb = cb;
        _blobAtS = _clock.nowS();
        pump();
    }

    //! SHA-256(token), the key shared with the bridge (null without a token).
    private function keyOf(tok as String) as ByteArray or Null {
        if (tok.length() == 0) {
            return null;
        }
        try {
            var h = new Cryptography.Hash({:algorithm => Cryptography.HASH_SHA256});
            h.update(Json.utf8(tok));
            return h.digest();
        } catch (e) {
            return null;
        }
    }

    //! HMAC-SHA256(key, a | b | c)[0..n), null on error.
    private function hmac(key as ByteArray, a as ByteArray, b as ByteArray or Null, c as ByteArray or Null,
            n as Number) as ByteArray or Null {
        try {
            var mac = new Cryptography.HashBasedMessageAuthenticationCode({
                :algorithm => Cryptography.HASH_SHA256,
                :key => key
            });
            mac.update(a);
            if (b != null) {
                mac.update(b);
            }
            if (c != null) {
                mac.update(c);
            }
            return mac.digest().slice(0, n);
        } catch (e) {
            return null;
        }
    }

    //! a[o..o+n) == b[0..n), without stopping at the first difference.
    private function same(a as ByteArray, o as Number, b as ByteArray or Null, n as Number) as Boolean {
        if (b == null || a.size() < o + n || b.size() < n) {
            return false;
        }
        var d = 0;
        for (var i = 0; i < n; i++) {
            d |= a[o + i] ^ b[i];
        }
        return d == 0;
    }

    private function blobPending() as Boolean {
        return _blob != null && _blobPhase < 3;
    }

    private function nextBlobFrame() as ByteArray {
        var b = _blob as ByteArray;
        var f;
        if (_blobPhase == 0) {
            f = new [7]b;
            f[1] = T_BLOB_START;
            put32(f, 2, _blobQ);
            f[6] = _blobAttempt;
            _blobPhase = 1;
        } else if (_blobPhase == 1) {
            var n = b.size() - _blobOff;
            if (n > 18) {
                n = 18;
            }
            f = new [2 + n]b;
            f[1] = T_BLOB_DATA;
            for (var i = 0; i < n; i++) {
                f[2 + i] = b[_blobOff + i];
            }
            _blobOff += n;
            if (_blobOff >= b.size()) {
                _blobPhase = 2;
            }
        } else {
            f = new [20]b;
            f[1] = T_BLOB_END;
            put16(f, 2, b.size());
            for (var i = 0; i < 16; i++) {
                f[4 + i] = _blobMac[i];
            }
            _blobPhase = 3;
            _blobAtS = _clock.nowS();
        }
        return f;
    }

    private function finishBlob(code as Number) as Void {
        var cb = _blobCb;
        _blob = null;
        _blobCb = null;
        _blobPhase = 0;
        if (cb != null) {
            if (code == 200) {
                blobsOk++;
            }
            cb.invoke(code, null);
        }
    }

    //! Status of a blob ack (0xB1 | q u32 | attempt u8 | status u8 | mac[8]) for the attempt in
    //! flight; -1 when it names another batch or attempt, -2 when it names ours but its MAC does
    //! not check: mac = HMAC(key, "pulso-ack" | nonce | q | attempt | status)[0..8).
    private function ackStatus(v as ByteArray) as Number {
        var q = v[3] | (v[4] << 8) | (v[5] << 16) | (v[6] << 24);
        if (q != _blobQ || v[7] != _blobAttempt || _key == null) {
            return -1;
        }
        var mac = hmac(_key as ByteArray, Json.utf8("pulso-ack"), _nonce, v.slice(3, 9), 8);
        return same(v, 9, mac, 8) ? v[8] : -2;
    }

    // ---- handshake ------------------------------------------------------------------------------

    //! HELLO: key id (SHA-256(key)[0..4)) + 12-byte nonce. The first try on a connection draws the
    //! nonce; resends keep it, so a late answer to an earlier try still checks.
    private function sendHello() as Boolean {
        if (state != BleSt.HELLO) {
            var tok = Cfg.token();
            var key = keyOf(tok);
            if (key == null) {
                authFail = tok.length() == 0;
                return false;
            }
            var nonce = Cryptography.randomBytes(12);
            var proof = hmac(key, Json.utf8("pulso-hello"), nonce, null, 16);
            if (proof == null) {
                return false;
            }
            _tok = tok;
            _key = key;
            _nonce = nonce;
            _proof = proof;
            _helloTries = 0;
            setState(BleSt.HELLO);
        }
        var id;
        try {
            var h = new Cryptography.Hash({:algorithm => Cryptography.HASH_SHA256});
            h.update(_key as ByteArray);
            id = h.digest();
        } catch (e) {
            return false;
        }
        var f = new [18]b;
        f[1] = T_HELLO;
        for (var i = 0; i < 4; i++) {
            f[2 + i] = id[i];
        }
        for (var i = 0; i < 12; i++) {
            f[6 + i] = _nonce[i];
        }
        _helloTries++;
        _helloAtS = _clock.nowS();
        return write(f, false);
    }

    //! 0xB2 | proof[16]: the bridge holds the key. 0xB2 alone: it does not know this token.
    private function helloAnswer(v as ByteArray) as Void {
        if (same(v, 3, _proof, 16)) {
            authFail = false;
            _rejected = null;
            setState(BleSt.READY);
            _ackAtS = _clock.nowS();
            _writtenAtAck = written;
            _timeouts = 0;
            _syncMs = 0l;
            pump();
        } else if (v.size() < 19) {
            authFail = true;   // our bridge, other token: nothing to do until a setting changes
            drop();
            setState(BleSt.WAIT);
        } else {
            _rejected = _devScan;   // not a bridge that holds our key: look for another one
            restart();
        }
    }

    // ---- writer -----------------------------------------------------------------------------

    private function pump() as Void {
        if (!ready() || _busy) {
            return;
        }
        var haveStream = _units.size() > 0;
        var haveBlob = blobPending();
        if (!haveStream && !haveBlob) {
            return;
        }
        var f;
        var isBlob = haveBlob && (_turn || !haveStream);
        if (isBlob) {
            f = nextBlobFrame();
            _turn = false;
        } else {
            var u = _units[0];
            f = u[_uIdx];
            _uIdx++;
            if (_uIdx >= u.size()) {
                _units = _units.slice(1, null) as Array<Array<ByteArray>>;
                _uIdx = 0;
            }
            _turn = true;
        }
        if (!write(f, isBlob) && isBlob) {
            finishBlob(Communications.BLE_HOST_TIMEOUT);
        }
    }

    //! The one GATT write in flight (seq stamped here, at transmission time).
    private function write(f as ByteArray, isBlob as Boolean) as Boolean {
        f[0] = _seq;
        _seq = (_seq + 1) & 0xFF;
        try {
            (_data as BluetoothLowEnergy.Characteristic).requestWrite(f, {
                :writeType => _withResp ? BluetoothLowEnergy.WRITE_TYPE_WITH_RESPONSE : BluetoothLowEnergy.WRITE_TYPE_DEFAULT
            });
            _busy = true;
            _late = false;
            _busyBlob = isBlob;
            _busyAt = System.getTimer();
            written++;
            _rateN++;
            return true;
        } catch (e) {
            fails++;
            return false;
        }
    }

    //! A write the system has not confirmed. Past WRITE_TIMEOUT_MS it counts as a failure, but its
    //! callback is still owed: no other write goes out before it (one GATT operation at a time, and
    //! a late callback is never mistaken for a newer write's). Returns false when the link had to
    //! be restarted (no callback after twice the timeout, or three slow writes in a row).
    private function writeAlive() as Boolean {
        var age = System.getTimer() - _busyAt;
        if (age <= WRITE_TIMEOUT_MS) {
            return true;
        }
        if (!_late) {
            _late = true;
            fails++;
            _timeouts++;
        }
        if (age > 2 * WRITE_TIMEOUT_MS || _timeouts >= 3) {
            restart();
            return false;
        }
        return true;
    }

    //! Once per second from the capture tick.
    function tick(nowS as Number, batPct as Number, drainX10 as Number) as Void {
        if (nowS - _rateAtS >= 10) {
            frameRate = (_rateAtS == 0) ? 0.0 : _rateN.toFloat() / (nowS - _rateAtS).toFloat();
            _rateAtS = nowS;
            _rateN = 0;
        }
        if (state == BleSt.SCAN && nowS - _stateAtS >= SCAN_S) {
            try {
                BluetoothLowEnergy.setScanState(BluetoothLowEnergy.SCAN_STATE_OFF);
            } catch (e) {
            }
            _rejected = null;   // nothing else around: the next scan may try it again
            setState(BleSt.WAIT);
        } else if (state == BleSt.WAIT && nowS - _stateAtS >= RETRY_S) {
            if (_registered) {
                startScan();
            } else {
                state = BleSt.OFF;
                enable();
            }
        } else if (state == BleSt.CONN && nowS - _stateAtS >= LOST_S) {
            restart();   // the system did not reconnect: forget the device and look again
        } else if (state == BleSt.HELLO) {
            if (_busy) {
                writeAlive();
            } else if (nowS - _helloAtS >= HELLO_S && (_helloTries >= HELLO_TRIES || !sendHello())) {
                _rejected = _devScan;   // no proof: not a Pulso bridge (or a dead one)
                restart();
            }
        } else if (state == BleSt.READY) {
            if (_busy && !writeAlive()) {
                return;
            }
            if (written - _writtenAtAck > 40 && nowS - _ackAtS > ACK_STALL_S) {
                restart();   // writes "succeed" but the bridge stopped acknowledging
                return;
            }
            if (_blob != null && _blobPhase == 3 && nowS - _blobAtS > BLOB_ACK_S) {
                // no valid answer: the bridge may have lost this connection's HELLO (restarted, or
                // another HELLO replaced it), so prove it again before the next attempt
                finishBlob(Communications.NETWORK_REQUEST_TIMED_OUT);
                restart();
                return;
            }
            if (streaming() && nowS - _batAtS >= BAT_S) {
                _batAtS = nowS;
                var f = new [4]b;
                f[1] = T_BAT;
                f[2] = (batPct < 0) ? 0 : ((batPct > 100) ? 100 : batPct);
                f[3] = (drainX10 < 0) ? 0 : ((drainX10 > 255) ? 255 : drainX10);
                addUnit([f] as Array<ByteArray>);
            }
            pump();
        }
    }

    // ---- connection management ----------------------------------------------------------------

    private function setState(s as Number) as Void {
        state = s;
        _stateAtS = _clock.nowS();
    }

    private function startScan() as Void {
        if (Cfg.token().length() == 0) {
            authFail = true;   // nothing to prove the bridge with: do not even scan
            setState(BleSt.WAIT);
            return;
        }
        try {
            BluetoothLowEnergy.setScanState(BluetoothLowEnergy.SCAN_STATE_SCANNING);
            setState(BleSt.SCAN);
        } catch (e) {
            setState(BleSt.WAIT);
        }
    }

    //! Forget the device (and whatever was queued for it) and start looking again.
    private function restart() as Void {
        drop();
        startScan();
    }

    private function drop() as Void {
        var d = _dev;
        _dev = null;
        _data = null;
        _busy = false;
        _late = false;
        _timeouts = 0;
        _units = [] as Array<Array<ByteArray>>;
        _uIdx = 0;
        _syncMs = 0l;
        if (d != null) {
            try {
                BluetoothLowEnergy.unpairDevice(d);
            } catch (e) {
            }
        }
        if (_blobCb != null) {
            finishBlob(Communications.BLE_CONNECTION_UNAVAILABLE);
        }
    }

    function onProfileRegister(uuid as BluetoothLowEnergy.Uuid, status as BluetoothLowEnergy.Status) as Void {
        if (state != BleSt.REG) {
            return;
        }
        if (status == BluetoothLowEnergy.STATUS_SUCCESS) {
            _registered = true;
            startScan();
        } else {
            setState(BleSt.WAIT);
        }
    }

    function onScanResults(scanResults as BluetoothLowEnergy.Iterator) as Void {
        if (state != BleSt.SCAN) {
            return;
        }
        var best = null as BluetoothLowEnergy.ScanResult or Null;
        var bad = _rejected;
        for (var r = scanResults.next(); r != null; r = scanResults.next()) {
            if (r instanceof BluetoothLowEnergy.ScanResult && advertises(r)
                && (bad == null || !r.isSameDevice(bad))
                && (best == null || r.getRssi() > best.getRssi())) {
                best = r;
            }
        }
        if (best == null) {
            return;
        }
        try {
            BluetoothLowEnergy.setScanState(BluetoothLowEnergy.SCAN_STATE_OFF);
            _devScan = best;
            _dev = BluetoothLowEnergy.pairDevice(best);
            setState(_dev != null ? BleSt.CONN : BleSt.WAIT);
        } catch (e) {
            setState(BleSt.WAIT);
        }
    }

    private function advertises(r as BluetoothLowEnergy.ScanResult) as Boolean {
        var it = r.getServiceUuids();
        for (var u = it.next(); u != null; u = it.next()) {
            if (u instanceof BluetoothLowEnergy.Uuid && u.equals(_svcU)) {
                return true;
            }
        }
        return false;
    }

    function onConnectedStateChanged(device as BluetoothLowEnergy.Device, cs as BluetoothLowEnergy.ConnectionState) as Void {
        if (state == BleSt.OFF || _dev == null) {
            return;   // switched off, or a late event for a device we already forgot
        }
        if (cs != BluetoothLowEnergy.CONNECTION_STATE_CONNECTED) {
            _data = null;
            _busy = false;
            _late = false;
            if (_blobCb != null) {
                finishBlob(Communications.BLE_CONNECTION_UNAVAILABLE);
            }
            setState(BleSt.CONN);   // the system may reconnect a paired device; LOST_S decides
            return;
        }
        _dev = device;
        var svc = device.getService(_svcU);
        var data = (svc != null) ? svc.getCharacteristic(_dataU) : null;
        var ctrl = (svc != null) ? svc.getCharacteristic(_ctrlU) : null;
        var cccd = (ctrl != null) ? ctrl.getDescriptor(BluetoothLowEnergy.cccdUuid()) : null;
        if (data == null || cccd == null) {
            restart();   // not a Pulso bridge after all
            return;
        }
        _data = data;
        try {
            cccd.requestWrite([0x01, 0x00]b);   // notifications on CTRL: acks
            _busy = true;
            _busyAt = System.getTimer();
        } catch (e) {
            restart();
        }
    }

    function onDescriptorWrite(descriptor as BluetoothLowEnergy.Descriptor, status as BluetoothLowEnergy.Status) as Void {
        if (state != BleSt.CONN) {
            return;   // late answer from a connection we already dropped
        }
        _busy = false;
        if (status == BluetoothLowEnergy.STATUS_SUCCESS && _data != null) {
            _timeouts = 0;
            if (!sendHello()) {
                drop();   // no token (or no crypto): retrying right away would only spin
                setState(BleSt.WAIT);
            }
        } else {
            restart();
        }
    }

    function onCharacteristicWrite(characteristic as BluetoothLowEnergy.Characteristic, status as BluetoothLowEnergy.Status) as Void {
        if (!_busy || (state != BleSt.READY && state != BleSt.HELLO)) {
            return;   // nothing of ours is outstanding: a dropped connection's late answer
        }
        var late = _late;
        _busy = false;
        _late = false;
        if (!late) {
            _timeouts = 0;
        }
        if (status != BluetoothLowEnergy.STATUS_SUCCESS) {
            if (!late) {
                fails++;
            }
            if (_busyBlob) {
                finishBlob(Communications.BLE_HOST_TIMEOUT);
            }
        }
        pump();
    }

    //! CTRL notification: last_seq | accel hint [| 0xB2 | proof] [| 0xB1 | q | attempt | status | mac]
    function onCharacteristicChanged(characteristic as BluetoothLowEnergy.Characteristic, value as ByteArray) as Void {
        if (value.size() < 2) {
            return;
        }
        ackSeq = value[0];
        hint = value[1];
        _ackAtS = _clock.nowS();
        _writtenAtAck = written;
        if (value.size() >= 3 && value[2] == HELLO_ACK) {
            if (state == BleSt.HELLO) {
                helloAnswer(value);
            }
            return;
        }
        if (value.size() >= 17 && value[2] == BLOB_ACK && _blobCb != null && state == BleSt.READY) {
            var st = ackStatus(value);
            if (st >= 0) {
                finishBlob(st == 0 ? 200 : (st == 1 ? 422 : (st == 2 ? 401 : 503)));
            } else if (st == -2) {
                // our attempt, signed for another nonce: the bridge's HELLO state moved on. Never
                // act on it (the batch stays queued); prove the bridge again.
                finishBlob(503);
                restart();
                return;
            }
        }
        pump();
    }

    // ---- bytes ----------------------------------------------------------------------------------

    private function i16(v as Numeric) as Number {
        var n = v.toNumber();
        return (n < -32768) ? -32768 : ((n > 32767) ? 32767 : n);
    }

    private function put16(f as ByteArray, o as Number, v as Number) as Void {
        f[o] = v & 0xFF;
        f[o + 1] = (v >> 8) & 0xFF;
    }

    private function put24(f as ByteArray, o as Number, v as Number) as Void {
        f[o] = v & 0xFF;
        f[o + 1] = (v >> 8) & 0xFF;
        f[o + 2] = (v >> 16) & 0xFF;
    }

    private function put32(f as ByteArray, o as Number, v as Number) as Void {
        f[o] = v & 0xFF;
        f[o + 1] = (v >> 8) & 0xFF;
        f[o + 2] = (v >> 16) & 0xFF;
        f[o + 3] = (v >> 24) & 0xFF;
    }
}
