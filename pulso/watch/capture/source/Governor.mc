import Toybox.Lang;
import Toybox.System;

module GovAct {
    const NONE = 0;
    const LEVEL = 1;
    const FLOOR = 2;
}

//! Battery governor. Samples the battery every 5 min, estimates drain (%/h) as an EMA of the
//! slope over a sliding window of up to 1 h (battery % is coarse, a 5-min delta alone is mostly
//! quantisation noise) and steps the capture down/up against the mode's budget with
//! hysteresis. It remembers the drain measured at the level it left, and does not climb back
//! to a level that was over budget less than 3 h ago (simulated with 1 %-quantised readings:
//! 5 instead of 12 level changes in 8 h, average drain at the budget instead of 13 % above).
//! Below the floor it asks the controller to stop and save.
//!   level 0 preset | 1 period 4 s, uploads x2 | 2 accel 10 Hz, uploads x3 | 3 accel off, uploads x4
class Governor {
    const SAMPLE_S = 300;
    const RING = 13;              // 1 h of 5-min samples
    const MIN_SLOPE_S = 600;      // slope needs >= 10 min
    const MIN_DECIDE_S = 1800;    // decisions need >= 30 min of window
    const HOLD_S = 1200;          // >= 20 min between level changes
    const ALPHA = 0.5;
    const UP = 1.2;               // step down when drain > budget * UP
    const DOWN = 0.7;             // restore when drain < budget * DOWN
    const MEMORY_S = 10800;       // how long a level's measured drain is trusted

    private var _t as Array<Number>;
    private var _b as Array<Float>;
    private var _n as Number = 0;
    private var _i as Number = 0;
    private var _lastSampleS as Number = 0;
    private var _lastChangeS as Number = 0;
    private var _lvlDrain as Array<Float>;     // drain measured when a level was left (-1 = unknown)
    private var _lvlDrainS as Array<Number>;

    var level as Number = 0;
    var drain as Float or Null = null;
    var battery as Float = 100.0;
    var days as Float or Null = null;
    var charging as Boolean = false;
    var budget as Float = 3.0;
    var floor as Number = 15;

    function initialize() {
        _t = new Array<Number>[RING];
        _b = new Array<Float>[RING];
        for (var k = 0; k < RING; k++) {
            _t[k] = 0;
            _b[k] = 0.0;
        }
        _lvlDrain = [-1.0, -1.0, -1.0, -1.0] as Array<Float>;
        _lvlDrainS = [0, 0, 0, 0] as Array<Number>;
        read();
    }

    function start(budgetPctH as Float, floorPct as Number, nowS as Number) as Void {
        budget = budgetPctH;
        floor = floorPct;
        level = 0;
        drain = null;
        _n = 0;
        _i = 0;
        _lastSampleS = 0;
        _lastChangeS = nowS;
        for (var k = 0; k < _lvlDrain.size(); k++) {
            _lvlDrain[k] = -1.0;
        }
    }

    //! Refresh battery fields (cheap; also used by the idle screen).
    function read() as Void {
        var st = System.getSystemStats();
        battery = st.battery;
        charging = (st has :charging) ? st.charging : false;
        days = (st has :batteryInDays) ? st.batteryInDays : null;
    }

    function uploadMult() as Number {
        return level + 1;
    }

    //! Call every second while capturing. Returns GovAct.*.
    function tick(nowS as Number) as Number {
        if (_lastSampleS != 0 && nowS - _lastSampleS < SAMPLE_S) {
            return GovAct.NONE;
        }
        _lastSampleS = nowS;
        read();
        if (!charging && battery <= floor) {
            return GovAct.FLOOR;
        }
        if (charging) {
            _n = 0;
            _i = 0;
            drain = null;
            if (level > 0) {
                level = 0;
                _lastChangeS = nowS;
                return GovAct.LEVEL;
            }
            return GovAct.NONE;
        }

        _t[_i] = nowS;
        _b[_i] = battery;
        _i = (_i + 1) % RING;
        if (_n < RING) {
            _n++;
        }
        var oldest = (_n < RING) ? 0 : _i;
        var dt = nowS - _t[oldest];
        if (_n >= 2 && dt >= MIN_SLOPE_S) {
            var r = (_b[oldest] - battery) * 3600.0 / dt;
            var d = drain;
            drain = (d == null) ? r : d + ALPHA * (r - d);
        }

        var dr = drain;
        if (budget <= 0.0 || dr == null || dt < MIN_DECIDE_S || nowS - _lastChangeS < HOLD_S) {
            return GovAct.NONE;
        }
        if (dr > budget * UP && level < Cfg.GOV_MAX_LEVEL) {
            _lvlDrain[level] = dr;
            _lvlDrainS[level] = nowS;
            level++;
        } else if (dr < budget * DOWN && level > 0) {
            var known = _lvlDrain[level - 1];
            if (known >= 0.0 && nowS - _lvlDrainS[level - 1] < MEMORY_S && known > budget * UP) {
                return GovAct.NONE;   // that level was measured over budget recently
            }
            level--;
        } else {
            return GovAct.NONE;
        }
        // new operating point: start a fresh window (keep the EMA for display)
        _lastChangeS = nowS;
        _t[0] = nowS;
        _b[0] = battery;
        _n = 1;
        _i = 1;
        return GovAct.LEVEL;
    }
}
