import Toybox.Complications;
import Toybox.Graphics;
import Toybox.Lang;
import Toybox.System;
import Toybox.Time;
import Toybox.WatchUi;

//! The face. Everything except the seconds is rendered into one off-screen BufferedBitmap
//! (graphics pool, not the 128 KB app heap) when the minute or the data changes; every other
//! update is a single blit. Seconds: drawn in onUpdate while awake, and in onPartialUpdate
//! (1 Hz, low power) inside a tight clip until the system reports the power budget exceeded.
class PulsoView extends WatchUi.WatchFace {
    private var _model as Model;
    private var _painter as Painter? = null;
    private var _buf as Graphics.BufferedBitmap? = null;
    private var _key as Number = -1;
    private var _awake as Boolean = true;
    private var _partialOk as Boolean = true;
    private var _showSec as Boolean = true;
    private var _secAllowed as Boolean = true;   // seconds allowed now (setting + battery), per onUpdate
    private var _saver as Boolean = true;        // "batterySaver" setting
    private var _subscribed as Boolean = false;

    function initialize() {
        WatchFace.initialize();
        _model = new Model();
        _partialOk = (WatchUi.WatchFace has :onPartialUpdate);
        readSettings();
    }

    function onLayout(dc as Graphics.Dc) as Void {
        _painter = new Painter(dc.getWidth(), dc.getHeight());
        _buf = null;
        if (Graphics has :createBufferedBitmap) {
            try {
                var ref = Graphics.createBufferedBitmap({:width => dc.getWidth(), :height => dc.getHeight()});
                _buf = ref.get() as Graphics.BufferedBitmap?;
            } catch (e) {
                _buf = null;   // OutOfGraphicsMemoryException: draw straight to the screen
            }
        }
        _key = -1;
        subscribe();
    }

    function onShow() as Void {
        _key = -1;
    }

    function onHide() as Void {
        _model.saveAcc();
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        var p = _painter;
        if (p == null) {
            return;
        }
        var now = Time.now().value();
        var ct = System.getClockTime();
        _model.tick(now, ct.timeZoneOffset);
        var key = (now / 60) * 1024 + (_model.version % 1024);
        dc.clearClip();
        var buf = _buf;
        if (buf != null) {
            if (key != _key) {
                p.draw(buf.getDc(), _model, now, ct);
                _key = key;
            }
            dc.drawBitmap(0, 0, buf);
        } else {
            p.draw(dc, _model, now, ct);
        }
        _secAllowed = secondsAllowed();
        if (_secAllowed && (_awake || _partialOk)) {
            p.drawSeconds(dc, ct.sec);
        }
    }

    //! Low-power 1 Hz update: only the seconds slot, clipped (a few hundred pixels).
    function onPartialUpdate(dc as Graphics.Dc) as Void {
        var p = _painter;
        if (p == null || !_partialOk || !_secAllowed || p.secW == 0) {
            return;
        }
        dc.setClip(p.secX, p.secY, p.secW, p.secH);
        p.drawSeconds(dc, System.getClockTime().sec);
        dc.clearClip();
    }

    function onEnterSleep() as Void {
        _awake = false;
        WatchUi.requestUpdate();
    }

    function onExitSleep() as Void {
        _awake = true;
        WatchUi.requestUpdate();
    }

    // ---- called by the app / delegate -------------------------------------------------------------

    function dataChanged() as Void {
        _model.load();
    }

    function settingsChanged() as Void {
        readSettings();
        _model.load();
    }

    function budgetExceeded() as Void {
        _partialOk = false;
        _key = -1;
        WatchUi.requestUpdate();
    }

    // ---- internals --------------------------------------------------------------------------------

    private function readSettings() as Void {
        _showSec = Cfg.flag("showSeconds", true);
        _saver = Cfg.flag("batterySaver", true);
    }

    //! Seconds off when disabled in the settings or (battery saver) below 15 % and not charging.
    private function secondsAllowed() as Boolean {
        if (!_showSec) {
            return false;
        }
        if (_saver) {
            var s = System.getSystemStats();
            if (!s.charging && s.battery < 15.0) {
                return false;
            }
        }
        return true;
    }

    private function subscribe() as Void {
        if (_subscribed || !(Toybox has :Complications)) {
            return;
        }
        _subscribed = true;
        try {
            Complications.registerComplicationChangeCallback(method(:onComplication));
        } catch (e) {
            return;
        }
        sub(Complications.COMPLICATION_TYPE_BODY_BATTERY);
        sub(Complications.COMPLICATION_TYPE_HEART_RATE);
        sub(Complications.COMPLICATION_TYPE_RESPIRATION_RATE);
        sub(Complications.COMPLICATION_TYPE_RECOVERY_TIME);
        sub(Complications.COMPLICATION_TYPE_STRESS);
        sub(Complications.COMPLICATION_TYPE_CALENDAR_EVENTS);
    }

    private function sub(type as Complications.Type) as Void {
        try {
            var id = new Complications.Id(type);
            if (Complications.subscribeToUpdates(id)) {
                onComplication(id);   // seed the current value
            }
        } catch (e) {
            // OperationNotAllowedException (too many subscriptions) or not on this device
        }
    }

    function onComplication(id as Complications.Id) as Void {
        try {
            var c = Complications.getComplication(id);
            _model.setComplication(id.getType(), c.value, _awake);
        } catch (e) {
        }
    }
}

//! Receives power-budget violations for onPartialUpdate.
class PulsoDelegate extends WatchUi.WatchFaceDelegate {
    private var _view as PulsoView;

    function initialize(view as PulsoView) {
        WatchFaceDelegate.initialize();
        _view = view;
    }

    function onPowerBudgetExceeded(info as WatchUi.WatchFacePowerInfo) as Void {
        System.println("Pulso: partial update " + info.executionTimeAverage + " ms > " + info.executionTimeLimit + " ms");
        _view.budgetExceeded();
    }
}
