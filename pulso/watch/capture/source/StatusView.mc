import Toybox.Graphics;
import Toybox.Lang;
import Toybox.System;
import Toybox.Time;
import Toybox.WatchUi;

//! Colours that map exactly onto the FR255's 64-colour (RGB222) MIP palette.
module Ui {
    const BG = 0x000000;
    const FG = 0xFFFFFF;
    const DIM = 0xAAAAAA;
    const DIM2 = 0x555555;
    const HR = 0xFF5555;
    const HRV = 0x00AAFF;
    const OK = 0x55FF55;
    const WARN = 0xFFAA00;
    const ERR = 0xFF0000;
    const SLEEP = 0x555555;
    const MODE_COLORS = [0x00AAFF, 0x5555FF, 0xFF5500, 0xFF00FF];
}

//! Status screen (buttons only, round 260x260). Page 0: live status; page 1: details.
//! In SONO the live page is a minimal dark screen refreshed once a minute.
class StatusView extends WatchUi.View {
    var page as Number = 0;

    private var _cap as Capture;
    private var _s as Dictionary<Symbol, String>;
    private var _linkTxt as String = "";
    private var _linkColor as Number = 0xAAAAAA;

    function initialize(cap as Capture) {
        View.initialize();
        _cap = cap;
        _s = {} as Dictionary<Symbol, String>;
        _s[:press] = WatchUi.loadResource(Rez.Strings.PressStart) as String;
        _s[:queue] = WatchUi.loadResource(Rez.Strings.Queue) as String;
        _s[:nocfg] = WatchUi.loadResource(Rez.Strings.NoCentral) as String;
        _s[:nophone] = WatchUi.loadResource(Rez.Strings.NoPhone) as String;
        _s[:sending] = WatchUi.loadResource(Rez.Strings.Sending) as String;
        _s[:auth] = WatchUi.loadResource(Rez.Strings.AuthErr) as String;
        _s[:ok] = WatchUi.loadResource(Rez.Strings.LinkOk) as String;
        _s[:idle] = WatchUi.loadResource(Rez.Strings.LinkIdle) as String;
        _s[:err] = WatchUi.loadResource(Rez.Strings.LinkErr) as String;
        _s[:aer] = WatchUi.loadResource(Rez.Strings.Aerobic) as String;
        _s[:tra] = WatchUi.loadResource(Rez.Strings.Transition) as String;
        _s[:ana] = WatchUi.loadResource(Rez.Strings.Anaerobic) as String;
        _s[:senserr] = WatchUi.loadResource(Rez.Strings.SensorErr) as String;
        _s[:fiton] = WatchUi.loadResource(Rez.Strings.FitOn) as String;
        _s[:fitoff] = WatchUi.loadResource(Rez.Strings.FitOff) as String;
        _s[:fiterr] = WatchUi.loadResource(Rez.Strings.FitErr) as String;
        _s[:details] = WatchUi.loadResource(Rez.Strings.Details) as String;
        _s[:sent] = WatchUi.loadResource(Rez.Strings.Sent) as String;
        _s[:dropped] = WatchUi.loadResource(Rez.Strings.Dropped) as String;
        _s[:rec] = WatchUi.loadResource(Rez.Strings.Recording) as String;
        _s[:foco] = WatchUi.loadResource(Rez.Strings.ModeFoco) as String;
        _s[:sono] = WatchUi.loadResource(Rez.Strings.ModeSono) as String;
        _s[:treino] = WatchUi.loadResource(Rez.Strings.ModeTreino) as String;
        _s[:max] = WatchUi.loadResource(Rez.Strings.ModeMax) as String;
    }

    function onUpdate(dc as Graphics.Dc) as Void {
        dc.setColor(Ui.BG, Ui.BG);
        dc.clear();
        if (dc has :setAntiAlias) {
            dc.setAntiAlias(true);
        }
        var nowS = Time.now().value();
        if (page == 1) {
            drawDetails(dc, nowS);
        } else if (!_cap.capturing) {
            drawIdle(dc, nowS);
        } else if (_cap.mode == Cfg.MODE_SONO) {
            drawSleep(dc, nowS);
        } else {
            drawLive(dc, nowS);
        }
    }

    // ---- pages ------------------------------------------------------------------------------

