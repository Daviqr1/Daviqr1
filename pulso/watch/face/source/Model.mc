import Toybox.ActivityMonitor;
import Toybox.Complications;
import Toybox.Lang;
import Toybox.SensorHistory;
import Toybox.System;
import Toybox.Time;

//! Freshness limits and ring modes.
module Lim {
    const RING_NONE = 0;
    const RING_CENTRAL = 1;       // face.tr, activation -9..9 per hour
    const RING_GARMIN = 2;        // Garmin stress 0..100 per local clock hour
    const FRESH_S = 900;          // central "agora" values older than 15 min are not shown
    const RING_MAX_AGE_S = 10800; // face.tr / face.nm older than 3 h are not shown
}

//! Everything the face shows, gathered from: the central's `face` object (Storage), the last
//! upload status, complications, ActivityMonitor, SensorHistory and a local stress accumulator.
//! Every source can be missing; getters return null and the renderer shows "--".
class Model {
    // central state
    var face as Dictionary? = null;
    var faceT as Number = 0;
    // last upload status
    var stCode as Number? = null;
    var stPending as Number = 0;
    // complications (null = not subscribed / no value)
    var cBB as Number? = null;
    var cHR as Number? = null;
    var cResp as Number? = null;
    var cRecMin as Number? = null;
    var cStress as Number? = null;
    var cCal as String? = null;
    // 12-hour ring
    var ringMode as Number = 0;
    var ringT0 as Number = 0;          // epoch s where bin 0 starts; bins are 3600 s
    var ring as Array = new [12];
    // bumped on every change that must re-render the buffer
    var version as Number = 0;

    private var _ringAt as Number = 0;
    private var _accHour as Array = new [12];   // local-hour index held by each slot
    private var _accSum as Array = new [12];
    private var _accN as Array = new [12];
    private var _accDirty as Boolean = false;
    private var _lastMinute as Number = -1;

    function initialize() {
        load();
        loadAcc();
    }

    //! Re-read the central state and upload status from Storage.
    function load() as Void {
        var f = Store.get(K.FACE);
        face = (f instanceof Dictionary) ? f : null;
        var t = J.num(face, "t");
        faceT = t != null ? t : 0;
        var st = Store.get(K.ST);
        if (st instanceof Array && st.size() >= 4) {
            stCode = J.toNum(st[0]);
            var n = J.toNum(st[3]);
            stPending = n != null ? n : 0;
        }
        _ringAt = 0;
        version++;
    }

    //! Called from every onUpdate. Once per minute it samples the 30-s stress score into the local
    //! accumulator; the ring is recomputed after new data, every 10 min and on each new hour.
    function tick(now as Number, tz as Number) as Void {
        var minute = now / 60;
        if (minute != _lastMinute) {
            _lastMinute = minute;
            var s = stressNow();
            if (s != null) {
                accAdd((now + tz) / 3600, s);
            }
            if (minute % 15 == 0) {
                saveAcc();
            }
        }
        if (_ringAt == 0 || now - _ringAt >= 600 || (now + tz) / 3600 != (_ringAt + tz) / 3600) {
            computeRing(now, tz);
            _ringAt = now;
            version++;
        }
    }

    //! Face-derived values (sleep, HRV, peaks) are hidden once the central state is older than this.
    function faceUsable(now as Number) as Boolean {
        return face != null && faceT > 0 && now - faceT < 36 * 3600;
    }

    //! `bump` = false for fast-changing values (HR, stress) while the screen is asleep: they are
    //! picked up by the next per-minute render instead of forcing a re-render on every change.
    function setComplication(type as Complications.Type or Null, v as Complications.Value or Null, bump as Boolean) as Void {
        if (type == Complications.COMPLICATION_TYPE_BODY_BATTERY) {
            cBB = J.toNum(v);
        } else if (type == Complications.COMPLICATION_TYPE_HEART_RATE) {
            cHR = J.toNum(v);
        } else if (type == Complications.COMPLICATION_TYPE_RESPIRATION_RATE) {
            cResp = J.toNum(v);
        } else if (type == Complications.COMPLICATION_TYPE_RECOVERY_TIME) {
            cRecMin = J.toNum(v);
        } else if (type == Complications.COMPLICATION_TYPE_STRESS) {
            cStress = J.toNum(v);
        } else if (type == Complications.COMPLICATION_TYPE_CALENDAR_EVENTS) {
            cCal = (v instanceof String && v.length() > 0) ? v : null;
        } else {
            return;
        }
        if (bump || (type != Complications.COMPLICATION_TYPE_HEART_RATE && type != Complications.COMPLICATION_TYPE_STRESS)) {
            version++;
        }
    }

