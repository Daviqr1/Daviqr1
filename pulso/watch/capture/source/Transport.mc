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
    private var _cb as SendCallback or Null;

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
        _cb = cb;
        var options = {
            :method => Communications.HTTP_REQUEST_METHOD_POST,
            :headers => {
                "Content-Type" => Communications.REQUEST_CONTENT_TYPE_JSON,
                "Authorization" => "Bearer " + Cfg.token()
            },
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
        };
        try {
            Communications.makeWebRequest(Cfg.centralUrl() + "/v1/ingest", batch as Dictionary<Object, Object>, options, method(:onResponse));
        } catch (e) {
            _cb = null;
            cb.invoke(Communications.UNKNOWN_ERROR, null);
        }
    }

    function onResponse(code as Number, data as Dictionary or String or PersistedContent.Iterator or Null) as Void {
        var cb = _cb;
        _cb = null;
        if (cb != null) {
            cb.invoke(code, (data instanceof Dictionary) ? data as Dictionary : null);
        }
    }
}

//! Direct BLE link to the Pulso bridge (laptop/ESP32), for raw accelerometer at full rate.
//! TODO(docs/ponte-ble.md): implement with Toybox.BluetoothLowEnergy once the bridge GATT
//! service is specified there (service/characteristic UUIDs, framing, ack). Needs the
//! BluetoothLowEnergy permission and a pairing/scan flow. Until then it reports unavailable
//! and the uploader never selects it.
class BleTransport extends Transport {
    function initialize() {
        Transport.initialize();
    }

    function name() as String {
        return "ble";
    }

    function isAvailable() as Boolean {
        return false;
    }

    function send(batch as Dictionary<String, Object>, cb as SendCallback) as Void {
        // Not implemented: see the TODO above. Report a link error so nothing is lost.
        cb.invoke(Communications.BLE_CONNECTION_UNAVAILABLE, null);
    }
}