    private function drawLive(dc as Graphics.Dc, nowS as Number) as Void {
        var w = dc.getWidth();
        var cx = w / 2;
        var cap = _cap;
        var xt = Graphics.FONT_XTINY;
        var xtH = dc.getFontHeight(xt);

        // mode + recording dot, elapsed time
        var y = 14;
        drawMode(dc, cx, y);
        y += dc.getFontHeight(Graphics.FONT_TINY);
        dc.setColor(Ui.FG, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, y, Graphics.FONT_SMALL, elapsed(nowS - cap.startS), Graphics.TEXT_JUSTIFY_CENTER);

        // big numbers: HR | RMSSD or alpha1
        var numF = Graphics.FONT_NUMBER_MEDIUM;
        var y0 = 74;
        var hr = cap.hr;
        dc.setColor(Ui.HR, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx - 56, y0, numF, (hr != null) ? hr.toString() : "--", Graphics.TEXT_JUSTIFY_CENTER);
        var yl = y0 + dc.getFontHeight(numF) - 2;
        dc.setColor(Ui.DIM, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx - 56, yl, xt, "bpm", Graphics.TEXT_JUSTIFY_CENTER);

        var hrv = cap.hrv;
        var dfa = Cfg.usesDfa(cap.mode);
        var right = "--";
        var label = "RMSSD ms";
        var color = Ui.HRV;
        if (dfa) {
            var a = hrv.alpha1;
            label = "α1";
            if (a != null && hrv.fresh()) {
                right = a.format("%.2f");
                color = zoneColor(HrvLive.zone(a));
            } else if (hrv.dfaBeats() < hrv.dfaWindow()) {
                label = "α1 " + hrv.dfaBeats().toString() + "/" + hrv.dfaWindow().toString();
            }
        } else {
            var r = hrv.rmssd;
            if (r != null && hrv.fresh()) {
                right = (r + 0.5).toNumber().toString();
            }
        }
        dc.setColor(color, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx + 56, y0, numF, right, Graphics.TEXT_JUSTIFY_CENTER);
        dc.setColor(Ui.DIM, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx + 56, yl, xt, label, Graphics.TEXT_JUSTIFY_CENTER);

        y = yl + xtH + 4;
        if (dfa) {
            y = drawZoneBar(dc, cx, y, hrv.fresh() ? hrv.alpha1 : null);
        }

        // RR quality + movement (+ pressure)
        var q = hrv.qualityPct();
        var line = "RR " + ((q < 0) ? "--" : q.toString() + "%") + "  mov " + cap.enmo.toString();
        var pa = cap.lastPa;
        if (pa != null && !dfa) {
            line += "  " + (pa / 100).toString() + " hPa";
        }
        dc.setColor((q >= 0 && q < 70) ? Ui.WARN : Ui.FG, Graphics.COLOR_TRANSPARENT);
        if (!cap.listenerOk) {
            dc.setColor(Ui.ERR, Graphics.COLOR_TRANSPARENT);
            line = _s[:senserr] as String;
        }
        dc.drawText(cx, y, xt, line, Graphics.TEXT_JUSTIFY_CENTER);
        y += xtH + 2;

        drawBattery(dc, cx, y);
        y += xtH + 2;
        drawQueue(dc, cx, y);
        y += xtH + 2;
        drawLink(dc, cx, y, nowS);
    }

    private function drawIdle(dc as Graphics.Dc, nowS as Number) as Void {
        var cx = dc.getWidth() / 2;
        var xt = Graphics.FONT_XTINY;
        var xtH = dc.getFontHeight(xt);
        var cap = _cap;

        dc.setColor(Ui.DIM, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, 22, Graphics.FONT_TINY, "Pulso", Graphics.TEXT_JUSTIFY_CENTER);
        dc.setColor(modeColor(cap.mode), Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, 50, Graphics.FONT_MEDIUM, modeLabel(cap.mode), Graphics.TEXT_JUSTIFY_CENTER);
        var y = 50 + dc.getFontHeight(Graphics.FONT_MEDIUM) + 2;
        dc.setColor(Ui.FG, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, y, xt, _s[:press] as String, Graphics.TEXT_JUSTIFY_CENTER);
        y += xtH + 6;
        var note = cap.note;
        if (note != null) {
            dc.setColor(Ui.WARN, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, y, xt, note, Graphics.TEXT_JUSTIFY_CENTER);
        }
        y = 150;
        cap.gov.read();
        drawBattery(dc, cx, y);
        y += xtH + 4;
        drawQueue(dc, cx, y);
        y += xtH + 4;
        drawLink(dc, cx, y, nowS);
    }

    private function drawSleep(dc as Graphics.Dc, nowS as Number) as Void {
        var cx = dc.getWidth() / 2;
        var cy = dc.getHeight() / 2;
        dc.setColor(0x550000, Graphics.COLOR_TRANSPARENT);
        dc.fillCircle(cx, cy - 40, 3);
        dc.setColor(Ui.SLEEP, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, cy - 28, Graphics.FONT_XTINY, _s[:sono] as String, Graphics.TEXT_JUSTIFY_CENTER);
        var e = nowS - _cap.startS;
        dc.drawText(cx, cy - 4, Graphics.FONT_SMALL, (e / 3600).toString() + ":" + ((e / 60) % 60).format("%02d"),
            Graphics.TEXT_JUSTIFY_CENTER);
    }

