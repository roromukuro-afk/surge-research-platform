"""Writing an outcome, which also means closing the episode.

There is one entry point and it calls one database function, because closing an
episode and recording how it ended are the same event. Two statements in a
caller's transaction would be equivalent when the caller remembers; a single
function makes a closed episode without an outcome unrepresentable.
"""

from __future__ import annotations

from surge.outcome.models import OutcomeReport

CLOSE_WITH_OUTCOME = """
select prod.close_episode_with_outcome(
  %(episode_id)s::uuid,
  %(primary_outcome)s::prod.episode_close_reason,
  %(closed_at)s,
  %(path_resolution)s::prod.path_resolution,
  %(granularity)s,
  %(resolved_session_index)s,
  %(resolved_at)s,
  %(primary_detail)s,
  %(counterfactual_path)s::prod.path_resolution,
  %(later_target_hit)s,
  %(later_target_hit_at)s,
  %(later_target_hit_session_index)s,
  %(mfe)s,
  %(mae)s,
  %(sessions_observed)s,
  %(corporate_action_ids)s,
  %(outcome_currency)s,
  %(engine_version)s,
  %(label_version)s,
  %(notes)s
)
"""


def outcome_params(report: OutcomeReport, *, closed_at, label_version: str | None = None) -> dict:
    primary = report.primary
    counterfactual = report.counterfactual
    return {
        "episode_id": report.episode_id,
        "primary_outcome": primary.verdict.value,
        "closed_at": closed_at,
        "path_resolution": primary.path_resolution.value if primary.path_resolution else None,
        "granularity": primary.granularity.value if primary.granularity else None,
        "resolved_session_index": primary.resolved_session_index,
        "resolved_at": primary.resolved_at,
        "primary_detail": primary.detail,
        "counterfactual_path": (
            counterfactual.path_resolution.value if counterfactual.path_resolution else None
        ),
        "later_target_hit": counterfactual.later_target_hit,
        "later_target_hit_at": counterfactual.later_target_hit_at,
        "later_target_hit_session_index": counterfactual.later_target_hit_session_index,
        "mfe": counterfactual.mfe,
        "mae": counterfactual.mae,
        "sessions_observed": counterfactual.sessions_observed,
        "corporate_action_ids": list(report.corporate_action_ids_applied),
        "outcome_currency": report.currency,
        "engine_version": report.engine_version,
        "label_version": label_version,
        "notes": "\n".join(report.notes) or None,
    }


def close_with_outcome(conn, report: OutcomeReport, *, closed_at, label_version=None) -> str:
    with conn.cursor() as cur:
        cur.execute(
            CLOSE_WITH_OUTCOME,
            outcome_params(report, closed_at=closed_at, label_version=label_version),
        )
        return str(cur.fetchone()[0])
