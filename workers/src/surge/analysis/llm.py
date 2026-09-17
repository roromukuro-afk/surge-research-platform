"""The model boundary, and a stand-in that needs no credential.

Stage 3 talks to an ``LLMProvider``. The only one implemented here is a
deterministic rule-based stand-in, and that is a deliberate ordering rather than
a placeholder: the project must not require a paid model subscription to run, so
the pipeline is completed against a provider that costs nothing and returns the
same answer for the same bundle every time.

The stand-in is honest about what it is. It produces a state and a rationale by
fixed rules over the bundle, and its rationale says so. It is a pipeline under
test, not a judgement, and the output row records ``DETERMINISTIC_MOCK`` so
nobody can later mistake its verdicts for analysis.

When a real model is connected, nothing above this boundary changes: the bundle,
the validator and the storage are already in place, and the provider is one row
in ``analysis.llm_providers``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from surge.analysis.bundle import InputBundle, canonical_json

MOCK_PROVIDER_VERSION = "deterministic-mock-1.0.0"


class ProviderKind(StrEnum):
    DETERMINISTIC_MOCK = "DETERMINISTIC_MOCK"
    HOSTED_LLM = "HOSTED_LLM"
    LOCAL_LLM = "LOCAL_LLM"


class Stage3State(StrEnum):
    """Mirrors ``analysis.stage3_state``.

    There is no ENTRY member, here or in the database. An end-of-day analysis
    cannot produce an entry prediction: that is a Phase 8 decision taken during
    the session against a live price, and leaving the value unrepresentable is
    cheaper than remembering not to use it.
    """

    TECHNICAL_SETUP_EOD = "TECHNICAL_SETUP_EOD"
    POST_CLOSE_CATALYST_SETUP = "POST_CLOSE_CATALYST_SETUP"
    WATCH_BREAKOUT = "WATCH_BREAKOUT"
    WATCH_PULLBACK = "WATCH_PULLBACK"
    WATCH_OTHER = "WATCH_OTHER"
    REJECT = "REJECT"


class ZoneBasisKind(StrEnum):
    """Permitted justifications for a reachable zone.

    PRIOR_HIGH is absent on purpose. A price a stock once traded at is where
    sellers are waiting, not a reason it will return (CLAUDE.md 1-11).
    """

    CURRENT_MATERIAL = "CURRENT_MATERIAL"
    SUPPLY_STRUCTURE = "SUPPLY_STRUCTURE"
    VOLUME_STRUCTURE = "VOLUME_STRUCTURE"
    SUPPORT_RESISTANCE = "SUPPORT_RESISTANCE"
    VOLATILITY_RANGE = "VOLATILITY_RANGE"
    SECTOR_MOVE = "SECTOR_MOVE"


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    bundle: InputBundle
    model_parameters: dict = field(default_factory=dict)

    @property
    def prompt_sha256(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LLMResponse:
    """What came back, before validation."""

    state: Stage3State
    rationale: str
    provider_id: str
    provider_kind: ProviderKind
    model_id: str | None = None
    confidence_note: str | None = None
    reachable_zone_low: float | None = None
    reachable_zone_high: float | None = None
    reachable_zone_basis_kinds: tuple[ZoneBasisKind, ...] = ()
    reachable_zone_basis: str | None = None
    concepts_considered: tuple[str, ...] = ()
    raw_text: str = ""

    @property
    def response_sha256(self) -> str:
        return hashlib.sha256((self.raw_text or self.rationale).encode("utf-8")).hexdigest()


class LLMProvider(Protocol):
    provider_id: str
    provider_kind: ProviderKind
    model_id: str | None

    def analyse(self, request: LLMRequest) -> LLMResponse: ...


def render_prompt(bundle: InputBundle, canonical_text: str, addenda_texts=()) -> str:
    """Build the text sent to the model.

    The canonical prompt, the addenda and the data are three labelled blocks and
    are never interleaved. A model reading this can tell which instruction is
    immutable and which is a later decision, and so can a person reading the
    stored prompt six months from now.
    """

    blocks = [
        "=== CANONICAL v5.1 (immutable) ===",
        canonical_text,
        "",
        "=== POST-v5.1 ADDENDA (later decisions; newer overrides older) ===",
    ]
    blocks.extend(addenda_texts or ["(none)"])
    blocks.extend(
        [
            "",
            "=== BUNDLE (data; knowledge cutoff " + bundle.knowledge_cutoff.isoformat() + ") ===",
            bundle.serialise(),
            "",
            "=== OUTPUT CONTRACT ===",
            "Return one state from: " + ", ".join(state.value for state in Stage3State) + ".",
            "Do not return an entry decision: this is an end-of-day analysis and no price is live.",
            "If you give a reachable zone, justify it from one or more of: "
            + ", ".join(kind.value for kind in ZoneBasisKind)
            + ". A prior high is an obstacle, never a reason to expect a rise.",
        ]
    )
    return "\n".join(blocks)


class DeterministicMockProvider:
    """A rule-based stand-in that returns the same answer for the same bundle.

    The rules are intentionally shallow. Their job is to exercise the pipeline -
    bundle, prompt, response, validation, storage - not to analyse anything. Every
    rationale it produces says as much, because an unlabelled mock verdict in a
    results table is a trap for a future reader.
    """

    provider_id = "deterministic_mock"
    provider_kind = ProviderKind.DETERMINISTIC_MOCK
    model_id = None
    version = MOCK_PROVIDER_VERSION

    def analyse(self, request: LLMRequest) -> LLMResponse:
        bundle = request.bundle
        stage2 = bundle.sections.get("stage2") or {}
        routes = bundle.sections.get("routes") or {}
        materials = bundle.sections.get("materials") or []
        obstacles = bundle.sections.get("price_obstacles") or []

        concepts = tuple(stage2.get("concepts_fired", ()) or ())
        technical_routes = tuple(routes.get("technical_routes", ()) or ())
        material_routes = tuple(routes.get("material_routes", ()) or ())

        state, reason = self._decide(concepts, technical_routes, material_routes, materials)

        zone_low = zone_high = None
        basis_kinds: tuple[ZoneBasisKind, ...] = ()
        basis: str | None = None
        close = stage2.get("close")
        atr_pct = stage2.get("atr_pct")

        if state is not Stage3State.REJECT and close and atr_pct:
            # Built from current volatility and the nearest resistance, never
            # from a prior high. Two obvious, defensible inputs - which is the
            # point: the zone has to have a basis from the permitted set.
            zone_low = float(close)
            zone_high = float(close) * (1 + 2 * float(atr_pct) / 100.0)
            basis_kinds = (ZoneBasisKind.VOLATILITY_RANGE,)
            basis = f"two sessions of the current ATR ({atr_pct}% of price) above the close"
            resistance = stage2.get("nearest_resistance")
            if resistance:
                zone_high = min(zone_high, float(resistance))
                basis_kinds = (*basis_kinds, ZoneBasisKind.SUPPORT_RESISTANCE)
                basis += f", capped at the nearest resistance ({resistance})"
            if obstacles:
                basis += (
                    f"; {len(obstacles)} obstacle(s) stand above the price and were treated as "
                    "resistance rather than as targets"
                )

        raw = canonical_json(
            {
                "provider": self.provider_id,
                "version": self.version,
                "state": state.value,
                "bundle_sha256": bundle.bundle_sha256,
                "concepts": list(concepts),
                "technical_routes": list(technical_routes),
                "material_routes": list(material_routes),
            }
        )

        return LLMResponse(
            state=state,
            rationale=(
                f"[deterministic stand-in {self.version}: rules, not judgement] {reason}"
            ),
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            model_id=self.model_id,
            confidence_note=(
                "Produced by the deterministic stand-in. This is evidence that the pipeline runs, "
                "and no evidence at all about the security."
            ),
            reachable_zone_low=zone_low,
            reachable_zone_high=zone_high,
            reachable_zone_basis_kinds=basis_kinds,
            reachable_zone_basis=basis,
            concepts_considered=concepts,
            raw_text=raw,
        )

    @staticmethod
    def _decide(concepts, technical_routes, material_routes, materials):
        if not technical_routes and not material_routes:
            return Stage3State.REJECT, "no technical or material route fired"

        # A material that arrived after the close is a catalyst setup rather than
        # a chart setup, and the project keeps those apart so that post-close
        # news is never read as if the close had already priced it.
        # A missing field is UNKNOWN, not "not post-close". Reading None as
        # absence-of-post-close is precisely how an evening disclosure would be
        # treated as something the close had already priced.
        timings = {
            (m.get("session_timing") or "UNKNOWN") for m in materials if isinstance(m, dict)
        }
        if material_routes and "POST_CLOSE" in timings:
            return (
                Stage3State.POST_CLOSE_CATALYST_SETUP,
                f"material routes {sorted(material_routes)} fired on material published after the close",
            )
        if material_routes and "UNKNOWN" in timings:
            # Neither setup state may be asserted. Whether the close had already
            # priced this decides between them, and without a verified trading
            # calendar that is not known - so the answer is to watch rather than
            # to pick the more convenient of two claims.
            return (
                Stage3State.WATCH_OTHER,
                f"material routes {sorted(material_routes)} fired, but whether the disclosure landed "
                "before or after the close is UNKNOWN - no verified trading calendar - so neither a "
                "technical setup nor a catalyst setup can be asserted",
            )

        if "FAILED_BREAKOUT" in concepts:
            return Stage3State.WATCH_OTHER, "a failed breakout leaves trapped supply above; watching only"

        if "HEALTHY_PULLBACK" in concepts:
            return Stage3State.WATCH_PULLBACK, "pullback on contracting volume, still above its reference average"

        if technical_routes and material_routes:
            return (
                Stage3State.TECHNICAL_SETUP_EOD,
                f"technical routes {sorted(technical_routes)} and material routes {sorted(material_routes)} both fired",
            )

        if technical_routes:
            return Stage3State.WATCH_BREAKOUT, f"technical routes {sorted(technical_routes)} fired without material"

        return Stage3State.WATCH_OTHER, f"material routes {sorted(material_routes)} fired without a chart setup"
