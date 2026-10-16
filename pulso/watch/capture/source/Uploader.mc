import Toybox.Communications;
import Toybox.Lang;
import Toybox.System;
import Toybox.Time;

module Link {
    const IDLE = 0;
    const SENDING = 1;
    const OK = 2;
    const NOPHONE = 3;
    const AUTH = 4;
    const ERR = 5;
    const NOCFG = 6;
}

//! One send attempt. The callback carries its own attempt id and q, so a late answer to an
//! attempt the uploader already gave up on (STUCK_MS) is never credited to another batch.
class UploadCall {
    private var _up as Uploader;
    private var _id as Number;
    private var _q as Number;

    function initialize(up as Uploader, id as Number, q as Number) {
        _up = up;
        _id = id;
        _q = q;
    }

    function onResult(code as Number, data as Dictionary or Null) as Void {
        _up.onResult(_id, _q, code, data);
    }
}

//! Drains the UploadQueue through a Transport: one request in flight, oldest batch first,
//! back-to-back while it succeeds, exponential backoff on errors (longer without a phone).
//! A batch is only ever dropped for errors that are about the batch itself; link, server
//! availability and configuration errors (no phone, timeouts, 404, 502-504, tunnel down) keep
//! it queued however long they last.
class Uploader {
    const STUCK_MS = 180000;      // the system always calls back; this only guards a lost callback
    const GIVE_UP_TRIES = 8;      // per batch, for batch-specific errors (500/501, -402/-403)

    private var _queue as UploadQueue;
    private var _transport as Transport;
    private var _inFlight as Number or Null = null;   // q of the current attempt
    private var _attempt as Number = 0;               // id of the current attempt
    private var _sentTimer as Number = 0;
    private var _wanted as Boolean = false;
    private var _retryAtS as Number = 0;
    private var _streak as Number = 0;
    private var _failQ as Number = -1;
    private var _failCount as Number = 0;

    var status as Number = 0;
    var lastCode as Number = 0;
    var lastOkS as Number = 0;
    var sent as Number = 0;
    var dups as Number = 0;

    function initialize(queue as UploadQueue) {
        _queue = queue;
        _transport = new HttpTransport();
        status = Cfg.hasCentral() ? Link.IDLE : Link.NOCFG;
    }

    function transportName() as String {
        return _transport.name();
    }

    //! Ask to drain the queue. force also clears any backoff (user "send now", new settings).
    function request(force as Boolean) as Void {
        _wanted = true;
        if (force) {
            resetBackoff();
        }
        pump();
    }

    function resetBackoff() as Void {
        _retryAtS = 0;
        _streak = 0;
        if (status == Link.NOCFG && Cfg.hasCentral()) {
            status = Link.IDLE;
        }
    }

    //! Call once per second.
    function tick(nowS as Number) as Void {
        if (_inFlight != null && System.getTimer() - _sentTimer > STUCK_MS) {
            _inFlight = null;
            lastCode = Communications.NETWORK_REQUEST_TIMED_OUT;
            status = Link.ERR;
            backoff(nowS, lastCode);
        }
        if (_retryAtS != 0 && nowS >= _retryAtS) {
            _retryAtS = 0;
            _wanted = true;
        }
        pump();
    }

    function busy() as Boolean {
        return _inFlight != null;
    }

    //! Seconds until the next retry (0 = not backing off).
    function retryIn(nowS as Number) as Number {
        return (_retryAtS == 0) ? 0 : _retryAtS - nowS;
    }

    private function pump() as Void {
        if (!_wanted || _inFlight != null || _retryAtS != 0) {
            return;
        }
        var q = _queue.peekId();
        if (q == null) {
            _wanted = false;
            return;
        }
        if (!_transport.isAvailable()) {
            status = Link.NOCFG;
            _wanted = false;
            return;
        }
        var b = _queue.load(q);
        if (b == null) {
            _queue.reject(q);   // index pointed at a missing value
            return;             // next tick continues
        }
        b["t"] = Time.now().value();
        _attempt++;
        var id = _attempt;
        _inFlight = q;
        _sentTimer = System.getTimer();
        status = Link.SENDING;
        var call = new UploadCall(self, id, q);
        _transport.send(b, call.method(:onResult) as SendCallback);
    }

    //! Result of attempt `id` for batch q (called through UploadCall).
    function onResult(id as Number, q as Number, code as Number, data as Dictionary or Null) as Void {
        var current = _inFlight != null && id == _attempt;
        var ok = code == 200 || code == 201;
        if (!current && !ok) {
            return;   // late error of an abandoned attempt: the batch is still queued
        }
        if (current) {
            _inFlight = null;
        }
        lastCode = code;
        var nowS = Time.now().value();
        if (ok) {
            if (data != null && data.get("dup") == 1) {
                dups++;
            }
            _queue.remove(q);   // no-op when a late duplicate answer arrives
            sent++;
            lastOkS = nowS;
            if (_inFlight == null) {
                status = Link.OK;
                _streak = 0;
                _failCount = 0;
                _retryAtS = 0;
                _wanted = true;
                pump();
            }
            return;
        }
        if (code == 400 || code == 413 || code == 422
            || code == Communications.BLE_REQUEST_TOO_LARGE
            || code == Communications.INVALID_HTTP_BODY_IN_REQUEST) {
            // the central (or the link) will never take this batch: drop it, keep going
            _queue.reject(q);
            status = Link.ERR;
            pump();
            return;
        }
        var linkDown = code == Communications.BLE_CONNECTION_UNAVAILABLE;
        var auth = code == 401 || code == 403;
        // Only failures tied to this batch count towards giving it up. A server that is down,
        // a tunnel with the laptop asleep (404/502/503/504/52x), timeouts and BLE errors are
        // retried forever with backoff, otherwise a night without the central would drop data.
        var batchFault = code == 500 || code == 501
            || code == Communications.NETWORK_RESPONSE_TOO_LARGE
            || code == Communications.NETWORK_RESPONSE_OUT_OF_MEMORY;
        if (batchFault) {
            if (q == _failQ) {
                _failCount++;
            } else {
                _failQ = q;
                _failCount = 1;
            }
            if (_failCount >= GIVE_UP_TRIES) {
                _queue.reject(q);
                _failCount = 0;
            }
        }
        status = linkDown ? Link.NOPHONE : (auth ? Link.AUTH : Link.ERR);
        backoff(nowS, code);
    }

    private function backoff(nowS as Number, code as Number) as Void {
        _streak++;
        var base = 15;
        var cap = 600;
        if (code == Communications.BLE_CONNECTION_UNAVAILABLE) {
            base = 60;
            cap = 1800;
        } else if (code == 401 || code == 403) {
            base = 1800;
            cap = 1800;
        }
        var d = base;
        for (var i = 1; i < _streak && d < cap; i++) {
            d *= 2;
        }
        if (d > cap) {
            d = cap;
        }
        _retryAtS = nowS + d;
    }
}
