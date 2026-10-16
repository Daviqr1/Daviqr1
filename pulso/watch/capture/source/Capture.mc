import Toybox.Activity;
import Toybox.ActivityMonitor;
import Toybox.Application;
import Toybox.Attention;
import Toybox.Lang;
import Toybox.Math;
import Toybox.Sensor;
import Toybox.SensorHistory;
import Toybox.System;
import Toybox.Time;
import Toybox.Timer;
import Toybox.UserProfile;
import Toybox.WatchUi;

module Stop {
    const USER = 0;
    const FLOOR = 1;
    const APP = 2;
    const SWITCH = 3;
    const NAMES = ["user", "floor", "app", "switch"];
}

//! Capture controller: owns the sensor listener, the 1-Hz tick, the open batch, the queue,
//! the uploader, the battery governor and the optional FIT recording.
class Capture {
    // Wire labels for markers (stable, independent of the display language).
    const MARKS = ["estresse", "café", "reunião começou", "reunião acabou", "pausa"];

    const DFA_EVERY_S = 15;
    const HIST_EVERY_S = 600;
    const HIST_MAX_S = 7200;
    const HIST_MAX_POINTS = 60;      // per series and harvest: 3 x 60 points stay ~3 KB of JSON
    const SONO_QUEUE_PUSH = 24000;   // in SONO upload early only when this much is queued
    const UI_SONO_S = 60;

    var clock as Clock;
    var hrv as HrvLive;
    var feat as AccelFeatures;
    var builder as BatchBuilder;
    var queue as UploadQueue;
    var uploader as Uploader;
    var ble as BleLink;
    var gov as Governor;
    var fit as FitRecorder;

    var mode as Number = 0;
    var capturing as Boolean = false;
    var startS as Number = 0;
    var sid as String or Null = null;
    var devId as String = "";

    var accMax as Number = 0;      // getMaxSampleRateForSensorType(:accelerometer)
    var sysMax as Number = 0;      // getMaxSampleRate()
    var gyroMax as Number = 0;     // getMaxSampleRateForSensorType(:gyroscope)
    var period as Number = 1;
    var accHz as Number = 25;      // requested
    var accSeen as Number = 0;     // delivered samples per second (last callback)
    var gyroOn as Boolean = false;
    var listenerOk as Boolean = false;
    var sensorErr as String = "";

    var hr as Number or Null = null;
    var enmo as Number = 0;
    var lastPa as Number or Null = null;
    var note as String or Null = null;   // persistent note for the idle screen (e.g. battery stop)

    private var _timer as Timer.Timer;
    private var _lastFlushS as Number = 0;   // upload cadence (size-triggered seals do not reset it)
    private var _lastBarS as Number = 0;
    private var _lastDfaS as Number = 0;
    private var _lastHistS as Number = 0;
    private var _lastUiS as Number = 0;
    private var _booted as Boolean = false;
    private var _histCursor as Dictionary<String, Number>;
    private var _histDirty as Boolean = false;   // cursor advanced in memory, batch not sealed yet
    private var _bleWasReady as Boolean = false;

    function initialize() {
        clock = new Clock();
        hrv = new HrvLive();
        feat = new AccelFeatures();
        builder = new BatchBuilder();
        queue = new UploadQueue();
        ble = new BleLink(clock);
        uploader = new Uploader(queue, ble);
        gov = new Governor();
        fit = new FitRecorder();
        mode = Cfg.defaultMode();
        devId = loadDeviceId();
        _histCursor = loadCursor();
        probeRates();
        _timer = new Timer.Timer();
        _timer.start(method(:onTick), 1000, true);
        if (Cfg.bleBridge()) {
            ble.enable();
        }
    }

    //! Ponte BLE on/off (menu or settings).
    function setBridge(on as Boolean) as Void {
        if (on) {
            ble.enable();
        } else {
            ble.disable();
        }
    }

    // ---- lifecycle --------------------------------------------------------------------------

