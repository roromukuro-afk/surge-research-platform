"""Provider interface (Phase 1 subset of docs/interfaces.md).

Phase 1 only needs the security master role. Price, intraday and realtime roles
are declared in the capabilities so a later phase can bind different providers
per role without changing calling code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from surge.models import FetchResult, ProviderCapabilities

ROLE_SECURITY_MASTER = "SECURITY_MASTER"
ROLE_EOD_UNIVERSE = "EOD_UNIVERSE"
ROLE_INTRADAY_HISTORY = "INTRADAY_HISTORY"
ROLE_TRADES_HISTORY = "TRADES_HISTORY"
ROLE_REALTIME_DECISION = "REALTIME_DECISION"

LATENCY_REALTIME = "REALTIME"
LATENCY_DELAYED_15M = "DELAYED_15M"
LATENCY_EOD = "EOD"
LATENCY_DAILY_BATCH = "DAILY_BATCH"


@runtime_checkable
class SecurityMasterProvider(Protocol):
    """Returns the listed securities of a market as the provider reports them."""

    provider_id: str

    def capabilities(self) -> ProviderCapabilities: ...

    def fetch_security_master(self) -> FetchResult: ...


def assert_role_allowed(capabilities: ProviderCapabilities, role: str) -> None:
    """Guard against binding a provider to a role it cannot serve.

    A daily-batch provider must never end up behind a realtime decision role:
    that is how a stale price silently becomes an entry price.
    """

    if role not in capabilities.roles:
        raise ValueError(f"{capabilities.provider_id} does not support role {role}")

    if role == ROLE_REALTIME_DECISION and capabilities.latency_class != LATENCY_REALTIME:
        raise ValueError(
            f"{capabilities.provider_id} has latency {capabilities.latency_class} "
            "and cannot be bound to REALTIME_DECISION"
        )
