import Toybox.Lang;
import Toybox.Math;
import Toybox.System;

//! On-watch HRV for the screen only: a light artifact gate, rolling RMSSD and DFA alpha1.
//! The raw RR stream is uploaded untouched; the central does the real cleaning.
//! Everything works on preallocated rings; DFA is spread over several 1-s ticks so no single
//! callback runs long (watchdog) and nothing is allocated per beat. The DFA mirrors
//! central/pulso/signal/hrv.py:dfa_alpha1 (scales 4..16, forward + backward boxes, linear
//! detrend, least-squares slope of ln F(n) on ln n) so screen and central agree.
class HrvLive {
    const MED_N = 9;            // running-median window for the gate
    const Q_N = 100;            // beats in the quality window
    const D2_N = 60;            // successive differences in the RMSSD window
    const DFA_N = 120;          // gated beats in the DFA window
    const DFA_MIN_SCALE = 4;
    const DFA_MAX_SCALE = 16;
    const DFA_SCALES_PER_STEP = 2;
    const STALE_MS = 30000;

    // gate
    private var _med as Array<Number>;
    private var _medCount as Number = 0;
    private var _medIdx as Number = 0;
    private var _sort as Array<Number>;
    private var _prevOk as Boolean = false;
    private var _prevRr as Number = 0;
    private var _lastOkTimer as Number = 0;

    // quality ring (1 = accepted)
    private var _q as Array<Number>;
    private var _qCount as Number = 0;
    private var _qIdx as Number = 0;
    private var _qSum as Number = 0;

    // RMSSD ring of squared successive differences
    private var _d2 as Array<Number>;
    private var _d2Count as Number = 0;
    private var _d2Idx as Number = 0;
    private var _d2Sum as Number = 0;

    // DFA
    private var _g as Array<Number>;
    private var _gCount as Number = 0;
    private var _gIdx as Number = 0;
    private var _y as Array<Float>;
    private var _scale as Number = 0;       // next scale to compute, 0 = idle
    private var _sx as Float = 0.0;
    private var _sy as Float = 0.0;
    private var _sxx as Float = 0.0;
    private var _sxy as Float = 0.0;
    private var _k as Number = 0;

    var total as Number = 0;
    var accepted as Number = 0;
    var rmssd as Float or Null = null;
    var alpha1 as Float or Null = null;

    function initialize() {
        _med = fill(MED_N);
        _sort = fill(MED_N);
        _q = fill(Q_N);
        _d2 = fill(D2_N);
        _g = fill(DFA_N);
        _y = new Array<Float>[DFA_N];
        for (var i = 0; i < DFA_N; i++) {
            _y[i] = 0.0;
        }
    }

    private function fill(n as Number) as Array<Number> {
        var a = new Array<Number>[n];
        for (var i = 0; i < n; i++) {
            a[i] = 0;
        }
        return a;
    }

    //! Feed one raw RR interval (ms). Returns true when it passed the gate.
    function add(rr as Number) as Boolean {
        total++;
        var ok = false;
        if (rr >= 300 && rr <= 2000) {
            var m = median();
            // |ΔRR| measured against the running median: < 30 % of it
            ok = (m == 0) || ((rr - m).abs() * 10 < m * 3);
            _med[_medIdx] = rr;
            _medIdx = (_medIdx + 1) % MED_N;
            if (_medCount < MED_N) {
                _medCount++;
            }
        }

        var flag = ok ? 1 : 0;
        if (_qCount == Q_N) {
            _qSum -= _q[_qIdx];
        } else {
            _qCount++;
        }
        _q[_qIdx] = flag;
        _qSum += flag;
        _qIdx = (_qIdx + 1) % Q_N;

        if (ok) {
            accepted++;
            _lastOkTimer = System.getTimer();
            if (_prevOk) {
                var d = rr - _prevRr;
                pushD2(d * d);
            }
            _g[_gIdx] = rr;
            _gIdx = (_gIdx + 1) % DFA_N;
            if (_gCount < DFA_N) {
                _gCount++;
            }
        }
        _prevOk = ok;
        _prevRr = rr;
        return ok;
    }

    //! The next beat is not adjacent to the previous one (sensor gap): no difference across it.
    function breakChain() as Void {
        _prevOk = false;
    }

    function reset() as Void {
        _medCount = 0;
        _medIdx = 0;
        _qCount = 0;
        _qIdx = 0;
        _qSum = 0;
        _d2Count = 0;
        _d2Idx = 0;
        _d2Sum = 0;
        _gCount = 0;
        _gIdx = 0;
        _scale = 0;
        _prevOk = false;
        total = 0;
        accepted = 0;
        rmssd = null;
        alpha1 = null;
    }

    //! Accepted share of the last Q_N beats, 0..100, or -1 before any beat.
    function qualityPct() as Number {
        return (_qCount == 0) ? -1 : (_qSum * 100) / _qCount;
    }

