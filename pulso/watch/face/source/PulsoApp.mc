import Toybox.Application;
import Toybox.Application.Properties;
import Toybox.Background;
import Toybox.Lang;
import Toybox.System;
import Toybox.Time;
import Toybox.WatchUi;

//! Application entry point. The class is (:background) because the background process
//! instantiates it to reach getServiceDelegate(); the foreground-only methods below are never
//! called there (the background check is disabled on them on purpose).
(:background)
class PulsoApp extends Application.AppBase {
    private var _view as PulsoView? = null;

    function initialize() {
        AppBase.initialize();
    }

    function onStart(state as Dictionary?) as Void {
    }

    function onStop(state as Dictionary?) as Void {
    }

    function getServiceDelegate() as [System.ServiceDelegate] {
        return [new PulsoService()];
    }

    (:typecheck(disableBackgroundCheck))
    function getInitialView() as [WatchUi.Views] or [WatchUi.Views, WatchUi.InputDelegates] {
        var v = new PulsoView();
        _view = v;
        Sched.apply();
        return [v, new PulsoDelegate(v)];
    }

    //! Called with the payload of Background.exit() (immediately when the face is running,
    //! otherwise the next time it starts).
    (:typecheck(disableBackgroundCheck))
    function onBackgroundData(data as Application.PersistableType) as Void {
        Sched.ingest(data);
        Sched.apply();
        var v = _view;
        if (v != null) {
            v.dataChanged();
        }
        WatchUi.requestUpdate();
    }

    (:typecheck(disableBackgroundCheck))
    function onSettingsChanged() as Void {
        Sched.apply();
        var v = _view;
        if (v != null) {
            v.settingsChanged();
        }
        WatchUi.requestUpdate();
    }
}

//! Foreground glue: background scheduling (with a battery governor) and persistence of what the
//! background service returns.
module Sched {
    //! Desired temporal-event interval in seconds; 0 = no background runs.
    function interval() as Number {
        if (!Cfg.configured() || !Cfg.flag("uploadEnabled", true)) {
            return 0;
        }
        var s = System.getSystemStats();
        if (!Cfg.flag("batterySaver", true) || s.charging) {
            return 300;
        }
        if (s.battery < 15.0) {
            return 1200;
        }
        if (s.battery < 30.0) {
            return 600;
        }
        return 300;
    }

    //! (Re)register the temporal event only when the wanted interval changed: re-registering
    //! the same Duration would restart the 5-minute clock.
    function apply() as Void {
        if (!(Toybox has :Background)) {
            return;
        }
        var want = interval();
        try {
            var cur = Background.getTemporalEventRegisteredTime();
            if (want <= 0) {
                if (cur != null) {
                    Background.deleteTemporalEvent();
                }
                return;
            }
            var curS = (cur instanceof Time.Duration) ? cur.value() : -1;
            if (curS != want) {
                Background.registerForTemporalEvent(new Time.Duration(want));
            }
        } catch (e) {
            System.println("Pulso: temporal event not registered");
        }
    }

    //! Persist {"face", "ok", "t", "q", "n"} from the background service.
    function ingest(data as Application.PersistableType) as Void {
        if (!(data instanceof Dictionary)) {
            return;
        }
        var f = data["face"];
        if (f instanceof Dictionary) {
            Store.put(K.FACE, f);
        }
        Store.put(K.ST, [data["ok"], data["t"], data["q"], data["n"]]);
        // expose the device id read-only in the Garmin Connect settings (helps matching on the central)
        var did = Store.get(K.DID);
        if (did instanceof String) {
            try {
                var cur = Properties.getValue("deviceId");
                if (!(cur instanceof String) || !did.equals(cur)) {
                    Properties.setValue("deviceId", did);
                }
            } catch (e) {
            }
        }
    }
}
