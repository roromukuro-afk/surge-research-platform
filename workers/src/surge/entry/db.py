"""The database side of Phase 8.

Statements and parameter dicts; the caller owns the transaction. One entry
decision commits once, so a prediction and the episode it belongs to are either
both visible or neither is.

There is deliberately no ``close_episode`` here. An episode closes through
``surge.outcome.db.close_with_outcome``, which calls the one database function
that closes it and records how it ended in the same breath - and the runtime no
longer has UPDATE on ``prod.episodes`` at all, so a second path would simply
fail.

The guards live in the database rather than here, and this module is written on
the assumption that they will fire. It does not pre-check the 3,000 yen limit or
the target arithmetic before inserting, because a Python check that agrees with
a database check adds nothing and a Python check that disagrees with one hides
the disagreement.
"""

from __future__ import annotations

from surge.entry.models import (
    EntryAttempt,
    Episode,
    Prediction,
    TransitionKind,
    WatchState,
)

INSERT_SETUP = """
insert into prod.setups (
  security_id, as_of_date, state, analysis_kind, stage3_output_id,
  signal_reference_price, signal_reference_currency,
  price_cutoff_at, knowledge_cutoff_at, priced_in_status,
  thesis_key, rationale, verification, run_id
) values (
  %(security_id)s, %(as_of_date)s, %(state)s::prod.decision_state,
  %(analysis_kind)s::prod.analysis_kind, %(stage3_output_id)s,
  %(signal_reference_price)s, %(signal_reference_currency)s,
  %(price_cutoff_at)s, %(knowledge_cutoff_at)s, %(priced_in_status)s,
  %(thesis_key)s, %(rationale)s, %(verification)s::prod.verification_status, %(run_id)s
)
on conflict (security_id, as_of_date, state, analysis_kind) do nothing
returning setup_id
"""

INSERT_WATCH = """
insert into prod.watches (
  watch_id, setup_id, security_id, state,
  trigger_description, trigger_price, trigger_currency, expires_after_date, verification
) values (
  coalesce(%(watch_id)s::uuid, gen_random_uuid()), %(setup_id)s, %(security_id)s,
  %(state)s::prod.watch_state,
  %(trigger_description)s, %(trigger_price)s, %(trigger_currency)s, %(expires_after_date)s,
  %(verification)s::prod.verification_status
)
returning watch_id
"""

INSERT_WATCH_TRANSITION = """
insert into prod.watch_transitions (
  watch_id, from_state, to_state, occurred_at,
  observed_price, observed_price_at, analysis_kind, note
) values (
  %(watch_id)s, %(from_state)s::prod.watch_state, %(to_state)s::prod.watch_state, %(occurred_at)s,
  %(observed_price)s, %(observed_price_at)s, %(analysis_kind)s::prod.analysis_kind, %(note)s
)
returning transition_id
"""

INSERT_ENTRY_ATTEMPT = """
insert into prod.entry_attempts (
  security_id, setup_id, watch_id, status, analysis_kind,
  decision_cutoff_at, decision_completed_at,
  decision_price, decision_price_observed_at, decision_price_currency,
  decision_price_jpy, decision_fx_rate, decision_fx_observed_at,
  entry_reference_price, entry_price_observed_at, entry_price_method,
  entry_price_jpy, entry_fx_rate, entry_fx_observed_at,
  universe_decision, universe_reason_code, thesis_key, reject_reason,
  provider_id, verification, run_id
) values (
  %(security_id)s, %(setup_id)s, %(watch_id)s,
  %(status)s::prod.entry_attempt_status, %(analysis_kind)s::prod.analysis_kind,
  %(decision_cutoff_at)s, %(decision_completed_at)s,
  %(decision_price)s, %(decision_price_observed_at)s, %(decision_price_currency)s,
  %(decision_price_jpy)s, %(decision_fx_rate)s, %(decision_fx_observed_at)s,
  %(entry_reference_price)s, %(entry_price_observed_at)s, %(entry_price_method)s,
  %(entry_price_jpy)s, %(entry_fx_rate)s, %(entry_fx_observed_at)s,
  %(universe_decision)s::universe.decision, %(universe_reason_code)s, %(thesis_key)s,
  %(reject_reason)s, %(provider_id)s, %(verification)s::prod.verification_status, %(run_id)s
)
returning attempt_id
"""

INSERT_EPISODE = """
insert into prod.episodes (
  security_id, thesis_key, status, opened_at, entry_price_observed_at,
  horizon_sessions, verification
) values (
  %(security_id)s, %(thesis_key)s, 'OPEN', %(opened_at)s, %(entry_price_observed_at)s,
  %(horizon_sessions)s, %(verification)s::prod.verification_status
)
returning episode_id
"""

