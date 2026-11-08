"""A test must not depend on the wall clock.

The incident this pins: the pricing tests were written with hardcoded October
2026 dates, and the reconciler measures a price's freshness in days-from-now.
The day the calendar passed `T1 + PRICE_FRESHNESS_DAYS`, twelve tests failed
for no reason connected to the code — a time bomb that fired on a date nobody
was watching.

The rule: a test may use a *historical* date (a fixture from 2024 is stable
forever), but it may not pin a date that is close enough to now that the code
under test will treat it as fresh, stale, or expiring. That window is
"the last 400 days and the next 365" — generous enough to let a fixture be
recent, tight enough that nothing expires unnoticed.

Comments and docstrings are not scanned: a date written in prose is a date
*about* the test, not a date the test depends on.
"""
from __future__ import annotations

import datetime as dt
import io
import pathlib
import re
import tokenize

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

#: `2026-10-01`, with an optional time part.
_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")

#: Files allowed to name a recent date, and why. Every entry is a judgement
#: call, written down so the next person can check it rather than guess.
ALLOWED: dict[str, str] = {
    "test_price_history.py":
        "the dates are the *history* being asserted — a provider's 2020 "
        "timestamp and an `as_of` in 2021 — plus one comparison against a "
        "past date that stays true as the calendar advances",
    "test_search.py":
        "a search result's own `date` field (payload content, not a clock "
        "reading): the assertion is about parsing, not about today",
    "test_complexity.py":
        "a date inside a *prompt* used to exercise the complexity estimator; "
        "the estimator matches patterns, not dates, so the string is inert",
}

#: How far back / forward a date may be and still count as "recent".
PAST_DAYS = 400
FUTURE_DAYS = 365


def _code_only(path: pathlib.Path) -> str:
    """The file's code, with comments and docstrings removed."""
    source = path.read_text(encoding="utf-8")
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    except tokenize.TokenError:
        return source
    kept: list[str] = []
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.line.strip().startswith(('"""', "'''")):
            continue
        kept.append(tok.string)
    return " ".join(kept)


def _recent_dates(text: str, today: dt.date) -> list[str]:
    now = dt.datetime.now(dt.timezone.utc).date()
    hits = []
    for match in _DATE.finditer(text):
        try:
            when = dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if (now - dt.timedelta(days=PAST_DAYS)) <= when <= (now + dt.timedelta(days=FUTURE_DAYS)):
            hits.append(match.group(0))
    return sorted(set(hits))


@pytest.mark.parametrize("path", sorted(p for p in TESTS.rglob("*.py")),
                         ids=lambda p: p.name)
def test_no_test_pins_a_date_that_will_expire(path):
    if path.name.startswith("test_no_wallclock"):
        return
    hits = _recent_dates(_code_only(path), dt.date.today())
    if not hits:
        return
    allowed = ALLOWED.get(path.name)
    assert allowed is not None, (
        f"{path.relative_to(ROOT)} pins the date(s) {', '.join(hits)}, which are "
        f"close enough to now that the code under test will treat them as "
        f"fresh, stale or expiring — and the test will fail once the calendar "
        f"moves on. Derive the date from `now` instead "
        f"(e.g. `dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)`). "
        f"If this date is deliberately historical, add the file to ALLOWED in "
        f"{path.name} with the reason.")
