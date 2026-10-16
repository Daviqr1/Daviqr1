import Toybox.ActivityMonitor;
import Toybox.Application;
import Toybox.Background;
import Toybox.Communications;
import Toybox.Lang;
import Toybox.Math;
import Toybox.SensorHistory;
import Toybox.System;
import Toybox.Time;
import Toybox.UserProfile;

//! Builds protocol-v1 batches from SensorHistory / ActivityMonitor (docs/protocolo.md).
//! Request budget from the background: <= ~3 KB of JSON. A full 60-min catch-up could reach
//! hr 61 + stress 21 + bb 21 + spo2/temp/press/elev 13 each = 155 points (~2.9 KB), so the batch
//! is capped at MAX_POINTS (~2.6 KB): the newest points are dropped and, because cursors only
//! advance to what was sent, they go out in the next run.
(:background)
module Harvest {
    const LOOKBACK_S = 3600;   // never read more than the last 60 min (iterator memory in 64 KB)
    const MAX_POINTS = 140;

    //! Returns [batch, windowStarts]; windowStarts[k] is the epoch after which this batch carries
    //! every point of series k (used to de-duplicate older batches still in the outbox).
    function build(cur as Dictionary) as Array {
        var now = Time.now().value();
        var h = {} as Dictionary<String, Array>;
        var win = {} as Dictionary<String, Number>;
        if (Toybox has :SensorHistory) {
            //                                 [min spacing s, max points, valid lo, valid hi, 1 = one decimal]
            series(h, win, cur, now, "hr", [60, 61, 25, 230, 0]);
            series(h, win, cur, now, "stress", [180, 21, 0, 100, 0]);
            series(h, win, cur, now, "bb", [180, 21, 0, 100, 0]);
            series(h, win, cur, now, "spo2", [300, 13, 70, 100, 0]);
            series(h, win, cur, now, "temp", [300, 13, -20, 60, 1]);
            series(h, win, cur, now, "press", [300, 13, 30000, 110000, 0]);
            series(h, win, cur, now, "elev", [300, 13, -500, 9000, 0]);
            cap(h, win);
        }
        var b = {
            "v" => 1,
            "d" => Ids.device(),
            "q" => Ids.nextSeq(),
            "src" => "face",
            "t" => now,
            "h" => h,
            "m" => snapshot(now),
            "bat" => battery(now)
        } as Dictionary<String, Object>;
        return [b, win];
    }

    //! Reads one SensorHistory series into h[key]. `spec` bundles the per-series limits so the call
    //! stays far below the VM's 10-argument limit (which counts the implicit `self`).
    function series(h as Dictionary<String, Array>, win as Dictionary<String, Number>, cur as Dictionary,
                    now as Number, key as String, spec as Array<Number>) as Void {
        var spacing = spec[0];
        var maxN = spec[1];
        var lo = spec[2];
        var hi = spec[3];
        var tenths = spec[4] == 1;
        // firmware timestamps jitter by a few seconds: a strict ">= spacing" would drop every other
        // 3-min stress sample that arrives at 179 s
        var minGap = spacing - spacing / 8;
        var since = now - LOOKBACK_S;
        var lastKept = since - spacing;
        var c = cur[key];
        // a cursor ahead of the clock (clock set back) would stall the series and give a negative period
        if (c instanceof Number && c > since && c <= now) {
            since = c;
            lastKept = c;
        }
        var pts = [] as Array<Array>;
        var complete = false;
        try {
            var it = iterator(key, new Time.Duration(now - since + 60));
            if (it != null) {
                var s = it.next();
                while (s != null && pts.size() < maxN) {
                    var v = s.data;
                    var w = s.when;
                    if (v != null && w != null) {
                        var t = w.value();
                        if (t > since && t - lastKept >= minGap) {
                            var f = v.toFloat();
                            if (f >= lo && f <= hi) {
                                pts.add([t, tenths ? Math.round(f * 10).toNumber() / 10.0 : Math.round(f).toNumber()]);
                                lastKept = t;
                            }
                        }
                    }
                    s = it.next();
                }
                complete = s == null;   // history exhausted, not cut at maxN
            }
        } catch (e) {
            // a history that throws (unsupported, out of memory) just contributes nothing
        }
        if (pts.size() > 0) {
            h[key] = pts;
        }
        // Only a complete read proves this batch carries every point after `since`; strip() relies on
        // that to drop older queued copies, so a failed or truncated read must not claim the window.
        if (complete) {
            win[key] = since;
        }
    }

