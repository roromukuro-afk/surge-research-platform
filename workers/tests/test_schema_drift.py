"""The parsing half of the drift check, which needs no database.

This is where the guard has actually caught things. Both bugs it has had made it
match *fewer* functions than it appeared to, and a guard that watches less than
it claims is worse than no guard: it reports "in sync" over the part it never
looked at.
"""

from __future__ import annotations

import pathlib

import pytest

from surge.runtime.schema_drift import (
    DriftReport,
    bodies_in_migrations,
    default_migrations_dir,
    normalise,
)


def test_the_migrations_actually_parse():
    bodies = bodies_in_migrations()

    assert len(bodies) > 60
    assert ("prod", "begin_entry_analysis") in bodies


def test_a_function_created_without_or_replace_is_still_seen():
    """`pipeline.snapshot_securities` is redefined by a later migration with a
    plain `create function`, because its signature changed. An earlier regex
    required `or replace`, so it read the superseded body as the only one and
    called the database drifted for holding the right one."""

    bodies = bodies_in_migrations()

    assert len(bodies[("pipeline", "snapshot_securities")]) == 2


def test_bare_dollar_quotes_are_matched():
    """Most of these functions use `$$` with no tag. Requiring `\\w+` matched a
    tenth of them."""

    bodies = bodies_in_migrations()

    assert len(bodies) > 60
    assert ("labels", "lineage_gaps") in bodies


def test_an_overloaded_name_keeps_every_body():
    """`ref.listings_as_of` has two signatures and the database holds both. A
    last-definition-wins map would report the older one as drift."""

    bodies = bodies_in_migrations()

    assert len(bodies[("ref", "listings_as_of")]) >= 2


def test_a_missing_migrations_directory_is_an_error_not_a_pass(tmp_path: pathlib.Path):
    """The failure mode worth refusing: answering "in sync" because the source
    of truth could not be found."""

    with pytest.raises(FileNotFoundError):
        bodies_in_migrations(tmp_path / "nowhere")


def test_comparing_nothing_is_not_being_in_sync():
    """An empty result set looks exactly like a clean one."""

    assert not DriftReport(examined=0).in_sync
    assert DriftReport(examined=10).in_sync
    assert not DriftReport(examined=10, drifted=("prod.f",)).in_sync
    assert not DriftReport(examined=10, unmanaged=("ref.tmp",)).in_sync


def test_normalisation_collapses_whitespace_and_keeps_comments():
    """Every drift found so far has been a stripped comment. Normalising them
    away would make the check blind to the only thing it has ever caught."""

    assert normalise("select\n   1") == "select 1"
    assert "-- why" in normalise("select 1\n  -- why\n")


def test_the_default_directory_is_the_repository_one():
    assert default_migrations_dir().name == "migrations"
    assert default_migrations_dir().is_dir()
