import Toybox.Application;
import Toybox.Application.Properties;
import Toybox.Application.Storage;
import Toybox.Lang;
import Toybox.Math;
import Toybox.System;
import Toybox.Time;

//! Code shared by the foreground face and the background service. Everything here is
//! (:background) and must stay small: the background process has 64 KB in total.

//! Storage keys and status codes.
(:background)
module K {
    const DID = "did";      // device id, generated once ("fr255-xxxxxxxx")
    const SEQ = "seq";      // last protocol sequence number used (q)
    const CUR = "cur";      // {series => epoch s of the newest sample the central acknowledged}
    const OBX = "obx";      // outbox: batches not yet acknowledged, oldest first (<= OUTBOX_MAX)
    const FACE = "face";    // last `face` object returned by the central
    const ST = "st";        // last upload status [code, epoch s, q, pending]
    const SACC = "sacc";    // foreground stress accumulator (12 hourly slots)
    const ALN = "aln";      // id of the last central alert shown as a notification

    const CODE_OK = 200;
    const CODE_NO_PHONE = -104;      // = Communications.BLE_CONNECTION_UNAVAILABLE
    const CODE_NO_CONFIG = -1001;    // URL/token missing in the settings
    const CODE_DISABLED = -1002;     // upload switched off in the settings
    const CODE_EXCEPTION = -1003;    // unexpected exception inside the service

    const OUTBOX_MAX = 3;
}

//! Exception-safe Application.Storage access.
(:background)
module Store {
    function get(key as String) as Storage.ValueType {
        try {
            return Storage.getValue(key);
        } catch (e) {
            return null;
        }
    }

    //! Returns false when the value could not be written (StorageFullException, size limits).
    function put(key as String, value as Object?) as Boolean {
        try {
            Storage.setValue(key, value as Storage.ValueType);
            return true;
        } catch (e) {
            return false;
        }
    }
}

//! App settings (Application.Properties, edited in Garmin Connect). Read-only from the background.
(:background)
module Cfg {
    function str(key as String) as String? {
        try {
            var v = Properties.getValue(key);
            if (v instanceof String) {
                var s = trim(v);
                return s.length() > 0 ? s : null;
            }
        } catch (e) {
        }
        return null;
    }

    function flag(key as String, def as Boolean) as Boolean {
        try {
            var v = Properties.getValue(key);
            if (v instanceof Boolean) {
                return v;
            }
        } catch (e) {
        }
        return def;
    }

    function num(key as String, def as Number) as Number {
        try {
            var v = Properties.getValue(key);
            if (v instanceof Number) {
                return v;
            }
        } catch (e) {
        }
        return def;
    }

    function trim(s as String) as String {
        var c = s.toCharArray();
        var a = 0;
        var b = c.size();
        while (a < b && c[a].toNumber() <= 32) {
            a++;
        }
        while (b > a && c[b - 1].toNumber() <= 32) {
            b--;
        }
        var r = s.substring(a, b);
        return r != null ? r : "";
    }

    //! Full ingest URL, or null when the setting is missing, still the placeholder, or not HTTPS
    //! (Connect IQ only talks HTTPS with a publicly trusted certificate).
    function ingestUrl() as String? {
        var u = str("centralUrl");
        if (u == null || u.find("https://") != 0 || u.find("example.") != null) {
            return null;
        }
        // trailing slashes first: ".../v1/ingest/" would get a 307 redirect, which a POST does not follow
        while (u.length() > 8 && "/".equals(u.substring(u.length() - 1, u.length()))) {
            var t = u.substring(0, u.length() - 1);
            u = t != null ? t : "";
        }
        if (u.find("/v1/ingest") != null) {
            return u;
        }
        return u + "/v1/ingest";
    }

    function token() as String? {
        return str("deviceToken");
    }

    function configured() as Boolean {
        return ingestUrl() != null && token() != null;
    }
}

//! Device identity and the per-device sequence number: (d, q) is the central's idempotency key.
(:background)
module Ids {
    function device() as String {
        var d = Store.get(K.DID);
        if (d instanceof String) {
            return d;
        }
        Math.srand(Time.now().value() ^ System.getTimer());
        var hex = "0123456789abcdef";
        var s = "fr255-";
        for (var i = 0; i < 8; i++) {
            var n = Math.rand() % 16;
            if (n < 0) {
                n = -n;
            }
            s += hex.substring(n, n + 1);
        }
        Store.put(K.DID, s);
        return s;
    }

    //! Next q, persisted before the request goes out so a killed run never reuses a number.
    function nextSeq() as Number {
        var q = Store.get(K.SEQ);
        var n = (q instanceof Number) ? q + 1 : 1;
        Store.put(K.SEQ, n);
        return n;
    }
}
