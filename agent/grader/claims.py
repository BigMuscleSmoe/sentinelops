"""Pull checkable claims out of a Hypothesis, by rule.

Each sentence is cut into tokens (columns, numbers, times, window words), and
claims are built from their order: a column and the numbers after it, the times
that scope them, a "first" with its time, a "no X in W", a gap between two
times, and the identifiers and quotes that must appear somewhere the run read.
Anything with a number that no rule takes is counted as unparsed, so a vague
Hypothesis can't score better than a precise one.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

DASHES = "".join(map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212)))
ARROWS = "".join(map(chr, (0x2192, 0x2190, 0x2194, 0x21D2)))
QUOTES = {chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"'}


def normalize(text: str) -> str:
    for ch in DASHES:
        text = text.replace(ch, "-")
    for ch in ARROWS:
        text = text.replace(ch, "->")
    for ch, plain in QUOTES.items():
        text = text.replace(ch, plain)
    return text.replace(chr(0x2248), "about ").replace(chr(0x2264), "at most ").replace("~", "about ")


class Kind(StrEnum):
    COL = "col"
    NUM = "num"
    TIME = "time"
    WORD = "word"
    PUNCT = "punct"


# Columns, longest first. "metric" names a table header; "log" a field on a log
# line. A "weak" column only takes a number that sits right beside it.
@dataclass(frozen=True)
class Column:
    pattern: str
    source: str  # "metric" or "log"
    name: str
    weak: bool = False
    ambiguous: bool = False  # a log field when the sentence is about log lines


COLUMNS = [
    Column(r"pool_wait_time_ms|pool_wait_time", "log", "pool_wait_time_ms"),
    Column(r"pool_active_connections", "log", "pool_active_connections"),
    Column(r"concurrent_requests", "log", "concurrent_requests"),
    Column(r"upstream_latency_ms|upstream_latency", "log", "upstream_latency_ms"),
    Column(r"(?:payments(?:-api)? )?upstream(?: payments)? latency", "log", "upstream_latency_ms"),
    Column(r"payments(?:-api)? upstream", "log", "upstream_latency_ms"),
    Column(r"upstream_timeout_ms", "log", "upstream_timeout_ms"),
    Column(r"upstream_timeout", "log", "upstream_timeout"),
    Column(r"db_query_ms", "log", "db_query_ms"),
    Column(r"latency_ms", "log", "latency_ms"),
    Column(r"pool_max_size", "log", "pool_max_size"),
    Column(r"pool[_ ]wait[_ ]p99(?:_ms)?", "metric", "pool_wait_p99_ms"),
    Column(r"pool[_ ]waits?", "metric", "pool_wait_p99_ms", ambiguous=True),
    Column(r"pool[_ ]active[_ ]max|pool_active", "metric", "pool_active_max"),
    Column(r"concurrent_max|concurrency", "metric", "concurrent_max"),
    Column(r"db[_ ]query[_ ]p50(?:_ms)?|db_query", "metric", "db_query_p50_ms"),
    Column(r"payments(?:[_ -]api)?[_ ]p99(?:_ms)?", "metric", "payments_p99_ms"),
    Column(r"payments[_ ]timeouts|timeouts", "metric", "payments_timeouts"),
    Column(r"(?:latency )?p50(?:_ms)?(?: latency)?", "metric", "p50_ms"),
    Column(r"(?:latency )?p99(?:_ms)?(?: latency)?", "metric", "p99_ms"),
    Column(r"error rates?|err%|err\b", "metric", "err%"),
    Column(r"(?:alarm )?datapoints?", "metric", "err%"),
    Column(r"errors", "metric", "err%", weak=True),
    Column(r"req\b", "metric", "req"),
    Column(r"requests", "metric", "req", weak=True),
    Column(r"latency", "log", "latency_ms"),
]
_COL_RE = re.compile("|".join(f"(?P<c{i}>{c.pattern})" for i, c in enumerate(COLUMNS)), re.I)

TIME_TEXT = r"(?:\d{4}-\d\d-\d\dT)?\d\d:\d\d(?::\d\d(?:\.\d+)?)?Z?"
NUMBER_TEXT = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_TOKEN = re.compile(
    r"(?P<version>\b\d+\.\d+\.\d+\b)"
    r"|(?P<file>\b[\w/]+\.py(?::\d+)?)"
    rf"|(?P<trange>(?<![\d.]){TIME_TEXT}\*?\s*-\s*{TIME_TEXT}\*?)"
    rf"|(?P<time>(?<![\d.]){TIME_TEXT}\*?)"
    rf"|(?P<num>(?<![\w.]){NUMBER_TEXT}(?:\s*(?:-|to)\s*(?:{NUMBER_TEXT}))?\s*(?:%|ms\b|s\b|sec\b|seconds?\b|minutes?\b|min\b|hours?\b)?)"
    r"|(?P<word>[A-Za-z][\w%-]*)"
    r"|(?P<punct>[;:.,()=*])"
)
_NUM = re.compile(
    rf"(?P<a>{NUMBER_TEXT})(?:\s*(?:-|to)\s*(?P<b>{NUMBER_TEXT}))?\s*(?P<unit>%|ms|s|sec|seconds?|minutes?|min|hours?)?$"
)


@dataclass
class Token:
    kind: Kind
    text: str
    start: int
    column: Column | None = None
    lo: float = 0.0
    hi: float = 0.0
    lo_text: str = ""
    hi_text: str = ""
    unit: str = ""
    times: tuple[str, ...] = ()
    used: bool = False

    @property
    def is_range(self) -> bool:
        return self.hi_text != ""


def tokens(sentence: str) -> list[Token]:
    out: list[Token] = []
    pos = 0
    while pos < len(sentence):
        col = _COL_RE.match(sentence, pos)
        if col and (pos == 0 or not (sentence[pos - 1].isalnum() or sentence[pos - 1] == "_")) and (
            col.end() == len(sentence) or not (sentence[col.end()].isalnum() or sentence[col.end()] == "_")
        ) and not ("_" in col.group() and col.group().isupper()):
            index = int(next(k for k, v in col.groupdict().items() if v is not None)[1:])
            out.append(Token(Kind.COL, col.group(), pos, column=COLUMNS[index]))
            pos = col.end()
            continue
        m = _TOKEN.match(sentence, pos)
        if not m:
            pos += 1
            continue
        kind, text = m.lastgroup, m.group()
        if kind in ("version", "file"):
            out.append(Token(Kind.WORD, text, pos))
        elif kind == "trange":
            a, b = re.split(r"\s*-\s*(?=\d)", text.replace("*", ""), maxsplit=1)
            out.append(Token(Kind.TIME, text, pos, times=(a.rstrip("Z"), b.rstrip("Z"))))
        elif kind == "time":
            out.append(Token(Kind.TIME, text, pos, times=(text.rstrip("*").rstrip("Z"),)))
        elif kind == "num":
            n = _NUM.match(text.strip())
            if n:
                a, b = n["a"].replace(",", ""), (n["b"] or "").replace(",", "")
                out.append(
                    Token(
                        Kind.NUM, text.strip(), pos, lo=float(a), hi=float(b or a), lo_text=a, hi_text=b,
                        unit=_unit(n["unit"] or ""),
                    )
                )
        elif kind == "word":
            out.append(Token(Kind.WORD, text.lower(), pos))
        else:
            out.append(Token(Kind.PUNCT, text, pos))
        pos = m.end()
    for k, tok in enumerate(out):
        nxt = out[k + 1] if k + 1 < len(out) else None
        if tok.kind == Kind.NUM and tok.unit == "s" and tok.lo in (10, 60) and not tok.is_range and nxt is not None and (
            (nxt.kind == Kind.WORD and nxt.text in PERIOD_WORDS) or (nxt.kind == Kind.PUNCT and nxt.text in ":)")
        ):
            out[k] = Token(Kind.WORD, tok.text, tok.start)
    return out


PERIOD_WORDS = {"row", "rows", "period", "periods", "bucket", "buckets", "datapoint", "datapoints", "window",
                "windows", "data", "metrics", "table", "resolution"}


def _unit(text: str) -> str:
    if text.startswith(("sec", "s")):
        return "s"
    if text.startswith("min"):
        return "min"
    if text.startswith("hour"):
        return "h"
    return text


class Scope(StrEnum):
    ROWS = "rows"  # the rows starting at the given times
    SPAN = "span"  # from .. to, inclusive at the stated precision
    BEFORE = "before"  # the start of what was read, up to (not including) a time
    SINCE = "since"  # a time to the end of what was read
    WHOLE = "whole"  # everything the run read
    INCIDENT = "incident"  # the trailing rows with errors
    ANY = "any"  # unscoped: some stretch of what was read


@dataclass
class Window:
    scope: Scope
    times: tuple[str, ...] = ()
    hours_before: float = 0.0  # "for hours before T"
    except_times: tuple[str, ...] = ()


@dataclass
class ValueClaim:
    """A column's value, range, or bound over a window, from a metrics table or log lines."""

    where: str
    text: str
    column: Column
    lo: float
    hi: float
    lo_text: str
    hi_text: str
    unit: str
    mode: str  # "value", "range", "max", "at_most", "each" (one of a list)
    hedged: bool
    window: Window
    log_filter: dict[str, object] = field(default_factory=dict)


