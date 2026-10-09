from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest

from app.backtest.dataset_cache import (
    DatasetRequest,
    ImmutableDatasetCache,
    canonical_json,
)

if TYPE_CHECKING:
    from pathlib import Path


ROWS = [
    {"o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000, "t": 1767623400000},
    {"o": 101, "h": 102, "l": 100, "c": 101.5, "v": 1200, "t": 1767623700000},
]


def request(**changes: Any) -> DatasetRequest:
    values: dict[str, Any] = {
        "provider": "polygon",
        "source_id": "polygon_aggregate_bars",
        "source_version": "v2",
        "symbols": ("AAPL",),
        "start": date(2026, 1, 5),
        "end": date(2026, 1, 5),
        "multiplier": 5,
        "timespan": "minute",
        "venue": "XNYS",
        "timezone": "America/New_York",
        "adjustment_mode": "provider_adjusted",
        "corporate_action_policy": "provider_adjusted_unqualified",
        "membership_source": "single_symbol_request",
    }
    values.update(changes)
    return DatasetRequest(**values)


def publish(cache: ImmutableDatasetCache, **changes: Any):
    return cache.publish(
        request(**changes),
        ROWS,
        retrieved_at=datetime(2026, 1, 6, 12, tzinfo=UTC),
        code_sha="a" * 40,
    )


def rewire_object(
    cache: ImmutableDatasetCache,
    envelope: dict[str, Any],
) -> None:
    object_bytes = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    content_id = hashlib.sha256(object_bytes).hexdigest()
    object_path = cache.object_path(content_id)
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(object_bytes)
    ref_path = cache.reference_path(request())
    ref = json.loads(ref_path.read_text())
    manifest_path = cache._manifest_path(ref["manifest_id"])
    manifest = json.loads(manifest_path.read_text())
    manifest["canonical_sha256"] = content_id
    manifest["cache_object_ids"] = {"bars": content_id}
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_id = hashlib.sha256(manifest_bytes).hexdigest()
    new_manifest_path = cache._manifest_path(manifest_id)
    new_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    new_manifest_path.write_bytes(manifest_bytes)
    ref.update({"content_id": content_id, "manifest_id": manifest_id})
    ref_path.write_text(json.dumps(ref))


def test_canonical_json_normalizes_keys_numbers_and_record_order() -> None:
    first = [
        {"t": 2, "c": 101.50, "o": 101, "h": 102, "l": 100, "v": 5},
        {"t": 1, "c": 100.50, "o": 100, "h": 101, "l": 99, "v": 4},
    ]
    second = [
        {"v": "4.0", "l": "99", "h": "101", "o": "100.0", "c": "100.500", "t": 1},
        {"v": "5.0", "l": "100.00", "h": "102", "o": "101.0", "c": "101.500", "t": 2},
    ]

    assert canonical_json(first) == canonical_json(second)


def test_publish_round_trip_has_complete_secret_free_manifest(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    handle = publish(cache)
    loaded = cache.load(request())

    assert loaded.bars == handle.bars
    assert loaded.times == handle.times
    assert loaded.manifest == handle.manifest
    assert handle.manifest["schema_version"] == 1
    assert handle.manifest["provider"] == "polygon"
    assert handle.manifest["source"] == {
        "id": "polygon_aggregate_bars",
        "version": "v2",
        "original_sha256": None,
    }
    assert handle.manifest["request_parameters"]["limit"] == 50000
    assert handle.manifest["venue"] == "XNYS"
    assert handle.manifest["calendar"] == "XNYS"
    assert handle.manifest["timezone"] == "America/New_York"
    assert handle.manifest["interval"] == "5-minute"
    assert handle.manifest["symbols"] == ["AAPL"]
    assert handle.manifest["universe"] == ["AAPL"]
    assert handle.manifest["membership_source"] == "single_symbol_request"
    assert handle.manifest["expected_sessions"] == ["XNYS:2026-01-05"]
    assert handle.manifest["observed_sessions"] == ["XNYS:2026-01-05"]
    assert handle.manifest["coverage"]["AAPL"]["coverage_pct"] == 100.0
    assert handle.manifest["canonical_sha256"] == handle.content_id
    assert handle.manifest["code_sha"] == "a" * 40
    assert handle.manifest["cache_object_ids"] == {"bars": handle.content_id}
    serialized = json.dumps(handle.manifest).lower()
    assert "api_key" not in serialized
    assert "authorization" not in serialized
    assert "test-key" not in serialized


def test_publish_sessionises_intraday_rows_before_hashing(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    before_open = {**ROWS[0], "t": 1767618000000}

    handle = cache.publish(
        request(),
        [before_open, ROWS[0]],
        retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
        code_sha="d" * 40,
    )

    assert len(handle.bars) == 1
    assert handle.manifest["sessionisation"] == {
        "policy": "XNYS_regular_sessions_only",
        "source_record_count": 2,
        "published_record_count": 1,
        "excluded_out_of_session_count": 1,
    }


def test_publish_sessionises_daily_rows_before_hashing(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    sunday = {**ROWS[0], "t": 1767502800000}
    monday = {**ROWS[0], "t": 1767589200000}

    handle = cache.publish(
        request(multiplier=1, timespan="day"),
        [sunday, monday],
        retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
        code_sha="d" * 40,
    )

    assert len(handle.bars) == 1
    assert handle.manifest["sessionisation"]["excluded_out_of_session_count"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"source_id": "https://example.test/bars?apiKey=secret"},
        {"provider": "https://example.test?apiKey=secret"},
        {"membership_source": "static?token=secret"},
    ],
)
def test_source_metadata_rejects_credential_bearing_values(
    tmp_path: Path,
    change: dict[str, str],
) -> None:
    cache = ImmutableDatasetCache(tmp_path)

    with pytest.raises(ValueError, match="source metadata"):
        cache.request_id(request(**change))


@pytest.mark.parametrize("mutation", ["truncated", "corrupt", "hash_mismatch"])
def test_cache_hit_rejects_invalid_object(tmp_path: Path, mutation: str) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    handle = publish(cache)
    object_path = cache.object_path(handle.content_id)
    if mutation == "truncated":
        object_path.write_text('{"schema_version":1')
    elif mutation == "corrupt":
        object_path.write_text("not-json")
    else:
        payload = json.loads(object_path.read_text())
        payload["records"][0]["c"] = "100.75"
        object_path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=r"object|JSON|hash"):
        cache.load(request())


def test_cache_hit_rejects_stale_schema_and_request_mismatch(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    publish(cache)
    ref_path = cache.reference_path(request())
    ref = json.loads(ref_path.read_text())
    ref["schema_version"] = 0
    ref_path.write_text(json.dumps(ref))

    with pytest.raises(ValueError, match="schema"):
        cache.load(request())

    ref["schema_version"] = 1
    ref["request_id"] = "0" * 64
    ref_path.write_text(json.dumps(ref))
    with pytest.raises(ValueError, match="request"):
        cache.load(request())


def test_cache_hit_rejects_manifest_content_identity_mismatch(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    publish(cache)
    ref_path = cache.reference_path(request())
    ref = json.loads(ref_path.read_text())
    ref["content_id"] = "0" * 64
    ref_path.write_text(json.dumps(ref))

    with pytest.raises(ValueError, match=r"manifest.*object identity mismatch"):
        cache.load(request())


def test_request_ticker_cannot_escape_cache_namespace(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)

    with pytest.raises(ValueError, match="symbol"):
        cache.reference_path(request(symbols=("../../outside",)))


def test_publish_revalidates_bar_contract(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    with pytest.raises(ValueError, match="high"):
        cache.publish(
            request(),
            [{"o": 100, "h": 99, "l": 98, "c": 100, "v": 1, "t": 1767623400000}],
            retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
            code_sha="b" * 40,
        )
    assert list(tmp_path.rglob("*.json")) == []


def test_cache_hit_revalidates_bar_contract_after_integrity_checks(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    handle = publish(cache)
    envelope = json.loads(cache.object_path(handle.content_id).read_text())
    envelope["records"][0]["h"] = "99"
    rewire_object(cache, envelope)

    with pytest.raises(ValueError, match="high"):
        cache.load(request())


def test_cache_hit_rejects_stale_object_schema_with_valid_hash(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)
    handle = publish(cache)
    envelope = json.loads(cache.object_path(handle.content_id).read_text())
    envelope["schema_version"] = 0
    rewire_object(cache, envelope)

    with pytest.raises(ValueError, match="cache schema"):
        cache.load(request())


def test_empty_dataset_is_never_published(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)

    with pytest.raises(ValueError, match="at least one bar"):
        cache.publish(
            request(),
            [],
            retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
            code_sha="e" * 40,
        )

    assert not cache.reference_path(request()).exists()


def test_request_semantics_change_request_identity(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)

    identities = {
        cache.request_id(request()),
        cache.request_id(request(end=date(2026, 1, 6))),
        cache.request_id(request(multiplier=1)),
        cache.request_id(request(adjustment_mode="unadjusted")),
        cache.request_id(
            request(
                membership_source="research_only_survivor_biased",
                universe=("AAPL", "MSFT"),
            )
        ),
    }

    assert len(identities) == 5
    assert all("AAPL" not in identity for identity in identities)


def test_concurrent_identical_publication_is_valid(tmp_path: Path) -> None:
    cache = ImmutableDatasetCache(tmp_path)

    def worker(_: int):
        return publish(cache)

    with ThreadPoolExecutor(max_workers=8) as pool:
        handles = list(pool.map(worker, range(24)))

    assert len({handle.content_id for handle in handles}) == 1
    assert len({handle.manifest_id for handle in handles}) == 1
    assert cache.load(request()).content_id == handles[0].content_id
    assert not list(tmp_path.rglob("*.tmp"))


def test_atomic_failure_cleans_temporary_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_replace(source: Path, destination: Path) -> Path:
        del source, destination
        raise OSError("simulated atomic publication failure")

    monkeypatch.setattr(type(tmp_path), "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        publish(ImmutableDatasetCache(tmp_path))

    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob("*.json"))


def test_reference_publication_failure_leaves_no_complete_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_replace = type(tmp_path).replace

    def fail_reference(source: Path, destination: Path) -> Path:
        if destination.parent.name == "requests":
            raise OSError("simulated reference failure")
        return original_replace(source, destination)

    monkeypatch.setattr(type(tmp_path), "replace", fail_reference)
    with pytest.raises(OSError, match="reference failure"):
        publish(ImmutableDatasetCache(tmp_path))

    assert not ImmutableDatasetCache(tmp_path).reference_path(request()).exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_local_ingest_uses_same_pipeline_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.ingest_research_dataset import main

    source = tmp_path / "bars.json"
    source.write_text(json.dumps(ROWS))
    monkeypatch.setenv("T212_CODE_SHA", "c" * 40)

    assert (
        main(
            [
                str(source),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--ticker",
                "AAPL",
                "--from-date",
                "2026-01-05",
                "--to-date",
                "2026-01-05",
                "--adjustment-mode",
                "unadjusted_unknown",
                "--source-label",
                "licensed_fixture_2026q1",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["manifest"]["canonical_sha256"] == output["content_id"]
    assert output["manifest"]["source"]["id"] == "licensed_fixture_2026q1"
    assert len(output["manifest"]["source"]["original_sha256"]) == 64
    assert (
        ImmutableDatasetCache(tmp_path / "cache")
        .load(
            request(
                provider="local_file",
                source_id="licensed_fixture_2026q1",
                source_version="user_supplied_v1",
                source_sha256=output["manifest"]["source"]["original_sha256"],
                adjustment_mode="unadjusted_unknown",
                corporate_action_policy="unadjusted_unknown_unqualified",
            )
        )
        .content_id
        == output["content_id"]
    )