    function start(m as Number) as Void {
        if (capturing) {
            return;
        }
        clock.discipline();
        var nowS = clock.nowS();
        mode = m;
        note = null;
        probeRates();
        gov.start(Cfg.budget(mode), Cfg.floorPct(), nowS);
        gov.read();
        if (!gov.charging && gov.battery <= gov.floor) {
            note = WatchUi.loadResource(Rez.Strings.BatLow) as String;
            return;
        }
        sealNow();                 // idle markers go out without a session id
        sid = "c-" + nowS.toString();
        builder.sid = sid;
        builder.breakBeats();
        hrv.reset();
        hr = null;
        capturing = true;
        startS = nowS;
        ble.setSession(nowS);
        _lastFlushS = nowS;
        _lastBarS = 0;
        _lastDfaS = nowS;
        _lastHistS = nowS - HIST_EVERY_S + 30;   // first SensorHistory harvest after 30 s

        enableHr();
        configure();
        if (Cfg.usesFit(mode)) {
            fit.start(mode, gyroMax > 0 && mode == Cfg.MODE_MAX);
        }
        registerListener();

        builder.addEv(nowS, "cap", "start:" + Cfg.modeName(mode));
        builder.addEv(nowS, "cfg", cfgLabel());
        vibe(200);
        WatchUi.requestUpdate();
    }

    function stop(reason as Number) as Void {
        if (!capturing) {
            return;
        }
        var nowS = clock.nowS();
        unregisterListener();
        try {
            Sensor.enableSensorEvents(null);
            Sensor.setEnabledSensors([]);
        } catch (e) {
        }
        var fitWasOn = fit.state == FitState.REC;
        fit.stop(true);
        builder.addEv(nowS, "cap", "stop:" + (Stop.NAMES[reason] as String)
            + " rr=" + hrv.total.toString() + " ok=" + hrv.accepted.toString()
            + (fitWasOn ? " fit=1" : ""));
        sealNow();
        capturing = false;
        ble.setSession(0);
        sid = null;
        builder.sid = null;
        hr = null;
        if (reason == Stop.FLOOR) {
            note = WatchUi.loadResource(Rez.Strings.BatLow) as String;
        } else if (reason == Stop.USER) {
            note = WatchUi.loadResource(Rez.Strings.Saved) as String;
        }
        if (reason != Stop.APP) {
            uploader.request(false);
            if (!(reason == Stop.FLOOR && mode == Cfg.MODE_SONO)) {
                vibe(400);
            }
        }
        WatchUi.requestUpdate();
    }

    //! Change mode; a running capture restarts under a new session id.
    function setMode(m as Number) as Void {
        if (m == mode && capturing) {
            return;
        }
        if (capturing) {
            stop(Stop.SWITCH);
            start(m);
        } else {
            mode = m;
        }
    }

    //! App is exiting: stop, persist, release the timer.
    function shutdown() as Void {
        stop(Stop.APP);
        sealNow();
        _timer.stop();
        ble.disable();
    }

    function onSettingsChanged() as Void {
        if (!capturing) {
            mode = Cfg.defaultMode();
        } else {
            gov.budget = Cfg.budget(mode);
            gov.floor = Cfg.floorPct();
        }
        setBridge(Cfg.bleBridge());
        uploader.request(true);
    }

    //! User marker. While idle it is sealed and sent right away (the app doubles as a
    //! stress-marker button even without a capture running).
    function mark(idx as Number) as Void {
        if (idx < 0 || idx >= MARKS.size()) {
            return;
        }
        var label = MARKS[idx] as String;
        var nowS = clock.nowS();
        builder.addEv(nowS, "mark", label);
        if (!capturing) {
            sealNow();
            uploader.request(false);
        }
        vibe(80);
        if (WatchUi has :showToast) {
            WatchUi.showToast((WatchUi.loadResource(Rez.Strings.Marked) as String) + " " + label, null);
        }
        WatchUi.requestUpdate();
    }

    // ---- sensors ----------------------------------------------------------------------------

    private function probeRates() as Void {
        try {
            if (Sensor has :getMaxSampleRate) {
                sysMax = Sensor.getMaxSampleRate();
            }
        } catch (e) {
            sysMax = 0;
        }
        try {
            if (Sensor has :getMaxSampleRateForSensorType) {
                accMax = Sensor.getMaxSampleRateForSensorType(:accelerometer);
            }
        } catch (e) {
            accMax = 0;
        }
        if (accMax <= 0) {
            accMax = sysMax;
        }
        try {
            if (Sensor has :getMaxSampleRateForSensorType) {
                gyroMax = Sensor.getMaxSampleRateForSensorType(:gyroscope);
            }
        } catch (e) {
            gyroMax = 0;
        }
        if (gyroMax < 0) {
            gyroMax = 0;
        }
    }

