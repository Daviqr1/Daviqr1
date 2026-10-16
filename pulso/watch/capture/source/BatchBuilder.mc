import Toybox.Lang;

//! The open (not yet sealed) protocol-v1 batch. Keeps the series in the exact wire shapes
//! (docs/protocolo.md) and an estimate of the serialized JSON size, so the controller can
//! seal it before the POST body gets near the ~6 KB link limit.
class BatchBuilder {
    const ENVELOPE_BYTES = 260;    // v, d, q, src, sid, t, m, bat and punctuation
    const MAX_RR_CHUNK = 600;      // central limit per rr chunk
    const MAX_ACC_CHUNK = 600;
    const RR_TOL_MS = 1500;        // estimate later than the callback by more than this: re-anchor
    const RR_SLACK_MS = 400;       // callback later than last beat by more than RR + this: gap
    const ACC_TOL_MS = 500;
    const GAP_FILL_MAX = 60;       // max nulls we pad into hr1/bar before sealing instead

    private var _rr as Array<Array>;
    private var _rrCur as Array<Number> or Null;
    private var _rrChunk as Array or Null;
    private var _lastBeatMs as Long = 0l;      // survives seals: beat continuity across batches

    private var _hrT0 as Number = 0;
    private var _hr as Array<Number or Null> or Null;

    private var _acc as Array<Array>;
    private var _accEnmo as Array<Number> or Null;
    private var _accMad as Array<Number> or Null;
    private var _accT0 as Long = 0l;

    private var _barT0 as Number = 0;
    private var _barStep as Number = 10;
    private var _bar as Array<Number or Null> or Null;

    private var _ev as Array<Array>;
    private var _h as Dictionary<String, Array> or Null;

    var bytes as Number = 0;
    var sid as String or Null = null;

    function initialize() {
        _rr = [];
        _acc = [];
        _ev = [];
        reset();
    }

    //! Drop the content (after sealing). Beat continuity (_lastBeatMs) is kept on purpose.
    function reset() as Void {
        _rr = [];
        _rrCur = null;
        _rrChunk = null;
        _hr = null;
        _acc = [];
        _accEnmo = null;
        _accMad = null;
        _bar = null;
        _ev = [];
        _h = null;
        bytes = ENVELOPE_BYTES;
    }

    //! Forget beat continuity (new session or sensor restart).
    function breakBeats() as Void {
        _lastBeatMs = 0l;
        _rrCur = null;
    }

    function isEmpty() as Boolean {
        return _rr.size() == 0 && _hr == null && _acc.size() == 0 && _bar == null && _ev.size() == 0 && _h == null;
    }

    static function digits(v as Number) as Number {
        if (v < 0) {
            return 1 + digits(-v);
        }
        if (v < 10) {
            return 1;
        }
        if (v < 100) {
            return 2;
        }
        if (v < 1000) {
            return 3;
        }
        if (v < 10000) {
            return 4;
        }
        return (v < 100000) ? 5 : 7;
    }

    //! Append RR intervals (ms, untouched) delivered by a callback at tMs. Consecutive callbacks
    //! are merged into one [t_last_ms, [rr...]] chunk while the beat sums agree with the clock;
    //! t_last then advances by the beat sums. A beat cannot close after the callback that
    //! delivers it, so t_last follows the lower envelope min(sum-based, callback time): the
    //! anchor latency bias shrinks towards the system's minimum delivery delay instead of
    //! staying at the first callback's latency (simulated: median beat-time error ~0-100 ms
    //! instead of ~650 ms). Slack larger than the last interval + RR_SLACK_MS means beats went
    //! missing: start a new chunk. Returns false when the beats are NOT adjacent.
    function addRr(tMs as Long, ivs as Array<Number>) as Boolean {
        var n = ivs.size();
        if (n == 0) {
            return true;
        }
        var sum = 0;
        for (var i = 0; i < n; i++) {
            sum += ivs[i];
        }
        var adjacent = false;
        var tLast = tMs;
        if (_lastBeatMs > 0l) {
            var expected = _lastBeatMs + sum.toLong();
            var slack = tMs - expected;
            if (slack >= -RR_TOL_MS && slack <= (ivs[n - 1] + RR_SLACK_MS).toLong()) {
                adjacent = true;
                tLast = (slack < 0l) ? tMs : expected;
            }
        }
        var cur = _rrCur;
        var chunk = _rrChunk;
        if (!adjacent || cur == null || chunk == null || cur.size() + n > MAX_RR_CHUNK) {
            cur = [] as Array<Number>;
            chunk = [tLast, cur];
            _rr.add(chunk);
            _rrCur = cur;
            _rrChunk = chunk;
            bytes += 20;
        }
        for (var i = 0; i < n; i++) {
            var v = ivs[i];
            cur.add(v);
            bytes += digits(v) + 1;
        }
        chunk[0] = tLast;
        _lastBeatMs = tLast;
        return adjacent;
    }

