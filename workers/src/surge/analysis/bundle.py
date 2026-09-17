"""Assembling exactly what a model is shown, and hashing all of it.

Two rules govern this module.

**The canonical prompt and the addenda never merge.** v5.1 is immutable and the
post-v5.1 decisions are versioned additions to it. They travel as separate
sections with separate hashes, so a bundle that quietly folded an addendum into
the canonical text would be detectable rather than invisible.

**Every section is hashed on its own.** A run that produced a different answer
from yesterday's should be diagnosable by comparing seven short digests, not by
diffing megabytes of serialised market data.

The bundle is deterministic: the same inputs produce the same bytes and therefore
the same hash. That is what makes an analysis reproducible at all, so sorting and
separator choices here are load-bearing rather than stylistic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime

BUNDLE_VERSION = "stage3-bundle-1.0.0"

#: The sections a bundle carries, in the order they are serialised. Fixed,
#: because reordering would change the hash without changing the content.
SECTION_ORDER = (
    "canonical_prompt",
    "addenda",
    "security",
    "market_data",
    "features",
    "routes",
    "materials",
    "entity_links",
    "price_obstacles",
    "stage2",
    "coverage",
)


class BundleError(ValueError):
    """Raised when a bundle would misrepresent what the model was shown."""


def canonical_json(payload) -> str:
    """Serialise deterministically.

    ``sort_keys`` and fixed separators matter: without them the same content
    hashes differently depending on dictionary insertion order, and a hash that
    changes without the content changing is worse than no hash.
    """

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_encode)


def _encode(value):
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not serialisable into a bundle")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class InputBundle:
    """Everything Stage 3 sees, plus the hashes that make it reproducible."""

    security_id: str
    market_code: str
    as_of_date: date
    knowledge_cutoff: datetime
    canonical_prompt_sha256: str
    addenda_sha256: list[str] = field(default_factory=list)
    sections: dict = field(default_factory=dict)
    versions: dict = field(default_factory=dict)
    bundle_version: str = BUNDLE_VERSION

    def __post_init__(self) -> None:
        if len(self.canonical_prompt_sha256) != 64:
            raise BundleError("canonical_prompt_sha256 must be a full SHA-256 hex digest")
        if "canonical_prompt" in self.sections or "addenda" in self.sections:
            raise BundleError(
                "the canonical prompt and its addenda are referenced by hash, not embedded as sections. "
                "Embedding them here is how v5.1 and post-v5.1 decisions get mixed"
            )
        unknown = set(self.sections) - set(SECTION_ORDER)
        if unknown:
            raise BundleError(f"unknown bundle sections: {sorted(unknown)}")

    @property
    def section_digests(self) -> dict[str, str]:
        """One hash per section, so a change is locatable without a full diff."""

        digests = {
            "canonical_prompt": self.canonical_prompt_sha256,
            "addenda": sha256_text(canonical_json(sorted(self.addenda_sha256))),
        }
        for name in SECTION_ORDER:
            if name in ("canonical_prompt", "addenda"):
                continue
            digests[name] = sha256_text(canonical_json(self.sections.get(name)))
        return digests

    def serialise(self) -> str:
        """The bundle's canonical bytes.

        Note that the canonical prompt appears as a hash rather than as text.
        The model is given the text; the *record* holds the hash, because the
        text is immutable and already stored once under that hash.
        """

        payload = {
            "bundle_version": self.bundle_version,
            "security_id": self.security_id,
            "market_code": self.market_code,
            "as_of_date": self.as_of_date,
            "knowledge_cutoff": self.knowledge_cutoff,
            "canonical_prompt_sha256": self.canonical_prompt_sha256,
            "addenda_sha256": sorted(self.addenda_sha256),
            "versions": self.versions,
            "sections": {name: self.sections.get(name) for name in SECTION_ORDER if name in self.sections},
        }
        return canonical_json(payload)

    @property
    def bundle_sha256(self) -> str:
        return sha256_text(self.serialise())

    def as_row(self, *, run_id: str) -> dict:
        """The shape ``analysis.input_bundles`` expects."""

        return {
            "run_id": run_id,
            "security_id": self.security_id,
            "as_of_date": self.as_of_date,
            "market_code": self.market_code,
            "bundle_version": self.bundle_version,
            "canonical_prompt_sha256": self.canonical_prompt_sha256,
            "addenda_sha256": sorted(self.addenda_sha256),
            "section_digests": self.section_digests,
            "bundle_sha256": self.bundle_sha256,
            "knowledge_cutoff": self.knowledge_cutoff,
            **{key: self.versions.get(key) for key in (
                "feature_version",
                "route_version",
                "material_route_version",
                "material_feature_version",
                "stage2_version",
                "concept_version",
                "relevance_ruleset_version",
                "merge_version",
            )},
        }


def build_bundle(
    *,
    security_id: str,
    market_code: str,
    as_of_date: date,
    knowledge_cutoff: datetime,
    canonical_prompt_sha256: str,
    addenda_sha256=(),
    market_data=None,
    features=None,
    routes=None,
    materials=None,
    entity_links=None,
    price_obstacles=None,
    stage2=None,
    coverage=None,
    security=None,
    versions=None,
) -> InputBundle:
    """Assemble a bundle, omitting sections that genuinely have no content.

    An absent section and an empty one are different: no materials reached this
    security is a fact worth recording, whereas the materials step never having
    run is a gap. Callers pass ``[]`` for the first and ``None`` for the second,
    and the section digests keep them distinguishable.
    """

    sections = {
        "security": security,
        "market_data": market_data,
        "features": features,
        "routes": routes,
        "materials": materials,
        "entity_links": entity_links,
        "price_obstacles": price_obstacles,
        "stage2": stage2,
        "coverage": coverage,
    }
    return InputBundle(
        security_id=security_id,
        market_code=market_code,
        as_of_date=as_of_date,
        knowledge_cutoff=knowledge_cutoff,
        canonical_prompt_sha256=canonical_prompt_sha256,
        addenda_sha256=list(addenda_sha256),
        sections={name: value for name, value in sections.items() if value is not None},
        versions=dict(versions or {}),
    )


def missing_sections(bundle: InputBundle) -> tuple[str, ...]:
    """Which sections were not supplied at all.

    Reported rather than filled. A Stage 3 answer produced without the materials
    section is a different answer from one produced with an empty materials
    section, and the output row should be able to say which it was.
    """

    return tuple(
        name
        for name in SECTION_ORDER
        if name not in ("canonical_prompt", "addenda") and name not in bundle.sections
    )
