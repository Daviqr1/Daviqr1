import Toybox.Communications;
import Toybox.Lang;
import Toybox.PersistedContent;

//! Result callback: code is an HTTP status (200 = stored) or a negative link error
//! (Communications.BLE_* / NETWORK_*); data is the parsed JSON response when there is one.
typedef SendCallback as Method(code as Number, data as Dictionary or Null) as Void;

//! A way to get one sealed batch to the central. The uploader keeps exactly one send in
//! flight and owns retries/backoff; a transport only performs a single attempt.
class Transport {
    function initialize() {
    }

    //! Short label for the status screen.
    function name() as String {
        return "?";
    }

    //! Can this transport be used right now (configured / connected)?
    function isAvailable() as Boolean {
        return false;
    }

    //! Send one batch; must call cb exactly once.
    function send(batch as Dictionary<String, Object>, cb as SendCallback) as Void {
        cb.invoke(Communications.UNKNOWN_ERROR, null);
    }
}

//! HTTPS POST <central>/v1/ingest through Garmin Connect Mobile (BLE proxy).
class HttpTransport extends Transport {
    function initialize() {
        Transport.initialize();
    }

    function name() as String {
        return "https";
    }

    function isAvailable() as Boolean {
        return Cfg.hasCentral();
    }

    function send(batch as Dictionary<String, Object>, cb as SendCallback) as Void {
        var options = {
            :method => Communications.HTTP_REQUEST_METHOD_POST,
            :headers => {
                "Content-Type" => Communications.REQUEST_CONTENT_TYPE_JSON,
                "Authorization" => "Bearer " + Cfg.token()
            },
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
        };
        // One callback object per request: a late answer to a request the uploader already gave
        // up on reaches that request's own callback, never the one of a newer request.
        var call = new HttpCall(cb);
        try {
            Communications.makeWebRequest(Cfg.centralUrl() + "/v1/ingest", batch as Dictionary<Object, Object>, options, call.method(:onResponse) as Method(responseCode as Number, data as Dictionary or String or PersistedContent.Iterator or Null) as Void);
        } catch (e) {
            cb.invoke(Communications.UNKNOWN_ERROR, null);
        }
    }
}

//! The pending state of one HTTP request.
class HttpCall {
    private var _cb as SendCallback;

    function initialize(cb as SendCallback) {
        _cb = cb;
    }

    function onResponse(code as Number, data as Dictionary or String or PersistedContent.Iterator or Null) as Void {
        _cb.invoke(code, (data instanceof Dictionary) ? data as Dictionary : null);
    }
}

//! An upload-queue batch over the BLE bridge (BleLink): JSON bytes + HMAC, acknowledged by the
//! bridge only once stored. Used by the uploader's router when the phone is away or the batch is
//! too big for the phone route.
class BleTransport extends Transport {
    private var _link as BleLink;

    function initialize(link as BleLink) {
        Transport.initialize();
        _link = link;
    }

    function name() as String {
        return "ble";
    }

    //! The bridge needs the device token to check the signature (HMAC key = SHA-256(token)).
    function isAvailable() as Boolean {
        return _link.ready() && Cfg.token().length() > 0;
    }

    function send(batch as Dictionary<String, Object>, cb as SendCallback) as Void {
        var bytes;
        try {
            bytes = Json.encode(batch);
        } catch (e) {
            cb.invoke(Communications.INVALID_HTTP_BODY_IN_REQUEST, null);
            return;
        }
        var q = batch["q"];
        if (bytes.size() > 65535 || !(q instanceof Number)) {
            cb.invoke(413, null);
            return;
        }
        _link.sendBlob(q, bytes, cb);
    }
}