    //! False when adding at tS would need more than GAP_FILL_MAX nulls (seal first).
    function hrFits(tS as Number) as Boolean {
        var hr = _hr;
        return hr == null || (tS - _hrT0 - hr.size()) <= GAP_FILL_MAX;
    }

    //! 1 Hz heart rate placed by epoch second; gaps become null.
    function addHr(tS as Number, bpm as Number or Null) as Void {
        var hr = _hr;
        if (hr == null) {
            if (bpm == null) {
                return;
            }
            _hrT0 = tS;
            _hr = [bpm] as Array<Number or Null>;
            bytes += 24;
            return;
        }
        var idx = tS - _hrT0;
        var n = hr.size();
        if (idx < 0) {
            return;
        }
        if (idx < n) {
            if (bpm != null) {
                hr[idx] = bpm;
            }
            return;
        }
        while (n < idx) {
            hr.add(null);
            bytes += 5;
            n++;
        }
        hr.add(bpm);
        bytes += (bpm == null) ? 5 : 4;
    }

    //! One second of movement features starting at t0Ms.
    function addAcc(t0Ms as Long, enmo as Number, mad as Number) as Void {
        var e = _accEnmo;
        var m = _accMad;
        var cont = false;
        if (e != null && m != null && e.size() < MAX_ACC_CHUNK) {
            var diff = _accT0 + 1000l * e.size() - t0Ms;
            if (diff < 0l) {
                diff = -diff;
            }
            cont = diff <= ACC_TOL_MS;
        }
        if (!cont || e == null || m == null) {
            e = [] as Array<Number>;
            m = [] as Array<Number>;
            _accEnmo = e;
            _accMad = m;
            _accT0 = t0Ms;
            _acc.add([t0Ms, (t0Ms / 1000l).toNumber(), e, m]);
            bytes += 34;
        }
        e.add(enmo);
        m.add(mad);
        bytes += digits(enmo) + digits(mad) + 2;
    }

    //! False when the pressure series cannot take a sample at tS with this step (seal first).
    function barFits(tS as Number, step as Number) as Boolean {
        var bar = _bar;
        if (bar == null) {
            return true;
        }
        return step == _barStep && ((tS - _barT0) / step - bar.size()) <= GAP_FILL_MAX;
    }

    //! Raw ambient pressure (Pa) on a fixed step grid starting at the first sample.
    function addBar(tS as Number, step as Number, pa as Number or Null) as Void {
        var bar = _bar;
        if (bar == null) {
            if (pa == null) {
                return;
            }
            _barT0 = tS;
            _barStep = step;
            _bar = [pa] as Array<Number or Null>;
            bytes += 26;
            return;
        }
        var idx = (tS - _barT0 + step / 2) / step;
        var n = bar.size();
        if (idx < n) {
            if (pa != null && idx >= 0) {
                bar[idx] = pa;
            }
            return;
        }
        while (n < idx) {
            bar.add(null);
            bytes += 5;
            n++;
        }
        bar.add(pa);
        bytes += 7;
    }

    function addEv(tS as Number, kind as String, label as String) as Void {
        _ev.add([tS, kind, label]);
        bytes += 22 + kind.length() + 2 * label.length();
    }

    //! SensorHistory points {"stress": [[t_s, v], ...], ...}; merged if called twice per batch.
    function setHistory(h as Dictionary<String, Array>, nBytes as Number) as Void {
        var cur = _h;
        if (cur == null) {
            _h = h;
        } else {
            var keys = h.keys();
            for (var i = 0; i < keys.size(); i++) {
                var k = keys[i];
                var add = h[k];
                var have = cur.get(k);
                if (have != null && add != null) {
                    have.addAll(add as Array<Object or Null>);
                } else if (add != null) {
                    cur[k] = add;
                }
            }
        }
        bytes += nBytes;
    }

    //! The batch as a JSON-ready dictionary (without "t", which is stamped at send time).
    function seal(dev as String, q as Number) as Dictionary<String, Object> {
        var b = {"v" => 1, "d" => dev, "q" => q, "src" => "cap"} as Dictionary<String, Object>;
        if (sid != null) {
            b["sid"] = sid;
        }
        if (_h != null) {
            b["h"] = _h;
        }
        if (_rr.size() > 0) {
            b["rr"] = _rr;
        }
        if (_hr != null) {
            b["hr1"] = [_hrT0, _hr];
        }
        if (_acc.size() > 0) {
            b["acc"] = _acc;
        }
        if (_bar != null) {
            b["bar"] = [_barT0, _barStep, _bar];
        }
        if (_ev.size() > 0) {
            b["ev"] = _ev;
        }
        return b;
    }
}
