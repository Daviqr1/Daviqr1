import Toybox.Application;
import Toybox.Lang;
import Toybox.System;
import Toybox.WatchUi;

//! Buttons on the status screen:
//!   START  start / stop (stop asks for confirmation)
//!   UP     quick marker "estresse"
//!   DOWN   toggle status / details page
//!   MENU   main menu (hold UP)
//!   BACK   exit (asks to stop first while capturing)
class MainDelegate extends WatchUi.BehaviorDelegate {
    private var _cap as Capture;
    private var _view as StatusView;

    function initialize(cap as Capture, view as StatusView) {
        BehaviorDelegate.initialize();
        _cap = cap;
        _view = view;
    }

    function onSelect() as Boolean {
        if (_cap.capturing) {
            confirm(Rez.Strings.ConfirmStop, method(:doStop));
        } else {
            _cap.start(_cap.mode);
        }
        WatchUi.requestUpdate();
        return true;
    }

    function onPreviousPage() as Boolean {
        _cap.mark(0);
        return true;
    }

    function onNextPage() as Boolean {
        _view.page = 1 - _view.page;
        WatchUi.requestUpdate();
        return true;
    }

    function onMenu() as Boolean {
        WatchUi.pushView(buildMainMenu(_cap), new MainMenuDelegate(_cap), WatchUi.SLIDE_UP);
        return true;
    }

    function onBack() as Boolean {
        if (_cap.capturing) {
            confirm(Rez.Strings.ConfirmExit, method(:doStopExit));
            return true;
        }
        if (_view.page != 0) {
            _view.page = 0;
            WatchUi.requestUpdate();
            return true;
        }
        return false;   // default: leave the app
    }

    function doStop() as Void {
        _cap.stop(Stop.USER);
    }

    function doStopExit() as Void {
        _cap.stop(Stop.USER);
        System.exit();
    }
}

function confirm(msg as ResourceId, action as Method() as Void) as Void {
    var text = WatchUi.loadResource(msg) as String;
    WatchUi.pushView(new WatchUi.Confirmation(text), new ConfirmDelegate(action), WatchUi.SLIDE_IMMEDIATE);
}

class ConfirmDelegate extends WatchUi.ConfirmationDelegate {
    private var _action as Method() as Void;

    function initialize(action as Method() as Void) {
        ConfirmationDelegate.initialize();
        _action = action;
    }

    function onResponse(response as WatchUi.Confirm) as Boolean {
        if (response == WatchUi.CONFIRM_YES) {
            _action.invoke();
        }
        return true;
    }
}

function buildMainMenu(cap as Capture) as WatchUi.Menu2 {
    var menu = new WatchUi.Menu2({:title => Rez.Strings.MenuTitle});
    menu.addItem(new WatchUi.MenuItem(cap.capturing ? Rez.Strings.MStop : Rez.Strings.MStart, null, :startStop, null));
    menu.addItem(new WatchUi.MenuItem(Rez.Strings.MMode, Cfg.modeName(cap.mode).toUpper(), :mode, null));
    menu.addItem(new WatchUi.MenuItem(Rez.Strings.MMark, null, :mark, null));
    menu.addItem(new WatchUi.ToggleMenuItem(Rez.Strings.MFit, null, :fit, Cfg.fitEnabled(), null));
    menu.addItem(new WatchUi.ToggleMenuItem(Rez.Strings.MBle, cap.ble.stateName(), :ble, Cfg.bleBridge(), null));
    var q = cap.queue;
    menu.addItem(new WatchUi.MenuItem(Rez.Strings.MSend,
        q.count().toString() + " / " + ((q.bytes + 512) / 1024).toString() + " KB", :send, null));
    return menu;
}

class MainMenuDelegate extends WatchUi.Menu2InputDelegate {
    private var _cap as Capture;

    function initialize(cap as Capture) {
        Menu2InputDelegate.initialize();
        _cap = cap;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        var id = item.getId();
        if (id == :startStop) {
            WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
            if (_cap.capturing) {
                confirm(Rez.Strings.ConfirmStop, method(:doStop));
            } else {
                _cap.start(_cap.mode);
            }
        } else if (id == :mode) {
            WatchUi.pushView(new Rez.Menus.ModeMenu(), new ModeMenuDelegate(_cap), WatchUi.SLIDE_LEFT);
        } else if (id == :mark) {
            WatchUi.pushView(new Rez.Menus.MarkMenu(), new MarkMenuDelegate(_cap), WatchUi.SLIDE_LEFT);
        } else if (id == :fit) {
            if (item instanceof WatchUi.ToggleMenuItem) {
                try {
                    Properties.setValue("fitEnabled", item.isEnabled());
                } catch (e) {
                }
            }
        } else if (id == :ble) {
            if (item instanceof WatchUi.ToggleMenuItem) {
                var on = item.isEnabled();
                try {
                    Properties.setValue("bleBridge", on);
                } catch (e) {
                }
                _cap.setBridge(on);
            }
        } else if (id == :send) {
            _cap.sealNow();
            _cap.uploader.request(true);
            WatchUi.popView(WatchUi.SLIDE_DOWN);
        }
    }

    function doStop() as Void {
        _cap.stop(Stop.USER);
    }
}

class ModeMenuDelegate extends WatchUi.Menu2InputDelegate {
    private var _cap as Capture;

    function initialize(cap as Capture) {
        Menu2InputDelegate.initialize();
        _cap = cap;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        var ids = [:m0, :m1, :m2, :m3];
        var m = ids.indexOf(item.getId());
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);   // mode menu
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);   // main menu
        if (m >= 0) {
            _cap.setMode(m);
        }
    }
}

class MarkMenuDelegate extends WatchUi.Menu2InputDelegate {
    private var _cap as Capture;

    function initialize(cap as Capture) {
        Menu2InputDelegate.initialize();
        _cap = cap;
    }

    function onSelect(item as WatchUi.MenuItem) as Void {
        var ids = [:k0, :k1, :k2, :k3, :k4];
        var k = ids.indexOf(item.getId());
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
        WatchUi.popView(WatchUi.SLIDE_IMMEDIATE);
        if (k >= 0) {
            _cap.mark(k);
        }
    }
}