    //! Keep the batch under MAX_POINTS by dropping the newest points, HR first.
    function cap(h as Dictionary<String, Array>, win as Dictionary<String, Number>) as Void {
        var keys = ["hr", "stress", "bb", "temp", "press", "elev", "spo2"] as Array<String>;
        var total = 0;
        for (var i = 0; i < keys.size(); i++) {
            var a = h[keys[i]];
            total += a != null ? a.size() : 0;
        }
        for (var i = 0; i < keys.size() && total > MAX_POINTS; i++) {
            var k = keys[i];
            var a = h[k];
            if (a == null || a.size() < 2) {
                continue;
            }
            var cut = total - MAX_POINTS;
            if (cut > a.size() - 1) {
                cut = a.size() - 1;
            }
            h[k] = a.slice(0, a.size() - cut);
            total -= cut;
            win.remove(k);
        }
    }

    function iterator(key as String, p as Time.Duration) as SensorHistory.SensorHistoryIterator? {
        var o = {:period => p, :order => SensorHistory.ORDER_OLDEST_FIRST};
        if (key.equals("hr")) {
            if (SensorHistory has :getHeartRateHistory) {
                return SensorHistory.getHeartRateHistory(o);
            }
        } else if (key.equals("stress")) {
            if (SensorHistory has :getStressHistory) {
                return SensorHistory.getStressHistory(o);
            }
        } else if (key.equals("bb")) {
            if (SensorHistory has :getBodyBatteryHistory) {
                return SensorHistory.getBodyBatteryHistory(o);
            }
        } else if (key.equals("spo2")) {
            if (SensorHistory has :getOxygenSaturationHistory) {
                return SensorHistory.getOxygenSaturationHistory(o);
            }
        } else if (key.equals("temp")) {
            if (SensorHistory has :getTemperatureHistory) {
                return SensorHistory.getTemperatureHistory(o);
            }
        } else if (key.equals("press")) {
            if (SensorHistory has :getPressureHistory) {
                return SensorHistory.getPressureHistory(o);
            }
        } else if (key.equals("elev")) {
            if (SensorHistory has :getElevationHistory) {
                return SensorHistory.getElevationHistory(o);
            }
        }
        return null;
    }

    //! ActivityMonitor snapshot `m` (the FR255 stress history can come back empty; this 30-s
    //! rolling stress score is the fallback the central relies on).
    function snapshot(now as Number) as Dictionary<String, Number> {
        var m = {"t" => now} as Dictionary<String, Number>;
        if (Toybox has :ActivityMonitor) {
            var info = ActivityMonitor.getInfo();
            if (info has :stressScore) {
                var s = info.stressScore;
                if (s != null && s >= 0 && s <= 100) {
                    m["stress"] = s;
                }
            }
            if (info has :respirationRate) {
                var r = info.respirationRate;
                if (r != null && r >= 3 && r <= 70) {
                    m["resp"] = r;
                }
            }
            var st = info.steps;
            if (st != null) {
                m["steps"] = st;
            }
        }
        if (Toybox has :UserProfile) {
            try {
                var p = UserProfile.getProfile();
                var rhr = null;
                if (p has :averageRestingHeartRate) {
                    rhr = p.averageRestingHeartRate;
                }
                if (rhr == null && p has :restingHeartRate) {
                    rhr = p.restingHeartRate;
                }
                if (rhr != null && rhr >= 25 && rhr <= 120) {
                    m["rhr"] = rhr;
                }
            } catch (e) {
            }
        }
        return m;
    }

