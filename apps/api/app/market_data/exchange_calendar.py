"""Deterministic regular-session calendar contracts for supported exchanges."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo

from holidays import financial_holidays
from holidays.constants import HALF_DAY, PUBLIC


class SessionClassification(StrEnum):
    """How a timestamp relates to the exchange's regular trading sessions."""

    REGULAR_SESSION = "regular_session"
    OUT_OF_SESSION = "out_of_session"
    NON_SESSION_DAY = "non_session_day"


@dataclass(frozen=True, slots=True)
class TradingSession:
    """One immutable regular trading session in exchange-local time."""

    venue: str
    session_id: str
    local_date: date
    exchange_timezone: str
    open_at: datetime
    close_at: datetime
    is_early_close: bool


class XNYSCalendar:
    """Regular-hours NYSE calendar backed by pinned XNYS holiday rules."""

    venue = "XNYS"
    exchange_timezone = "America/New_York"
    supports_extended_hours = False
    supported_from = date(1993, 1, 1)
    supported_through = date(2100, 12, 31)
    _timezone = ZoneInfo(exchange_timezone)
    _regular_open = time(9, 30)
    _regular_close = time(16, 0)
    _early_close = time(13, 0)

    def _validate_date_range(self, start: date, end: date) -> None:
        if start > end:
            raise ValueError("start must be on or before end")
        self._validate_supported_date(start)
        self._validate_supported_date(end)

    def _validate_supported_date(self, local_date: date) -> None:
        if local_date < self.supported_from:
            raise ValueError(
                f"XNYS regular-session semantics are supported from {self.supported_from}"
            )
        if local_date > self.supported_through:
            raise ValueError(
                f"XNYS regular-session semantics are supported through {self.supported_through}"
            )

    @staticmethod
    def _validate_timestamp(timestamp: datetime) -> None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

    @staticmethod
    @lru_cache(maxsize=256)
    def _public_holidays(year: int) -> frozenset[date]:
        calendar = financial_holidays("XNYS", years=year, categories=(PUBLIC,))
        return frozenset(calendar)

    @staticmethod
    @lru_cache(maxsize=256)
    def _half_days(year: int) -> frozenset[date]:
        calendar = financial_holidays("XNYS", years=year, categories=(HALF_DAY,))
        return frozenset(calendar)

    def _session_on(self, local_date: date) -> TradingSession | None:
        self._validate_supported_date(local_date)
        if local_date.weekday() >= 5 or local_date in self._public_holidays(local_date.year):
            return None

        early_close = local_date in self._half_days(local_date.year)
        close_time = self._early_close if early_close else self._regular_close
        return TradingSession(
            venue=self.venue,
            session_id=f"{self.venue}:{local_date.isoformat()}",
            local_date=local_date,
            exchange_timezone=self.exchange_timezone,
            open_at=datetime.combine(local_date, self._regular_open, self._timezone),
            close_at=datetime.combine(local_date, close_time, self._timezone),
            is_early_close=early_close,
        )

    def expected_sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        """Return all regular sessions in the inclusive exchange-local date range."""

        self._validate_date_range(start, end)
        sessions: list[TradingSession] = []
        cursor = start
        while cursor <= end:
            session = self._session_on(cursor)
            if session is not None:
                sessions.append(session)
            cursor += timedelta(days=1)
        return tuple(sessions)

    def session_for_timestamp(self, timestamp: datetime) -> TradingSession | None:
        """Map an aware timestamp to a regular session, excluding extended hours."""

        self._validate_timestamp(timestamp)
        local_timestamp = timestamp.astimezone(self._timezone)
        session = self._session_on(local_timestamp.date())
        if session is None:
            return None
        if session.open_at <= local_timestamp < session.close_at:
            return session
        return None

    def classify_timestamp(self, timestamp: datetime) -> SessionClassification:
        """Classify regular, out-of-session, and non-session-day timestamps."""

        self._validate_timestamp(timestamp)
        local_timestamp = timestamp.astimezone(self._timezone)
        session = self._session_on(local_timestamp.date())
        if session is None:
            return SessionClassification.NON_SESSION_DAY
        if session.open_at <= local_timestamp < session.close_at:
            return SessionClassification.REGULAR_SESSION
        return SessionClassification.OUT_OF_SESSION

    def session_open(self, session: TradingSession) -> datetime:
        self._validate_session(session)
        return session.open_at.astimezone(UTC)

    def session_close(self, session: TradingSession) -> datetime:
        self._validate_session(session)
        return session.close_at.astimezone(UTC)

    def is_early_close(self, session: TradingSession) -> bool:
        self._validate_session(session)
        return session.is_early_close

    def is_terminal_bar(
        self,
        session: TradingSession,
        timestamp: datetime,
        *,
        interval_minutes: int,
    ) -> bool:
        """Return whether ``timestamp`` opens the session's final regular bar."""
        self._validate_session(session)
        self._validate_timestamp(timestamp)
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        expected = self.session_close(session) - timedelta(minutes=interval_minutes)
        return timestamp.astimezone(UTC) == expected

    def to_reference_session_clock(
        self,
        timestamp: datetime,
        *,
        reference_open_utc: str,
    ) -> datetime:
        """Map a regular bar to a stable session-relative UTC clock.

        Strategy parameters historically use winter UTC values (14:30 for the
        XNYS open). Mapping by elapsed exchange-session time preserves those
        parameter meanings across DST without changing the source timestamp.
        """
        session = self.session_for_timestamp(timestamp)
        if session is None:
            raise ValueError("timestamp must be inside an XNYS regular session")
        try:
            hours, minutes = map(int, reference_open_utc.split(":"))
            reference_open = datetime.combine(
                session.local_date,
                time(hours, minutes),
                UTC,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("reference_open_utc must use HH:MM") from exc
        elapsed = timestamp.astimezone(UTC) - self.session_open(session)
        return reference_open + elapsed

    def previous_session(self, session: TradingSession) -> TradingSession:
        self._validate_session(session)
        return self._adjacent_session(session.local_date, direction=-1)

    def next_session(self, session: TradingSession) -> TradingSession:
        self._validate_session(session)
        return self._adjacent_session(session.local_date, direction=1)

    def _validate_session(self, session: TradingSession) -> None:
        if session.venue != self.venue:
            raise ValueError(f"Session {session.session_id!r} does not belong to {self.venue}")

    def _adjacent_session(self, local_date: date, *, direction: int) -> TradingSession:
        cursor = local_date + timedelta(days=direction)
        while True:
            if cursor < self.supported_from:
                raise ValueError(f"No prior supported XNYS session before {self.supported_from}")
            if cursor > self.supported_through:
                raise ValueError(f"No next supported XNYS session after {self.supported_through}")
            session = self._session_on(cursor)
            if session is not None:
                return session
            cursor += timedelta(days=direction)


_XNYS_CALENDAR = XNYSCalendar()


def calendar_for_venue(venue: str) -> XNYSCalendar:
    """Return the explicit calendar for a supported exchange identifier."""

    normalized = venue.strip().upper()
    if normalized in {"XNYS", "NYSE"}:
        return _XNYS_CALENDAR
    raise ValueError(f"Unsupported exchange calendar: {venue!r}")