INSERT_PREDICTION = """
insert into prod.predictions (
  episode_id, attempt_id, security_id, thesis_key, analysis_kind, state,
  entry_reference_price, entry_price_observed_at, entry_price_currency,
  entry_price_method, entry_price_jpy,
  decision_price, decision_price_observed_at, decision_price_jpy,
  initial_failure_line, target_price, data_cutoff, source_setup_ids,
  provider_id, provider_kind, model_id, prompt_sha256, bundle_sha256,
  canonical_prompt_sha256, rule_version, label_version,
  universe_decision, verification, run_id
) values (
  %(episode_id)s, %(attempt_id)s, %(security_id)s, %(thesis_key)s,
  %(analysis_kind)s::prod.analysis_kind, %(state)s::prod.decision_state,
  %(entry_reference_price)s, %(entry_price_observed_at)s, %(entry_price_currency)s,
  %(entry_price_method)s, %(entry_price_jpy)s,
  %(decision_price)s, %(decision_price_observed_at)s, %(decision_price_jpy)s,
  %(initial_failure_line)s, %(target_price)s, %(data_cutoff)s, %(source_setup_ids)s,
  %(provider_id)s, %(provider_kind)s, %(model_id)s, %(prompt_sha256)s, %(bundle_sha256)s,
  %(canonical_prompt_sha256)s, %(rule_version)s, %(label_version)s,
  %(universe_decision)s::universe.decision, %(verification)s::prod.verification_status, %(run_id)s
)
returning prediction_id
"""

INSERT_STATE_TRANSITION = """
insert into prod.state_transitions (
  episode_id, kind, occurred_at, session_index, observed_price, note, analysis_kind
) values (
  %(episode_id)s, %(kind)s::prod.transition_kind, %(occurred_at)s, %(session_index)s,
  %(observed_price)s, %(note)s, %(analysis_kind)s::prod.analysis_kind
)
returning transition_id
"""

INSERT_RISK_LINE_UPDATE = """
insert into prod.risk_line_updates (
  episode_id, transition_id, risk_line, previous_risk_line, reason, effective_at
) values (
  %(episode_id)s, %(transition_id)s, %(risk_line)s, %(previous_risk_line)s,
  %(reason)s, %(effective_at)s
)
returning update_id
"""

SELECT_OPEN_EPISODE = """
select episode_id::text, entry_price_observed_at, opened_at
from prod.episodes
where security_id = %(security_id)s and thesis_key = %(thesis_key)s and status = 'OPEN'
"""


def attempt_params(attempt: EntryAttempt) -> dict:
    decision = attempt.decision_price
    entry = attempt.entry_price
    return {
        "security_id": attempt.security_id,
        "setup_id": attempt.setup_id,
        "watch_id": attempt.watch_id,
        "status": attempt.status.value,
        "analysis_kind": attempt.analysis_kind.value,
        "decision_cutoff_at": attempt.decision_cutoff_at,
        "decision_completed_at": attempt.decision_completed_at,
        "decision_price": decision.amount if decision else None,
        "decision_price_observed_at": decision.observed_at if decision else None,
        "decision_price_currency": decision.currency if decision else None,
        "decision_price_jpy": decision.jpy if decision else None,
        "decision_fx_rate": decision.fx_rate if decision else None,
        "decision_fx_observed_at": decision.fx_observed_at if decision else None,
        "entry_reference_price": entry.amount if entry else None,
        "entry_price_observed_at": entry.observed_at if entry else None,
        "entry_price_method": attempt.entry_price_method,
        "entry_price_jpy": entry.jpy if entry else None,
        "entry_fx_rate": entry.fx_rate if entry else None,
        "entry_fx_observed_at": entry.fx_observed_at if entry else None,
        "universe_decision": attempt.universe_decision,
        "universe_reason_code": attempt.universe_reason_code,
        "thesis_key": attempt.thesis_key,
        "reject_reason": attempt.reject_reason,
        "provider_id": attempt.provider_id,
        "verification": attempt.verification.value,
        "run_id": attempt.run_id,
    }