    function battery(now as Number) as Array {
        var s = System.getSystemStats();
        var days = null;
        if (s has :batteryInDays) {
            var d = s.batteryInDays;
            if (d != null) {
                days = (d * 10).toNumber() / 10.0;
            }
        }
        return [now, Math.round(s.battery).toNumber(), days];
    }

    //! Remove from an older queued batch the points a newer batch also carries (t > window start).
    function strip(old as Dictionary, win as Dictionary<String, Number>) as Void {
        var h = old["h"];
        if (!(h instanceof Dictionary)) {
            return;
        }
        var keys = h.keys();
        for (var i = 0; i < keys.size(); i++) {
            var k = keys[i];
            var ws = (k instanceof String) ? win[k] : null;
            var pts = h[k];
            if (ws == null || !(pts instanceof Array)) {
                continue;
            }
            var keep = [] as Array<Array>;
            for (var j = 0; j < pts.size(); j++) {
                var p = pts[j];
                if (p instanceof Array && p.size() == 2) {
                    var t = p[0];
                    if (t instanceof Number && t <= ws) {
                        keep.add(p);
                    }
                }
            }
            if (keep.size() == 0) {
                h.remove(k);
            } else {
                h[k] = keep;
            }
        }
    }
}

//! Background service: one temporal event every 5 min (while this is the active watch face).
//!
//! Each run builds a fresh batch from the acknowledged cursors (last 60 min at most), POSTs it to
//! <central>/v1/ingest with the device bearer token and, if it is acknowledged, advances the
//! cursors and flushes older queued batches while time allows. A batch that fails for a
//! transient reason goes to a bounded outbox (3 batches, oldest dropped) and is re-sent later
//! with the SAME q, so the central's (d, q) idempotency makes retries safe. Cursors only move on
//! a 200, so after an outage the next fresh batch re-reads the last hour from SensorHistory.
//! Background.exit hands {"ok", "t", "q", "n", "face"} to AppBase.onBackgroundData.
(:background)
class PulsoService extends System.ServiceDelegate {
    private const MAX_REQUESTS = 3;
    private const TIME_BUDGET_MS = 15000;   // the system kills the service at 30 s

    private var _url as String = "";
    private var _auth as String = "";
    private var _cur as Dictionary = {};
    private var _obx as Array = [];
    private var _inflight as Dictionary? = null;
    private var _win as Dictionary<String, Number> = {} as Dictionary<String, Number>;
    private var _fresh as Boolean = false;
    private var _face as Dictionary? = null;
    private var _q as Number = 0;
    private var _status as Number? = null;   // result of the fresh batch: what the face reports
    private var _sent as Number = 0;
    private var _t0 as Number = 0;

    function initialize() {
        ServiceDelegate.initialize();
    }

    function onTemporalEvent() as Void {
        _t0 = System.getTimer();
        try {
            start();
        } catch (e) {
            finish(K.CODE_EXCEPTION);
        }
    }

    private function start() as Void {
        var url = Cfg.ingestUrl();
        var tok = Cfg.token();
        if (url == null || tok == null) {
            finish(K.CODE_NO_CONFIG);
            return;
        }
        if (!Cfg.flag("uploadEnabled", true)) {
            finish(K.CODE_DISABLED);
            return;
        }
        _url = url;
        _auth = "Bearer " + tok;

        var c = Store.get(K.CUR);
        if (c instanceof Dictionary) {
            _cur = c;
        }
        var o = Store.get(K.OBX);
        if (o instanceof Array) {
            _obx = o;
        }

        var built = Harvest.build(_cur);
        var b = built[0] as Dictionary;
        _win = built[1] as Dictionary<String, Number>;
        var q = b["q"];
        _q = (q instanceof Number) ? q : 0;

        if (!linkUp()) {
            enqueue(b);
            save();
            finish(K.CODE_NO_PHONE);
            return;
        }
        _fresh = true;
        send(b);
    }

    private function linkUp() as Boolean {
        var ds = System.getDeviceSettings();
        if (ds has :connectionAvailable) {
            return ds.connectionAvailable;
        }
        return ds.phoneConnected;
    }