@dataclass
class FirstClaim:
    where: str
    text: str
    times: tuple[str, ...]
    version: str | None


@dataclass
class AbsenceClaim:
    where: str
    text: str
    what: str  # "errors", "deploys", "timeouts", or an error type
    keyword: str | None
    other: bool
    window: Window
    mentioned: tuple[str, ...]  # times named in the same item, left out by "other"


@dataclass
class GapClaim:
    where: str
    text: str
    lo: float
    hi: float
    precision: float
    hedged: bool
    direction: str  # "before", "after", "later", "earlier"
    subject: str  # a time
    target: tuple[str, ...]  # times, or ("first_error_row",)


@dataclass
class LiteralClaim:
    where: str
    text: str
    kind: str  # "identifier", "quote", "time"


@dataclass
class Unparsed:
    where: str
    text: str


Claim = ValueClaim | FirstClaim | AbsenceClaim | GapClaim | LiteralClaim

HEDGES = {"about", "around", "roughly", "approximately", "nearly", "almost", "approx"}
MAX_WORDS = {"rose", "peaked", "reached", "up", "max", "maximum", "high"}
RISE_VERBS = {"rose", "rises", "climbed", "peaked", "reached", "jumped", "spiked"}
AT_MOST = {"under", "below", "less", "most"}
WHOLE_WORDS = ("throughout", "across the window", "every row", "every minute", "every bucket", "every period",
               "every 60 s", "every 10 s", "whole window", "all along", "in every", "before and during",
               "the recent minutes")
