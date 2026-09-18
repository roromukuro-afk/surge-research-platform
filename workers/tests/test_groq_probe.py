"""The probe's record: what "fits" may and may not be taken to mean.

The quota probe is stubbed - these pin how its result is reported, not Groq.
"""

from __future__ import annotations

import pytest

from surge.analysis.groq_provider import GroqHostedProvider, Quota
from surge.jobs import groq_probe

ENV = {"GROQ_API_KEY": "gsk_" + "x" * 32, "GROQ_ZDR_CONFIRMED_ON": "2026-09-01"}


@pytest.fixture
def generous_headers(monkeypatch):
    """A 70K-per-minute allowance - what compound's own headers report."""

    monkeypatch.setattr(
        GroqHostedProvider,
        "quota_probe",
        lambda self: Quota(max_tokens_per_minute=70_000, requests_per_day=250),
    )


def test_a_compound_fit_is_not_reported_as_evidence_of_acceptance(generous_headers):
    """Measured (D-256): compound's headers describe the system, while the
    models it calls - which its usage breakdown shows receiving the whole
    prompt - keep limits no header shows. The Entry request "fit" those headers
    and was refused with 413."""

    record = groq_probe.probe_model("groq/compound", env=ENV)

    assert record["entry_request_fits"] is True
    assert "only the contract smoke can say" in record["entry_request_reason"]
    assert record["probe_version"] == groq_probe.PROBE_VERSION


def test_a_single_model_fit_carries_no_compound_caveat(generous_headers):
    record = groq_probe.probe_model("meta-llama/llama-4-scout-17b-16e-instruct", env=ENV)

    assert record["entry_request_fits"] is True
    assert "compound" not in record["entry_request_reason"]
