"""Name and code normalisation used for issuer identity and future entity linking."""

from __future__ import annotations

import re
import unicodedata

_SUFFIXES = (
    "common stock",
    "ordinary shares",
    "class a common stock",
    "incorporated",
    "corporation",
    "company",
    "limited",
    "holdings",
    "inc",
    "corp",
    "co",
    "ltd",
    "plc",
    "sa",
    "nv",
    "ag",
    "株式会社",
    "(株)",
)

_PUNCT = re.compile(r"[^\w\s぀-ヿ一-鿿]+", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Fold case, width and punctuation so the same issuer maps to one key.

    Kept intentionally conservative: it removes decoration, not meaning, because
    an over-eager rule would merge two different issuers into one identity.
    """

    folded = unicodedata.normalize("NFKC", name).strip().lower()
    folded = _PUNCT.sub(" ", folded)
    folded = _SPACE.sub(" ", folded).strip()

    changed = True
    while changed:
        changed = False
        for suffix in _SUFFIXES:
            if folded.endswith(" " + suffix):
                folded = folded[: -(len(suffix) + 1)].strip()
                changed = True
    return folded or unicodedata.normalize("NFKC", name).strip().lower()


def normalize_jp_code(code: str) -> str:
    """JPX publishes 4 or 5 character codes; keep them as published, trimmed."""

    return unicodedata.normalize("NFKC", str(code)).strip()


def normalize_symbol(symbol: str) -> str:
    return unicodedata.normalize("NFKC", symbol).strip().upper()
