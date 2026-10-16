"""Stable pseudonyms for colleagues' names ("Pessoa A", "Pessoa B", ...).

Applied wherever text with names leaves the machine: the facts the report sends to Claude (the
answer is mapped back locally, so your own report still reads naturally) and the Google Docs export
(unless `--com-nomes`). The people are every speaker/attendee the store knows; your own aliases
(PULSO_ME, the same list the tagger uses) stay as they are.

* One pass of one regex, longest name fragment first, so "Ana Lima" never turns into
  "Pessoa A Lima" through a first-name rule, and an alias is never rewritten again.
* Fragments are the full name and every shorter run of its words (first name, surname, "Ana Paula"),
  so a surname alone does not leak either. A fragment shared by several people ("Ana" for both Ana
  Souza and Ana Lima) becomes "Pessoa A/B": two people are never merged under one pseudonym.
  A surname alone must be capitalised to count ("Dias", "Costa" or "Ramos" are also everyday words:
  "últimos 7 dias"), unless it is glued into an identifier such as a file stem ("1-1-souza", "sync_lima").
  Labels with a digit ("Speaker 1", "SPEAKER_00") only count whole: "speaker" is nobody.
* Spaces, '-', '_', '.' and ',' all separate words ("1-1-ana-souza"); case, accents and the kind of
  apostrophe (D'Ávila, D’Ávila) are ignored.
* The letters are kept in the store (artefact 'pseudonyms'), so Pessoa A stays Pessoa A across runs
  as new people show up; newcomers take the next letters in order of first appearance.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from typing import Any

from pulso import store

_APOS = "'’ʼ‘´"                                     # calendars on macOS/iOS write D’Ávila
_WORD = re.compile(r"[^\W_](?:[^\W_]|[" + _APOS + r"](?=[^\W_]))*")   # letters/digits, inner apostrophes
_SEP = r"[\s._,-]+"
_GLUE = "-_."                                       # inside identifiers: file stems, e-mail local parts
_EDGE_L, _EDGE_R = r"(?<![^\W_])", r"(?![^\W_])"     # not glued to another letter or digit
_PARTICLES = {"da", "de", "do", "das", "dos", "e", "di", "du", "del", "della", "der", "van", "von", "la", "le"}


def me_aliases() -> set[str]:
    """Your own names (lowercase): never pseudonymised; the tagger turns them into EU."""
    raw = os.environ.get("PULSO_ME", "eu,me,davi,gabi")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def _fold(s: str) -> str:
    """Lowercase without accents, one char per char, so match offsets map back to the original."""
    out = []
    for c in s:
        if c in _APOS:
            out.append("'")
            continue
        b = unicodedata.normalize("NFKD", c)[:1] or c
        lo = b.lower()
        out.append(lo if len(lo) == 1 else b)
    return "".join(out)


def alias(i: int) -> str:
    return f"Pessoa {chr(ord('A') + i % 26)}{'' if i < 26 else i // 26}"


class Pseudonyms:
    """`T(text)` replaces names by pseudonyms; `T.reveal(x)` maps pseudonyms back (locally)."""

    def __init__(self, conn: sqlite3.Connection, me: set[str] | None = None):
        me = me_aliases() if me is None else me
        people: dict[str, list] = {}   # folded words joined by ' ' -> [first seen ms, display name, only from e-mail]

        def add(raw: Any, ms: int | None) -> None:
            if not isinstance(raw, str) or not raw.strip():
                return
            name, email = unicodedata.normalize("NFC", raw.strip()), False
            if "@" in name:  # an attendee without a display name: only "ana.souza@..." says who it is
                parts = re.split(r"[._+-]+", name.split("@")[0])
                if len(parts) < 2 or not all(p.isalpha() and len(p) >= 2 for p in parts):
                    return
                name, email = " ".join(p.capitalize() for p in parts), True
            words = [_fold(w) for w in _WORD.findall(name)]
            if not any(w.isalpha() for w in words):
                return
            key = " ".join(words)
            if key in me or words[0] in me:   # same rule as the tagger's EU
                return
            old = people.get(key)
            if old is None:
                people[key] = [ms or 0, name, email]
                return
            old[0] = min(old[0], ms or 0)
            if old[2] and not email:
                old[1], old[2] = name, False

        for r in conn.execute("SELECT start_ms, attrs FROM events WHERE kind IN ('meeting', 'calendar') ORDER BY start_ms, id"):
            a = json.loads(r["attrs"] or "{}")
            for k in ("speakers", "attendees"):
                v = a.get(k) or []
                for n in [v] if isinstance(v, str) else v:
                    add(n, r["start_ms"])
        for r in conn.execute("SELECT speaker, MIN(start_ms) AS ms FROM transcript_segments "
                              "WHERE speaker IS NOT NULL GROUP BY speaker ORDER BY ms, speaker"):
            add(r["speaker"], r["ms"])

        saved = store.get_artefact(conn, "pseudonyms", "people") or {}
        order: list[str] = list(saved.get("order") or [])
        known = set(order)
        new = sorted((k for k in people if k not in known), key=lambda k: (people[k][0], k))
        if new:
            order += new
            store.put_artefact(conn, "pseudonyms", "people", {"order": order})
        index = {k: i for i, k in enumerate(order)}

        # every fragment of every name -> the people it can refer to
        frags: dict[tuple[str, ...], list[str]] = {}
        for key, (_, _, email) in people.items():
            toks = tuple(key.split())
            if len(toks) == 1 and len(key) <= 2:  # diarisation labels "A", "B": only as a whole label, never "a" in a sentence
                continue
            frags.setdefault(toks, []).append(key)
            # "Time Eng" from time.eng@ must not turn every "time" into a person, nor "Speaker 1" every "speaker"
            if email or any(c.isdigit() for c in key):
                continue
            for i in range(len(toks)):
                for j in range(i + 1, len(toks) + 1):
                    span = toks[i:j]
                    if span == toks or span[0] in _PARTICLES or span[-1] in _PARTICLES:
                        continue
                    if len(span) == 1 and (len(span[0]) <= 2 or span[0] in me or any(c.isdigit() for c in span[0])):
                        continue
                    frags.setdefault(span, []).append(key)

        # single words that are only ever a surname/middle name: matched when capitalised ("Dias", not "dias")
        firsts = {k.split()[0] for k in people}
        self._cased = {s for s in frags if len(s) == 1 and s[0] not in firsts}

        self.alias: dict[tuple[str, ...], str] = {}
        self.back: dict[str, str] = {}
        for span, keys in frags.items():
            if " ".join(span) in people:   # a whole name is that person, even inside a longer one
                keys = [" ".join(span)]
            letters = sorted({alias(index[k]).removeprefix("Pessoa ") for k in keys}, key=lambda s: (len(s), s))
            a = "Pessoa " + "/".join(letters)
            self.alias[span] = a
            if a not in self.back:
                self.back[a] = _display(people[keys[0]][1], span)
        self._whole = {k: alias(index[k]) for k in people}                 # a label that is exactly one name
        self.map = {v[1]: alias(index[k]) for k, v in people.items()}      # display name -> alias

        alts = sorted(self.alias, key=lambda s: (-sum(len(w) + 1 for w in s), s))
        self._rx = (re.compile(_EDGE_L + "(?:" + "|".join(_SEP.join(map(re.escape, s)) for s in alts) + ")" + _EDGE_R)
                    if alts else None)
        backs = sorted(self.back, key=len, reverse=True)
        self._rx_back = re.compile("(?:" + "|".join(map(re.escape, backs)) + ")" + _EDGE_R) if backs else None

    def __call__(self, text: str) -> str:
        if not text:
            return text
        text = unicodedata.normalize("NFC", text)
        whole = " ".join(_fold(w) for w in _WORD.findall(text))
        if whole in self._whole and len(whole) == len(text.strip()):
            return self._whole[whole]
        if self._rx is None:
            return text
        out, last = [], 0
        for m in self._rx.finditer(_fold(text)):
            span = tuple(_WORD.findall(m.group(0)))
            if span in self._cased and not text[m.start()].isupper() and not _glued(text, m.start(), m.end()):
                continue
            out += [text[last : m.start()], self.alias[span]]
            last = m.end()
        return "".join(out) + text[last:]

    def reveal(self, x: Any) -> Any:
        """Pseudonyms back to names, through str / list / dict (Claude's answer, kept locally)."""
        if isinstance(x, str):
            return x if self._rx_back is None else self._rx_back.sub(lambda m: self.back[m.group(0)], x)
        if isinstance(x, list):
            return [self.reveal(v) for v in x]
        if isinstance(x, dict):
            return {k: self.reveal(v) for k, v in x.items()}
        return x


def _glued(text: str, i: int, j: int) -> bool:
    """text[i:j] is a part of an identifier ("1-1-souza", "sync_lima.vtt"), not a word in a sentence:
    a '-', '_' or '.' joins it to another letter or digit (a full stop ending a sentence does not)."""
    return ((i >= 2 and text[i - 1] in _GLUE and text[i - 2].isalnum())
            or (j + 1 < len(text) and text[j] in _GLUE and text[j + 1].isalnum()))


def _display(name: str, span: tuple[str, ...]) -> str:
    """The words of `name` that `span` (folded) stands for, as written: ('ana',) in 'Ána Souza' -> 'Ána'."""
    words = _WORD.findall(name)
    folded = [_fold(w) for w in words]
    for i in range(len(folded) - len(span) + 1):
        if tuple(folded[i : i + len(span)]) == span:
            return " ".join(words[i : i + len(span)])
    return " ".join(span)
