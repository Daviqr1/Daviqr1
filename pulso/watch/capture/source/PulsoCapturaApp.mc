import Toybox.Application;
import Toybox.Lang;
import Toybox.WatchUi;

//! Pulso Captura: the only path for high-resolution data off the FR255 (a watch face cannot
//! read the sensors). Captures RR + movement features + pressure, shows live HRV, queues
//! protocol-v1 batches and ships them to the central.
class PulsoCapturaApp extends Application.AppBase {
    private var _capture as Capture or Null = null;

    function initialize() {
        AppBase.initialize();
    }

    function capture() as Capture {
        var c = _capture;
        if (c == null) {
            c = new Capture();
            _capture = c;
        }
        return c;
    }

    function onStart(state as Dictionary or Null) as Void {
        capture();
    }

    function onStop(state as Dictionary or Null) as Void {
        var c = _capture;
        if (c != null) {
            c.shutdown();
        }
    }

    function getInitialView() as [WatchUi.Views] or [WatchUi.Views, WatchUi.InputDelegates] {
        var c = capture();
        var view = new StatusView(c);
        return [view, new MainDelegate(c, view)];
    }

    function onSettingsChanged() as Void {
        var c = _capture;
        if (c != null) {
            c.onSettingsChanged();
        }
        WatchUi.requestUpdate();
    }
}
