"""Does a database actually contain what `supabase/migrations/` says?

`supabase/migrations/` is the single source of truth for the schema (CLAUDE.md
§4). That is a statement of intent, and intent is not self-enforcing: a function
applied from a paste rather than from the file leaves the two agreeing on
behaviour and disagreeing on text, and the next person to read the file reads
something the database is not running.

Two directions of divergence, and they need different names:

* **drifted** - the database and a migration both define the function, and the
  bodies differ.
* **unmanaged** - the database has a function in a project schema that no
  migration defines at all. Not "the file says something else" but "no file says
  anything", which is how a hand-made object survives unnoticed.

This lives in ``surge.runtime`` rather than in the test that first needed it
because of where it has to run. Continuous integration builds its database *from
these files*, so a comparison there is very nearly a tautology: it can only
catch a bug in this module's own parsing. The divergence this is meant to find
happens in the cloud database, which CI never opens. So the comparison has to be
callable against any connection - by the test, by the readiness report, and by
``python -m surge.jobs.schema_drift_cli`` pointed at the real thing.

Bodies are compared with whitespace normalised, because formatting is not the
point and Postgres does not preserve it exactly. Comments are *not* stripped:
every drift found so far has been a stripped comment, so treating them as noise
would silence the only signal this has ever produced.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

#: The schemas this project owns. Everything else in the database belongs to an
#: extension or to Supabase, and is none of this check's business.
PROJECT_SCHEMAS: tuple[str, ...] = (
    "prod",
    "labels",
    "market",
    "analysis",
    "ref",
    "pipeline",
    "universe",
    "news",
    "research",
    "ui",
)

#: `create [or replace] function <schema>.<name>(...) ... as $tag$ <body> $tag$`.
#:
#: Two things this has to tolerate, both of which an earlier version got wrong,
#: and both of which failed the same way - by quietly watching less than it
#: appeared to:
#:
#: * the dollar-quote tag is usually empty (a bare `$$`), so `\w*` not `\w+`.
#:   Requiring a tag matched a tenth of the functions.
#: * `or replace` is optional, because a migration that changes a function's
#:   *signature* has to drop it and `create` it plain. Missing that form meant
#:   the newest definition of `pipeline.snapshot_securities` was invisible, and
#:   the database was reported as drifted for holding the right body.
_FUNCTION = re.compile(
    r"create\s+(?:or\s+replace\s+)?function\s+(\w+)\.(\w+)\s*\(.*?\$(\w*)\$(.*?)\$\3\$",
    re.S | re.I,
)


def default_migrations_dir() -> pathlib.Path:
    """`<repo>/supabase/migrations`, from this file's position in the tree."""

    return pathlib.Path(__file__).resolve().parents[4] / "supabase" / "migrations"


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def bodies_in_migrations(migrations_dir: pathlib.Path | None = None) -> dict[tuple[str, str], set[str]]:
    """Every body each function name is given across the migrations.

    A set rather than "the last definition wins", because a name can be
    overloaded: ``ref.listings_as_of`` has two signatures and the database holds
    both. A last-wins map would call the older signature drifted for no better
    reason than that it is not the newer one. Thirty of the names here have more
    than one body.
    """

    directory = migrations_dir or default_migrations_dir()
    if not directory.is_dir():
        # Never answer "in sync" because the files could not be found. A missing
        # source of truth is an unanswered question, not a passing check.
        raise FileNotFoundError(f"no migrations directory at {directory}")

    bodies: dict[tuple[str, str], set[str]] = {}
    for path in sorted(directory.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for schema, name, _tag, body in _FUNCTION.findall(text):
            bodies.setdefault((schema.lower(), name.lower()), set()).add(normalise(body))
    return bodies


@dataclass(frozen=True)
class DriftReport:
    """What the database has that the files do not describe, and vice versa."""

    #: Functions defined in both, with different bodies.
    drifted: tuple[str, ...] = ()
    #: Functions in a project schema that no migration defines.
    unmanaged: tuple[str, ...] = ()
    #: How many live functions were examined. Zero means the query found
    #: nothing, which is a finding of its own rather than a clean bill.
    examined: int = 0
    #: How many distinct function names the migrations define.
    defined_in_migrations: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def in_sync(self) -> bool:
        return not self.drifted and not self.unmanaged and self.examined > 0

    def summary(self) -> str:
        if self.examined == 0:
            return "no functions found in the project schemas; the comparison proved nothing"
        if self.in_sync:
            return f"{self.examined} live functions all match supabase/migrations"
        parts = []
        if self.drifted:
            parts.append("drifted: " + ", ".join(self.drifted))
        if self.unmanaged:
            parts.append("defined in no migration: " + ", ".join(self.unmanaged))
        return "; ".join(parts)


_LIVE_FUNCTIONS = """
    select n.nspname, p.proname, p.prosrc
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = any(%(schemas)s)
"""


def compare(conn, migrations_dir: pathlib.Path | None = None) -> DriftReport:
    """Compare every live function in the project schemas against the files.

    Takes an open connection rather than a DSN so a caller already inside a
    read-only transaction can reuse it.
    """

    bodies = bodies_in_migrations(migrations_dir)

    with conn.cursor() as cur:
        cur.execute(_LIVE_FUNCTIONS, {"schemas": list(PROJECT_SCHEMAS)})
        live = cur.fetchall()

    drifted: list[str] = []
    unmanaged: list[str] = []
    for schema, name, source in live:
        expected = bodies.get((schema.lower(), name.lower()))
        if not expected:
            unmanaged.append(f"{schema}.{name}")
            continue
        if normalise(source) not in expected:
            drifted.append(f"{schema}.{name}")

    return DriftReport(
        drifted=tuple(sorted(set(drifted))),
        unmanaged=tuple(sorted(set(unmanaged))),
        examined=len(live),
        defined_in_migrations=len(bodies),
    )