    private function cfgLabel() as String {
        return "acc_max=" + accMax.toString() + " sys_max=" + sysMax.toString()
            + " gyro_max=" + gyroMax.toString() + " p=" + period.toString()
            + " hz=" + accHz.toString() + " gyro=" + (gyroOn ? "1" : "0")
            + " fit=" + (fit.state == FitState.REC ? fit.logged : "0")
            + " v=" + Cfg.VERSION;
    }

    private function enableHr() as Void {
        try {
            if (mode == Cfg.MODE_TREINO || mode == Cfg.MODE_MAX) {
                // paired chest strap (ANT+) when present; the wrist sensor stays on below
                Sensor.setEnabledSensors([Sensor.SENSOR_HEARTRATE]);
            }
            if (Sensor has :enableSensorType) {
                Sensor.enableSensorType(Sensor.SENSOR_ONBOARD_HEARTRATE);
            } else {
                Sensor.setEnabledSensors([Sensor.SENSOR_HEARTRATE]);
            }
            Sensor.enableSensorEvents(method(:onSensorInfo));
        } catch (e) {
            sensorErr = "hr";
        }
    }

    //! Listener parameters from the mode preset and the governor level.
    private function configure() as Void {
        var lvl = gov.level;
        period = Cfg.period(mode);
        if (lvl >= 1) {
            period = 4;
        }
        accHz = Cfg.accHz(mode, accMax);
        if (lvl >= 2 && accHz > 10) {
            accHz = 10;
        }
        if (lvl >= 3) {
            accHz = 0;
        }
        gyroOn = mode == Cfg.MODE_MAX && gyroMax > 0 && lvl == 0;
    }

    //! One combined listener (accelerometer + RR [+ gyroscope]); falls back step by step:
    //! without gyro, at 25 Hz, then RR only.
    private function registerListener() as Void {
        unregisterListener();
        listenerOk = false;
        if (gyroOn) {
            if (tryRegister(period, accHz, gyroMax)) {
                return;
            }
            gyroOn = false;
        }
        if (accHz > 0) {
            if (tryRegister(period, accHz, 0)) {
                return;
            }
            if (accHz != 25 && tryRegister(period, 25, 0)) {
                accHz = 25;
                return;
            }
        }
        enmo = 0;   // no accelerometer from here on: do not keep the last value on screen / in FIT
        if (tryRegister(period, 0, 0)) {
            accHz = 0;
            return;
        }
        builder.addEv(clock.nowS(), "err", "listener:" + sensorErr);
    }

    private function tryRegister(p as Number, hz as Number, gHz as Number) as Boolean {
        try {
            if (gHz > 0) {
                Sensor.registerSensorDataListener(method(:onSensorData), {
                    :period => p,
                    :accelerometer => {:enabled => true, :sampleRate => hz},
                    :gyroscope => {:enabled => true, :sampleRate => gHz},
                    :heartBeatIntervals => {:enabled => true}
                });
            } else if (hz > 0) {
                Sensor.registerSensorDataListener(method(:onSensorData), {
                    :period => p,
                    :accelerometer => {:enabled => true, :sampleRate => hz},
                    :heartBeatIntervals => {:enabled => true}
                });
            } else {
                Sensor.registerSensorDataListener(method(:onSensorData), {
                    :period => p,
                    :heartBeatIntervals => {:enabled => true}
                });
            }
            listenerOk = true;
            sensorErr = "";
            return true;
        } catch (e) {
            var m = e.getErrorMessage();
            sensorErr = (m != null) ? m : "register";
            return false;
        }
    }

    private function unregisterListener() as Void {
        try {
            Sensor.unregisterSensorDataListener();
        } catch (e) {
        }
        listenerOk = false;
    }

