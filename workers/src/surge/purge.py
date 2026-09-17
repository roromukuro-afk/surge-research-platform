"""Deleting licensed data, and being able to show that we did.

J-Quants obliges deletion of everything we stored when the subscription ends and
also when it is merely downgraded - dropping from Standard to Light obliges
deletion of the years only Standard could reach. EODHD's terms say nothing about
deletion, which is recorded as NOT_SPECIFIED rather than assumed either way.

The purge runs in four steps, deliberately separate:

1. Enumerate. The database lists every live object in scope and freezes that
   list as purge targets. A dry run stops being a different code path here - it
   runs the same enumeration and simply does not delete.
2. Delete the bytes, wherever they are: object storage, the local cache, and any
   scratch copy.
3. Record each object's outcome, one at a time, so a crash halfway through
   leaves an accurate partial record rather than an optimistic total.
4. Delete the derived Postgres rows, then close the request - which only closes
   if nothing is still pending or failed.

The manifest row itself is never deleted. "We held this and no longer do" is
the evidence that the obligation was met.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from surge.storage.base import ObjectStore


@dataclass
class PurgeOutcome:
    purge_request_id: int
    dry_run: bool
    targets: int = 0
    deleted: int = 0
    not_found: int = 0
    failed: int = 0
    local_files_removed: int = 0
    row_counts: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.failed == 0 and (self.deleted + self.not_found) == self.targets

    def as_dict(self) -> dict[str, object]:
        return {
            "purge_request_id": self.purge_request_id,
            "dry_run": self.dry_run,
            "targets": self.targets,
            "deleted": self.deleted,
            "not_found": self.not_found,
            "failed": self.failed,
            "local_files_removed": self.local_files_removed,
            "rows": dict(self.row_counts),
            "complete": self.complete,
            "failures": list(self.failures),
        }


class PurgeRunner:
    """Runs a licence purge against a database and one or more object stores.

    The connection must be made as a principal holding ``surge_purge``. The
    ingestion worker's role deliberately cannot execute any of this.
    """

    def __init__(
        self,
        connection,
        stores: dict[str, ObjectStore],
        *,
        local_cache_dirs: Iterable[Path | str] = (),
    ) -> None:
        self._connection = connection
        self._stores = stores
        self._cache_dirs = [Path(directory) for directory in local_cache_dirs]

    def run(
        self,
        *,
        provider_id: str,
        reason: str,
        requested_by: str,
        dataset_key: str | None = None,
        retained_plan: str | None = None,
        dry_run: bool = True,
    ) -> PurgeOutcome:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "select market.open_purge_request(%s, %s, %s, %s, %s, %s)",
                (provider_id, dataset_key, retained_plan, reason, requested_by, dry_run),
            )
            purge_request_id = int(cursor.fetchone()[0])

            cursor.execute(
                "select object_key, store_id from market.purge_targets where purge_request_id = %s "
                "order by object_key",
                (purge_request_id,),
            )
            targets = cursor.fetchall()

        outcome = PurgeOutcome(purge_request_id=purge_request_id, dry_run=dry_run, targets=len(targets))

        for object_key, store_id in targets:
            status, detail = self._delete_object(object_key, store_id, dry_run=dry_run)
            if status == "DELETED":
                outcome.deleted += 1
            elif status == "NOT_FOUND":
                outcome.not_found += 1
            elif status == "FAILED":
                outcome.failed += 1
                outcome.failures.append((object_key, detail or ""))

            with self._connection.cursor() as cursor:
                cursor.execute(
                    "select market.record_purge_result(%s, %s, %s, %s)",
                    (purge_request_id, object_key, status, detail),
                )

        outcome.local_files_removed = self._sweep_local_cache(
            {key for key, _ in targets}, dry_run=dry_run
        )

        with self._connection.cursor() as cursor:
            cursor.execute("select market.purge_market_rows(%s)", (purge_request_id,))
            outcome.row_counts = dict(cursor.fetchone()[0] or {})

            cursor.execute("select market.complete_purge_request(%s)", (purge_request_id,))
            summary = cursor.fetchone()[0] or {}

        # Trust the database's own count over our tally: it is what an auditor
        # would read.
        outcome.targets = int(summary.get("targets", outcome.targets))
        return outcome

    def _delete_object(self, object_key: str, store_id: str, *, dry_run: bool) -> tuple[str, str | None]:
        store = self._stores.get(store_id)
        if store is None:
            return "FAILED", f"no store configured for {store_id}"
        if dry_run:
            # SKIPPED keeps the request open, which is what a rehearsal should do.
            return "SKIPPED", "dry run"
        try:
            removed = store.delete(object_key)
        except Exception as exc:  # noqa: BLE001 - any store failure is a purge failure
            return "FAILED", f"{type(exc).__name__}: {exc}"
        return ("DELETED", None) if removed else ("NOT_FOUND", "already absent")

    def _sweep_local_cache(self, object_keys: set[str], *, dry_run: bool) -> int:
        """Remove cached and scratch copies of the same objects.

        Storage is not the only place bytes end up. A cache left behind is still
        data we hold.
        """

        removed = 0
        for directory in self._cache_dirs:
            if not directory.exists():
                continue
            for key in object_keys:
                candidate = directory / key
                if candidate.is_file():
                    if not dry_run:
                        candidate.unlink()
                    removed += 1
        return removed
