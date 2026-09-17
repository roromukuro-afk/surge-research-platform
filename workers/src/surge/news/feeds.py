"""RSS and Atom parsing, with the stdlib only.

Most of the official sources this project is allowed to use publish RSS or Atom.
Adding a feed library would be a dependency for something the standard library
already does, so this parses the two formats directly.

Two things here are deliberate rather than incidental:

* **The entity guard.** ``xml.etree`` expands internal entities, which makes a
  hostile feed a denial of service (the "billion laughs" shape). These feeds come
  from governments and exchanges, so the risk is low - but "the source is
  trustworthy" is an assumption about the network as well as the publisher, and
  it costs one substring check to not depend on it.
* **Date precision.** A feed that gives ``2026-09-17`` and a feed that gives
  ``2026-09-17T08:30:00+09:00`` are not telling us the same thing. Collapsing the
  first to midnight would move a morning release nine hours earlier, so the
  precision travels with the value.
"""

from __future__ import annotations

import email.utils
import re
from datetime import UTC, date, datetime, timedelta, timezone
from xml.etree import ElementTree

from surge.news.models import FeedItem, TimePrecision

ATOM_NS = "http://www.w3.org/2005/Atom"
RSS10_NS = "http://purl.org/rss/1.0/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

# A feed larger than this is not a feed we want to parse in memory.
MAX_FEED_BYTES = 64 * 1024 * 1024

_DOCTYPE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLASH_DATE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")


class FeedError(ValueError):
    """The payload is not a feed we can parse, or is not one we should."""


def parse_datetime(value: str | None, *, assume_tz: timezone | None = None) -> tuple[datetime | None, TimePrecision]:
    """Parse a feed timestamp, reporting how much of it was actually given.

    ``assume_tz`` is for sources that publish local times with no offset - a
    Japanese ministry writing ``2026-09-17 08:30`` means JST, and guessing UTC
    would shift every item by nine hours. The caller has to say so explicitly;
    there is no default, because a wrong default here is invisible.
    """

    if value is None:
        return None, TimePrecision.UNKNOWN
    text = value.strip()
    if not text:
        return None, TimePrecision.UNKNOWN

    if _DATE_ONLY.match(text):
        parsed = date.fromisoformat(text)
        tz = assume_tz or UTC
        return datetime(parsed.year, parsed.month, parsed.day, tzinfo=tz), TimePrecision.DATE_ONLY

    slash = _SLASH_DATE.match(text)
    if slash:
        year, month, day = (int(part) for part in slash.groups())
        tz = assume_tz or UTC
        return datetime(year, month, day, tzinfo=tz), TimePrecision.DATE_ONLY

    # RFC 3339 / ISO 8601, which is what Atom uses.
    iso_candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed_dt = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed_dt = None
    if parsed_dt is not None:
        if parsed_dt.tzinfo is None:
            if assume_tz is None:
                # A local time with no offset and no instruction about which zone
                # it is in. Recording it as INFERRED-with-UTC would be a guess
                # dressed as data, so say we do not know.
                return parsed_dt.replace(tzinfo=UTC), TimePrecision.INFERRED
            parsed_dt = parsed_dt.replace(tzinfo=assume_tz)
        return parsed_dt.astimezone(UTC), TimePrecision.EXACT

    # RFC 822, which is what RSS 2.0 uses.
    try:
        rfc822 = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None, TimePrecision.UNKNOWN
    if rfc822.tzinfo is None:
        rfc822 = rfc822.replace(tzinfo=assume_tz or UTC)
    return rfc822.astimezone(UTC), TimePrecision.EXACT


def _guard(payload: bytes) -> None:
    if len(payload) > MAX_FEED_BYTES:
        raise FeedError(f"feed is {len(payload)} bytes, over the {MAX_FEED_BYTES} byte cap")
    # Only the leading chunk: a DOCTYPE has to be near the top to matter, and
    # scanning megabytes of legitimate content for it is wasted work.
    if _DOCTYPE.search(payload[:8192]):
        raise FeedError("feed declares a DOCTYPE or ENTITY; refusing to parse it")


def _text(element: ElementTree.Element | None) -> str | None:
    if element is None:
        return None
    text = "".join(element.itertext()).strip()
    return text or None