    //! 1 Hz Sensor.Info: heart rate for hr1.
    function onSensorInfo(info as Sensor.Info) as Void {
        if (!capturing) {
            return;
        }
        var b = info.heartRate;
        hr = (b != null && b > 0 && b < 255) ? b : null;
        var tS = clock.nowS();
        if (!builder.hrFits(tS)) {
            sealNow();
        }
        builder.addHr(tS, hr);
    }

    //! High-rate callback, once per period: RR intervals + accelerometer (+ gyroscope).
    function onSensorData(data as Sensor.SensorData) as Void {
        if (!capturing) {
            return;
        }
        var tMs = clock.nowMs();

        var hrd = data.heartRateData;
        if (hrd != null) {
            var ivs = hrd.heartBeatIntervals as Array<Number> or Null;
            if (ivs != null && ivs.size() > 0) {
                if (!builder.addRr(tMs, ivs)) {
                    hrv.breakChain();
                }
                for (var i = 0; i < ivs.size(); i++) {
                    var v = ivs[i];
                    if (v != null) {
                        hrv.add(v);
                    }
                }
                fit.pushRr(ivs);
            }
        }

        var ad = data.accelerometerData;
        if (ad != null && accHz > 0) {
            var x = ad.x as Array<Numeric> or Null;
            var y = ad.y as Array<Numeric> or Null;
            var z = ad.z as Array<Numeric> or Null;
            if (x != null && y != null && z != null) {
                var n = x.size();
                if (y.size() < n) {
                    n = y.size();
                }
                if (z.size() < n) {
                    n = z.size();
                }
                // raw samples straight to the laptop when the BLE bridge is up (too big for the phone)
                ble.pushAccel(tMs - (period * 1000).toLong(), x, y, z, n, period);
                // the callback covers `period` seconds (API reference); one window per second
                var w = period;
                accSeen = n / w;
                for (var k = 0; k < w && n > 0; k++) {
                    if (feat.window(x, y, z, k * n / w, (k + 1) * n / w)) {
                        builder.addAcc(tMs - ((w - k) * 1000).toLong(), feat.enmo, feat.mad);
                        enmo = feat.enmo;
                    }
                }
            }
        }

        if (gyroOn && (data has :gyroscopeData)) {
            var gd = data.gyroscopeData;
            if (gd != null) {
                var gx = gd.x as Array<Numeric> or Null;
                var gy = gd.y as Array<Numeric> or Null;
                var gz = gd.z as Array<Numeric> or Null;
                if (gx != null && gy != null && gz != null) {
                    var gn = gx.size();
                    if (gy.size() < gn) {
                        gn = gy.size();
                    }
                    if (gz.size() < gn) {
                        gn = gz.size();
                    }
                    feat.gyroWindow(gx, gy, gz, 0, gn);
                }
            }
        }

        if (builder.bytes >= Cfg.sealBytes(mode)) {
            sealNow();
            if (mode != Cfg.MODE_SONO) {
                uploader.request(false);
            }
        }
    }

    // ---- 1 Hz tick --------------------------------------------------------------------------

    function onTick() as Void {
        clock.discipline();
        var nowS = clock.nowS();
        if (!_booted) {
            _booted = true;
            uploader.request(false);   // batches left over from a previous run
        }
        if (capturing) {
            tickCapture(nowS);
        }
        var d = gov.drain;
        ble.tick(nowS, gov.battery.toNumber(), (d != null) ? (d * 10.0).toNumber() : 0);
        var bleReady = ble.ready();
        if (bleReady && !_bleWasReady) {
            uploader.request(true);   // the bridge just came up: queued batches can go now
        }
        _bleWasReady = bleReady;
        uploader.tick(nowS);

        var sono = capturing && mode == Cfg.MODE_SONO;
        if (!sono || nowS - _lastUiS >= UI_SONO_S) {
            _lastUiS = nowS;
            WatchUi.requestUpdate();
        }
    }

