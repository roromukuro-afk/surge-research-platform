"""The database side of Phase 5.

Same division as ``surge.news.db``: statements and parameter dicts live here,
transaction control belongs to the caller, and nothing in this module decides
anything. The judgements - which events merge, which relation type applies, how
confident the mapping is - were all made upstream, and re-deciding them at the
write boundary is how a weaker claim quietly becomes a stronger one.

Two things the database enforces that this module deliberately does not
duplicate in Python:

``first_known_at``
    Maintained by a trigger as ``min(available_to_model_at)`` over the event's
    sources. Discovery happened when discovery happened; an official document
    fetched hours later joins as a second source with its own later time and
    cannot move the first. Writing that value from here would make it possible
    to overwrite it by accident.

the discovery/verification role check
    A source whose policy says it cannot verify cannot be linked as
    ``VERIFICATION``. That check is a trigger for the same reason.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from surge.material.models import EntityRelation, EventSecurityFeatures, MaterialEvent

INSERT_EVENT = """
insert into material.events (
  event_key, event_type, headline, scope, occurred_at, occurred_at_precision, merge_version, run_id
) values (
  %(event_key)s, %(event_type)s, %(headline)s, %(scope)s::news.source_scope,
  %(occurred_at)s, %(occurred_at_precision)s::news.time_precision, %(merge_version)s, %(run_id)s
)
on conflict (event_key, merge_version) do nothing
returning event_id
"""

SELECT_EVENT_ID = """
select event_id from material.events
where event_key = %(event_key)s and merge_version = %(merge_version)s
"""

INSERT_EVENT_SOURCE = """
insert into material.event_sources (
  event_id, document_id, source_role, available_to_model_at, source_key, match_evidence
) values (
  %(event_id)s, %(document_id)s, %(source_role)s::material.source_role,
  %(available_to_model_at)s, %(source_key)s, %(match_evidence)s::jsonb
)
on conflict (event_id, document_id) do nothing
"""

INSERT_RELATION = """
insert into material.entity_relations (
  event_id, security_id, issuer_id, relation_type, confidence,
  causal_path, evidence, extractor, extractor_version
) values (
  %(event_id)s, %(security_id)s, %(issuer_id)s,
  %(relation_type)s::material.relation_type, %(confidence)s::material.link_confidence,
  %(causal_path)s, %(evidence)s::jsonb, %(extractor)s, %(extractor_version)s
)
on conflict (event_id, security_id, issuer_id, relation_type, extractor_version) do nothing
"""

INSERT_FEATURES = """
insert into material.event_security_features (
  event_id, security_id, feature_version, knowledge_cutoff,
  novelty, novelty_method, surprise, surprise_method,
  directness, directness_method, magnitude, magnitude_method,
  persistence, persistence_method, market_reaction, market_reaction_method,
  priced_in, priced_in_method, evidence
) values (
  %(event_id)s, %(security_id)s, %(feature_version)s, %(knowledge_cutoff)s,
  %(novelty)s, %(novelty_method)s, %(surprise)s, %(surprise_method)s,
  %(directness)s, %(directness_method)s, %(magnitude)s, %(magnitude_method)s,
  %(persistence)s, %(persistence_method)s, %(market_reaction)s, %(market_reaction_method)s,
  %(priced_in)s, %(priced_in_method)s, %(evidence)s::jsonb
)
on conflict (event_id, security_id, feature_version) do nothing
"""


def _json(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def event_params(event: MaterialEvent, *, run_id: str | None = None) -> dict:
    return {
        "event_key": event.event_key,
        "event_type": event.event_type,
        "headline": event.headline,
        "scope": event.scope,
        "occurred_at": event.occurred_at,
        "occurred_at_precision": event.occurred_at_precision.value,
        "merge_version": event.merge_version,
        "run_id": run_id,
    }


def event_source_params(source, *, event_id: str, document_id: str | None = None) -> dict:
    """``document_id`` overrides the one carried on the source.

    The event source is built before the document row exists, so it carries the
    provider's own id as a stand-in. The caller passes the real ``news.documents``
    key once the insert has returned it.
    """

    return {
        "event_id": event_id,
        "document_id": document_id or source.document_id,
        "source_role": source.role.value,
        "available_to_model_at": source.available_to_model_at,
        "source_key": source.source_key,
        "match_evidence": _json(source.match_evidence),
    }


def relation_params(relation: EntityRelation, *, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "security_id": relation.security_id,
        "issuer_id": relation.issuer_id,
        "relation_type": relation.relation_type.value,
        "confidence": relation.confidence.value,
        "causal_path": relation.causal_path,
        "evidence": _json(relation.evidence),
        "extractor": relation.extractor,
        "extractor_version": relation.extractor_version,
    }


def feature_params(features: EventSecurityFeatures, *, event_id: str) -> dict:
    methods = features.methods or {}
    params = {
        "event_id": event_id,
        "security_id": features.security_id,
        "feature_version": features.feature_version,
        "knowledge_cutoff": features.knowledge_cutoff,
        "evidence": _json(features.evidence),
    }
    for name in (
        "novelty",
        "surprise",
        "directness",
        "magnitude",
        "persistence",
        "market_reaction",
        "priced_in",
    ):
        params[name] = getattr(features, name)
        params[f"{name}_method"] = methods.get(name)
    return params


def write_event(conn, event: MaterialEvent, *, run_id: str | None = None) -> str:
    """Insert the event if it is new and return its id either way.

    ``on conflict do nothing`` plus a follow-up read rather than an upsert: an
    event that already exists must keep the row it has, because its
    ``first_known_at`` records when it was first discovered and an upsert would
    be one more way to move that.
    """

    with conn.cursor() as cur:
        cur.execute(INSERT_EVENT, event_params(event, run_id=run_id))
        row = cur.fetchone()
        if row is not None:
            return str(row[0])
        cur.execute(
            SELECT_EVENT_ID,
            {"event_key": event.event_key, "merge_version": event.merge_version},
        )
        return str(cur.fetchone()[0])


def write_event_sources(
    conn,
    event: MaterialEvent,
    *,
    event_id: str,
    document_ids: dict[str, str] | None = None,
) -> None:
    """``document_ids`` maps a source's provider-side id to its documents key."""

    document_ids = document_ids or {}
    with conn.cursor() as cur:
        for source in event.sources:
            cur.execute(
                INSERT_EVENT_SOURCE,
                event_source_params(
                    source,
                    event_id=event_id,
                    document_id=document_ids.get(source.document_id),
                ),
            )


def write_relations(conn, relations: Sequence[EntityRelation], *, event_id: str) -> None:
    with conn.cursor() as cur:
        for relation in relations:
            cur.execute(INSERT_RELATION, relation_params(relation, event_id=event_id))


def write_features(conn, features: EventSecurityFeatures, *, event_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(INSERT_FEATURES, feature_params(features, event_id=event_id))
