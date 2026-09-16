"""Security master providers.

Every provider is interchangeable behind :class:`SecurityMasterProvider`, so the
platform is never tied to one vendor for one market.
"""

from surge.providers.base import SecurityMasterProvider

__all__ = ["SecurityMasterProvider"]
