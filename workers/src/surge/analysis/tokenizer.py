"""Exact token counts, where an exact count is actually obtainable.

The GPT-OSS models are tokenised with ``o200k_harmony``, which OpenAI publishes
openly. That means the size of a request is measurable here, with no credential
and no account - which matters because "does this analysis fit a free tier" is a
question worth answering *before* deciding whether the tier is worth signing up
for.

Two rules keep this honest.

**An exact count is only reported when it is exact.** ``tiktoken`` is an optional
dependency; if it is absent, or the encoding is not one this build knows, the
answer is ``None`` rather than a fallback dressed up as a measurement. A number
labelled exact has to be exact, or the label is worse than no number.

**The estimate stays.** Not every provider publishes its tokeniser, and the
estimate is what covers the ones that do not. Where both exist they are reported
side by side: agreement is evidence the estimate can be trusted elsewhere, and
disagreement is worth seeing rather than hiding.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

#: What each model family is tokenised with. A family whose tokeniser is not
#: published is absent, and absence means the exact count is unavailable rather
#: than approximated with somebody else's encoding - two tokenisers agreeing on
#: English says nothing about how they split Japanese.
FAMILY_ENCODINGS = {
    "openai/gpt-oss-*": "o200k_harmony",
}


class TokeniserUnavailable(RuntimeError):
    """The encoding cannot be loaded, so no exact count can be produced."""


@lru_cache(maxsize=8)
def _encoding(name: str):
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise TokeniserUnavailable(
            "tiktoken is not installed, so no exact token count is available. Install the "
            "'tokens' extra to measure exactly; the estimate still works without it"
        ) from exc
    try:
        return tiktoken.get_encoding(name)
    except (ValueError, KeyError) as exc:
        raise TokeniserUnavailable(f"this tiktoken build has no encoding named {name!r}") from exc


def exact_tokens(text: str, *, encoding: str) -> int:
    """Count with the real tokeniser, or raise. Never approximate."""

    # disallowed_special=() because the canonical prompt is prose that may
    # legitimately contain sequences a chat template treats as special. Counting
    # them as text is what a provider does with a user message, and refusing to
    # count them would make the whole measurement unavailable over a string that
    # happens to look like a control token.
    return len(_encoding(encoding).encode(text, disallowed_special=()))


def exact_tokens_or_none(text: str, *, encoding: str | None) -> int | None:
    if not encoding:
        return None
    try:
        return exact_tokens(text, encoding=encoding)
    except TokeniserUnavailable:
        return None


def encoding_for_family(family: str) -> str | None:
    return FAMILY_ENCODINGS.get(family)


@dataclass(frozen=True)
class Count:
    """One measurement, carrying how it was arrived at.

    Both numbers travel together on purpose. A caller that wants to be safe uses
    ``worst``; a caller reporting to a person shows both, because "the estimate
    and the real tokeniser agree to within a percent" is itself a finding.
    """

    estimated: int
    exact: int | None = None
    encoding: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.exact is not None

    @property
    def best(self) -> int:
        """The number to quote: exact when it exists."""

        return self.exact if self.exact is not None else self.estimated

    @property
    def worst(self) -> int:
        """The number to decide on. Never smaller than either measurement."""

        return max(self.estimated, self.exact) if self.exact is not None else self.estimated

    @property
    def summary(self) -> dict:
        return {
            "estimated": self.estimated,
            "exact": self.exact,
            "encoding": self.encoding,
            "is_exact": self.is_exact,
        }


__all__ = [
    "FAMILY_ENCODINGS",
    "Count",
    "TokeniserUnavailable",
    "encoding_for_family",
    "exact_tokens",
    "exact_tokens_or_none",
]
