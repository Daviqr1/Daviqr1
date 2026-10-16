import Toybox.Application;
import Toybox.Lang;

//! Persistent FIFO of sealed batches in Application.Storage. Each batch lives under its own
//! key ("b<q>", well below the 32 KB per-value limit); one index record ("qx") keeps order,
//! size estimates, the drop counter and the sequence number q, so q is persisted in the same
//! write that makes a batch sendable and can never be reused for a different batch (the
//! central deduplicates on (d, q)). Total is bounded: the oldest batches are dropped (and
//! counted) first.
class UploadQueue {
    const MAX_BYTES = 81920;

    private var _ids as Array<Number>;
    private var _sizes as Array<Number>;

    var bytes as Number = 0;
    var seq as Number = 0;            // last q handed out (persisted)
    var drops as Number = 0;          // lifetime drops (persisted)
    var pendingDrops as Number = 0;   // drops not yet reported to the central

    function initialize() {
        _ids = [] as Array<Number>;
        _sizes = [] as Array<Number>;
        try {
            var x = Storage.getValue("qx");
            if (x instanceof Dictionary) {
                var i = x.get("i");
                var s = x.get("s");
                if (i instanceof Array && s instanceof Array && i.size() == s.size()) {
                    _ids = i as Array<Number>;
                    _sizes = s as Array<Number>;
                }
                var d = x.get("d");
                if (d instanceof Number) {
                    drops = d;
                }
                var q = x.get("q");
                if (q instanceof Number) {
                    seq = q;
                }
            }
        } catch (e) {
        }
        bytes = 0;
        for (var k = 0; k < _sizes.size(); k++) {
            bytes += _sizes[k];
        }
    }

    //! Next sequence number; persisted by the push() that stores the batch.
    function nextSeq() as Number {
        seq++;
        return seq;
    }

    function count() as Number {
        return _ids.size();
    }

    function peekId() as Number or Null {
        return (_ids.size() > 0) ? _ids[0] : null;
    }

    //! Store a sealed batch. Returns false if it could not be stored (counted as a drop).
    function push(q as Number, batch as Dictionary<String, Object>, size as Number) as Boolean {
        while (_ids.size() > 0 && bytes + size > MAX_BYTES) {
            dropOldest();
        }
        var ok = false;
        for (var attempt = 0; attempt < 4 && !ok; attempt++) {
            try {
                Storage.setValue("b" + q.toString(), batch as Storage.ValueType);
                ok = true;
            } catch (e) {
                // StorageFullException: make room and retry
                if (_ids.size() == 0) {
                    break;
                }
                dropOldest();
            }
        }
        if (!ok) {
            drops++;
            pendingDrops++;
            saveIndex();
            return false;
        }
        _ids.add(q);
        _sizes.add(size);
        bytes += size;
        saveIndex();
        return true;
    }

    function load(q as Number) as Dictionary<String, Object> or Null {
        try {
            var v = Storage.getValue("b" + q.toString());
            if (v instanceof Dictionary) {
                return v as Dictionary<String, Object>;
            }
        } catch (e) {
        }
        return null;
    }

    function remove(q as Number) as Void {
        var i = _ids.indexOf(q);
        if (i < 0) {
            return;
        }
        try {
            Storage.deleteValue("b" + q.toString());
        } catch (e) {
        }
        bytes -= _sizes[i];
        if (bytes < 0) {
            bytes = 0;
        }
        _ids = cut(_ids, i);
        _sizes = cut(_sizes, i);
        saveIndex();
    }

    //! Remove a batch the central will never accept (or that vanished); counted as a drop.
    function reject(q as Number) as Void {
        remove(q);
        drops++;
        pendingDrops++;
        saveIndex();
    }

    private function dropOldest() as Void {
        var q = _ids[0];
        try {
            Storage.deleteValue("b" + q.toString());
        } catch (e) {
        }
        bytes -= _sizes[0];
        _ids = cut(_ids, 0);
        _sizes = cut(_sizes, 0);
        drops++;
        pendingDrops++;
    }

    private function cut(a as Array<Number>, i as Number) as Array<Number> {
        var out = a.slice(0, i) as Array<Number>;
        out.addAll(a.slice(i + 1, null));
        return out;
    }

    private function saveIndex() as Void {
        try {
            var x = {"i" => _ids, "s" => _sizes, "d" => drops, "q" => seq} as Dictionary<String, Array<Number> or Number>;
            Storage.setValue("qx", x as Storage.ValueType);
        } catch (e) {
        }
    }
}