def prediction_params(
    prediction: Prediction, *, episode_id: str, attempt_id: str, label_version: str | None = None
) -> dict:
    return {
        "episode_id": episode_id,
        "attempt_id": attempt_id,
        "security_id": prediction.security_id,
        "thesis_key": prediction.thesis_key,
        "analysis_kind": prediction.analysis_kind.value,
        "state": prediction.state.value,
        "entry_reference_price": prediction.entry_reference_price,
        "entry_price_observed_at": prediction.entry_price_observed_at,
        "entry_price_currency": prediction.entry_price_currency,
        "entry_price_method": prediction.entry_price_method,
        "entry_price_jpy": prediction.entry_price_jpy,
        "decision_price": prediction.decision_price,
        "decision_price_observed_at": prediction.decision_price_observed_at,
        "decision_price_jpy": prediction.decision_price_jpy,
        "initial_failure_line": prediction.initial_failure_line,
        "target_price": prediction.target_price,
        "data_cutoff": prediction.data_cutoff,
        "source_setup_ids": list(prediction.source_setup_ids),
        "provider_id": prediction.provider_id,
        "provider_kind": prediction.provider_kind,
        "model_id": prediction.model_id,
        "prompt_sha256": prediction.prompt_sha256,
        "bundle_sha256": prediction.bundle_sha256,
        "canonical_prompt_sha256": prediction.canonical_prompt_sha256,
        "rule_version": prediction.rule_version,
        "label_version": label_version,
        "universe_decision": prediction.universe_decision,
        "verification": prediction.verification.value,
        "run_id": prediction.run_id,
    }


def episode_params(episode: Episode) -> dict:
    return {
        "security_id": episode.security_id,
        "thesis_key": episode.thesis_key,
        "opened_at": episode.opened_at,
        "entry_price_observed_at": episode.entry_price_observed_at,
        "horizon_sessions": episode.horizon_sessions,
        # Carried, not asserted. Hard-coding this was how an episode could
        # disagree with the prediction inside it; the database now refuses the
        # pair when they differ.
        "verification": episode.verification.value,
    }


def write_attempt(conn, attempt: EntryAttempt) -> str:
    with conn.cursor() as cur:
        cur.execute(INSERT_ENTRY_ATTEMPT, attempt_params(attempt))
        return str(cur.fetchone()[0])


def read_open_episode(conn, *, security_id: str, thesis_key: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(SELECT_OPEN_EPISODE, {"security_id": security_id, "thesis_key": thesis_key})
        row = cur.fetchone()
    if row is None:
        return None
    return {"episode_id": row[0], "entry_price_observed_at": row[1], "opened_at": row[2]}


def write_episode(conn, episode: Episode) -> str:
    with conn.cursor() as cur:
        cur.execute(INSERT_EPISODE, episode_params(episode))
        return str(cur.fetchone()[0])


def write_prediction(
    conn, prediction: Prediction, *, episode_id: str, attempt_id: str, label_version=None
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            INSERT_PREDICTION,
            prediction_params(
                prediction,
                episode_id=episode_id,
                attempt_id=attempt_id,
                label_version=label_version,
            ),
        )
        return str(cur.fetchone()[0])


def write_transition(
    conn,
    *,
    episode_id: str,
    kind: TransitionKind,
    occurred_at,
    session_index: int | None = None,
    observed_price=None,
    note: str | None = None,
    analysis_kind=None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            INSERT_STATE_TRANSITION,
            {
                "episode_id": episode_id,
                "kind": kind.value,
                "occurred_at": occurred_at,
                "session_index": session_index,
                "observed_price": observed_price,
                "note": note,
                "analysis_kind": analysis_kind.value if analysis_kind else None,
            },
        )
        return int(cur.fetchone()[0])


def write_risk_line(
    conn,
    *,
    episode_id: str,
    risk_line,
    reason: str,
    effective_at,
    previous_risk_line=None,
    transition_id: int | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            INSERT_RISK_LINE_UPDATE,
            {
                "episode_id": episode_id,
                "transition_id": transition_id,
                "risk_line": risk_line,
                "previous_risk_line": previous_risk_line,
                "reason": reason,
                "effective_at": effective_at,
            },
        )
        return int(cur.fetchone()[0])


def write_watch_transition(
    conn,
    *,
    watch_id: str,
    from_state: WatchState | None,
    to_state: WatchState,
    occurred_at,
    observed_price=None,
    observed_price_at=None,
    analysis_kind=None,
    note: str | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            INSERT_WATCH_TRANSITION,
            {
                "watch_id": watch_id,
                "from_state": from_state.value if from_state else None,
                "to_state": to_state.value,
                "occurred_at": occurred_at,
                "observed_price": observed_price,
                "observed_price_at": observed_price_at,
                "analysis_kind": analysis_kind.value if analysis_kind else None,
                "note": note,
            },
        )
        return int(cur.fetchone()[0])
