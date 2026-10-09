"""Immutable, content-addressed storage for historical research datasets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.backtest.data_contract import validate_bar_series
from app.market_data.exchange_calendar import calendar_for_venue
from app.strategies.indicators import Bar

SCHEMA_VERSION = 1
_BAR_FIELDS = ("t", "o", "h", "l", "c", "v")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


def _identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if _IDENTIFIER.fullmatch(cleaned) is None:
        raise ValueError(f"{label} source metadata must be a secret-free identifier")
    return cleaned


@dataclass(frozen=True, slots=True)
class DatasetRequest:
    provider: str
    source_id: str
    source_version: str
    symbols: tuple[str, ...]
    start: date
    end: date
    multiplier: int
    timespan: str
    venue: str
    timezone: str
    adjustment_mode: str
    corporate_action_policy: str
    membership_source: str
    limit: int = 50_000
    universe: tuple[str, ...] | None = None
    source_sha256: str | None = None

    @staticmethod
    def _symbols(values: tuple[str, ...]) -> list[str]:
        symbols = sorted({value.strip().upper() for value in values})
        if not symbols or any(
            re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", item) is None for item in symbols
        ):
            raise ValueError("dataset symbols must be valid XNYS tickers")
        return symbols

    def as_dict(self) -> dict[str, Any]:
        if self.start > self.end:
            raise ValueError("dataset start must be on or before end")
        timespan = self.timespan.strip().lower()
        if self.multiplier <= 0 or self.limit <= 0 or timespan not in {"minute", "day"}:
            raise ValueError("dataset interval and limit must be supported and positive")
        symbols = self._symbols(self.symbols)
        if len(symbols) != 1:
            raise ValueError("one symbol is required per dataset object")
        source_sha = self.source_sha256.lower() if self.source_sha256 else None
        if source_sha is not None and re.fullmatch(r"[0-9a-f]{64}", source_sha) is None:
            raise ValueError("source_sha256 must be a full hexadecimal SHA-256")
        return {
            "provider": _identifier(self.provider, "provider").lower(),
            "source": {
                "id": _identifier(self.source_id, "source"),
                "version": _identifier(self.source_version, "source version"),
                "original_sha256": source_sha,
            },
            "symbols": symbols,
            "universe": self._symbols(self.universe or self.symbols),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "multiplier": self.multiplier,
            "timespan": timespan,
            "venue": self.venue.strip().upper(),
            "timezone": self.timezone,
            "adjustment_mode": _identifier(self.adjustment_mode, "adjustment"),
            "corporate_action_policy": _identifier(self.corporate_action_policy, "policy"),
            "membership_source": _identifier(self.membership_source, "membership"),
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class DatasetHandle:
    bars: list[Bar]
    times: list[datetime]
    manifest: dict[str, Any]
    content_id: str
    manifest_id: str


def _decimal_text(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric bar value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("bar values must be finite")
    rendered = format(number.normalize(), "f")
    return "0" if number == 0 else rendered


def _canonical_records(rows: list[dict[str, Any]]) -> list[dict[str, int | str]]:
    records: list[dict[str, int | str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(_BAR_FIELDS) - row.keys():
            raise ValueError(f"bar row {index} missing required fields")
        timestamp = Decimal(_decimal_text(row["t"]))
        if timestamp != timestamp.to_integral_value():
            raise ValueError(f"bar row {index} timestamp must be an integer")
        records.append(
            {"t": int(timestamp), **{field: _decimal_text(row[field]) for field in _BAR_FIELDS[1:]}}
        )
    return sorted(records, key=lambda record: int(record["t"]))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(rows: list[dict[str, Any]]) -> str:
    return _json_bytes(_canonical_records(rows)).decode()


class ImmutableDatasetCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def request_id(self, request: DatasetRequest) -> str:
        return _sha256(_json_bytes({"schema_version": SCHEMA_VERSION, **request.as_dict()}))

    def reference_path(self, request: DatasetRequest) -> Path:
        return self.root / "requests" / f"{self.request_id(request)}.json"

    def object_path(self, identity: str) -> Path:
        return self.root / "objects" / identity[:2] / f"{identity}.json"

    def _manifest_path(self, identity: str) -> Path:
        return self.root / "manifests" / identity[:2] / f"{identity}.json"

    @staticmethod
    def _provenance(request: DatasetRequest, content_id: str) -> dict[str, Any]:
        data = request.as_dict()
        return {
            "provider": data["provider"],
            "source": data["source"],
            "request_parameters": {
                "multiplier": data["multiplier"],
                "timespan": data["timespan"],
                "sort": "asc",
                "limit": data["limit"],
            },
            "venue": data["venue"],
            "calendar": data["venue"],
            "timezone": data["timezone"],
            "interval": f"{data['multiplier']}-{data['timespan']}",
            "symbols": data["symbols"],
            "universe": data["universe"],
            "membership_source": data["membership_source"],
            "start": data["start"],
            "end": data["end"],
            "adjustment_mode": data["adjustment_mode"],
            "corporate_action_policy": data["corporate_action_policy"],
            "canonical_sha256": content_id,
            "cache_object_ids": {"bars": content_id},
        }

    def publish(
        self,
        request: DatasetRequest,
        rows: list[dict[str, Any]],
        *,
        retrieved_at: datetime,
        code_sha: str,
    ) -> DatasetHandle:
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        code_sha = code_sha.strip().lower()
        if re.fullmatch(r"[0-9a-f]{40,64}", code_sha) is None:
            raise ValueError("code_sha must be a full hexadecimal revision")
        source_records = _canonical_records(rows)
        _, source_times = self._parse(source_records)
        records = self._sessionise(request, source_records, source_times)
        bars, times = self._parse(records)
        if not bars:
            raise ValueError("historical dataset must contain at least one bar")
        object_bytes = _json_bytes({"schema_version": SCHEMA_VERSION, "records": records})
        content_id = _sha256(object_bytes)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "request_id": self.request_id(request),
            **self._provenance(request, content_id),
            **self._coverage(request, times),
            "sessionisation": {
                "policy": "XNYS_regular_sessions_only",
                "source_record_count": len(source_records),
                "published_record_count": len(records),
                "excluded_out_of_session_count": len(source_records) - len(records),
            },
            "retrieval_timestamp": retrieved_at.astimezone(UTC).isoformat(),
            "code_sha": code_sha,
        }
        manifest_bytes = _json_bytes(manifest)
        manifest_id = _sha256(manifest_bytes)
        reference = {
            "schema_version": SCHEMA_VERSION,
            "request_id": manifest["request_id"],
            "manifest_id": manifest_id,
            "content_id": content_id,
        }
        self._atomic_write(self.object_path(content_id), object_bytes)
        self._atomic_write(self._manifest_path(manifest_id), manifest_bytes)
        self._atomic_write(self.reference_path(request), _json_bytes(reference))
        return DatasetHandle(bars, times, manifest, content_id, manifest_id)

    def load(self, request: DatasetRequest) -> DatasetHandle:
        request_id = self.request_id(request)
        reference = self._read_json(self.reference_path(request), "request reference")
        if reference.get("request_id") != request_id:
            raise ValueError("cache request identity mismatch")
        manifest_id = self._identity(reference.get("manifest_id"), "manifest")
        content_id = self._identity(reference.get("content_id"), "object")
        manifest = self._verified_json(self._manifest_path(manifest_id), manifest_id, "manifest")
        if (
            manifest.get("request_id") != request_id
            or manifest.get("canonical_sha256") != content_id
        ):
            raise ValueError("manifest request or object identity mismatch")
        envelope = self._verified_json(self.object_path(content_id), content_id, "object")
        if any(
            item.get("schema_version") != SCHEMA_VERSION for item in (reference, manifest, envelope)
        ):
            raise ValueError("unsupported cache schema")
        records = envelope.get("records")
        if (
            not isinstance(records, list)
            or _json_bytes(envelope) != self.object_path(content_id).read_bytes()
        ):
            raise ValueError("object is not canonical JSON")
        normalized = _canonical_records(records)
        bars, times = self._parse(normalized)
        if normalized != records or self._sessionise(request, normalized, times) != normalized:
            raise ValueError("object violates canonical sessionised content")
        self._validate_manifest(request, manifest, content_id, len(records))
        coverage = self._coverage(request, times)
        if any(manifest.get(key) != value for key, value in coverage.items()):
            raise ValueError("manifest coverage does not match object")
        return DatasetHandle(bars, times, manifest, content_id, manifest_id)

    @staticmethod
    def _parse(records: list[dict[str, Any]]) -> tuple[list[Bar], list[datetime]]:
        bars = [
            Bar(*(Decimal(str(row[field])) for field in ("o", "h", "l", "c", "v")))
            for row in records
        ]
        times = [datetime.fromtimestamp(int(row["t"]) / 1000, tz=UTC) for row in records]
        validate_bar_series(bars, times, label="immutable historical dataset")
        return bars, times

    @staticmethod
    def _session_ids(request: DatasetRequest, times: list[datetime]) -> list[str | None]:
        calendar = calendar_for_venue(request.venue)
        if request.timezone != calendar.exchange_timezone:
            raise ValueError("dataset timezone does not match venue calendar")
        if request.timespan.strip().lower() == "day":
            timezone = ZoneInfo(request.timezone)
            return [
                f"{calendar.venue}:{stamp.astimezone(timezone).date().isoformat()}"
                for stamp in times
            ]
        return [
            session.session_id if (session := calendar.session_for_timestamp(stamp)) else None
            for stamp in times
        ]

    @classmethod
    def _sessionise(
        cls,
        request: DatasetRequest,
        records: list[dict[str, int | str]],
        times: list[datetime],
    ) -> list[dict[str, int | str]]:
        calendar = calendar_for_venue(request.venue)
        expected = {
            item.session_id for item in calendar.expected_sessions(request.start, request.end)
        }
        return [
            record
            for record, session_id in zip(records, cls._session_ids(request, times), strict=True)
            if session_id in expected
        ]

    @classmethod
    def _coverage(cls, request: DatasetRequest, times: list[datetime]) -> dict[str, Any]:
        calendar = calendar_for_venue(request.venue)
        expected = [
            item.session_id for item in calendar.expected_sessions(request.start, request.end)
        ]
        observed = sorted({item for item in cls._session_ids(request, times) if item is not None})
        missing = sorted(set(expected) - set(observed))
        present_count = len(expected) - len(missing)
        symbol = request.as_dict()["symbols"][0]
        return {
            "expected_sessions": expected,
            "observed_sessions": observed,
            "coverage": {
                symbol: {
                    "expected_count": len(expected),
                    "observed_count": present_count,
                    "coverage_pct": 100.0 * present_count / len(expected) if expected else 0.0,
                    "missing_session_ids": missing,
                    "extra_session_ids": [],
                }
            },
        }

    @classmethod
    def _validate_manifest(
        cls, request: DatasetRequest, manifest: dict[str, Any], content_id: str, count: int
    ) -> None:
        session = manifest.get("sessionisation")
        if not isinstance(session, dict):
            raise ValueError("manifest metadata is invalid")
        try:
            source_count = session["source_record_count"]
            retrieved_at = datetime.fromisoformat(str(manifest["retrieval_timestamp"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("manifest metadata is invalid") from exc
        if (
            not isinstance(source_count, int)
            or source_count < count
            or session.get("policy") != "XNYS_regular_sessions_only"
            or session.get("published_record_count") != count
            or session.get("excluded_out_of_session_count") != source_count - count
            or len(session) != 4
            or retrieved_at.tzinfo is None
            or re.fullmatch(r"[0-9a-f]{40,64}", str(manifest.get("code_sha", ""))) is None
            or any(
                manifest.get(key) != value
                for key, value in cls._provenance(request, content_id).items()
            )
        ):
            raise ValueError("manifest metadata is invalid")

    @staticmethod
    def _identity(value: Any, label: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"invalid {label} identity")
        return value

    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, Any]:
        return ImmutableDatasetCache._decode(path.read_bytes(), label)

    @staticmethod
    def _decode(payload: bytes, label: str) -> dict[str, Any]:
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid {label} JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be a JSON object")
        return value

    @classmethod
    def _verified_json(cls, path: Path, identity: str, label: str) -> dict[str, Any]:
        payload = path.read_bytes()
        if _sha256(payload) != identity:
            raise ValueError(f"{label} object hash mismatch")
        return cls._decode(payload, label)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
