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
    const READY = 4;     // notifications on, writing frames
    const WAIT = 5;      // nothing found / error: retry later
    const NAMES = ["off", "reg", "busca", "conect", "ok", "espera"];
}

//! Direct BLE link to the Pulso bridge on the laptop (or an ESP32): the watch is the GATT central
//! and writes 20-byte frames to DATA (docs/ponte-ble.md, central/pulso/bridge/frames.py).
//!
//! Two kinds of traffic share the link, one write in flight at a time, alternating:
//!   - the live stream (raw accelerometer + battery), too big for the phone route. It is queued
//!     in "units" (SYNC, BAT, or one ACC_KEY with its deltas); when the link cannot keep up the
//!     oldest unit is dropped whole, so the bridge never sees an orphaned delta;
//!   - one upload-queue batch at a time as a BLOB (JSON, HMAC-SHA256 with SHA-256(token) as key),
//!     acknowledged by the bridge on CTRL only once it is stored.
//! The frame seq is written at transmission time, so a gap on the bridge means frames lost on air.
class BleLink extends BluetoothLowEnergy.BleDelegate {
    const MAX_UNITS = 40;            // ~40 s of 25-Hz accelerometer (~6 frames each)
    const WRITE_TIMEOUT_MS = 3000;
    const SCAN_S = 60;
    const RETRY_S = 120;
    const LOST_S = 20;
    const SYNC_S = 30;
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
    const BLOB_ACK = 0xB1;

    var state as Number = BleSt.OFF;
    var hint as Number = 25;          // accel rate the bridge wants (0 = no raw accelerometer)
    var ackSeq as Number = -1;
    var written as Number = 0;
    var dropped as Number = 0;        // stream units dropped because the link was too slow
    var fails as Number = 0;          // write errors / timeouts
    var blobsOk as Number = 0;
    var frameRate as Float = 0.0;     // frames/s written, last 10 s

    private var _clock as Clock;
    private var _svcU as BluetoothLowEnergy.Uuid;
    private var _dataU as BluetoothLowEnergy.Uuid;
    private var _ctrlU as BluetoothLowEnergy.Uuid;
    private var _registered as Boolean = false;
    private var _dev as BluetoothLowEnergy.Device or Null = null;
    private var _data as BluetoothLowEnergy.Characteristic or Null = null;
    private var _withResp as Boolean = false;

    private var _units as Array<Array<ByteArray>> = [] as Array<Array<ByteArray>>;
    private var _uIdx as Number = 0;              // next frame of _units[0]
    private var _seq as Number = 0;
    private var _busy as Boolean = false;
    private var _busyAt as Number = 0;
    private var _busyBlob as Boolean = false;
    private var _turn as Boolean = false;
    private var _timeouts as Number = 0;
    private var _stateAtS as Number = 0;
    private var _ackAtS as Number = 0;
    private var _writtenAtAck as Number = 0;
    private var _rateAtS as Number = 0;
    private var _rateN as Number = 0;

    private var _session as Number = 0;           // capture session (0 = not streaming)
    private var _syncMs as Long = 0l;             // time base of the last SYNC queued
    private var _syncAtS as Number = 0;
    private var _batAtS as Number = 0;

    private var _blob as ByteArray or Null = null;
    private var _blobQ as Number = 0;
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

