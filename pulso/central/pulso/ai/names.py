"""Stable pseudonyms for colleagues' names ("Pessoa A", "Pessoa B", ...).

Applied wherever text with names leaves the machine: the facts the report sends to Claude (the
answer is mapped back locally, so your own report still reads naturally) and the Google Docs export
(unless `--com-nomes`). The people are every speaker/attendee the store knows; your own aliases
(PULSO_ME, the same list the tagger uses) stay as they are. Only a WHOLE name listed there is you:
a colleague who merely shares your first name ("Davi Costa" with PULSO_ME=davi) is a person like
any other, so list your full name as calendars and transcripts write it ("davi becam").

* One pass of one regex, longest name fragment first, so "Ana Lima" never turns into
  "Pessoa A Lima" through a first-name rule, and an alias is never rewritten again.
* Fragments are the full name and every shorter run of its words (first name, surname, "Ana Paula"),
  so a surname alone does not leak either. A fragment shared by several people ("Ana" for both Ana
  Souza and Ana Lima) becomes "Pessoa A/B": two people are never merged under one pseudonym.
  A surname alone must be capitalised to count ("Dias", "d'Ávila"; "Costa" or "Ramos" are also everyday
  words: "últimos 7 dias"), unless it is glued into an identifier such as a file stem ("1-1-souza", "sync_lima").
  Labels with a digit ("Speaker 1", "SPEAKER_00") only count whole: "speaker" is nobody.
* Spaces, '-', '_', '.' and ',' all separate words ("1-1-ana-souza"); case, accents and the kind of
  apostrophe (D'Ávila, D’Ávila) are ignored. A name does not end at an inner apostrophe ("Pessoa D" is
  not a name in "Pessoa D'Ávila"); a possessive "'s" ends it ("Ana's notes").
* The letters are kept in the store (artefact 'pseudonyms'), so Pessoa A stays Pessoa A across runs
  as new people show up; newcomers take the next letters in order of first appearance.
* A pseudonym already in the text is left alone, so T(T(x)) == T(x) even for a colleague whose
  surname is "Pessoa" (the export runs a last pass over finished documents). "Pessoa D'Ávila" is a
  name, not a pseudonym, and so is "Caio Pessoa D. Lima" (see _keep_pseudonyms).
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
# not glued to another letter or digit; on the right not across an inner apostrophe either ("Pessoa D" is
# not a word of "Pessoa D'Ávila"), except a clitic: "Ana's notes", "Ana'll", "Souza'd". On the left an
# apostrophe still ends a word: "dell'Ana" leaks nothing.
_EDGE_L = r"(?<![^\W_])"
_EDGE_R = r"(?![^\W_]|[" + _APOS + r"](?!(?i:s|d|m|ll|re|ve)(?![^\W_]))[^\W_])"
_PARTICLES = {"da", "de", "do", "das", "dos", "e", "di", "du", "del", "della", "der", "van", "von", "la", "le"}
# two-letter words that start sentences far more often than they name someone ("Zé", "Lu", "Sá", "Wu" do count)
_SHORT = {"eu", "em", "no", "na", "nos", "os", "as", "ao", "se", "me", "te", "ou", "um", "ja", "la", "so", "ha", "o", "a"}
_PSEUDO = re.compile(_EDGE_L + r"Pessoa [A-Z]\d*(?:/[A-Z]\d*)*" + _EDGE_R)   # what alias() writes, as written


def me_aliases() -> set[str]:
    """Your own names (lowercase): never pseudonymised; the tagger turns them into EU."""
    raw = os.environ.get("PULSO_ME", "eu,me,davi,gabi")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def _key(name: str) -> str:
    """Folded words joined by ' ': 'Davi  Bécam' -> 'davi becam'."""
    return " ".join(_fold(w) for w in _WORD.findall(unicodedata.normalize("NFC", name)))


def is_me(name: str, me: set[str] | None = None) -> bool:
    """`name` is exactly one of your aliases (case, accents and spacing aside). Never by first name
    alone: "Davi Costa" is a colleague even with PULSO_ME=davi."""
    me = me_aliases() if me is None else me
    k = _key(name or "")
    return bool(k) and k in {_key(a) for a in me}


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
        me = {_key(a) for a in (me_aliases() if me is None else me)}
        people: dict[str, list] = {}   # folded words joined by ' ' -> [first seen ms, display name, only from e-mail]

        def add(raw: Any, ms: int | None) -> None:
            if not isinstance(raw, str) or not raw.strip():
                return
            name, email = unicodedata.normalize("NFC", raw.strip()), False
            name = re.sub(r"(?i)^mailto:", "", name)   # Outlook/Exchange exports write 'MAILTO:'
            if "@" in name:  # an attendee without a display name: only "ana.souza@..." says who it is
                parts = re.split(r"[._+-]+", name.split("@")[0])
                if len(parts) < 2 or not all(p.isalpha() and len(p) >= 2 for p in parts):
                    return
                name, email = " ".join(p.capitalize() for p in parts), True
            words = [_fold(w) for w in _WORD.findall(name)]
            if not any(w.isalpha() for w in words):
                return
            key = " ".join(words)
            if key in me:   # same rule as the tagger's EU (is_me): the whole name, never the first name alone
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
            if len(toks) == 1 and (len(key) <= 1 or key in _SHORT):  # diarisation labels "A", "B": only as a whole label
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
                    if len(span) == 1 and (len(span[0]) <= 1 or span[0] in _SHORT or span[0] in me
                                           or any(c.isdigit() for c in span[0])):
                        continue
                    frags.setdefault(span, []).append(key)

        # single words that are only ever a surname/middle name: matched when capitalised ("Dias", not "dias")
        firsts = {k.split()[0] for k in people}
        # two-letter names likewise ("Zé", "Lu"): only capitalised, so "ze"/"lu" inside words never match
        self._cased = {s for s in frags if len(s) == 1 and (s[0] not in firsts or len(s[0]) <= 2)}

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

    def __call__(self, text: str, final: bool = False) -> str:
        """final: `text` is T's own output (summary._final); its pseudonyms stay, T(T(x), final=True) ==
        T(x). Raw fields never contain T's output, so they get no such exemption: a colleague named
        "Zé Pessoa" is not mistaken for a pseudonym there."""
        if not text:
            return text
        text = unicodedata.normalize("NFC", text)
        whole = " ".join(_fold(w) for w in _WORD.findall(text))
        if whole in self._whole and len(whole) == len(text.strip()):
            return self._whole[whole]
        if self._rx is None:
            return text
        folded = self._keep_pseudonyms(text, _fold(text)) if final else _fold(text)
        out, last = [], 0
        for m in self._rx.finditer(folded):
            span = tuple(_WORD.findall(m.group(0)))
            if not self._counts(text, m.start(), m.end(), span):
                continue
            out += [text[last : m.start()], self.alias[span]]
            last = m.end()
        return "".join(out) + text[last:]

    def _counts(self, text: str, i: int, j: int, span: tuple[str, ...]) -> bool:
        """The fragment `span`, found at text[i:j], stands for a person there: a word that is only ever
        a surname must be capitalised ("Dias", "d'Ávila"; not "dias") or glued into an identifier."""
        return span not in self._cased or _capitalised(text[i:j]) or _glued(text, i, j)

    def _keep_pseudonyms(self, text: str, folded: str) -> str:
        """Blank the pseudonyms already in `text` out of `folded`, so a colleague surnamed Pessoa does
        not turn "Pessoa A" into "Pessoa B A". The blank is as long and made of a letter no folded name
        has ('X'): the words around keep the edges they had next to the name ("d’Pessoa D" from
        "d’Souza"). Not a "Pessoa D" inside a name that also has a word T replaces on its own ("Caio
        Pessoa D. Lima"): T's own output never has one next to a pseudonym, so T(T(x)) == T(x)."""
        spans = [p.span() for p in _PSEUDO.finditer(text)]
        if not spans:
            return folded

        def name_word(w: re.Match) -> bool:   # outside every pseudonym, replaced wherever it stands
            return (not any(s <= w.start() < e for s, e in spans) and (w[0],) in self.alias
                    and self._counts(text, w.start(), w.end(), (w[0],)))

        in_names = set()
        for m in self._rx.finditer(folded):
            if any(map(name_word, _WORD.finditer(folded, m.start(), m.end()))):
                in_names |= {k for k, (s, e) in enumerate(spans) if m.start() < e and s < m.end()}
        out = list(folded)
        for k, (s, e) in enumerate(spans):
            if k not in in_names:
                out[s:e] = "X" * (e - s)
        return "".join(out)

    def reveal(self, x: Any) -> Any:
        """Pseudonyms back to names, through str / list / dict (Claude's answer, kept locally)."""
        if isinstance(x, str):
            return x if self._rx_back is None else self._rx_back.sub(lambda m: self.back[m.group(0)], x)
        if isinstance(x, list):
            return [self.reveal(v) for v in x]
        if isinstance(x, dict):
            return {k: self.reveal(v) for k, v in x.items()}
        return x


def _capitalised(word: str) -> bool:
    """'Dias', 'D'Ávila' and 'd'Ávila' are capitalised; 'dias' and 'd'ávila' are not."""
    return word[:1].isupper() or any(word[k - 1] in _APOS and word[k].isupper() for k in range(1, len(word)))


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
