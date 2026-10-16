import Toybox.Lang;
import Toybox.StringUtil;

//! Minimal JSON encoder for protocol-v1 batches (Dictionary/Array/Number/Long/Float/Double/
//! String/Boolean/null) straight into UTF-8 bytes. makeWebRequest serializes on its own; this is
//! only for the BLE route, where the batch travels as bytes. Output goes through a short String
//! buffer so the cost stays linear (Monkey C strings are immutable).
module Json {
    function encode(v as Object or Null) as ByteArray {
        var w = new JsonWriter();
        w.value(v);
        return w.bytes();
    }

    function utf8(s as String) as ByteArray {
        return StringUtil.convertEncodedString(s, {
            :fromRepresentation => StringUtil.REPRESENTATION_STRING_PLAIN_TEXT,
            :toRepresentation => StringUtil.REPRESENTATION_BYTE_ARRAY
        }) as ByteArray;
    }
}

class JsonWriter {
    private var _out as ByteArray;
    private var _buf as String = "";

    function initialize() {
        _out = []b;
    }

    function bytes() as ByteArray {
        flush();
        return _out;
    }

    private function put(s as String) as Void {
        _buf += s;
        if (_buf.length() >= 160) {
            flush();
        }
    }

    private function flush() as Void {
        if (_buf.length() > 0) {
            _out.addAll(Json.utf8(_buf));
            _buf = "";
        }
    }

    function value(v as Object or Null) as Void {
        if (v == null) {
            put("null");
        } else if (v instanceof Boolean) {
            put(v ? "true" : "false");
        } else if (v instanceof Number || v instanceof Long) {
            put(v.toString());
        } else if (v instanceof Float || v instanceof Double) {
            put(num(v.toDouble()));
        } else if (v instanceof String) {
            put("\"");
            put(escape(v));
            put("\"");
        } else if (v instanceof Array) {
            put("[");
            for (var i = 0; i < v.size(); i++) {
                if (i > 0) {
                    put(",");
                }
                value(v[i] as Object or Null);
            }
            put("]");
        } else if (v instanceof Dictionary) {
            put("{");
            var ks = v.keys();
            for (var i = 0; i < ks.size(); i++) {
                if (i > 0) {
                    put(",");
                }
                var k = ks[i];
                put("\"");
                put(escape(k.toString()));
                put("\":");
                value(v[k] as Object or Null);
            }
            put("}");
        } else {
            put("null");   // nothing else is ever put in a batch
        }
    }

    //! Shortest fixed-point form with up to 4 decimals; NaN/inf are not JSON, so they become null.
    private function num(d as Double) as String {
        if (d != d || d > 1.0e15 || d < -1.0e15) {
            return "null";
        }
        var s = d.format("%.4f");
        if (s.find(".") == null) {
            return s;
        }
        var cs = s.toCharArray();
        var end = cs.size();
        while (end > 0 && cs[end - 1] == '0') {
            end--;
        }
        if (end > 0 && cs[end - 1] == '.') {
            end--;
        }
        var out = s.substring(0, end);
        if (out == null || out.length() == 0 || out.equals("-")) {
            return "0";
        }
        return out;
    }

    private function escape(s as String) as String {
        if (s.find("\"") == null && s.find("\\") == null && !hasControl(s)) {
            return s;
        }
        var cs = s.toCharArray();
        var out = "";
        for (var i = 0; i < cs.size(); i++) {
            var c = cs[i];
            var n = c.toNumber();
            if (c == '"') {
                out += "\\\"";
            } else if (c == '\\') {
                out += "\\\\";
            } else if (n < 0x20) {
                out += "\\u" + n.format("%04x");
            } else {
                out += c.toString();
            }
        }
        return out;
    }

    private function hasControl(s as String) as Boolean {
        var a = s.toCharArray();
        for (var i = 0; i < a.size(); i++) {
            if (a[i].toNumber() < 0x20) {
                return true;
            }
        }
        return false;
    }
}
