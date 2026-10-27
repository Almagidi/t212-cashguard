"""Last outbound boundary for the optional Sentry SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sentry_sdk.envelope import Envelope, Item, PayloadRef
from sentry_sdk.transport import Transport, make_transport

from app.core.telemetry_privacy import sanitize_envelope_headers, sanitize_event

if TYPE_CHECKING:
    from sentry_sdk._types import EventDataCategory


class ConfidentialTransport(Transport):
    """Delegate only sanitized error/transaction items to the existing transport."""

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        # Preserve the SDK's configured DSN, proxy/TLS and normal transport choice.
        # Reset only the factory reference to avoid constructing this wrapper again.
        self._delegate = make_transport({**options, "transport": None})

    def capture_envelope(self, envelope: Envelope) -> None:
        try:
            if type(envelope) is not Envelope or type(envelope.items) is not list:
                return
            clean = Envelope(headers=sanitize_envelope_headers(envelope.headers))
            for item in envelope.items[:100]:
                if type(item) is not Item or type(item.headers) is not dict:
                    continue
                kind = item.headers.get("type")
                if type(kind) is not str or kind not in ("event", "transaction"):
                    continue
                if type(item.payload) is not PayloadRef:
                    continue
                event = sanitize_event(item.payload.json)
                if event is None or (event.get("type") == "transaction") != (kind == "transaction"):
                    continue
                clean.add_item(Item(payload=PayloadRef(json=event), type=kind))
            if clean.items and self._delegate is not None:
                self._delegate.capture_envelope(clean)
        except Exception:
            # No original-envelope fallback, payload log, attachment or side channel.
            return

    def flush(self, timeout: float, callback: Any = None) -> None:
        if self._delegate is not None:
            self._delegate.flush(timeout, callback)

    def kill(self) -> None:
        if self._delegate is not None:
            self._delegate.kill()

    def is_healthy(self) -> bool:
        return self._delegate.is_healthy() if self._delegate is not None else True

    def record_lost_event(
        self,
        reason: str,
        data_category: EventDataCategory | None = None,
        item: Item | None = None,
        *,
        quantity: int = 1,
    ) -> None:
        if self._delegate is not None:
            self._delegate.record_lost_event(reason, data_category, item, quantity=quantity)
