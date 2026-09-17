"""Phase 2.1: the licence guard and the write-once object store.

These are the two mechanisms that stand between a subscription's terms and a
directory full of data nobody is allowed to hold, so they are tested for what
they REFUSE rather than for what they permit.
"""

from __future__ import annotations

import pytest

from surge.licensing import (
    DATASETS,
    LicenseAction,
    LicenseMode,
    LicensePolicy,
    LicenseViolation,
    Obligation,
    Permission,
    dataset,
)
from surge.storage.base import ImmutableObjectConflict, content_addressed_key, sha256_hex
from surge.storage.local import LocalObjectStore


def _policy(**overrides) -> LicensePolicy:
    base = {
        "provider_id": "test",
        "dataset_key": "TEST",
        "policy_version": "test-1",
        "license_mode": LicenseMode.PRIVATE_PERSONAL_RESEARCH_ONLY,
        "public_display_allowed": Permission.PROHIBITED,
        "third_party_access_allowed": Permission.PROHIBITED,
        "commercial_use_allowed": Permission.PROHIBITED,
        "academic_use_allowed": Permission.NOT_SPECIFIED,
        "raw_redistribution_allowed": Permission.PROHIBITED,
        "derived_output_sharing_allowed": Permission.UNKNOWN,
        "delete_on_cancel": Obligation.REQUIRED,
        "delete_on_downgrade": Obligation.REQUIRED,
        "attribution_required": Obligation.NOT_SPECIFIED,
        "modification_disclosure_required": Obligation.NOT_SPECIFIED,
        "entitlement_plan": "Standard",
        "terms_url": "https://example.invalid/terms",
    }
    base.update(overrides)
    return LicensePolicy(**base)


# ------------------------------------------------------------------- licence
@pytest.mark.parametrize(
    "permission",
    [Permission.PROHIBITED, Permission.NOT_SPECIFIED, Permission.UNKNOWN],
)
def test_only_an_explicit_permission_passes(permission):
    """Silence is not consent, and neither is not having read the terms."""

    policy = _policy(public_display_allowed=permission)
    with pytest.raises(LicenseViolation, match="PUBLIC_DISPLAY"):
        policy.assert_allows(LicenseAction.PUBLIC_DISPLAY)


def test_an_allowed_action_passes():
    policy = _policy(public_display_allowed=Permission.ALLOWED)
    policy.assert_allows(LicenseAction.PUBLIC_DISPLAY)


def test_the_violation_message_names_the_terms():
    """Whoever hits this needs to go and read the document, so link it."""

    policy = _policy()
    with pytest.raises(LicenseViolation) as raised:
        policy.assert_allows(LicenseAction.COMMERCIAL_USE)
    assert "https://example.invalid/terms" in str(raised.value)
    assert "PROHIBITED" in str(raised.value)


def test_deletion_obligation_only_counts_when_the_terms_require_it():
    assert _policy().must_delete_on_exit is True
    assert (
        _policy(
            delete_on_cancel=Obligation.NOT_SPECIFIED,
            delete_on_downgrade=Obligation.NOT_SPECIFIED,
        ).must_delete_on_exit
        is False
    )


def test_every_dataset_declares_a_provider_and_a_minimum_plan():
    for key, spec in DATASETS.items():
        assert spec.dataset_key == key
        assert spec.provider_id
        assert spec.required_min_plan
        assert spec.extension in {"json", "csv"}


def test_an_unknown_dataset_is_an_error_not_a_default():
    with pytest.raises(KeyError, match="unknown dataset"):
        dataset("NOT_A_DATASET")


# ------------------------------------------------------------------- storage
def test_content_addressed_keys_are_derived_from_the_bytes():
    digest = sha256_hex(b"hello")
    key = content_addressed_key("jquants", "JQ_EQ_BARS_DAILY", digest, "json")
    assert key == f"raw/jquants/JQ_EQ_BARS_DAILY/{digest}.json"


def test_a_key_needs_a_real_digest():
    with pytest.raises(ValueError, match="sha256"):
        content_addressed_key("p", "d", "not-a-digest", "json")


def test_writing_the_same_bytes_twice_is_not_a_second_object(tmp_path):
    store = LocalObjectStore(tmp_path)
    first = store.put_content_addressed("p", "D", b"payload", "application/json", "json")
    again = store.put_content_addressed("p", "D", b"payload", "application/json", "json")

    assert first.created is True
    assert again.created is False
    assert first.key == again.key
    assert list(store.list("raw/")) == [first.key]


def test_the_store_refuses_to_overwrite_different_bytes(tmp_path):
    store = LocalObjectStore(tmp_path)
    stored = store.put_immutable("raw/p/D/fixed.json", b"first", "application/json")

    with pytest.raises(ImmutableObjectConflict, match="new key"):
        store.put_immutable(stored.key, b"second", "application/json")

    # and the original is untouched
    assert store.get(stored.key) == b"first"


def test_delete_reports_whether_anything_was_there(tmp_path):
    store = LocalObjectStore(tmp_path)
    stored = store.put_content_addressed("p", "D", b"payload", "application/json", "json")

    assert store.delete(stored.key) is True
    assert store.delete(stored.key) is False, "a purge treats already-absent as satisfied"
    assert store.head(stored.key) is None


def test_a_key_cannot_escape_the_store_root(tmp_path):
    store = LocalObjectStore(tmp_path)
    for key in ("../outside.json", "/etc/passwd", "raw/../../outside.json"):
        with pytest.raises(ValueError, match="escapes the store root"):
            store.put_immutable(key, b"x", "application/json")