    private function drawDetails(dc as Graphics.Dc, nowS as Number) as Void {
        var cx = dc.getWidth() / 2;
        var xt = Graphics.FONT_XTINY;
        var step = dc.getFontHeight(xt) + 1;
        var cap = _cap;
        var u = cap.uploader;
        var st = System.getSystemStats();
        var lines = [
            cap.devId,
            "q " + cap.queue.seq.toString() + "  " + ((cap.sid != null) ? cap.sid as String : "-"),
            "acc " + cap.accHz.toString() + "/" + cap.accSeen.toString() + "Hz p" + cap.period.toString()
                + "s max " + cap.accMax.toString() + " g " + cap.gyroMax.toString(),
            "gov N" + cap.gov.level.toString() + "  " + cap.gov.budget.format("%.1f") + "%/h"
                + "  min " + cap.gov.floor.toString() + "%",
            fitText(),
            _s[:sent] as String + " " + u.sent.toString() + "  dup " + u.dups.toString(),
            _s[:dropped] as String + " " + cap.queue.drops.toString() + "  cod " + u.lastCode.toString(),
            "mem " + (st.usedMemory / 1024).toString() + "/" + (st.totalMemory / 1024).toString() + " KB  v"
                + Cfg.VERSION
        ] as Array<String>;
        dc.setColor(Ui.DIM, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, 18, Graphics.FONT_TINY, _s[:details] as String, Graphics.TEXT_JUSTIFY_CENTER);
        var y = 18 + dc.getFontHeight(Graphics.FONT_TINY) + 2;
        dc.setColor(Ui.FG, Graphics.COLOR_TRANSPARENT);
        for (var i = 0; i < lines.size(); i++) {
            dc.drawText(cx, y, xt, lines[i], Graphics.TEXT_JUSTIFY_CENTER);
            y += step;
        }
        if (cap.sensorErr.length() > 0) {
            dc.setColor(Ui.ERR, Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, y, xt, cap.sensorErr, Graphics.TEXT_JUSTIFY_CENTER);
        }
    }

    // ---- pieces -----------------------------------------------------------------------------

    private function drawMode(dc as Graphics.Dc, cx as Number, y as Number) as Void {
        var f = Graphics.FONT_TINY;
        var txt = modeLabel(_cap.mode);
        var tw = dc.getTextWidthInPixels(txt, f);
        dc.setColor(Ui.ERR, Graphics.COLOR_TRANSPARENT);
        dc.fillCircle(cx - tw / 2 - 9, y + dc.getFontHeight(f) / 2, 4);
        dc.setColor(modeColor(_cap.mode), Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, y, f, txt, Graphics.TEXT_JUSTIFY_CENTER);
    }

    //! alpha1 scale 0.25..1.25 with the < 0.5 / 0.5-0.75 / >= 0.75 zones. Returns next y.
    private function drawZoneBar(dc as Graphics.Dc, cx as Number, y as Number, a as Float or Null) as Number {
        var x0 = cx - 70;
        var bw = 140;
        var lo = 0.25;
        var hi = 1.25;
        var x50 = x0 + ((0.5 - lo) / (hi - lo) * bw).toNumber();
        var x75 = x0 + ((0.75 - lo) / (hi - lo) * bw).toNumber();
        dc.setColor(Ui.ERR, Graphics.COLOR_TRANSPARENT);
        dc.fillRectangle(x0, y, x50 - x0, 5);
        dc.setColor(Ui.WARN, Graphics.COLOR_TRANSPARENT);
        dc.fillRectangle(x50, y, x75 - x50, 5);
        dc.setColor(Ui.OK, Graphics.COLOR_TRANSPARENT);
        dc.fillRectangle(x75, y, x0 + bw - x75, 5);
        var z = HrvLive.zone(a);
        if (a != null) {
            var v = (a < lo) ? lo : ((a > hi) ? hi : a);
            var mx = x0 + ((v - lo) / (hi - lo) * bw).toNumber();
            dc.setColor(Ui.FG, Graphics.COLOR_TRANSPARENT);
            dc.fillRectangle(mx - 1, y - 3, 3, 11);
        }
        y += 9;
        var t = (z == 0) ? _s[:aer] : ((z == 1) ? _s[:tra] : ((z == 2) ? _s[:ana] : null));
        if (t != null) {
            dc.setColor(zoneColor(z), Graphics.COLOR_TRANSPARENT);
            dc.drawText(cx, y, Graphics.FONT_XTINY, t, Graphics.TEXT_JUSTIFY_CENTER);
        }
        return y + dc.getFontHeight(Graphics.FONT_XTINY);
    }

