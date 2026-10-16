import Toybox.Lang;
import Toybox.System;
import Toybox.Time;

//! Millisecond epoch clock. Time.now() only has 1-s resolution, System.getTimer() has ms but
//! an arbitrary origin and wraps after ~24.8 days. The estimate runs on getTimer() and is
//! disciplined against Time.now(): it is clamped into [wall, wall + 999] ms on every call to
//! discipline(), which also re-bases the timer anchor so the 32-bit difference never wraps.
class Clock {
    private var _epochMs as Long;   // epoch ms at _timer
    private var _timer as Number;

    function initialize() {
        _timer = System.getTimer();
        _epochMs = Time.now().value().toLong() * 1000l + 500l;
    }

    function nowMs() as Long {
        // Number subtraction wraps in two's complement, so this is safe across the timer wrap
        // as long as the anchor is recent (discipline() runs every second).
        return _epochMs + (System.getTimer() - _timer).toLong();
    }

    function nowS() as Number {
        return Time.now().value();
    }

    //! Call about once per second (and at least once per minute).
    function discipline() as Void {
        var t = System.getTimer();
        var est = _epochMs + (t - _timer).toLong();
        var wall = Time.now().value().toLong() * 1000l;
        if (est < wall) {
            est = wall;
        } else if (est > wall + 999l) {
            est = wall + 999l;
        }
        _epochMs = est;
        _timer = t;
    }
}