    private function tickCapture(nowS as Number) as Void {
        var bs = barStep();
        if (nowS - _lastBarS >= bs) {
            _lastBarS = nowS;
            samplePressure(nowS, bs);
        }

        if (Cfg.usesDfa(mode)) {
            if (nowS - _lastDfaS >= DFA_EVERY_S) {
                _lastDfaS = nowS;
                hrv.dfaBegin();
            }
            hrv.dfaStep();
        }

        fit.tick(enmo, hrv.alpha1, feat.gyro);

        var act = gov.tick(nowS);
        if (act == GovAct.FLOOR) {
            stop(Stop.FLOOR);
            return;
        }
        if (act == GovAct.LEVEL) {
            configure();
            registerListener();
            var d = gov.drain;
            builder.addEv(nowS, "gov", "lvl=" + gov.level.toString()
                + " p=" + period.toString() + " hz=" + accHz.toString()
                + " drain=" + ((d != null) ? d.format("%.1f") : "?"));
        }

        if (nowS - _lastHistS >= HIST_EVERY_S) {
            _lastHistS = nowS;
            harvestHistory(nowS);
        }

        var every = Cfg.uploadS(mode) * gov.uploadMult();
        if (nowS - _lastFlushS >= every) {
            _lastFlushS = nowS;
            sealNow();
            uploader.request(false);
        } else if (builder.bytes >= Cfg.sealBytes(mode)) {
            // normally sealed by onSensorData; this covers a listener that never registered
            sealNow();
            if (mode != Cfg.MODE_SONO) {
                uploader.request(false);
            }
        } else if (mode == Cfg.MODE_SONO && queue.bytes >= SONO_QUEUE_PUSH) {
            uploader.request(false);
        }
    }

    function barStep() as Number {
        return Cfg.barS(mode) * ((gov.level >= 3) ? 3 : 1);
    }

    private function samplePressure(nowS as Number, step as Number) as Void {
        var pa = null;
        try {
            var ai = Activity.getActivityInfo();
            if (ai != null && (ai has :rawAmbientPressure)) {
                var r = ai.rawAmbientPressure;
                if (r != null && r > 0.0) {
                    pa = (r + 0.5).toNumber();
                }
            }
        } catch (e) {
        }
        lastPa = pa;
        if (!builder.barFits(nowS, step)) {
            sealNow();
        }
        builder.addBar(nowS, step, pa);
    }

    // ---- batches ----------------------------------------------------------------------------

    //! Close the open batch: number it, stamp battery + ActivityMonitor snapshot, persist it.
    function sealNow() as Void {
        var nowS = clock.nowS();
        if (queue.pendingDrops > 0) {
            builder.addEv(nowS, "q", "drop=" + queue.pendingDrops.toString());
            queue.pendingDrops = 0;
        }
        if (builder.isEmpty()) {
            return;
        }
        var q = queue.nextSeq();
        var b = builder.seal(devId, q);
        gov.read();
        b["bat"] = [nowS, gov.battery, gov.days];
        b["m"] = snapshot(nowS);
        queue.push(q, b, builder.bytes);
        builder.reset();
        if (_histDirty) {
            // only now are the harvested points in the queue; a crash before this re-harvests them
            _histDirty = false;
            try {
                Storage.setValue("hc", _histCursor as Storage.ValueType);
            } catch (e) {
            }
        }
    }

    private function snapshot(nowS as Number) as Dictionary<String, Object> {
        var m = {"t" => nowS} as Dictionary<String, Object>;
        try {
            var ai = ActivityMonitor.getInfo();
            if (ai has :stressScore) {
                var s = ai.stressScore;
                if (s != null) {
                    m["stress"] = s;
                }
            }
            if (ai has :respirationRate) {
                var r = ai.respirationRate;
                if (r != null) {
                    m["resp"] = r;
                }
            }
            var st = ai.steps;
            if (st != null) {
                m["steps"] = st;
            }
        } catch (e) {
        }
        try {
            // averageRestingHeartRate is measured; restingHeartRate is the value typed into the
            // user profile, so it is only a fallback (the central stores "rhr" as a measurement)
            var p = UserProfile.getProfile();
            var rhr = (p has :averageRestingHeartRate) ? p.averageRestingHeartRate : null;
            if (rhr == null && (p has :restingHeartRate)) {
                rhr = p.restingHeartRate;
            }
            if (rhr != null && rhr > 0) {
                m["rhr"] = rhr;
            }
        } catch (e) {
        }
        return m;
    }