STAY_WORDS = {"stays", "stayed", "stay", "remained", "remains", "held"}
INCIDENT_WORDS = ("during the incident", "error window", "failure window", "during the alarm", "during the errors")
LOG_CONTEXT = re.compile(
    r"\blog(?:s|ged)?\b|\blines?\b|\bsampled?\b|(?<![\d,.])(?:200|201|500)s?\b(?![.,]\d)|\bstatus\b|has_promo|\bsuccessful\b"
    r"|\bfailing (?:requests|lines|checkouts)\b|\bon the .*requests\b",
    re.I,
)
FAIL_WORDS = re.compile(r"\berrors?\b|\bfail(?:s|ed|ing|ures?)?\b|(?<![\d,.])500s?\b(?![.,]\d)|\bERROR\b|TypeError", re.I)
OK_WORDS = re.compile(r"\bsuccess(?:ful|es)?\b|\bsucceed(?:s|ed)?\b|(?<![\d,.])20[01]s?\b(?![.,]\d)", re.I)
VERSION = re.compile(r"\b\d+\.\d+\.\d+\b")
ROUTE = re.compile(r"\b(GET /products/\{id\}|POST /cart/items|POST /checkout)")


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", text.strip())
    return [p for p in parts if p]


def extract(where: str, text: str) -> tuple[list[Claim], list[Unparsed]]:
    """Every checkable claim in one statement (an evidence item, the root cause), and what had no rule."""
    text = normalize(text)
    claims: list[Claim] = []
    unparsed: list[Unparsed] = []
    claims += _literals(where, text)
    item_times = tuple(t for tok in tokens(text) if tok.kind == Kind.TIME for t in tok.times)
    item_filter = _log_filter(text)
    previous_times: tuple[str, ...] = ()
    so_far: list[str] = []
    for sentence in sentences(text):
        so_far.append(sentence)
        toks = tokens(sentence)
        claims += _firsts(where, sentence, toks, item_filter)
        claims += _gaps(where, sentence, toks, previous_times)
        claims += _absences(where, sentence, toks, item_times)
        claims += _values(where, sentence, toks, item_filter, " ".join(so_far))
        leftover = [t.text for t in toks if t.kind == Kind.NUM and not t.used and t.unit in ("%", "ms")]
        if leftover:
            unparsed.append(Unparsed(where, f"{sentence} [unbound: {', '.join(leftover)}]"))
        previous_times += tuple(x for t in toks if t.kind == Kind.TIME for x in t.times)
    return claims, unparsed


# --- literals -------------------------------------------------------------------

_UPPER = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_FILE = re.compile(r"\b[\w/]+\.py(?::\d+)?\b")
_COMMIT = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_DOUBLE = re.compile(r'"([^"]{6,}?)"')
_SINGLE = re.compile(r"(?<![\w])'((?:[^']|'(?=\w))+?)'(?![\w])")
_SECONDS = re.compile(rf"(?<![\d.])({TIME_TEXT})")


def _literals(where: str, text: str) -> list[LiteralClaim]:
    out = []
    for pattern in (_UPPER, _SNAKE, _FILE, _COMMIT, VERSION):
        for m in pattern.finditer(text):
            out.append(LiteralClaim(where, m.group(), "identifier"))
    for pattern in (_DOUBLE, _SINGLE):
        for m in pattern.finditer(text):
            quote = m.group(1).strip()
            if len(quote) >= 6 and " " in quote and not re.search(r"=|->", quote):
                out.append(LiteralClaim(where, quote, "quote"))
    for m in _SECONDS.finditer(text):
        if m.group(1).count(":") == 2:
            out.append(LiteralClaim(where, m.group(1).rstrip("Z"), "time"))
    seen, unique = set(), []
    for claim in out:
        if (claim.kind, claim.text) not in seen:
            seen.add((claim.kind, claim.text))
            unique.append(claim)
    return unique


# --- value claims ------------------------------------------------------------------

def _log_filter(text: str) -> dict[str, object]:
    found: dict[str, object] = {}
    versions = set(VERSION.findall(text))
    if len(versions) == 1:
        found["version"] = versions.pop()
    routes = set(ROUTE.findall(text))
    if len(routes) == 1:
        found["route"] = routes.pop()
    fail, ok = bool(FAIL_WORDS.search(text)), bool(OK_WORDS.search(text))
    if fail != ok:
        found["failed"] = fail
    return found


REFERENCE_BEFORE = {"crossed", "above", "over", "exceeds", "exceeded", "past", "beyond", "than", "threshold"}
SKIP_AFTER = {"threshold", "limit", "cap", "timeout", "budget", "client", "alarm", "cutoff"}
ERROR_SENTENCE = re.compile(r"\berr|\bfail|\balarm|\bdatapoints?\b|\b500s?\b|\bperiods?\b|\bbuckets?\b|\brows?\b", re.I)
FAST_FAIL = re.compile(
    r"\b(?:500s?|fails?|failing|failures?|errors?)\b[^.;]{0,40}?\bin (?P<q>about |under |less than |roughly |around )?"
    r"(?P<n>\d+(?:\.\d+)?) ?ms\b",
    re.I,
)


def _mark_references(toks: list[Token]) -> None:
    """Numbers that name a threshold or a count of periods, not an observation: "the 5% threshold", "2 of 3"."""
    for j, t in enumerate(toks):
        if t.kind != Kind.NUM:
            continue
        prev = toks[j - 1].text if j > 0 and toks[j - 1].kind == Kind.WORD else ""
        if prev in REFERENCE_BEFORE or _next_words(toks, j, 2) & SKIP_AFTER:
            t.used = True
        elif (
            not t.unit and not t.is_range and j + 2 < len(toks) and toks[j + 1].kind == Kind.WORD
            and toks[j + 1].text == "of" and toks[j + 2].kind == Kind.NUM and not toks[j + 2].unit
        ):
            t.used = True


