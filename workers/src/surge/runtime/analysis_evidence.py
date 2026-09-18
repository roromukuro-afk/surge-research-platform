"""The analysis provider's readiness, read from evidence rather than asserted.

Six facts, and none of them is taken from a command line when there is
something better to read:

* the credential - whether GROQ_API_KEY is in this process's environment (the
  value is never looked at, only whether it is there);
* Zero Data Retention - the dated console observation, through the same policy
  the provider enforces;
* the account's limits, the output contract and whether the real Entry request
  is accepted - from the record the live probe wrote (``surge.jobs.groq_probe``).

A fact with no evidence reads as false, with a sentence saying there is none.
That is the whole difference from the flags: a flag says whatever it is given,
and an absent record cannot claim anything.
"""

from __future__ import annotations

import os
from pathlib import Path

from surge.analysis.groq_provider import ENV_API_KEY, ENV_MODEL, policy_from_env
from surge.jobs.groq_probe import default_state_dir, read_record


def analysis_facts(env=None, *, state_dir: Path | None = None, model: str | None = None) -> dict:
    source = env if env is not None else os.environ
    model = model or (source.get(ENV_MODEL) or "").strip() or None
    state_dir = state_dir or default_state_dir()
    detail: dict[str, str] = {}

    credential = bool((source.get(ENV_API_KEY) or "").strip())
    detail["analysis_credential_configured"] = (
        f"{ENV_API_KEY} is present in this process's environment (the value was not read)"
        if credential
        else f"{ENV_API_KEY} is not in this process's environment"
    )

    policy = policy_from_env(source)
    zdr = policy.privacy_gate_passes
    detail["analysis_zdr_confirmed"] = policy.privacy_gate_detail + (
        "" if zdr else " (ANALYSIS_PRIVACY_GATE_BLOCKED)"
    )

    facts = {
        "analysis_credential_configured": credential,
        "analysis_zdr_confirmed": zdr,
        "analysis_quota_measured": False,
        "analysis_output_mode_usable": False,
        "analysis_live_smoke_passed": False,
        "analysis_request_accepted": False,
    }

    if not model:
        for step in ("analysis_quota_measured", "analysis_output_mode_usable",
                     "analysis_live_smoke_passed", "analysis_request_accepted"):
            detail[step] = f"no analysis model is configured ({ENV_MODEL} is not set)"
        facts["analysis_detail"] = detail
        return facts

    record = read_record(state_dir, model)
    if record is None:
        for step in ("analysis_quota_measured", "analysis_output_mode_usable",
                     "analysis_live_smoke_passed", "analysis_request_accepted"):
            detail[step] = f"{model}: the live probe has never been run for this model"
        facts["analysis_detail"] = detail
        return facts

    when = record.get("measured_at", "?")
    if record.get("live_request_succeeded"):
        limits = record.get("limits") or {}
        facts["analysis_quota_measured"] = True
        detail["analysis_quota_measured"] = (
            f"{model}: measured live {when}: {limits.get('max_tokens_per_minute')} tokens/minute, "
            f"{limits.get('requests_per_day')} requests/day"
        )
    else:
        detail["analysis_quota_measured"] = f"{model}: the live probe failed ({record.get('error')})"

    smoke = record.get("contract_smoke_passed")
    if smoke is True:
        facts["analysis_live_smoke_passed"] = True
        facts["analysis_output_mode_usable"] = True
        facts["analysis_request_accepted"] = True
        detail["analysis_live_smoke_passed"] = (
            f"{model}: a live Entry request returned a schema-valid "
            f"{record.get('contract_smoke_returned_state')} ({record.get('contract_smoke_at')})"
        )
        detail["analysis_output_mode_usable"] = (
            f"{model}: {record.get('output_mode')} carried the Entry contract live"
        )
        detail["analysis_request_accepted"] = f"{model}: the real Entry request was accepted"
    else:
        why = (
            record.get("contract_smoke_error")
            or record.get("contract_smoke_skipped")
            or "the live contract smoke has not been run"
        )
        detail["analysis_live_smoke_passed"] = f"{model}: {why}"
        detail["analysis_output_mode_usable"] = (
            f"{model}: {record.get('output_mode')} has not carried the Entry contract live - {why}"
        )
        refused = "413" in why or "request_too_large" in why or record.get("entry_request_fits") is False
        detail["analysis_request_accepted"] = (
            f"{model}: the real Entry request is refused by this account - {why}"
            if refused
            else f"{model}: acceptance of the real Entry request is unproven - {why}"
        )

    facts["analysis_detail"] = detail
    return facts


__all__ = ["analysis_facts"]
