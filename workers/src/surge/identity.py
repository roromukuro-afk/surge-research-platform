"""Identity resolution for issuers and securities.

Phase 1.1 rule: a ticker is an attribute, never an identity. Identity comes from
a stable registry identifier when one exists (SEC CIK, EDINET code, the
exchange-assigned JPX local code) and is explicitly marked PROVISIONAL when it
does not, so a weak identity can be merged later instead of silently deciding
that two different companies are the same one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STRONG = "STRONG"
PROVISIONAL = "PROVISIONAL"

SOURCE_SEC_CIK = "SEC_CIK"
SOURCE_EDINET = "EDINET_CODE"
SOURCE_JPX_LOCAL_CODE = "JPX_LOCAL_CODE"
SOURCE_PROVISIONAL_NAME = "PROVISIONAL_NAME"
SOURCE_PROVISIONAL_SYMBOL = "PROVISIONAL_EXCHANGE_SYMBOL"

# Share class / series wording that distinguishes instruments of one issuer.
_CLASS_PATTERNS = (
    re.compile(r"\bclass\s+([a-z0-9]{1,3})\b"),
    re.compile(r"\bseries\s+([a-z0-9]{1,3})\b"),
)


@dataclass(frozen=True)
class Identity:
    source: str
    key: str
    confidence: str


def class_token(name: str) -> str:
    """Extract 'class a' / 'series b' style discriminators from a security name."""

    lowered = name.lower()
    for pattern in _CLASS_PATTERNS:
        found = pattern.search(lowered)
        if found:
            return found.group(1)
    return ""


def issuer_identity(
    *,
    market_code: str,
    normalized_name: str,
    cik: str | None = None,
    edinet_code: str | None = None,
) -> Identity:
    if cik:
        return Identity(SOURCE_SEC_CIK, f"CIK:{cik}", STRONG)
    if edinet_code:
        return Identity(SOURCE_EDINET, f"EDINET:{edinet_code}", STRONG)
    # No registry identifier: name-derived identity, explicitly provisional so it
    # can be merged once a stable identifier arrives.
    return Identity(SOURCE_PROVISIONAL_NAME, f"NAME:{market_code}:{normalized_name}", PROVISIONAL)


def security_identity(
    *,
    market_code: str,
    exchange_id: str | None,
    local_code: str,
    symbol: str | None,
    name: str,
    security_type: str,
    cik: str | None = None,
) -> Identity:
    if market_code == "JP":
        # The JPX local code is assigned by the exchange and survives name
        # changes; it is not the display ticker of a US listing.
        return Identity(SOURCE_JPX_LOCAL_CODE, f"JP:JPX:{local_code}", STRONG)

    if cik:
        discriminator = class_token(name)
        return Identity(SOURCE_SEC_CIK, f"US:CIK:{cik}:{security_type}:{discriminator}", STRONG)

    return Identity(
        SOURCE_PROVISIONAL_SYMBOL,
        f"US:{exchange_id or 'NA'}:SYMBOL:{symbol or local_code}",
        PROVISIONAL,
    )


def demote_colliding_identities(
    identities: list[Identity],
    *,
    exchange_ids: list[str | None],
    symbols: list[str | None],
    local_codes: list[str],
) -> tuple[list[Identity], list[str]]:
    """Two different instruments must never collapse into one identity.

    If a strong key is produced more than once inside a single snapshot, every
    record sharing it falls back to a provisional per-symbol identity and the
    collision is reported rather than silently merged.
    """

    counts: dict[str, int] = {}
    for identity in identities:
        if identity.confidence == STRONG:
            counts[identity.key] = counts.get(identity.key, 0) + 1

    collisions = {key for key, count in counts.items() if count > 1}
    if not collisions:
        return identities, []

    resolved: list[Identity] = []
    notes: list[str] = []
    for identity, exchange_id, symbol, local_code in zip(
        identities, exchange_ids, symbols, local_codes, strict=True
    ):
        if identity.confidence == STRONG and identity.key in collisions:
            resolved.append(
                Identity(
                    SOURCE_PROVISIONAL_SYMBOL,
                    f"US:{exchange_id or 'NA'}:SYMBOL:{symbol or local_code}",
                    PROVISIONAL,
                )
            )
            notes.append(f"identity collision on {identity.key}: fell back to symbol identity for {symbol or local_code}")
        else:
            resolved.append(identity)

    return resolved, notes