def _values(where: str, sentence: str, toks: list[Token], item_filter: dict, item_text: str) -> list[ValueClaim]:
    out: list[ValueClaim] = []
    clauses = _clauses(toks)
    item_span = _item_span(item_text)
    _mark_references(toks)
    for i, tok in enumerate(toks):
        if tok.kind != Kind.COL:
            continue
        column = tok.column
        assert column is not None
        clause_text = _clause_text(sentence, toks, clauses, i)
        if column.ambiguous and LOG_CONTEXT.search(clause_text):
            column = Column(column.pattern, "log", "pool_wait_time_ms")
        nums: list[Token] = [] if column.weak else _numbers_after(toks, i, column, clauses)
        before = False
        if not nums and (column.weak or column.name in ("err%", "req", "payments_timeouts")):
            b = _number_before(toks, i, column)
            if b is not None:
                nums, before = [b], True
        if nums:
            out += _claims_for(where, sentence, toks, clauses, i, tok.text, column, nums, before, item_filter,
                               clause_text, item_span)
    err = next(c for c in COLUMNS if c.name == "err%" and not c.weak)
    if ERROR_SENTENCE.search(sentence):
        j = 0
        while j < len(toks):
            t = toks[j]
            if t.kind == Kind.NUM and not t.used and t.unit == "%" and t.hi <= 100:
                group, k = [t], j + 1
                while k + 1 < len(toks) and toks[k].text in (",", "and") and toks[k + 1].kind == Kind.NUM and (
                        not toks[k + 1].used and toks[k + 1].unit == "%" and clauses[k + 1] == clauses[j]):
                    group.append(toks[k + 1])
                    k += 2
                    if k < len(toks) and toks[k].text == "and" and toks[k - 1].text == ",":
                        k -= 0
                out += _claims_for(where, sentence, toks, clauses, j, "err%", err, group, True, item_filter,
                                   _clause_text(sentence, toks, clauses, j), item_span)
                j = k
                continue
            j += 1
    for m in FAST_FAIL.finditer(sentence):
        num = next((t for t in toks if t.kind == Kind.NUM and t.start == m.start("n")), None)
        if num is None or num.used:
            continue
        num.used = True
        q = (m["q"] or "").strip()
        mode = "at_most" if q in ("under", "less than") else "value"
        flt = {**item_filter, "failed": True, "failed_from": "clause"}
        if item_span is not None:
            flt["window"] = item_span
        latency = next(c for c in COLUMNS if c.name == "latency_ms" and c.pattern == "latency")
        out.append(ValueClaim(where, m.group(), latency, num.lo, num.hi, num.lo_text, num.hi_text, "ms", mode,
                              q in ("about", "roughly", "around"), Window(Scope.ANY), flt))
    return out


def _claims_for(where: str, sentence: str, toks: list[Token], clauses: list[int], i: int, label: str, column: Column,
                nums: list[Token], before: bool, item_filter: dict, clause_text: str,
                item_span: "Window | None") -> list[ValueClaim]:
    out = []
    first_j, last_j = toks.index(nums[0]), toks.index(nums[-1])
    own: dict[int, Window | None] = {}
    for n in nums:
        j = toks.index(n)
        own[id(n)] = _time_phrase(toks, (i if before else j) + 1, clauses) or (
            _parenthetical(toks, j, clauses) if len(nums) > 1 else None)
    transition = set()
    for n in nums:
        j = toks.index(n)
        if j > 0 and toks[j - 1].kind == Kind.WORD and toks[j - 1].text == "from" and _next_words(toks, j, 1) & {"to"}:
            transition |= {id(n)} | {id(t) for t in toks[j + 1 : j + 4] if t.kind == Kind.NUM}
    shared = None
    if not all(own.values()):
        shared = _window_for(toks, first_j if before else i, first_j, i if before else last_j, clauses,
                             clause_text, sentence, column.source)
    if shared and shared.scope == Scope.ROWS and len(shared.times) == len(nums) > 1 and not any(own.values()):
        # "20:00:30 and 20:00:40 had 26.1% and 15.2%": respectively
        own = {id(n): Window(Scope.ROWS, (x,)) for n, x in zip(nums, shared.times)}
    for num in nums:
        num.used = True
        j = toks.index(num)
        words = _words_between(toks, min(i, j), max(i, j)) | _words_between(toks, j - 3, j)
        hedged = bool(words & HEDGES) or _hedged_before(toks, j)
        after = _next_words(toks, j, 2)
        if words & AT_MOST or {"or", "less"} <= after or {"or", "below"} <= after:
            mode = "at_most"
        elif id(num) in transition:
            mode = "each"
        elif not num.is_range and (words & MAX_WORDS or (
                "to" in words and set(re.findall(r"[a-z]+", clause_text.lower())) & RISE_VERBS)):
            mode = "max"
        elif num.is_range:
            mode = "range"
        elif len(nums) > 1 and not own[id(num)]:
            mode = "each"
        else:
            mode = "value"
        window = Window(Scope.ANY) if id(num) in transition else (own[id(num)] or shared or Window(Scope.ANY))
        excepted = _exception_after(toks, j, clauses)
        if excepted and window.scope in (Scope.SPAN, Scope.SINCE, Scope.BEFORE, Scope.WHOLE):
            window = Window(window.scope, window.times, window.hours_before, excepted)
        flt: dict[str, object] = {}
        if column.source == "log":
            clause_f, sentence_f = _log_filter(clause_text), _log_filter(sentence)
            flt = {**item_filter, **sentence_f, **clause_f}
            flt["failed_from"] = "clause" if "failed" in clause_f else ("item" if "failed" in flt else None)
            if window.scope in (Scope.SPAN, Scope.ROWS, Scope.BEFORE, Scope.SINCE):
                flt["window"] = window
            elif item_span is not None:
                flt["window"] = item_span
        text = f"{label} {num.text}" + (f" ({_window_text(window)})" if window.scope != Scope.ANY else "")
        out.append(ValueClaim(where, text, column, num.lo, num.hi, num.lo_text, num.hi_text, num.unit, mode, hedged,
                              window, flt))
    return out