    private function send(b as Dictionary) as Void {
        _inflight = b;
        _sent += 1;
        b["t"] = Time.now().value();   // send time (clock-skew estimate); not part of the (d, q) key
        Communications.makeWebRequest(_url, b as Dictionary<Object, Object>, {
            :method => Communications.HTTP_REQUEST_METHOD_POST,
            :headers => {
                "Content-Type" => Communications.REQUEST_CONTENT_TYPE_JSON,
                "Authorization" => _auth
            },
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
        }, method(:onResponse));
    }

    function onResponse(code as Number, data as Dictionary or String or Null) as Void {
        try {
            handle(code, data);
        } catch (e) {
            save();
            // an error while flushing old batches must not hide that the fresh upload went through
            var st = _status;
            finish(st != null ? st : K.CODE_EXCEPTION);
        }
    }

    private function handle(code as Number, data as Dictionary or String or Null) as Void {
        var b = _inflight;
        _inflight = null;
        if (b == null) {
            finish(code);
            return;
        }
        if (code == 200) {
            advance(b);
            if (!_fresh) {
                _obx = _obx.slice(1, null);
            }
            if (data instanceof Dictionary) {
                var f = data["face"];
                if (f instanceof Dictionary) {
                    _face = f;
                }
            }
        } else if (_fresh) {
            if (permanent(code)) {
                advance(b);       // the central rejects this content: do not loop on it
            } else {
                enqueue(b);
            }
        } else if (permanent(code)) {
            _obx = _obx.slice(1, null);
        }
        if (_fresh) {
            _status = code;
        }
        _fresh = false;
        save();
        if (code == 200 && _obx.size() > 0 && _sent < MAX_REQUESTS
            && System.getTimer() - _t0 < TIME_BUDGET_MS) {
            var next = _obx[0];
            if (next instanceof Dictionary) {
                send(next);
                return;
            }
            _obx = _obx.slice(1, null);
            save();
        }
        // a failed flush of an old batch must not hide that the fresh upload went through
        var st = _status;
        finish(st != null ? st : code);
    }

    //! 4xx that will not get better by retrying the same bytes (auth and URL errors are config
    //! problems: keep the data until the user fixes the settings).
    private function permanent(code as Number) as Boolean {
        return code == 400 || code == 413 || code == 415 || code == 422;
    }

    //! cursor[k] = max(cursor[k], newest t of series k in an acknowledged batch)
    private function advance(b as Dictionary) as Void {
        var h = b["h"];
        if (!(h instanceof Dictionary)) {
            return;
        }
        var keys = h.keys();
        for (var i = 0; i < keys.size(); i++) {
            var k = keys[i];
            var pts = h[k];
            if (pts instanceof Array && pts.size() > 0) {
                var last = pts[pts.size() - 1];
                if (last instanceof Array && last.size() > 0) {
                    var t = last[0];
                    var c = _cur[k];
                    if (t instanceof Number && (!(c instanceof Number) || t > c)) {
                        _cur[k] = t;
                    }
                }
            }
        }
    }

    private function enqueue(b as Dictionary) as Void {
        for (var i = 0; i < _obx.size(); i++) {
            var o = _obx[i];
            if (o instanceof Dictionary) {
                Harvest.strip(o, _win);
            }
        }
        _obx.add(b);
        while (_obx.size() > K.OUTBOX_MAX) {
            _obx = _obx.slice(1, null);
        }
    }

    private function save() as Void {
        Store.put(K.CUR, _cur);
        // storage full: shed the oldest batches until it fits (data loss is bounded and explicit)
        while (!Store.put(K.OBX, _obx) && _obx.size() > 0) {
            _obx = _obx.slice(1, null);
        }
    }

    private function finish(code as Number) as Void {
        var out = {"ok" => code, "t" => Time.now().value(), "q" => _q, "n" => _obx.size()} as Dictionary<String, Object?>;
        var f = _face;
        if (f != null) {
            out["face"] = f;
        }
        try {
            Background.exit(out as Application.PropertyValueType);
        } catch (e) {
            // ExitDataSizeLimitException: the face object was too big; report the status only
            out.remove("face");
            Background.exit(out as Application.PropertyValueType);
        }
    }
}