    // ---- central values ---------------------------------------------------------------------------

    function faceFresh(now as Number) as Boolean {
        return face != null && faceT > 0 && now - faceT < Lim.FRESH_S;
    }

    function az() as Float? {
        return J.flt(face, "az");
    }

    function ahr() as Number? {
        return J.num(face, "ahr");
    }

    // ---- watch values (complication first, then ActivityMonitor / SensorHistory) -------------------

    //! Garmin stress 0..100 now: ActivityMonitor 30-s rolling score, else the complication.
    function stressNow() as Number? {
        var info = ActivityMonitor.getInfo();
        if (info has :stressScore) {
            var s = info.stressScore;
            if (s != null && s >= 0 && s <= 100) {
                return s;
            }
        }
        var c = cStress;
        return (c != null && c >= 0 && c <= 100) ? c : null;
    }

    function heartRate() as Number? {
        var c = cHR;
        if (c != null && c > 0 && c < 255) {
            return c;
        }
        try {
            // a null deref is a fatal VM error that try/catch does not catch: check every handle
            var it = ActivityMonitor.getHeartRateHistory(1, true);
            var s = it != null ? it.next() : null;
            if (s != null) {
                var hr = s.heartRate;
                if (hr != null && hr != ActivityMonitor.INVALID_HR_SAMPLE && hr > 0) {
                    return hr;
                }
            }
        } catch (e) {
        }
        return null;
    }

    function bodyBattery() as Number? {
        var c = cBB;
        if (c != null && c >= 0 && c <= 100) {
            return c;
        }
        if ((Toybox has :SensorHistory) && (SensorHistory has :getBodyBatteryHistory)) {
            try {
                var it = SensorHistory.getBodyBatteryHistory({:period => 1});
                var s = it != null ? it.next() : null;
                var d = s != null ? s.data : null;
                if (d != null) {
                    var v = d.toNumber();
                    if (v >= 0 && v <= 100) {
                        return v;
                    }
                }
            } catch (e) {
            }
        }
        return null;
    }

    function respiration() as Number? {
        var c = cResp;
        if (c != null && c > 0) {
            return c;
        }
        var info = ActivityMonitor.getInfo();
        if (info has :respirationRate) {
            var r = info.respirationRate;
            if (r != null && r > 0) {
                return r;
            }
        }
        return null;
    }

    //! Recovery time in minutes.
    function recoveryMin() as Number? {
        var c = cRecMin;
        if (c != null && c >= 0) {
            return c;
        }
        var info = ActivityMonitor.getInfo();
        if (info has :timeToRecovery) {
            var h = info.timeToRecovery;
            if (h != null && h >= 0) {
                return h * 60;
            }
        }
        return null;
    }

    // ---- 12-hour ring ---------------------------------------------------------------------------------

    private function computeRing(now as Number, tz as Number) as Void {
        var src = Cfg.num("ringSource", 0);
        var tr = J.arr(face, "tr");
        if (src != 2 && tr != null && tr.size() == 12 && faceT > 0 && now - faceT < Lim.RING_MAX_AGE_S) {
            ringMode = Lim.RING_CENTRAL;
            ringT0 = faceT - 12 * 3600;
            for (var i = 0; i < 12; i++) {
                var v = J.toNum(tr[i]);
                ring[i] = (v != null && v >= -9 && v <= 9) ? v : null;
            }
            return;
        }
        if (src == 1) {
            ringMode = Lim.RING_NONE;
            return;
        }
        // Garmin stress per local clock hour: SensorHistory, filled in by the local accumulator
        var hourNow = (now + tz) / 3600;
        ringT0 = (hourNow - 11) * 3600 - tz;
        var sums = new [12];
        var cnts = new [12];
        for (var i = 0; i < 12; i++) {
            sums[i] = 0;
            cnts[i] = 0;
        }
        if ((Toybox has :SensorHistory) && (SensorHistory has :getStressHistory)) {
            try {
                var it = SensorHistory.getStressHistory({
                    :period => new Time.Duration(now - ringT0 + 60),
                    :order => SensorHistory.ORDER_OLDEST_FIRST
                });
                var s = it != null ? it.next() : null;
                while (s != null) {
                    var d = s.data;
                    var w = s.when;
                    if (d != null && w != null && w.value() >= ringT0) {
                        var i = (w.value() - ringT0) / 3600;
                        var v = d.toNumber();
                        if (i < 12 && v >= 0 && v <= 100) {
                            sums[i] = (sums[i] as Number) + v;
                            cnts[i] = (cnts[i] as Number) + 1;
                        }
                    }
                    s = it.next();
                }
            } catch (e) {
                // known FR255 firmware issue: the stress history can be empty or throw
            }
        }
        var any = false;
        for (var i = 0; i < 12; i++) {
            var n = cnts[i] as Number;
            var v = null;
            if (n > 0) {
                v = (sums[i] as Number) / n;
            } else {
                v = accAvg(hourNow - 11 + i);
            }
            ring[i] = v;
            if (v != null) {
                any = true;
            }
        }
        ringMode = any ? Lim.RING_GARMIN : Lim.RING_NONE;
    }