EXCEPT_WORDS = {"except", "apart", "besides", "excluding", "other"}


def _exception_after(toks: list[Token], j: int, clauses: list[int]) -> tuple[str, ...]:
    """The times a value's own exception names, before the next value: ("18:59-19:02",) or ("19:05",)."""
    for k in range(j + 1, min(len(toks), j + 6)):
        t = toks[k]
        if clauses[k] != clauses[j] or t.kind == Kind.NUM:
            return ()
        if t.kind == Kind.WORD and t.text in EXCEPT_WORDS:
            if t.text == "other" and not (k + 1 < len(toks) and toks[k + 1].text == "than"):
                continue
            for m in range(k + 1, min(len(toks), k + 6)):
                if toks[m].kind == Kind.TIME:
                    w = _times_from(toks, m)
                    return tuple("-".join(w.times) if w.scope == Scope.SPAN else x for x in
                                 ([w.times] if w.scope == Scope.SPAN else w.times))
                if toks[m].kind in (Kind.NUM, Kind.COL):
                    return ()
            return ()
    return ()


def _times_from(toks: list[Token], m: int) -> Window:
    """The times that start at token m: "18:59-19:02", "18:59 to 19:02", "19:05"."""
    t = toks[m]
    if len(t.times) == 2:
        return Window(Scope.SPAN, t.times)
    if m + 2 < len(toks) and toks[m + 2].kind == Kind.TIME and toks[m + 1].kind == Kind.WORD and toks[m + 1].text in (
            "to", "through", "until", "-"):
        return Window(Scope.SPAN, (t.times[0], toks[m + 2].times[0]))
    return Window(Scope.ROWS, t.times)


def _is_exception(toks: list[Token], k: int) -> bool:
    """Whether the time at k is named as an exception: "apart from the new 19:05 errors"."""
    for back in range(max(0, k - 4), k):
        if toks[back].kind == Kind.WORD and toks[back].text in EXCEPT_WORDS - {"other"}:
            return True
        if toks[back].kind == Kind.WORD and toks[back].text == "other" and back + 1 < k and toks[back + 1].text == "than":
            return True
    return False


CLAUSE_JOINS = {"and", "while", "but", "so", "which", "with", "whereas"}


def _clauses(toks: list[Token]) -> list[int]:
    """A clause number for each token: ";" and "." end one, and so does a comma before "and", "with", or a column."""
    out, n = [], 0
    for k, t in enumerate(toks):
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        if t.kind == Kind.PUNCT and t.text in ";.":
            n += 1
        elif t.kind == Kind.PUNCT and t.text == "," and nxt is not None and (
            (nxt.kind == Kind.WORD and nxt.text in CLAUSE_JOINS) or nxt.kind == Kind.COL
        ):
            n += 1
        out.append(n)
    return out


def _clause_text(sentence: str, toks: list[Token], clauses: list[int], i: int) -> str:
    """The words of token i's clause, from its last colon before i: what the claim's filters come from."""
    ks = [k for k in range(len(toks)) if clauses[k] == clauses[i]]
    first = ks[0]
    for k in ks:
        if k < i and toks[k].kind == Kind.PUNCT and toks[k].text == ":":
            first = k + 1
    end = toks[ks[-1] + 1].start if ks[-1] + 1 < len(toks) else len(sentence)
    return sentence[toks[first].start if first < len(toks) else 0 : end]


def _numbers_after(toks: list[Token], i: int, column: Column, clauses: list[int]) -> list[Token]:
    """The numbers a column takes: the first after it, and a list joined by commas, "and", or "then"."""
    found: list[Token] = []
    words = 0
    for j in range(i + 1, len(toks)):
        t = toks[j]
        if clauses[j] != clauses[i] or t.kind == Kind.COL:
            break
        if t.kind == Kind.NUM:
            if t.used:
                if found:
                    break
                continue
            nxt = toks[j + 1] if j + 1 < len(toks) else None
            if nxt is not None and nxt.kind == Kind.COL:
                break  # "0 payments_timeouts": the number is the next column's
            if not _unit_fits(column, t):
                if found:
                    break
                continue
            found.append(t)
            words = 0
            continue
        if t.kind == Kind.WORD:
            if found and t.text in ("of", "against", "out", "per", "on", "for", "under", "which", "over"):
                break
            if found and t.text not in LIST_WORDS and not _is_time_word(t.text):
                break
            words += 1
            if not found and words > 5:
                break
    return found


LIST_WORDS = {"and", "then", "to", "up", "ms", "s", "the", "at", "in", "partial", "still", "filling", "or", "less"}