    private function drawBattery(dc as Graphics.Dc, cx as Number, y as Number) as Void {
        var g = _cap.gov;
        var txt = g.battery.format("%.0f") + "%";
        var d = g.drain;
        if (_cap.capturing && d != null) {
            txt += "  " + ((d >= 0.0) ? "-" : "+") + ((d >= 0.0) ? d : -d).format("%.1f") + "%/h";
        }
        if (_cap.capturing && g.level > 0) {
            txt += "  N" + g.level.toString();
        }
        var c = Ui.FG;
        if (g.charging) {
            c = Ui.OK;
        } else if (g.battery <= g.floor + 5) {
            c = Ui.ERR;
        } else if (_cap.capturing && g.level > 0) {
            c = Ui.WARN;
        }
        dc.setColor(c, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, y, Graphics.FONT_XTINY, txt, Graphics.TEXT_JUSTIFY_CENTER);
    }

    private function drawQueue(dc as Graphics.Dc, cx as Number, y as Number) as Void {
        var q = _cap.queue;
        var kb = (q.bytes + 512) / 1024;
        dc.setColor(Ui.DIM, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, y, Graphics.FONT_XTINY, (_s[:queue] as String) + " " + q.count().toString() + " (" + kb.toString() + " KB)",
            Graphics.TEXT_JUSTIFY_CENTER);
    }

    private function drawLink(dc as Graphics.Dc, cx as Number, y as Number, nowS as Number) as Void {
        linkStatus(nowS);
        dc.setColor(_linkColor, Graphics.COLOR_TRANSPARENT);
        dc.drawText(cx, y, Graphics.FONT_XTINY, _linkTxt, Graphics.TEXT_JUSTIFY_CENTER);
    }

    private function linkStatus(nowS as Number) as Void {
        var u = _cap.uploader;
        var st = u.status;
        var retry = u.retryIn(nowS);
        var rs = (retry > 0) ? " (" + ago(retry) + ")" : "";
        if (!Cfg.hasCentral()) {
            _linkTxt = _s[:nocfg] as String;
            _linkColor = Ui.WARN;
        } else if (st == Link.SENDING) {
            _linkTxt = _s[:sending] as String;
            _linkColor = Ui.DIM;
        } else if (st == Link.NOPHONE) {
            _linkTxt = (_s[:nophone] as String) + rs;
            _linkColor = Ui.WARN;
        } else if (st == Link.AUTH) {
            _linkTxt = _s[:auth] as String;
            _linkColor = Ui.ERR;
        } else if (st == Link.ERR) {
            _linkTxt = (_s[:err] as String) + " " + u.lastCode.toString() + rs;
            _linkColor = Ui.ERR;
        } else if (u.lastOkS > 0) {
            _linkTxt = (_s[:ok] as String) + " " + ago(nowS - u.lastOkS);
            _linkColor = Ui.OK;
        } else {
            _linkTxt = _s[:idle] as String;
            _linkColor = Ui.DIM;
        }
    }

    private function fitText() as String {
        var f = _cap.fit;
        if (f.state == FitState.REC) {
            var n = f.samples();
            return (_s[:fiton] as String) + " " + f.logged + ((n >= 0) ? " " + n.toString() : "");
        }
        if (f.state == FitState.ERR) {
            return (_s[:fiterr] as String) + " " + f.err;
        }
        return _s[:fitoff] as String;
    }

    private function modeLabel(m as Number) as String {
        var k = [:foco, :sono, :treino, :max];
        return (m >= 0 && m < 4) ? _s[k[m]] as String : "?";
    }

    private function modeColor(m as Number) as Number {
        return (m >= 0 && m < 4) ? Ui.MODE_COLORS[m] as Number : Ui.FG;
    }

    private function zoneColor(z as Number) as Number {
        return (z == 0) ? Ui.OK : ((z == 1) ? Ui.WARN : ((z == 2) ? Ui.ERR : Ui.DIM));
    }

    static function elapsed(s as Number) as String {
        if (s < 0) {
            s = 0;
        }
        return (s / 3600).toString() + ":" + ((s / 60) % 60).format("%02d") + ":" + (s % 60).format("%02d");
    }

    static function ago(s as Number) as String {
        if (s < 0) {
            s = 0;
        }
        if (s < 60) {
            return s.toString() + "s";
        }
        if (s < 3600) {
            return (s / 60).toString() + "min";
        }
        return (s / 3600).toString() + "h";
    }
}