    //! SensorHistory points (Garmin stress, Body Battery, SpO2) since per-series cursors.
    //! getStressHistory may come back empty on the FR255; the "m" snapshot covers that.
    //! Each series is read on its own (one failing does not cost the others), and the open batch
    //! is sealed first when the points would push it past the POST size limit.
    private function harvestHistory(nowS as Number) as Void {
        if (!(Toybox has :SensorHistory)) {
            return;
        }
        var h = {} as Dictionary<String, Array>;
        var adv = {} as Dictionary<String, Number>;   // new cursor per series, applied below
        var nBytes = 0;
        if (SensorHistory has :getStressHistory) {
            try {
                nBytes += collect(SensorHistory.getStressHistory(histOpts("stress", nowS)), "stress", h, adv);
            } catch (e) {
            }
        }
        if (SensorHistory has :getBodyBatteryHistory) {
            try {
                nBytes += collect(SensorHistory.getBodyBatteryHistory(histOpts("bb", nowS)), "bb", h, adv);
            } catch (e) {
            }
        }
        if (SensorHistory has :getOxygenSaturationHistory) {
            try {
                nBytes += collect(SensorHistory.getOxygenSaturationHistory(histOpts("spo2", nowS)), "spo2", h, adv);
            } catch (e) {
            }
        }
        if (h.size() > 0) {
            if (builder.bytes + nBytes > Cfg.sealBytes(mode)) {
                sealNow();   // persists only cursors of points already in that batch
            }
            builder.setHistory(h, nBytes);
            var ks = adv.keys();
            for (var i = 0; i < ks.size(); i++) {
                var k = ks[i];
                var t = adv[k];
                if (t != null) {
                    _histCursor[k] = t;
                }
            }
            _histDirty = true;
        }
    }

    private function histOpts(key as String, nowS as Number) as { :period as Time.Duration, :order as SensorHistory.Order } {
        var cur = _histCursor.get(key);
        var span = (cur == null) ? HIST_MAX_S : nowS - cur + 60;
        if (span > HIST_MAX_S) {
            span = HIST_MAX_S;
        }
        if (span < 60) {
            span = 60;
        }
        return {:period => new Time.Duration(span), :order => SensorHistory.ORDER_OLDEST_FIRST};
    }

    private function collect(it as SensorHistory.SensorHistoryIterator or Null, key as String, h as Dictionary<String, Array>,
            adv as Dictionary<String, Number>) as Number {
        if (it == null) {
            return 0;
        }
        var cur = _histCursor.get(key);
        var since = (cur == null) ? 0 : cur;
        var pts = [] as Array<Array>;
        var bytes = 0;
        var newest = since;
        var s = it.next();
        while (s != null && pts.size() < HIST_MAX_POINTS) {
            var t = s.when.value();
            var v = s.data;
            if (t > since && v != null) {
                pts.add([t, v]);
                bytes += 18;
                if (t > newest) {
                    newest = t;
                }
            }
            s = it.next();
        }
        if (pts.size() > 0) {
            h[key] = pts;
            adv[key] = newest;
            bytes += 10;
        }
        return bytes;
    }

    // ---- persistence helpers -----------------------------------------------------------------

    private function loadCursor() as Dictionary<String, Number> {
        try {
            var v = Storage.getValue("hc");
            if (v instanceof Dictionary) {
                return v as Dictionary<String, Number>;
            }
        } catch (e) {
        }
        return {} as Dictionary<String, Number>;
    }

    //! Random device id created once ("fr255-cap-xxxxxx"); the watch face has its own.
    private function loadDeviceId() as String {
        try {
            var v = Storage.getValue("dev");
            if (v instanceof String && v.length() >= 3) {
                return v;
            }
        } catch (e) {
        }
        Math.srand(System.getTimer() + Time.now().value());
        var hex = "0123456789abcdef";
        var s = "fr255-cap-";
        for (var i = 0; i < 6; i++) {
            var k = Math.rand() % 16;
            s += hex.substring(k, k + 1) as String;
        }
        try {
            Storage.setValue("dev", s);
        } catch (e) {
        }
        return s;
    }

    private function vibe(ms as Number) as Void {
        if (Attention has :vibrate) {
            try {
                Attention.vibrate([new Attention.VibeProfile(50, ms)]);
            } catch (e) {
            }
        }
    }
}