def _is_time_word(word: str) -> bool:
    return word in {"at", "from", "to", "through", "since", "before", "after", "in", "between", "minute", "minutes",
                    "bucket", "buckets", "row", "rows", "period", "periods", "partial", "filling", "still", "and",
                    "then", "onward", "every", "during", "window", "the"}


def _number_before(toks: list[Token], i: int, column: Column) -> Token | None:
    """A number right before a column: "10.1% errors", "276 requests", "on 23 requests", "0 timeouts"."""
    j = i - 1
    while j >= 0 and toks[j].kind == Kind.WORD and toks[j].text in ("of", "on", "over", "the", "payments", "no"):
        j -= 1
    if j >= 0 and toks[j].kind == Kind.NUM and not toks[j].used and _unit_fits(column, toks[j]):
        return toks[j]
    return None


def _unit_fits(column: Column, num: Token) -> bool:
    if column.name == "err%":
        return num.hi <= 100 and (num.unit == "%" or (num.unit == "" and "." in num.lo_text))
    if column.name in ("req", "payments_timeouts", "pool_active_max", "concurrent_max", "pool_active_connections",
                       "concurrent_requests", "pool_max_size", "upstream_timeout"):
        return num.unit == "" and float(num.lo).is_integer() and float(num.hi).is_integer()
    return num.unit in ("ms", "")


def _words_between(toks: list[Token], i: int, j: int) -> set[str]:
    return {t.text for t in toks[max(0, i + 1) : max(0, j)] if t.kind == Kind.WORD}


def _hedged_before(toks: list[Token], j: int) -> bool:
    return j > 0 and toks[j - 1].kind == Kind.WORD and toks[j - 1].text in HEDGES


def _next_words(toks: list[Token], j: int, n: int) -> set[str]:
    return {t.text for t in toks[j + 1 : j + 1 + n] if t.kind == Kind.WORD}


SCOPE_START = {"at", "in", "from", "between", "through", "since", "before", "after", "during", "for", "on", "over",
               "throughout", "across", "until", "till"}


def _time_phrase(toks: list[Token], start: int, clauses: list[int], stop_at_value: bool = True) -> Window | None:
    """A time phrase that starts at `start` (after at most a few words), up to the next value or the clause's end."""
    if start >= len(toks):
        return None
    clause = clauses[start]
    words: list[str] = []
    times: list[str] = []
    ranged: tuple[str, ...] = ()
    hours = 0.0
    opened = False
    for k in range(start, len(toks)):
        t = toks[k]
        if clauses[k] != clause or t.kind == Kind.COL:
            break
        if t.kind == Kind.PUNCT:
            if t.text in ";.":
                break
            if t.text == "(" and k == start:
                opened = True
                continue
            if t.text == "," and k == start:
                return None
            if t.text == ")" or (t.text == "," and (times or ranged)):
                break
            continue
        if t.kind == Kind.NUM:
            if t.unit == "h" or (words and words[-1] == "for"):
                hours = t.lo
                continue
            if stop_at_value:
                break
            continue
        if t.kind == Kind.TIME:
            if len(t.times) == 2:
                ranged = t.times
            else:
                times.append(t.times[0])
            continue
        if not times and not ranged:
            if not words and not opened and t.text not in SCOPE_START and t.text != "the":
                return None
            if len(words) >= 4:
                return None
        elif t.text not in ("and", "to", "through", "the", "until", "-") and not _is_time_word(t.text):
            break
        words.append(t.text)
    if not times and not ranged:
        return None
    w = set(words)
    if "hours" in w and "before" in w and times:
        return Window(Scope.BEFORE, (times[-1],), hours_before=max(hours, 2.0))
    if ranged:
        return Window(Scope.SPAN, ranged, except_times=_except(words, times))
    if len(times) >= 2 and ({"from", "between"} & w or "to" in w or "until" in w):
        return Window(Scope.SPAN, (times[0], times[1]))
    if {"through", "until", "till"} & w:
        return Window(Scope.SPAN, ("start", times[0]))
    if {"since", "onward", "after"} & w:
        return Window(Scope.SINCE, (times[0],))
    if "before" in w:
        return Window(Scope.BEFORE, (times[0],))
    return Window(Scope.ROWS, tuple(times))


def _except(words: list[str], times: list[str]) -> tuple[str, ...]:
    return tuple(times) if {"except", "apart", "besides"} & set(words) else ()


def _window_for(toks: list[Token], anchor: int, first_j: int, last_j: int, clauses: list[int], clause_text: str,
                sentence: str, source: str) -> Window | None:
    """The window for values with no time phrase of their own, nearest first."""
    for found in (
        _time_phrase(toks, last_j + 1, clauses),
        _parenthetical(toks, first_j, clauses),
        _time_phrase_before(toks, anchor, clauses),
    ):
        if found:
            return found
    in_clause = _phrases(toks, clauses, clauses[anchor])
    if len({(p.scope, p.times) for p in in_clause}) == 1:
        return in_clause[0]
    if source == "log":
        return None
    lowered = clause_text.lower()
    if any(w in lowered for w in WHOLE_WORDS):
        return Window(Scope.WHOLE)
    if any(w in lowered for w in INCIDENT_WORDS):
        return Window(Scope.INCIDENT)
    if set(re.findall(r"[a-z]+", lowered)) & STAY_WORDS:
        return Window(Scope.WHOLE)
    if any(w in sentence.lower() for w in INCIDENT_WORDS):
        return Window(Scope.INCIDENT)
    return Window(Scope.ANY)