def _first(parent: ElementTree.Element, *paths: str) -> ElementTree.Element | None:
    for path in paths:
        found = parent.find(path)
        if found is not None:
            return found
    return None


def parse_feed(payload: bytes, *, assume_tz: timezone | None = None) -> list[FeedItem]:
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom into a common shape."""

    _guard(payload)
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise FeedError(f"not well-formed XML: {exc}") from exc

    tag = root.tag.split("}")[-1]
    if tag == "feed":
        return _parse_atom(root, assume_tz)
    if tag in ("rss", "RDF"):
        return _parse_rss(root, assume_tz)
    raise FeedError(f"unrecognised feed root element <{tag}>")


def _parse_atom(root: ElementTree.Element, assume_tz: timezone | None) -> list[FeedItem]:
    items: list[FeedItem] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        link = None
        for candidate in entry.findall(f"{{{ATOM_NS}}}link"):
            rel = candidate.get("rel", "alternate")
            if rel == "alternate":
                link = candidate.get("href")
                break
        if link is None:
            first_link = entry.find(f"{{{ATOM_NS}}}link")
            link = first_link.get("href") if first_link is not None else None

        # published is when it first appeared; updated is when it last changed.
        # Preferring published keeps an edited item anchored to its release.
        raw_time = _text(_first(entry, f"{{{ATOM_NS}}}published", f"{{{ATOM_NS}}}updated"))
        published_at, precision = parse_datetime(raw_time, assume_tz=assume_tz)

        identifier = _text(entry.find(f"{{{ATOM_NS}}}id")) or link
        if identifier is None:
            continue
        items.append(
            FeedItem(
                source_document_id=identifier,
                title=_text(entry.find(f"{{{ATOM_NS}}}title")),
                url=link,
                published_at=published_at,
                published_precision=precision,
                summary=_text(_first(entry, f"{{{ATOM_NS}}}summary", f"{{{ATOM_NS}}}content")),
                raw_fields={"updated": _text(entry.find(f"{{{ATOM_NS}}}updated")) or ""},
            )
        )
    return items


def _child(entry: ElementTree.Element, name: str) -> str | None:
    """Find a child by local name, whatever namespace it is in.

    RSS 2.0 leaves its elements unqualified; RSS 1.0 puts the same names in the
    RSS 1.0 namespace. Searching for the bare name finds the first and silently
    misses the second - which is how a feed that parses to zero items looks
    identical to a quiet day.
    """

    for candidate in (name, f"{{{RSS10_NS}}}{name}"):
        found = entry.find(candidate)
        if found is not None:
            return _text(found)
    return None


def _parse_rss(root: ElementTree.Element, assume_tz: timezone | None) -> list[FeedItem]:
    # RSS 2.0 nests items under <channel>; RSS 1.0 (RDF) puts them beside it.
    entries = [node for node in root.iter() if node.tag.split("}")[-1] == "item"]

    items: list[FeedItem] = []
    for entry in entries:
        link = _child(entry, "link")
        guid = _child(entry, "guid")
        raw_time = _child(entry, "pubDate") or _text(entry.find(f"{{{DC_NS}}}date"))
        published_at, precision = parse_datetime(raw_time, assume_tz=assume_tz)

        # RSS 1.0 identifies an item by rdf:about rather than by a guid element.
        identifier = guid or entry.get(f"{{{RDF_NS}}}about") or link
        if identifier is None:
            continue
        items.append(
            FeedItem(
                source_document_id=identifier,
                title=_child(entry, "title"),
                url=link,
                published_at=published_at,
                published_precision=precision,
                summary=_child(entry, "description") or _text(entry.find(f"{{{CONTENT_NS}}}encoded")),
                raw_fields={"guid": guid or ""},
            )
        )
    return items


JST = timezone(timedelta(hours=9))
"""Japan Standard Time. Japan has no daylight saving, so a fixed offset is correct."""

ET_STANDARD = timezone(timedelta(hours=-5))
"""US Eastern Standard Time.

Only for sources that stamp a bare local time. Eastern *does* observe daylight
saving, so a fixed offset is wrong for half the year - which is why no US source
adapter in this package passes it. It is here so that if one ever needs it, the
choice is visible and this comment is attached to it.
"""
