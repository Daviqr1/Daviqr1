import Toybox.Lang;
import Toybox.Math;

//! Per-second movement features from raw accelerometer samples (milli-g):
//!   ENMO = mean(max(|a| - 1000, 0)),  MAD = mean(| |a| - mean|a| |)
//! Results are rounded to whole mg (the wire carries integers to save bytes).
class AccelFeatures {
    const MAX_WIN = 256;

    private var _vm as Array<Float>;

    var enmo as Number = 0;
    var mad as Number = 0;
    var gyro as Number = 0;     // mean angular speed, deg/s (when a gyroscope is present)

    function initialize() {
        _vm = new Array<Float>[MAX_WIN];
        for (var i = 0; i < MAX_WIN; i++) {
            _vm[i] = 0.0;
        }
    }

    //! Compute ENMO/MAD over samples [from, to). Returns false for an empty window.
    function window(x as Array<Numeric>, y as Array<Numeric>, z as Array<Numeric>, from as Number, to as Number) as Boolean {
        var n = to - from;
        if (n <= 0) {
            return false;
        }
        if (n > MAX_WIN) {
            from = to - MAX_WIN;
            n = MAX_WIN;
        }
        var sum = 0.0;
        var en = 0.0;
        for (var i = 0; i < n; i++) {
            var a = x[from + i];
            var b = y[from + i];
            var c = z[from + i];
            var v = Math.sqrt(a * a + b * b + c * c).toFloat();
            _vm[i] = v;
            sum += v;
            if (v > 1000.0) {
                en += v - 1000.0;
            }
        }
        var mean = sum / n;
        var md = 0.0;
        for (var i = 0; i < n; i++) {
            var d = _vm[i] - mean;
            md += (d < 0.0) ? -d : d;
        }
        enmo = (en / n + 0.5).toNumber();
        mad = (md / n + 0.5).toNumber();
        return true;
    }

    //! Mean gyroscope magnitude over [from, to), deg/s.
    function gyroWindow(x as Array<Numeric>, y as Array<Numeric>, z as Array<Numeric>, from as Number, to as Number) as Void {
        var n = to - from;
        if (n <= 0) {
            gyro = 0;
            return;
        }
        var sum = 0.0;
        for (var i = from; i < to; i++) {
            var a = x[i];
            var b = y[i];
            var c = z[i];
            sum += Math.sqrt(a * a + b * b + c * c).toFloat();
        }
        gyro = (sum / n + 0.5).toNumber();
    }
}
