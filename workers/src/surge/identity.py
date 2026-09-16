"""Identity resolution for issuers and securities.

Phase 1.1 rule: a ticker is an attribute, never an identity. Identity comes from
a stable registry identifier when one exists (SEC CIK, EDINET code, the
exchange-assigned JPX local code) and is explicitly marked PROVISIONAL when it
does not, so a weak identity can be merged later instead of silently deciding
that two different companies are the same one.

Phase 1.1a sharpens two things:

* Confidence is honest. A US security key is built from a CIK *plus* a security
  type and a share-class token read out of the provider's display name. That is
  anchored in a registry but it is not a registry-issued security identifier,
  so it is ``REGISTRY_ANCHORED`` rather than ``STRONG``.
* A name is never an identity, not even a weak one. Without a registry
  identifier the issuer key follows the security's own coordinate, so two
  different companies that happen to normalise to the same string stay apart.
  A false split can be merged later; a false merge silently destroys data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Version of the identity ruleset that produced a key. Stored with every
# security so a later promotion (a real security-level registry id) can be
# applied as a recorded migration instead of a silent re-key.
IDENTITY_VERSION = "identity-1.1a"

STRONG = "STRONG"
REGISTRY_ANCHORED = "REGISTRY_ANCHORED"
PROVISIONAL = "PROVISIONAL"

CONFIDENCE_LEVELS = (STRONG, REGISTRY_ANCHORED, PROVISIONAL)

# Levels that claim to be a reusable key: two records landing on the same one is
# a defect, so both are demoted instead of being merged into a single row.
DEMOTABLE = frozenset({STRONG, REGISTRY_ANCHORED})

SOURCE_SEC_CIK = "SEC_CIK"
SOURCE_EDINET = "EDINET_CODE"
SOURCE_JPX_LOCAL_CODE = "JPX_LOCAL_CODE"
SOURCE_PROVISIONAL_COORDINATE = "PROVISIONAL_SECURITY_COORDINATE"
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


@dataclass(frozen=True)
class IdentityCollision:
    """Two instruments resolved to one key; both were demoted, neither merged."""

    identity_key: str
    symbol: str
    exchange_id: str | None

    @property
    def message(self) -> str:
        return (
            f"identity collision on {self.identity_key}: "
            f"fell back to symbol identity for {self.symbol}"
        )

    @property
    def context(self) -> dict[str, str | None]:
        return {
            "identity_key": self.identity_key,
            "symbol": self.symbol,
            "exchange_id": self.exchange_id,
        }


def class_token(name: str) -> str:
    """Extract 'class a' / 'series b' style discriminators from a security name."""

    lowered = name.lower()
    for pattern in _CLASS_PATTERNS:
        found = pattern.search(lowered)
        if found:
            return found.group(1)
    return ""


def provisional_symbol_key(
    *, market_code: str, exchange_id: str | None, symbol: str | None, local_code: str
) -> str:
    return f"{market_code}:{exchange_id or 'NA'}:SYMBOL:{symbol or local_code}"


def issuer_identity(
    *,
    market_code: str,
    security_identity_key: str,
    cik: str | None = None,
    edinet_code: str | None = None,
) -> Identity:
    """Issuer identity from a registry, or one issuer per security coordinate.

    The fallback deliberately does *not* group by name. A normalised name is
    matching evidence, kept as an alias, and a merge only happens once a stable
    identifier (CIK, EDINET code, or another registry id) actually says so.
    """

    if cik:
        return Identity(SOURCE_SEC_CIK, f"CIK:{cik}", STRONG)
    if edinet_code:
        return Identity(SOURCE_EDINET, f"EDINET:{edinet_code}", STRONG)
    return Identity(
        SOURCE_PROVISIONAL_COORDINATE,
        f"ISSUER-OF:{security_identity_key}",
        PROVISIONAL,
    )


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
        # The JPX local code is assigned by the exchange to the security itself
        # and survives name changes; it is not the display ticker of a US listing.
        return Identity(SOURCE_JPX_LOCAL_CODE, f"JP:JPX:{local_code}", STRONG)

    if cik:
        # Anchored in a registry at the *issuer* level only: the type and the
        # share-class token are read out of the provider's display name, so a
        # pure formatting change can move this key. Never claim STRONG for it.
        discriminator = class_token(name)
        return Identity(
            SOURCE_SEC_CIK,
            f"US:CIK:{cik}:{security_type}:{discriminator}",
            REGISTRY_ANCHORED,
        )

    return Identity(
        SOURCE_PROVISIONAL_SYMBOL,
        provisional_symbol_key(
            market_code=market_code, exchange_id=exchange_id, symbol=symbol, local_code=local_code
        ),
        PROVISIONAL,
    )


def demote_colliding_identities(
    identities: list[Identity],
    *,
    exchange_ids: list[str | None],
    symbols: list[str | None],
    local_codes: list[str],
    market_codes: list[str] | None = None,
) -> tuple[list[Identity], list[IdentityCollision]]:
    """Two different instruments must never collapse into one identity.

    Any key that claims to be reusable (STRONG or REGISTRY_ANCHORED) and is
    produced more than once inside a single snapshot demotes every record that
    carries it to a provisional per-symbol identity, and the collision is
    reported rather than silently merged.
    """

    markets = market_codes or ["US"] * len(identities)
    counts: dict[str, int] = {}
    for identity in identities:
        if identity.confidence in DEMOTABLE:
            counts[identity.key] = counts.get(identity.key, 0) + 1

    collisions = {key for key, count in counts.items() if count > 1}
    if not collisions:
        return identities, []

    resolved: list[Identity] = []
    reported: list[IdentityCollision] = []
    for identity, exchange_id, symbol, local_code, market_code in zip(
        identities, exchange_ids, symbols, local_codes, markets, strict=True
    ):
        if identity.confidence in DEMOTABLE and identity.key in collisions:
            resolved.append(
                Identity(
                    SOURCE_PROVISIONAL_SYMBOL,
                    provisional_symbol_key(
                        market_code=market_code,
                        exchange_id=exchange_id,
                        symbol=symbol,
                        local_code=local_code,
                    ),
                    PROVISIONAL,
                )
            )
            reported.append(
                IdentityCollision(
                    identity_key=identity.key,
                    symbol=symbol or local_code,
                    exchange_id=exchange_id,
                )
            )
        else:
            resolved.append(identity)

    return resolved, reported