    //! True when a gated beat arrived recently (values on screen are current).
    function fresh() as Boolean {
        return accepted > 0 && (System.getTimer() - _lastOkTimer) < STALE_MS;
    }

    function dfaBeats() as Number {
        return _gCount;
    }

    function dfaWindow() as Number {
        return DFA_N;
    }

    private function median() as Number {
        var n = _medCount;
        if (n < 3) {
            return 0;
        }
        for (var i = 0; i < n; i++) {
            var v = _med[i];
            var j = i - 1;
            while (j >= 0 && _sort[j] > v) {
                _sort[j + 1] = _sort[j];
                j--;
            }
            _sort[j + 1] = v;
        }
        return _sort[n / 2];
    }

    private function pushD2(v as Number) as Void {
        if (_d2Count == D2_N) {
            _d2Sum -= _d2[_d2Idx];
        } else {
            _d2Count++;
        }
        _d2[_d2Idx] = v;
        _d2Sum += v;
        _d2Idx = (_d2Idx + 1) % D2_N;
        rmssd = (_d2Count >= 10) ? Math.sqrt(_d2Sum.toFloat() / _d2Count).toFloat() : null;
    }

    // ---- DFA alpha1 -------------------------------------------------------------------------

    //! Snapshot and integrate the last DFA_N gated beats. Returns false when not enough beats
    //! yet or a computation is already running.
    function dfaBegin() as Boolean {
        if (_scale != 0) {
            return false;
        }
        if (_gCount < DFA_N) {
            alpha1 = null;
            return false;
        }
        var sum = 0;
        for (var i = 0; i < DFA_N; i++) {
            sum += _g[i];
        }
        var mean = sum.toFloat() / DFA_N;
        var acc = 0.0;
        for (var i = 0; i < DFA_N; i++) {
            acc += _g[(_gIdx + i) % DFA_N] - mean;   // _gIdx is the oldest beat once full
            _y[i] = acc;
        }
        _sx = 0.0;
        _sy = 0.0;
        _sxx = 0.0;
        _sxy = 0.0;
        _k = 0;
        _scale = DFA_MIN_SCALE;
        return true;
    }

    //! Advance a running DFA by a few scales; call once per tick. No-op when idle.
    function dfaStep() as Void {
        if (_scale == 0) {
            return;
        }
        var last = _scale + DFA_SCALES_PER_STEP - 1;
        if (last > DFA_MAX_SCALE) {
            last = DFA_MAX_SCALE;
        }
        for (var s = _scale; s <= last; s++) {
            var f = fluct(s);
            if (f > 0.0) {
                var lx = Math.ln(s).toFloat();
                var ly = Math.ln(f).toFloat();
                _sx += lx;
                _sy += ly;
                _sxx += lx * lx;
                _sxy += lx * ly;
                _k++;
            }
        }
        _scale = last + 1;
        if (_scale > DFA_MAX_SCALE) {
            _scale = 0;
            var den = _k * _sxx - _sx * _sx;
            alpha1 = (_k >= 4 && den > 0.0) ? (_k * _sxy - _sx * _sy) / den : null;
        }
    }

    //! Root-mean-square residual of the integrated series around per-box least-squares lines;
    //! non-overlapping boxes of size s taken from the start and again from the end, so the
    //! tail is not ignored when s does not divide the window.
    private function fluct(s as Number) as Float {
        var boxes = DFA_N / s;
        var tail = DFA_N - boxes * s;
        var f2 = boxRss(s, boxes, 0);
        f2 += boxRss(s, boxes, tail);
        return Math.sqrt(f2 / (2 * boxes * s)).toFloat();
    }

    private function boxRss(s as Number, boxes as Number, start as Number) as Float {
        var xm = (s - 1) / 2.0;
        var sxxc = (s * (s * s - 1)) / 12.0;   // sum of (j - xm)^2
        var f2 = 0.0;
        for (var b = 0; b < boxes; b++) {
            var o = start + b * s;
            var sy = 0.0;
            var sjy = 0.0;
            for (var j = 0; j < s; j++) {
                var v = _y[o + j];
                sy += v;
                sjy += j * v;
            }
            var ym = sy / s;
            var slope = (sjy - xm * sy) / sxxc;
            for (var j = 0; j < s; j++) {
                var r = _y[o + j] - ym - slope * (j - xm);
                f2 += r * r;
            }
        }
        return f2;
    }

    //! 0 = aerobic (>= 0.75), 1 = transition (0.5..0.75), 2 = anaerobic (< 0.5), -1 = n/a.
    static function zone(a as Float or Null) as Number {
        if (a == null) {
            return -1;
        }
        if (a >= 0.75) {
            return 0;
        }
        return (a >= 0.5) ? 1 : 2;
    }
}
