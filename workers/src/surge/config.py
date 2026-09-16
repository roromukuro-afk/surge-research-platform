"""Runtime configuration.

Everything that could be a secret comes from the environment. Nothing in this
package hard-codes a project URL, key or password.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_CONTACT = "contact-not-configured@example.invalid"


@dataclass(frozen=True)
class Settings:
    contact_email: str
    user_agent: str
    database_url: str | None

    @property
    def has_database(self) -> bool:
        return bool(self.database_url)


def load_settings() -> Settings:
    contact = os.environ.get("SURGE_CONTACT_EMAIL", DEFAULT_CONTACT)
    return Settings(
        contact_email=contact,
        user_agent=f"surge-research-platform/0.1 ({contact})",
        database_url=os.environ.get("SUPABASE_DB_URL") or os.environ.get("SURGE_DATABASE_URL"),
    )