    // ---- local stress accumulator (survives the FR255 empty-stress-history bug) -----------------------

    private function accAdd(hour as Number, s as Number) as Void {
        var slot = hour % 12;
        if (_accHour[slot] != hour) {
            _accHour[slot] = hour;
            _accSum[slot] = 0;
            _accN[slot] = 0;
        }
        _accSum[slot] = (_accSum[slot] as Number) + s;
        _accN[slot] = (_accN[slot] as Number) + 1;
        _accDirty = true;
    }

    private function accAvg(hour as Number) as Number? {
        var slot = hour % 12;
        var n = _accN[slot];
        if (_accHour[slot] == hour && n instanceof Number && n > 0) {
            return (_accSum[slot] as Number) / n;
        }
        return null;
    }

    private function loadAcc() as Void {
        var a = Store.get(K.SACC);
        var ok = a instanceof Array && a.size() == 36;
        for (var i = 0; i < 12; i++) {
            var arr = a as Array<Object?>;
            _accHour[i] = ok ? J.toNum(arr[i]) : null;
            var s = ok ? J.toNum(arr[12 + i]) : null;
            var n = ok ? J.toNum(arr[24 + i]) : null;
            _accSum[i] = s != null ? s : 0;
            _accN[i] = n != null ? n : 0;
        }
    }

    function saveAcc() as Void {
        if (!_accDirty) {
            return;
        }
        var a = [] as Array<Number or Null>;
        for (var i = 0; i < 12; i++) {
            a.add(_accHour[i] as Number?);
        }
        for (var i = 0; i < 12; i++) {
            a.add(_accSum[i] as Number?);
        }
        for (var i = 0; i < 12; i++) {
            a.add(_accN[i] as Number?);
        }
        if (Store.put(K.SACC, a)) {
            _accDirty = false;
        }
    }
}

//! Typed access to JSON-ish values (the central's face object, Storage arrays).
module J {
    function toNum(v as Object?) as Number? {
        if (v instanceof Number) {
            return v;
        }
        if (v instanceof Float || v instanceof Double || v instanceof Long) {
            return v.toNumber();
        }
        return null;
    }

    function num(d as Dictionary?, k as String) as Number? {
        return d != null ? toNum(d[k] as Object?) : null;
    }

    function flt(d as Dictionary?, k as String) as Float? {
        if (d == null) {
            return null;
        }
        var v = d[k];
        if (v instanceof Float) {
            return v;
        }
        if (v instanceof Number || v instanceof Double || v instanceof Long) {
            return v.toFloat();
        }
        return null;
    }

    function str(d as Dictionary?, k as String) as String? {
        if (d == null) {
            return null;
        }
        var v = d[k];
        return (v instanceof String && v.length() > 0) ? v : null;
    }

    function arr(d as Dictionary?, k as String) as Array<Object?>? {
        if (d == null) {
            return null;
        }
        var v = d[k];
        return (v instanceof Array) ? v as Array<Object?> : null;
    }

    function dict(d as Dictionary?, k as String) as Dictionary? {
        if (d == null) {
            return null;
        }
        var v = d[k];
        return (v instanceof Dictionary) ? v : null;
    }
}
