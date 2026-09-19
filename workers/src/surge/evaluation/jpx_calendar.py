"""The Phase B job's trading calendar: weekends and the closed days JPX publishes (D-277).

Source: JPX, 営業時間・休業日一覧
(https://www.jpx.co.jp/corporate/about-jpx/calendar/index.html), the page as
updated 2026/02/06 and read 2026-09-19 (``SOURCE_SHA256`` is the page as
read). It lists 2026 and 2027. A date in any other year is not answered
(``CalendarUnknown``) rather than guessed, and JPX notes the list can change
with the holiday law, so ``diff_against_page`` compares a fresh read with it.

This decides one thing: whether the scheduled Phase B job treats a date as a
business day. On a closed day it sends nothing and ends normally. It is not a
horizon calendar - outcome windows still count the sessions that actually
traded (D-142) - and a calendar business day whose data shows no session
(fewer than 30% of the universe with a bar) is an anomaly the day stops on,
never a holiday the job assumes.
"""

from __future__ import annotations

import html
import re
from datetime import date

CALENDAR_VERSION = "jpx-closed-days-2026-02-06"
SOURCE_URL = "https://www.jpx.co.jp/corporate/about-jpx/calendar/index.html"
SOURCE_UPDATED = date(2026, 2, 6)
SOURCE_READ_AT = "2026-09-19T11:05:00+00:00"
SOURCE_SHA256 = "d6106b352ebdbecd922291c17933df6c10278634a4e69812a4746e37bf35559e"
#: JPX's 休業日一覧 as published, weekends included where JPX lists them.
CLOSED_DAYS: dict[date, str] = {
    date(2026, 1, 1): "元日",
    date(2026, 1, 2): "休業日",
    date(2026, 1, 3): "休業日",
    date(2026, 1, 12): "成人の日",
    date(2026, 2, 11): "建国記念の日",
    date(2026, 2, 23): "天皇誕生日",
    date(2026, 3, 20): "春分の日",
    date(2026, 4, 29): "昭和の日",
    date(2026, 5, 3): "憲法記念日",
    date(2026, 5, 4): "みどりの日",
    date(2026, 5, 5): "こどもの日",
    date(2026, 5, 6): "振替休日",
    date(2026, 7, 20): "海の日",
    date(2026, 8, 11): "山の日",
    date(2026, 9, 21): "敬老の日",
    date(2026, 9, 22): "休日※",
    date(2026, 9, 23): "秋分の日",
    date(2026, 10, 12): "スポーツの日",
    date(2026, 11, 3): "文化の日",
    date(2026, 11, 23): "勤労感謝の日",
    date(2026, 12, 31): "休業日",
    date(2027, 1, 1): "元日",
    date(2027, 1, 2): "休業日",
    date(2027, 1, 3): "休業日",
    date(2027, 1, 11): "成人の日",
    date(2027, 2, 11): "建国記念の日",
    date(2027, 2, 23): "天皇誕生日",
    date(2027, 3, 21): "春分の日",
    date(2027, 3, 22): "振替休日",
    date(2027, 4, 29): "昭和の日",
    date(2027, 5, 3): "憲法記念日",
    date(2027, 5, 4): "みどりの日",
    date(2027, 5, 5): "こどもの日",
    date(2027, 7, 19): "海の日",
    date(2027, 8, 11): "山の日",
    date(2027, 9, 20): "敬老の日",
    date(2027, 9, 23): "秋分の日",
    date(2027, 10, 11): "スポーツの日",
    date(2027, 11, 3): "文化の日",
    date(2027, 11, 23): "勤労感謝の日",
    date(2027, 12, 31): "休業日",
}
COVERED_YEARS = (2026, 2027)
_ROW = re.compile(r"^(\d{4})/(\d{2})/(\d{2})（.）$")


class CalendarUnknown(ValueError):
    """The date is outside the years JPX's list was read for."""


def closed_reason(day: date) -> str | None:
    """Why ``day`` is not a TSE business day ("weekend" or JPX's name for it), or None when it is one."""

    if day.year not in COVERED_YEARS:
        raise CalendarUnknown(f"{day.isoformat()}: the JPX calendar here covers {COVERED_YEARS} only "
                              f"({CALENDAR_VERSION}); read {SOURCE_URL} again")
    if day in CLOSED_DAYS:
        return CLOSED_DAYS[day]
    return "weekend" if day.weekday() >= 5 else None


def is_business_day(day: date) -> bool:
    return closed_reason(day) is None


def parse_closed_days(page: str) -> dict[date, str]:
    """The closed days in JPX's 休業日一覧 page: each ``YYYY/MM/DD（曜）`` cell and the name in the next cell.

    Read from the page's text rather than its markup: the published page has a
    stray quote in one row (2026/12/31) that a markup pattern would skip.
    """

    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S | re.I)
    lines = [line.strip() for line in html.unescape(re.sub(r"<[^>]*>", "\n", body)).splitlines() if line.strip()]
    closed = {}
    for i, line in enumerate(lines[:-1]):
        match = _ROW.match(line)
        if match:
            closed[date(int(match.group(1)), int(match.group(2)), int(match.group(3)))] = lines[i + 1]
    return closed


def diff_against_page(page: str) -> dict:
    """What a fresh read of JPX's page says that this calendar does not, for the years it covers."""

    read = {d: name for d, name in parse_closed_days(page).items() if d.year in COVERED_YEARS}
    return {
        "added_on_page": sorted(d.isoformat() for d in set(read) - set(CLOSED_DAYS)),
        "removed_from_page": sorted(d.isoformat() for d in set(CLOSED_DAYS) - set(read)),
        "renamed": sorted(d.isoformat() for d in set(read) & set(CLOSED_DAYS) if read[d] != CLOSED_DAYS[d]),
        "years_on_page": sorted({d.year for d in parse_closed_days(page)}),
    }


__all__ = ["CALENDAR_VERSION", "CLOSED_DAYS", "COVERED_YEARS", "SOURCE_SHA256", "SOURCE_UPDATED", "SOURCE_URL",
           "CalendarUnknown", "closed_reason", "diff_against_page", "is_business_day", "parse_closed_days"]
