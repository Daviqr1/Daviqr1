import Toybox.Activity;
import Toybox.ActivityRecording;
import Toybox.FitContributor;
import Toybox.Lang;
import Toybox.SensorLogging;

module FitState {
    const OFF = 0;
    const REC = 1;
    const ERR = 2;
}

//! Optional FIT recording (TREINO / MAX): an ActivityRecording session with a SensorLogger
//! (raw accelerometer, plus gyroscope when present, into the activity FIT) and developer
//! fields per 1-s record:
//!   0 rr      UINT16[6] ms   beats that arrived in that second, 0 = empty slot
//!   1 enmo    UINT16    mg
//!   2 dfa_a1  UINT16    alpha1 x100, 0 = not available
//!   3 gyro    UINT16    deg/s (only when a gyroscope is logged)
//! RR arrive in bursts (one callback per period); a FIFO spreads them over the 1-s records.
class FitRecorder {
    const RR_SLOTS = 6;
    const FIFO_MAX = 48;

    private var _session as ActivityRecording.Session or Null = null;
    private var _logger as SensorLogging.SensorLogger or Null = null;
    private var _fRr as FitContributor.Field or Null = null;
    private var _fEnmo as FitContributor.Field or Null = null;
    private var _fDfa as FitContributor.Field or Null = null;
    private var _fGyro as FitContributor.Field or Null = null;
    private var _fifo as Array<Number>;

    var state as Number = 0;
    var err as String = "";
    var logged as String = "-";    // what the SensorLogger writes: "acc+gyro", "acc" or "-"

    function initialize() {
        _fifo = [] as Array<Number>;
    }

    //! Open and start the FIT session. The raw IMU logger is tried with accelerometer +
    //! gyroscope, then accelerometer only, then without a logger (developer fields still work).
    function start(mode as Number, gyro as Boolean) as Boolean {
        if (state == FitState.REC) {
            return true;
        }
        for (var attempt = gyro ? 0 : 1; attempt < 3; attempt++) {
            _fifo = [] as Array<Number>;
            var s = openSession(mode, attempt);
            if (s == null) {
                continue;
            }
            _session = s;
            try {
                _fRr = s.createField("rr", 0, FitContributor.DATA_TYPE_UINT16,
                    {:count => RR_SLOTS, :mesgType => FitContributor.MESG_TYPE_RECORD, :units => "ms"});
                _fEnmo = s.createField("enmo", 1, FitContributor.DATA_TYPE_UINT16,
                    {:mesgType => FitContributor.MESG_TYPE_RECORD, :units => "mg"});
                _fDfa = s.createField("dfa_a1", 2, FitContributor.DATA_TYPE_UINT16,
                    {:mesgType => FitContributor.MESG_TYPE_RECORD, :units => "x100"});
                if (gyro) {
                    _fGyro = s.createField("gyro", 3, FitContributor.DATA_TYPE_UINT16,
                        {:mesgType => FitContributor.MESG_TYPE_RECORD, :units => "deg/s"});
                }
                s.start();
                state = FitState.REC;
                err = "";
                return true;
            } catch (e) {
                var m = e.getErrorMessage();
                err = (m != null) ? m : "FIT";
                discardQuietly();
            }
        }
        state = FitState.ERR;
        logged = "-";
        return false;
    }

    //! attempt 0: accel + gyro logger, 1: accel logger, 2: no logger.
    private function openSession(mode as Number, attempt as Number) as ActivityRecording.Session or Null {
        _logger = null;
        try {
            var logger = null;
            if (attempt < 2 && (Toybox has :SensorLogging)) {
                logger = (attempt == 0)
                    ? new SensorLogging.SensorLogger({:accelerometer => {:enabled => true}, :gyroscope => {:enabled => true}})
                    : new SensorLogging.SensorLogger({:accelerometer => {:enabled => true}});
            }
            var name = (mode == Cfg.MODE_MAX) ? "Pulso Max" : "Pulso Treino";
            var s = (logger != null)
                ? ActivityRecording.createSession({:name => name, :sport => Activity.SPORT_TRAINING,
                    :subSport => Activity.SUB_SPORT_CARDIO_TRAINING, :sensorLogger => logger})
                : ActivityRecording.createSession({:name => name, :sport => Activity.SPORT_TRAINING,
                    :subSport => Activity.SUB_SPORT_CARDIO_TRAINING});
            _logger = logger;
            logged = (attempt == 0) ? "acc+gyro" : ((attempt == 1) ? "acc" : "-");
            return s;
        } catch (e) {
            var m = e.getErrorMessage();
            err = (m != null) ? m : "createSession";
            return null;
        }
    }

    //! Raw IMU samples logged so far (accelerometer), or -1 when unknown.
    function samples() as Number {
        var l = _logger;
        if (l == null) {
            return -1;
        }
        try {
            var st = l.getStats();
            if (st != null) {
                var n = st.sampleCount;
                return (n != null) ? n : -1;
            }
        } catch (e) {
        }
        return -1;
    }

    function pushRr(ivs as Array<Number>) as Void {
        if (state != FitState.REC) {
            return;
        }
        for (var i = 0; i < ivs.size(); i++) {
            _fifo.add(ivs[i]);
        }
        var extra = _fifo.size() - FIFO_MAX;
        if (extra > 0) {
            _fifo = _fifo.slice(extra, null) as Array<Number>;
        }
    }

    //! Once per second: write the developer fields for the next record.
    function tick(enmo as Number, alpha1 as Float or Null, gyro as Number) as Void {
        if (state != FitState.REC) {
            return;
        }
        try {
            var rec = new Array<Number>[RR_SLOTS];
            var take = (_fifo.size() < RR_SLOTS) ? _fifo.size() : RR_SLOTS;
            for (var i = 0; i < RR_SLOTS; i++) {
                rec[i] = (i < take) ? clamp16(_fifo[i]) : 0;
            }
            if (take > 0) {
                _fifo = _fifo.slice(take, null) as Array<Number>;
            }
            var f = _fRr;
            if (f != null) {
                f.setData(rec);
            }
            f = _fEnmo;
            if (f != null) {
                f.setData(clamp16(enmo));
            }
            f = _fDfa;
            if (f != null) {
                f.setData((alpha1 == null) ? 0 : clamp16((alpha1 * 100.0 + 0.5).toNumber()));
            }
            f = _fGyro;
            if (f != null) {
                f.setData(clamp16(gyro));
            }
        } catch (e) {
            err = "setData";
        }
    }

    //! Stop and save (or discard) the FIT. Safe to call in any state.
    function stop(save as Boolean) as Void {
        var s = _session;
        if (s != null) {
            try {
                if (s.isRecording()) {
                    s.stop();
                }
                if (save) {
                    s.save();
                } else {
                    s.discard();
                }
            } catch (e) {
                err = "save";
            }
        }
        clear();
        if (state == FitState.REC) {
            state = FitState.OFF;
        }
    }

    private function discardQuietly() as Void {
        var s = _session;
        if (s != null) {
            try {
                s.discard();
            } catch (e) {
            }
        }
        clear();
    }

    private function clear() as Void {
        _session = null;
        _logger = null;
        _fRr = null;
        _fEnmo = null;
        _fDfa = null;
        _fGyro = null;
        _fifo = [] as Array<Number>;
    }

    private static function clamp16(v as Number) as Number {
        return (v < 0) ? 0 : ((v > 65534) ? 65534 : v);
    }
}