def _phrases(toks: list[Token], clauses: list[int], clause: int) -> list[Window]:
    """The time phrases in one clause."""
    out = []
    k = 0
    while k < len(toks):
        if clauses[k] == clause and toks[k].kind == Kind.TIME and _is_exception(toks, k):
            k += 1
            continue
        if clauses[k] == clause and toks[k].kind == Kind.TIME:
            back = k
            while back > 0 and clauses[back - 1] == clause and toks[back - 1].kind == Kind.WORD and k - back < 3:
                back -= 1
            w = _time_phrase(toks, back, clauses, stop_at_value=False) or _time_phrase(toks, k, clauses, stop_at_value=False)
            if w:
                out.append(w)
            k += 1
            while k < len(toks) and clauses[k] == clause and (toks[k].kind == Kind.TIME or (
                toks[k].kind == Kind.WORD and toks[k].text in ("and", "to", "through", "the"))):
                k += 1
            continue
        k += 1
    return out


def _parenthetical(toks: list[Token], j: int, clauses: list[int]) -> Window | None:
    """For a value inside "( ... )": the time phrase just before the "("."""
    k = j - 1
    while k >= 0:
        if toks[k].kind == Kind.PUNCT and toks[k].text in ");.":
            return None
        if toks[k].kind == Kind.PUNCT and toks[k].text == "(":
            for back in range(k - 1, max(-1, k - 7), -1):
                if toks[back].kind == Kind.TIME and clauses[back] == clauses[k]:
                    start = back
                    while start > 0 and toks[start - 1].kind == Kind.WORD and back - start < 2:
                        start -= 1
                    return _time_phrase(toks, start, clauses, stop_at_value=False) or _time_phrase(
                        toks, back, clauses, stop_at_value=False)
            return None
        k -= 1
    return None


def _time_phrase_before(toks: list[Token], anchor: int, clauses: list[int]) -> Window | None:
    """A time just before a column or value: "19:05:00* req=276", "Pool at 19:05: pool_active_max 4",
    "18:59 to 19:02 with error rates of", or a sentence's opening phrase: "Since 19:03, pool wait is"."""
    j = anchor - 1
    words = 0
    while j >= 0 and clauses[j] == clauses[anchor]:
        t = toks[j]
        if t.kind == Kind.TIME:
            return _times_ending_at(toks, j)
        if t.kind in (Kind.COL, Kind.NUM) or (t.kind == Kind.PUNCT and t.text in ",()"):
            break
        if t.kind == Kind.WORD:
            words += 1
            if words > 4:
                return None
        j -= 1
    return _opening_phrase(toks, anchor, clauses)


def _times_ending_at(toks: list[Token], j: int) -> Window:
    """The times that end at token j: "18:59 to 19:02" is a span, "20:00:30 and 20:00:40" two rows."""
    t = toks[j]
    if len(t.times) == 2:
        return Window(Scope.SPAN, t.times)
    if j >= 2 and toks[j - 2].kind == Kind.TIME and toks[j - 1].kind == Kind.WORD:
        joiner = toks[j - 1].text
        if joiner in ("to", "through", "until", "-"):
            return Window(Scope.SPAN, (toks[j - 2].times[-1], t.times[0]))
        if joiner == "and":
            before = toks[j - 3].text if j >= 3 and toks[j - 3].kind == Kind.WORD else ""
            if before == "between":
                return Window(Scope.SPAN, (toks[j - 2].times[-1], t.times[0]))
            return Window(Scope.ROWS, (toks[j - 2].times[-1], t.times[0]))
    return Window(Scope.ROWS, t.times)


def _opening_phrase(toks: list[Token], anchor: int, clauses: list[int]) -> Window | None:
    """A time phrase that opens the sentence, or the part after its colon, and ends in a comma."""
    first = 0
    for k in range(anchor):
        if toks[k].kind == Kind.PUNCT and toks[k].text == ":":
            first = k + 1
    if first >= anchor or toks[first].kind != Kind.WORD or toks[first].text not in SCOPE_START:
        return None
    for k in range(first, min(anchor, first + 8)):
        if toks[k].kind == Kind.PUNCT and toks[k].text == ",":
            return _time_phrase(toks, first, clauses, stop_at_value=False)
        if toks[k].kind in (Kind.COL, Kind.NUM):
            return None
    return None


def _item_span(text: str) -> Window | None:
    """An evidence item's one time span, which its log claims inherit: "(19:05:33-19:05:55)"."""
    toks = tokens(text)
    clauses = _clauses(toks)
    spans = []
    for k, t in enumerate(toks):
        if t.kind == Kind.TIME:
            back = k
            while back > 0 and toks[back - 1].kind == Kind.WORD and k - back < 3:
                back -= 1
            w = _time_phrase(toks, back, clauses, stop_at_value=False) or _time_phrase(toks, k, clauses, stop_at_value=False)
            if w and w.scope == Scope.SPAN and w.times[0] != "start":
                spans.append(w)
    unique = {s.times: s for s in spans}
    if len(unique) != 1:
        return None
    span = next(iter(unique.values()))
    named = {x for tok in toks if tok.kind == Kind.TIME for x in tok.times}
    return span if named <= set(span.times) else None


def _window_text(window: Window) -> str:
    return f"{window.scope} {' '.join(window.times)}".strip()


# --- first, gaps, absences -----------------------------------------------------------

_FIRST = re.compile(r"\bfirst\b", re.I)


