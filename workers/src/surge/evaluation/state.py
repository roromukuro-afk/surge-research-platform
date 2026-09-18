"""The point-in-time state one evaluation request carries (``eval-state-1``).

The method in full - Canonical v5.1 and every addendum, not shortened
(CLAUDE.md 1-2) - and, for the security at S0, only what was available by the
data cutoff (the evening of S0): the last 20 sessions on S0's share count, the
S0 feature row and screening result, and TDnet disclosure titles published
before the cutoff. What code decides - the S0 close, the target, the 3,000 yen
test, the evaluation window - is given as fact and never asked.

The anonymized variant (D-270) differs only in identity: ticker, company and
issuer name, and strings that directly identify the company are replaced;
segment, sector, numbers, prices, volume, disclosure meaning, screening and the
method are kept.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from surge.analysis.jev_questions import GATEWAY_MODEL, questions, to_gateway
from surge.entry.eod_prediction import CHECKPOINT_SESSIONS
from surge.evaluation.population import Sample, Screen
from surge.providers.yahoo_finance import JST

STATE_VERSION = "eval-state-1"
BARS_IN_STATE = 20
DISCLOSURE_DAYS = 60
#: Only titles published at least this long before the cutoff are used: a
#: backfilled index row says when TDnet published, not when the system could
#: have seen it (CLAUDE.md 1-7), so a margin stands in for collection time.
DISCLOSURE_AVAILABILITY_MARGIN = timedelta(minutes=30)
REDACTED_CODE = "[CODE]"
REDACTED_NAME = "[COMPANY]"

#: Features that are measurements of the security, not its identity.
_FEATURE_EXCLUDE = {"market_code", "provider_id", "native_symbol", "trade_date", "feature_version", "series_basis"}


def data_cutoff(s0: date) -> datetime:
    return datetime(s0.year, s0.month, s0.day, tzinfo=JST) + timedelta(days=1)


def question_schema_hash() -> str:
    return hashlib.sha256(json.dumps(questions(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _num(value) -> str | float | int | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        # Plain notation: normalize() alone turns 3000 into "3E+3".
        return format(value.normalize(), "f")
    return round(value, 6) if isinstance(value, float) else value


def _plain(value):
    """Route evidence as JSON values: numbers as ``_num`` gives them, dates as ISO text."""

    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return _num(value)


def build_state(sample: Sample, screened: Screen, disclosures: list[dict], *, canonical: str, addenda: list[str],
                anonymized: bool = False) -> dict:
    series = screened.series
    bars = [
        {"date": b.trade_date.isoformat(), "open": _num(b.open), "high": _num(b.high), "low": _num(b.low),
         "close": _num(b.close), "volume": _num(b.volume)}
        for b in series.bars[-BARS_IN_STATE:]
    ]
    features = {k: _num(v) for k, v in asdict(screened.features).items() if k not in _FEATURE_EXCLUDE}
    cutoff = data_cutoff(sample.s0)
    target = (sample.s0_close_as_traded * Decimal("1.20")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    security = {"market": "JP", "code": sample.code, "name": sample.name, "segment": sample.segment,
                "sector33": sample.sector33, "size_category": sample.size_category}
    titles = [{"published_at": d["published_at"], "title": d["title"]} for d in disclosures]
    if anonymized:
        security.update({"code": REDACTED_CODE, "name": REDACTED_NAME})
        titles = [{**t, "title": redact(t["title"], sample)} for t in titles]
    return {
        "method": {"canonical_v5_1": canonical, "addenda_newer_overrides_older": list(addenda)},
        "security": security,
        "s0": {"session_date": sample.s0.isoformat(), "confirmed_close_jpy": _num(sample.s0_close_as_traded),
               "close_basis": "AS_TRADED"},
        "given_by_code_not_to_be_judged": {
            "target_price_jpy": _num(target),
            "price_limit_3000_yen_passed": True,
            "evaluation_starts": "S1 (the next session)",
            "checkpoints": [f"T+{n}" for n in CHECKPOINT_SESSIONS],
            "deadline": "T+20",
        },
        "data_cutoff": cutoff.isoformat(),
        "daily_bars_up_to_s0": bars,
        "features_at_s0": features,
        "screening_at_s0": {"passed": screened.passed, "routes_fired": screened.routes,
                            "route_evidence": _plain(screened.route_evidence)},
        "tdnet_disclosure_titles_up_to_cutoff": titles,
    }


def name_variants(name: str) -> list[str]:
    """The name as JPX writes it, without spaces or corporate affixes, and in half-width form.

    JPX writes Latin letters full-width (ＸＹテック); a disclosure title may
    write the same company half-width (XYテック), so the NFKC form of each
    variant is a variant too.
    """

    stripped = name
    for affix in ("株式会社", "（株）", "(株)", "ホールディングス", "グループ"):
        stripped = stripped.replace(affix, "")
    variants = {name, name.replace(" ", "").replace("　", ""), stripped.strip()}
    variants |= {unicodedata.normalize("NFKC", v) for v in variants}
    return sorted((v for v in variants if len(v) >= 2), key=len, reverse=True)


def code_pattern(code: str) -> re.Pattern:
    """The code as a token of its own, in half- or full-width characters.

    A code is four characters that also occur inside numbers - an amount of
    13,015 million yen contains 1301 - so only a free-standing occurrence is
    the code.
    """

    full_width = "".join(chr(ord(c) + 0xFEE0) for c in code)
    return re.compile(rf"(?<![0-9A-Za-z０-９Ａ-Ｚａ-ｚ])(?:{re.escape(code)}|{re.escape(full_width)})"
                      rf"(?![0-9A-Za-z０-９Ａ-Ｚａ-ｚ])")


def redact(text: str, sample: Sample) -> str:
    for variant in name_variants(sample.name):
        text = text.replace(variant, REDACTED_NAME)
    return code_pattern(sample.code).sub(REDACTED_CODE, text)


def request_body(state: dict) -> dict:
    """What the Gateway runner receives: model, state, the typed questions."""

    return {"model": GATEWAY_MODEL, "state": state, "questions": to_gateway(questions())}


def select_disclosures(items: list, s0: date) -> list[dict]:
    """Titles in the 60 days before S0 published at least the margin before the cutoff, oldest first."""

    cutoff = data_cutoff(s0)
    since = cutoff - timedelta(days=DISCLOSURE_DAYS)
    latest_allowed = cutoff - DISCLOSURE_AVAILABILITY_MARGIN
    return [
        {"published_at": item.pubdate.isoformat(), "title": item.title}
        for item in sorted(items, key=lambda it: it.pubdate)
        if since <= item.pubdate.astimezone(JST) <= latest_allowed
    ]


def disclosure_coverage(s0: date, *, returned: int, limit: int, oldest_returned: datetime | None) -> str:
    """Whether the index reached back over S0's whole window.

    The per-code index returns at most ``limit`` of the newest rows. Fewer than
    that is the whole history; a full page covers the window only if its oldest
    row is at or before the window's start.
    """

    since = data_cutoff(s0) - timedelta(days=DISCLOSURE_DAYS)
    if returned < limit or (oldest_returned is not None and oldest_returned.astimezone(JST) <= since):
        return "COMPLETE"
    return "INCOMPLETE"


__all__ = ["BARS_IN_STATE", "DISCLOSURE_AVAILABILITY_MARGIN", "REDACTED_CODE", "REDACTED_NAME", "STATE_VERSION",
           "build_state", "code_pattern", "data_cutoff", "disclosure_coverage", "name_variants",
           "question_schema_hash", "redact", "request_body", "select_disclosures"]
