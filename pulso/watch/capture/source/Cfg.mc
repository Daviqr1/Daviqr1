import Toybox.Application;
import Toybox.Lang;

//! Settings (Garmin Connect app) and per-mode presets. Every getter has a safe default so a
//! missing or malformed setting never stops capture.
module Cfg {
    const VERSION = "0.1.0";

    const MODE_FOCO = 0;
    const MODE_SONO = 1;
    const MODE_TREINO = 2;
    const MODE_MAX = 3;
    const MODE_NAMES = ["foco", "sono", "treino", "max"];

    // Governor levels: 0 = preset, 1 = 4-s period, 2 = accel 10 Hz, 3 = accel off (RR + HR only).
    const GOV_MAX_LEVEL = 3;

    function str(key as String, dflt as String) as String {
        try {
            var v = Properties.getValue(key);
            if (v instanceof String) {
                return v;
            }
        } catch (e) {
        }
        return dflt;
    }

    function num(key as String, dflt as Number) as Number {
        try {
            var v = Properties.getValue(key);
            if (v instanceof Number) {
                return v;
            }
            if (v instanceof Float || v instanceof Double || v instanceof Long) {
                return v.toNumber();
            }
        } catch (e) {
        }
        return dflt;
    }

    function flt(key as String, dflt as Float) as Float {
        try {
            var v = Properties.getValue(key);
            if (v instanceof Float || v instanceof Number || v instanceof Double || v instanceof Long) {
                return v.toFloat();
            }
        } catch (e) {
        }
        return dflt;
    }

    function bool(key as String, dflt as Boolean) as Boolean {
        try {
            var v = Properties.getValue(key);
            if (v instanceof Boolean) {
                return v;
            }
        } catch (e) {
        }
        return dflt;
    }

    //! Central base URL without surrounding spaces or trailing slashes ("" when unset).
    function centralUrl() as String {
        var u = str("centralUrl", "");
        var a = 0;
        var b = u.length();
        while (a < b && isCh(u, a, " ")) {
            a++;
        }
        while (b > a && (isCh(u, b - 1, " ") || isCh(u, b - 1, "/"))) {
            b--;
        }
        var out = u.substring(a, b);
        return (out == null) ? "" : out;
    }

    function isCh(s as String, i as Number, c as String) as Boolean {
        var x = s.substring(i, i + 1);
        return x != null && x.equals(c);
    }

    function token() as String {
        var t = str("deviceToken", "");
        var a = 0;
        var b = t.length();
        while (a < b && isCh(t, a, " ")) {
            a++;
        }
        while (b > a && isCh(t, b - 1, " ")) {
            b--;
        }
        var out = t.substring(a, b);
        return (out == null) ? "" : out;
    }

    function hasCentral() as Boolean {
        return centralUrl().find("http") == 0 && token().length() > 0;
    }

    function defaultMode() as Number {
        var m = num("defaultMode", MODE_FOCO);
        return (m < MODE_FOCO || m > MODE_MAX) ? MODE_FOCO : m;
    }

    function floorPct() as Number {
        var f = num("floorPct", 15);
        return (f < 5) ? 5 : ((f > 50) ? 50 : f);
    }

    function fitEnabled() as Boolean {
        return bool("fitEnabled", true);
    }

    //! BLE bridge to the laptop (raw accelerometer + batches without the phone).
    function bleBridge() as Boolean {
        return bool("bleBridge", false);
    }

    //! Battery budget in %/h; 0 disables the governor (the floor still applies).
    //! FOCO: budgetPctH. SONO: sleepBudgetPctH. TREINO: 2 x budgetPctH (short, intentional
    //! sessions with a strap and FIT). MAX: no budget, only the floor.
    function budget(mode as Number) as Float {
        if (mode == MODE_MAX) {
            return 0.0;
        }
        var b = (mode == MODE_SONO) ? flt("sleepBudgetPctH", 1.0) : flt("budgetPctH", 3.0);
        if (mode == MODE_TREINO) {
            b = b * 2.0;
        }
        return (b < 0.0) ? 0.0 : b;
    }

    // ---- presets ----------------------------------------------------------------------------

    //! Sensor listener period in seconds (1..4).
    function period(mode as Number) as Number {
        return (mode == MODE_SONO) ? 4 : 1;
    }

    //! Accelerometer rate before the governor; maxHz is the device limit (0 = unknown).
    function accHz(mode as Number, maxHz as Number) as Number {
        var want = (mode == MODE_MAX) ? maxHz : 25;
        if (want <= 0) {
            want = 25;
        }
        if (maxHz > 0 && want > maxHz) {
            want = maxHz;
        }
        return want;
    }

    //! Seconds between uploads (before the governor multiplier).
    function uploadS(mode as Number) as Number {
        return (mode == MODE_SONO) ? 600 : 60;
    }

    //! Seconds between raw ambient pressure samples.
    function barS(mode as Number) as Number {
        return (mode == MODE_SONO) ? 60 : 10;
    }

    //! Seal the open batch at this estimated JSON size (bytes); the POST must stay below 6 KB.
    function sealBytes(mode as Number) as Number {
        return (mode == MODE_SONO) ? 4500 : 4000;
    }

    function usesDfa(mode as Number) as Boolean {
        return mode == MODE_TREINO || mode == MODE_MAX;
    }

    function usesFit(mode as Number) as Boolean {
        return mode == MODE_MAX || (mode == MODE_TREINO && fitEnabled());
    }

    function modeName(mode as Number) as String {
        return (mode >= 0 && mode < MODE_NAMES.size()) ? MODE_NAMES[mode] as String : "?";
    }
}