    function enable() as Void {
        if (state != BleSt.OFF) {
            return;
        }
        _withResp = Cfg.bool("bleWithResponse", false);
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
    //! starts a new key). t0Ms = time of the first sample, n samples over `period` seconds.
    function pushAccel(t0Ms as Long, x as Array<Numeric>, y as Array<Numeric>, z as Array<Numeric>, n as Number,
            period as Number) as Void {
        if (!streaming() || n <= 0) {
            return;
        }
        var hz = (n + period / 2) / period;
        hz = (hz < 1) ? 1 : ((hz > 255) ? 255 : hz);
        var dt = syncFor(t0Ms);
        var unit = [] as Array<ByteArray>;
        var px = 0;
        var py = 0;
        var pz = 0;
        var d = null as ByteArray or Null;
        var k = 0;
        for (var i = 0; i < n; i++) {
            var sx = i16(x[i]);
            var sy = i16(y[i]);
            var sz = i16(z[i]);
            var dx = sx - px;
            var dy = sy - py;
            var dz = sz - pz;
            if (i > 0 && dx >= -127 && dx <= 127 && dy >= -127 && dy <= 127 && dz >= -127 && dz <= 127) {
                if (d == null) {
                    d = new [18]b;
                    d[1] = T_ACC_DELTA;
                    k = 0;
                }
                var o = 3 + 3 * k;
                d[o] = dx & 0xFF;
                d[o + 1] = dy & 0xFF;
                d[o + 2] = dz & 0xFF;
                k++;
                if (k == 5) {
                    d[2] = 5;
                    unit.add(d);
                    d = null;
                }
            } else {
                if (d != null) {
                    d[2] = k;
                    unit.add(d.slice(0, 3 + 3 * k));
                    d = null;
                }
                var f = new [12]b;
                f[1] = T_ACC_KEY;
                put24(f, 2, dt + (i * 1000) / hz);
                f[5] = hz;
                put16(f, 6, sx);
                put16(f, 8, sy);
                put16(f, 10, sz);
                unit.add(f);
            }
            px = sx;
            py = sy;
            pz = sz;
        }
        if (d != null) {
            d[2] = k;
            unit.add(d.slice(0, 3 + 3 * k));
        }
        addUnit(unit);
        pump();
    }

    //! ms of t since the current SYNC; queues a new SYNC (based 10 s back, so samples a few
    //! seconds old still have dt >= 0) when there is none, it is old, or t falls outside it.
    private function syncFor(tMs as Long) as Number {
        var nowS = _clock.nowS();
        var dt = (_syncMs == 0l) ? -1l : tMs - _syncMs;
        if (_syncMs == 0l || nowS - _syncAtS >= SYNC_S || dt < 0l || dt > 0xFFFFFFl) {
            _syncMs = tMs - 10000l;
            _syncAtS = nowS;
            var f = new [12]b;
            f[1] = T_SYNC;
            var s = _syncMs / 1000l;
            put32(f, 2, s.toNumber());
            put16(f, 6, (_syncMs - s * 1000l).toNumber());
            put32(f, 8, _session);
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
    //! (signature: watch and bridge tokens differ), 503 (retry) or a link error.
    function sendBlob(q as Number, json as ByteArray, cb as SendCallback) as Void {
        if (!ready()) {
            cb.invoke(Communications.BLE_CONNECTION_UNAVAILABLE, null);
            return;
        }
        if (_blobCb != null) {
            finishBlob(Communications.BLE_HOST_TIMEOUT);   // the uploader gave up on the previous one
        }
        var mac = sign(json);
        if (mac == null) {
            cb.invoke(401, null);
            return;
        }
        _blob = json;
        _blobQ = q;
        _blobOff = 0;
        _blobPhase = 0;
        _blobMac = mac;
        _blobCb = cb;
        _blobAtS = _clock.nowS();
        pump();
    }

    //! HMAC-SHA256(SHA-256(token), json)[0..16)
    private function sign(json as ByteArray) as ByteArray or Null {
        var tok = Cfg.token();
        if (tok.length() == 0) {
            return null;
        }
        try {
            var h = new Cryptography.Hash({:algorithm => Cryptography.HASH_SHA256});
            h.update(Json.utf8(tok));
            var mac = new Cryptography.HashBasedMessageAuthenticationCode({
                :algorithm => Cryptography.HASH_SHA256,
                :key => h.digest()
            });
            mac.update(json);
            return mac.digest().slice(0, 16);
        } catch (e) {
            return null;
        }
    }

    private function blobPending() as Boolean {
        return _blob != null && _blobPhase < 3;
    }

    private function nextBlobFrame() as ByteArray {
        var b = _blob as ByteArray;
        var f;
        if (_blobPhase == 0) {
            f = new [6]b;
            f[1] = T_BLOB_START;
            put32(f, 2, _blobQ);
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
        f[0] = _seq;
        _seq = (_seq + 1) & 0xFF;
        try {
            (_data as BluetoothLowEnergy.Characteristic).requestWrite(f, {
                :writeType => _withResp ? BluetoothLowEnergy.WRITE_TYPE_WITH_RESPONSE : BluetoothLowEnergy.WRITE_TYPE_DEFAULT
            });
            _busy = true;
            _busyBlob = isBlob;
            _busyAt = System.getTimer();
            written++;
            _rateN++;
        } catch (e) {
            fails++;
            if (isBlob) {
                finishBlob(Communications.BLE_HOST_TIMEOUT);
            }
        }
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
        } else if (state == BleSt.READY) {
            if (_busy && System.getTimer() - _busyAt > WRITE_TIMEOUT_MS) {
                _busy = false;
                fails++;
                _timeouts++;
                if (_busyBlob) {
                    finishBlob(Communications.BLE_HOST_TIMEOUT);
                }
                if (_timeouts >= 3) {
                    restart();
                    return;
                }
            }
            if (written - _writtenAtAck > 40 && nowS - _ackAtS > ACK_STALL_S) {
                restart();   // writes "succeed" but the bridge stopped acknowledging
                return;
            }
            if (_blob != null && _blobPhase == 3 && nowS - _blobAtS > BLOB_ACK_S) {
                finishBlob(Communications.NETWORK_REQUEST_TIMED_OUT);
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
        for (var r = scanResults.next(); r != null; r = scanResults.next()) {
            if (r instanceof BluetoothLowEnergy.ScanResult && advertises(r)
                && (best == null || r.getRssi() > best.getRssi())) {
                best = r;
            }
        }
        if (best == null) {
            return;
        }
        try {
            BluetoothLowEnergy.setScanState(BluetoothLowEnergy.SCAN_STATE_OFF);
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
            setState(BleSt.READY);
            _ackAtS = _clock.nowS();
            _writtenAtAck = written;
            _timeouts = 0;
            _syncMs = 0l;
            pump();
        } else {
            restart();
        }
    }

    function onCharacteristicWrite(characteristic as BluetoothLowEnergy.Characteristic, status as BluetoothLowEnergy.Status) as Void {
        _busy = false;
        _timeouts = 0;
        if (status != BluetoothLowEnergy.STATUS_SUCCESS) {
            fails++;
            if (_busyBlob) {
                finishBlob(Communications.BLE_HOST_TIMEOUT);
            }
        }
        pump();
    }

    //! CTRL notification: last_seq | accel hint [| 0xB1 | q u32 | status]
    function onCharacteristicChanged(characteristic as BluetoothLowEnergy.Characteristic, value as ByteArray) as Void {
        if (value.size() < 2) {
            return;
        }
        ackSeq = value[0];
        hint = value[1];
        _ackAtS = _clock.nowS();
        _writtenAtAck = written;
        if (value.size() >= 8 && value[2] == BLOB_ACK && _blobCb != null) {
            var q = value[3] | (value[4] << 8) | (value[5] << 16) | (value[6] << 24);
            if (q == _blobQ) {
                var st = value[7];
                finishBlob(st == 0 ? 200 : (st == 1 ? 422 : (st == 2 ? 401 : 503)));
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
