import Toybox.Lang;
import Toybox.StringUtil;

//! Minimal JSON encoder for protocol-v1 batches (Dictionary/Array/Number/Long/Float/Double/
//! String/Boolean/null) straight into UTF-8 bytes. makeWebRequest serializes on its own; this is
//! only for the BLE route, where the batch travels as bytes. Output goes through a short String
//! buffer so the cost stays linear (Monkey C strings are immutable).
module Json {
    function utf8(s as String) as ByteArray {
        return StringUtil.convertEncodedString(s, {
            :fromRepresentation => StringUtil.REPRESENTATION_STRING_PLAIN_TEXT,
            :toRepresentation => StringUtil.REPRESENTATION_BYTE_ARRAY
        }) as ByteArray;
    }
}

//! Resumable encoder: step(n) writes at most n values and returns true when the document is
//! complete. A 4-6 KB batch has ~1500 values; the non-Music FR255 has half the watchdog budget
//! of the other 5.x watches, so the BLE transport spreads the work over a few ticks.
class JsonJob {
    const MAX_DEPTH = 8;

    private var _w as JsonWriter;
    private var _c as Array<Array or Dictionary or Null>;   // open containers
    private var _k as Array<Array or Null>;                 // their keys (dictionaries)
    private var _i as Array<Number>;                        // next index in each
    private var _depth as Number = 0;

    function initialize(root as Object or Null) {
        _w = new JsonWriter();
        _c = new [MAX_DEPTH] as Array<Array or Dictionary or Null>;
        _k = new [MAX_DEPTH] as Array<Array or Null>;
        _i = new [MAX_DEPTH] as Array<Number>;
        open(root);
    }

    function bytes() as ByteArray {
        return _w.bytes();
    }

    function step(budget as Number) as Boolean {
        while (budget > 0 && _depth > 0) {
            var d = _depth - 1;
            var c = _c[d];
            var keys = _k[d];
            var i = _i[d];
            var n = (keys != null) ? keys.size() : (c as Array).size();
            if (i >= n) {
                _w.raw((keys != null) ? "}" : "]");
                _c[d] = null;
                _k[d] = null;
                _depth = d;
                continue;
            }
            if (i > 0) {
                _w.raw(",");
            }
            var v;
            if (keys != null) {
                var key = keys[i] as Object;
                _w.str(key.toString());
                _w.raw(":");
                v = (c as Dictionary)[key];
            } else {
                v = (c as Array)[i];
            }
            _i[d] = i + 1;
            open(v as Object or Null);
            budget--;
        }
        return _depth == 0;
    }

    private function open(v as Object or Null) as Void {
        if ((v instanceof Array || v instanceof Dictionary) && _depth >= MAX_DEPTH) {
            _w.raw("null");   // never happens with batches (depth <= 4)
        } else if (v instanceof Array) {
            _w.raw("[");
            push(v, null);
        } else if (v instanceof Dictionary) {
            _w.raw("{");
            push(v, v.keys() as Array);
        } else {
            _w.value(v);
        }
    }

    private function push(c as Array or Dictionary, keys as Array or Null) as Void {
        _c[_depth] = c;
        _k[_depth] = keys;
        _i[_depth] = 0;
        _depth++;
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

    function raw(s as String) as Void {
        put(s);
    }

    function str(s as String) as Void {
        put("\"");
        put(escape(s));
        put("\"");
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

    //! Scalars; containers are opened by JsonJob.
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
            str(v);
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