def _firsts(where: str, sentence: str, toks: list[Token], item_filter: dict) -> list[FirstClaim]:
    out = []
    for m in _FIRST.finditer(sentence):
        following = [t for t in toks if t.start > m.start()]
        times: list[str] = []
        between: list[str] = []
        for t in following:
            if t.kind == Kind.TIME:
                times.extend(t.times)
                continue
            if times:
                if t.kind == Kind.WORD and t.text == "and":
                    continue
                break
            if t.kind == Kind.PUNCT and t.text in ";.,":
                break
            if t.kind == Kind.WORD:
                between.append(t.text)
                if len(between) > 8:
                    break
        if not times:
            continue
        what = " ".join(between)
        if "deploy" in what or "rollback" in what or not (FAIL_WORDS.search(what) or item_filter.get("failed") or not what):
            continue
        versions = VERSION.findall(what)
        out.append(FirstClaim(where, sentence[m.start() : m.start() + 80].split(".")[0], tuple(times),
                              versions[0] if versions else item_filter.get("version") if "new" not in what else None))
    return out


_GAP = re.compile(
    r"(?P<hedge>(?:about|around|roughly|approximately|nearly|almost|only|just)\s+)?"
    r"(?P<a>\d+(?:\.\d+)?)(?:\s*-\s*(?P<b>\d+(?:\.\d+)?))?\s*(?P<unit>ms|s|sec|secs|seconds?|minutes?|min)\s+"
    r"(?P<dir>before|after|later|earlier)\b(?!\s+and\b)",
    re.I,
)


def _gaps(where: str, sentence: str, toks: list[Token], previous: tuple[str, ...]) -> list[GapClaim]:
    out = []
    for m in _GAP.finditer(sentence):
        unit = m["unit"].lower()
        scale = 0.001 if unit == "ms" else 60.0 if unit.startswith("min") else 1.0
        a, b = float(m["a"]) * scale, float(m["b"] or m["a"]) * scale
        decimals = len(m["a"].split(".")[1]) if "." in m["a"] else 0
        precision = 10 ** -decimals * scale
        before = [x for t in toks if t.kind == Kind.TIME and t.start < m.start() for x in t.times]
        after = [x for t in toks if t.kind == Kind.TIME and t.start > m.end() for x in t.times]
        direction = m["dir"].lower()
        if direction in ("later", "earlier"):
            chain = list(previous) + before
            if len(chain) < 2:
                continue
            subject, target = chain[-2], (chain[-1],)
        else:
            chain = list(previous) + before
            if not chain:
                continue
            subject = chain[-1]
            rest = sentence[m.end() :].lower()
            if after:
                target = tuple(after)
            elif re.match(r"\s*the first (?:error|failing|failure)\w* (?:bucket|row|period|minute)", rest):
                target = ("first_error_row",)
            else:
                continue
        for t in toks:
            if t.kind == Kind.NUM and m.start() <= t.start < m.end():
                t.used = True
        out.append(GapClaim(where, m.group(), a, b, precision, bool(m["hedge"]), direction, subject, target))
    return out


_ABSENCE = re.compile(
    r"\bno (?P<other>other )?(?P<mod>[\w-]+ )?(?P<what>errors?|failures?|500s?|timeouts?|TypeErrors?"
    r"|deploy or config changes?|deploys?(?: or config changes?)?|config changes?)(?![\w-])"
    r"(?!\s+(?:signature|rate|type|types|change|changes|pattern|spike|count|budget|code|path|page|class)\b)"
    r"(?P<rest>[^;]*?)(?=[;]|\.\s|\.$|$)",
    re.I,
)


def _absences(where: str, sentence: str, toks: list[Token], item_times: tuple[str, ...]) -> list[AbsenceClaim]:
    out = []
    for m in _ABSENCE.finditer(sentence):
        rest = m["rest"].lower()
        if re.match(r"\s+(?:on|for|with|from|to) (?:a|an|the|any)\b", rest):
            continue  # a qualifier the grader can't evaluate
        what = m["what"].lower()
        kind = (
            "deploys" if "deploy" in what or "config" in what
            else "timeouts" if "timeout" in what
            else "TypeError" if "typeerror" in what
            else "errors"
        )
        mod = (m["mod"] or "").strip().lower()
        keyword = mod.split("-")[0] if mod and mod not in ("new", "such", "more", "real", "visible") else None
        window = _absence_window(sentence, toks, m)
        out.append(AbsenceClaim(where, m.group().strip()[:120], kind, keyword, bool(m["other"]), window, item_times))
    return out


def _absence_window(sentence: str, toks: list[Token], m: re.Match) -> Window:
    after = [t for t in toks if t.start >= m.end("what") and t.start < m.end("what") + 80]
    w = None
    if after:
        clauses = _clauses(after)
        for k in range(min(4, len(after))):
            w = _time_phrase(after, k, clauses, stop_at_value=False)
            if w:
                break
    if w:
        if w.scope == Scope.ROWS and len(w.times) == 1:
            # "in the 01:56 window": that minute, or that row
            return Window(Scope.SPAN, (w.times[0], w.times[0]))
        return w
    lowered = sentence.lower()
    if re.search(r"\b(?:that|this|the same) (?:period|window|time|span)\b", lowered[m.start():]) or re.search(
        r"\bcarr(?:y|ies) no\b", lowered):
        named = [x for t in toks if t.kind == Kind.TIME and t.start < m.start() for x in t.times]
        if named:
            return Window(Scope.SPAN, (named[0], named[-1]))
    return Window(Scope.WHOLE)
