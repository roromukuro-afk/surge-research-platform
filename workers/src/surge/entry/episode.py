"""Episodes, horizons and the two failure lines.

An episode is one security under one thesis, from the first entry to the close.
Scoring counts episodes rather than predictions, because a reaffirmed entry is
the same claim stated twice and counting it twice would make repetition look
like accuracy.

Two numbers in this module are deliberately kept apart:

``initial_failure_line``
    Fixed when the prediction is made. The primary outcome, the teacher label
    and ``INITIAL_FAILURE_HIT`` are all judged against it. It lives on the
    prediction and this module refuses to change it.
``current_risk_line``
    May move on reanalysis. Lives in its own append-only list. Touching it never
    closes an episode - which is what makes it safe to move, and useless for
    scoring.

The horizon is 20 trading sessions from S0, where S0 is the session in which the
entry price was observed. Sessions are counted from sessions that actually
traded rather than predicted from a calendar: there is no verified trading
calendar yet (D-135), and a horizon boundary computed from a guessed holiday
table would be a fabricated date on a real record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from surge.entry.models import (
    PRIMARY_HORIZON_SESSIONS,
    EntryError,
    Episode,
    EpisodeCloseReason,
    EpisodeStatus,
    Prediction,
    SessionCalendarUnknown,
    TransitionKind,
)

EPISODE_VERSION = "episode-1.0.0"


@dataclass(frozen=True)
class RiskLineUpdate:
    risk_line: Decimal
    reason: str
    effective_at: datetime
    previous_risk_line: Decimal | None = None


@dataclass
class EpisodeBook:
    """The open episodes, keyed the way the deduplication rule reads them.

    One open episode per (security, thesis). The database holds the same rule as
    a partial unique index, because the code that would check it here is the code
    that would be racing with itself.
    """

    episodes: dict[str, Episode] = field(default_factory=dict)
    risk_lines: dict[str, list[RiskLineUpdate]] = field(default_factory=dict)

    @staticmethod
    def key(security_id: str, thesis_key: str) -> str:
        return f"{security_id}::{thesis_key}"

    def open_for(self, security_id: str, thesis_key: str) -> Episode | None:
        episode = self.episodes.get(self.key(security_id, thesis_key))
        if episode is not None and episode.status is EpisodeStatus.OPEN:
            return episode
        return None

    def open_episode(self, prediction: Prediction, *, episode_id: str, at: datetime) -> Episode:
        """Start an episode from a prediction.

        Refuses if one is already open under the same thesis. That case is not an
        error in the pipeline - it is the ordinary result of the same setup
        firing twice - but it has to come through
        :meth:`reaffirm`, which records the fact without restarting anything.
        """

        existing = self.open_for(prediction.security_id, prediction.thesis_key)
        if existing is not None:
            raise EntryError(
                f"episode {existing.episode_id} is already open for {prediction.security_id} under "
                f"thesis {prediction.thesis_key!r}; record a REAFFIRMED transition instead of "
                "opening a second one"
            )

        episode = Episode(
            episode_id=episode_id,
            security_id=prediction.security_id,
            thesis_key=prediction.thesis_key,
            # S0 is where the horizon starts: the moment the entry price was
            # observed, not the setup and not the watch.
            entry_price_observed_at=prediction.entry_price_observed_at,
            opened_at=at,
            horizon_sessions=PRIMARY_HORIZON_SESSIONS,
        )
        episode.record(TransitionKind.EPISODE_OPENED, at, f"prediction at {prediction.entry_reference_price}")
        self.episodes[self.key(prediction.security_id, prediction.thesis_key)] = episode
        # The risk line starts where the failure line is. They diverge only when
        # something moves one of them, and only one of them can move.
        self.risk_lines[episode_id] = [
            RiskLineUpdate(
                risk_line=prediction.initial_failure_line,
                reason="initial: equal to the prediction's initial_failure_line",
                effective_at=prediction.entry_price_observed_at,
            )
        ]
        return episode

    def reaffirm(self, episode: Episode, *, at: datetime, note: str | None = None) -> Episode:
        """The same thesis fired again. Nothing restarts."""

        if episode.status is not EpisodeStatus.OPEN:
            raise EntryError(f"episode {episode.episode_id} is closed and cannot be reaffirmed")
        episode.record(
            TransitionKind.REAFFIRMED,
            at,
            note or "same thesis re-entered; horizon unchanged, no new prediction",
        )
        return episode

    def move_risk_line(
        self, episode: Episode, *, to: Decimal, reason: str, at: datetime
    ) -> RiskLineUpdate:
        """Move the operational line. The scored line is not touched."""

        if to <= 0:
            raise EntryError("a risk line must be positive")
        history = self.risk_lines.setdefault(episode.episode_id, [])
        previous = history[-1].risk_line if history else None
        update = RiskLineUpdate(
            risk_line=to, reason=reason, effective_at=at, previous_risk_line=previous
        )
        history.append(update)
        episode.record(TransitionKind.RISK_LINE_MOVED, at, f"{previous} -> {to}: {reason}")
        return update

    def current_risk_line(self, episode: Episode) -> Decimal | None:
        history = self.risk_lines.get(episode.episode_id) or []
        return history[-1].risk_line if history else None

    def risk_line_hit(self, episode: Episode, *, at: datetime, price: Decimal) -> None:
        """Record that the operational line was touched.

        Explicitly not a close. The episode is scored against
        ``initial_failure_line``, and letting the movable line end it would make
        the scored line decorative.
        """

        episode.record(
            TransitionKind.RISK_LINE_HIT,
            at,
            f"price {price} reached the current risk line; the episode stays open because the "
            "primary outcome is judged against initial_failure_line",
        )

    def close(
        self, episode: Episode, *, reason: EpisodeCloseReason, at: datetime
    ) -> Episode:
        episode.close(reason, at)
        return episode


class Horizon:
    """S0..S20 over the sessions that actually traded.

    Deliberately not a calendar. Asking for a session that has not been observed
    raises instead of extrapolating, so a horizon boundary is either a date that
    happened or an explicit unknown.
    """

    def __init__(self, entry_at: datetime, sessions: list[date]) -> None:
        self.entry_at = entry_at
        entry_date = entry_at.date()
        self._sessions = [d for d in sorted(set(sessions)) if d >= entry_date]

    @property
    def observed(self) -> int:
        return len(self._sessions)

    def session(self, index: int) -> date:
        if index < 0:
            raise ValueError("session indices start at S0, the entry session")
        if index >= len(self._sessions):
            raise SessionCalendarUnknown(requested_index=index, observed_sessions=len(self._sessions))
        return self._sessions[index]

    def index_of(self, day: date) -> int | None:
        try:
            return self._sessions.index(day)
        except ValueError:
            return None

    @property
    def last_session(self) -> date | None:
        """S20's date, or None while it is still in the future."""

        try:
            return self.session(PRIMARY_HORIZON_SESSIONS)
        except SessionCalendarUnknown:
            return None

    @property
    def is_complete(self) -> bool:
        return self.last_session is not None

    def has_expired_by(self, day: date) -> bool:
        last = self.last_session
        return last is not None and day > last


__all__ = [
    "EPISODE_VERSION",
    "EpisodeBook",
    "Horizon",
    "RiskLineUpdate",
]
