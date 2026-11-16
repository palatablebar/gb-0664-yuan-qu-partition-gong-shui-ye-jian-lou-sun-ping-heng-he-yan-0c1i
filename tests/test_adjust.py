import csv
import io
import pytest
from app import catalog as registry
from app.modules.m7_batches.service import run as archive
from app.modules.m8_adjust.service import adjustment_evidence, create, listing, load, review
from app.store import fetch
from app.errors import DomainError


@pytest.fixture
def base(dirty):
    return archive(dirty)


def test_adjustment_leaves_original_account_untouched(base):
    rid = base["manifest"]["run_id"]
    before = fetch(rid)
    adjusted = create(rid)
    after = fetch(rid)
    assert adjusted["base_run"] == rid
    assert after["manifest"]["run_id"] == rid
    assert after["manifest"]["sha256"] == before["manifest"]["sha256"]
    assert after["revision"] == before["revision"]
    assert after["decision"] == before["decision"]
    assert after["rows"] == before["rows"]
    assert after["quarantine"] == before["quarantine"]
    assert after["results"] == before["results"]


def test_adjustment_shows_affected_zones_and_deltas(base):
    rid = base["manifest"]["run_id"]
    adjusted = create(rid, {"catalog_version": "CAT-2026-11-B"})
    zones = {item["zone"]: item for item in adjusted["zones"]}
    # AC 由 A→C 改为 B→C，A 区转供减少 6×200，B 区转供增加 6×200，C 区不变
    assert zones["A"]["delta_residual"] == "1200.000"
    assert zones["B"]["delta_residual"] == "-1200.000"
    assert zones["C"]["delta_residual"] == "0.000"
    assert {item["zone"] for item in adjusted["zones"] if item["affected"]} == {"A", "B"}
    reclassified = adjusted["reclassified"]
    assert len(reclassified) == 6
    assert {tuple(row["recorded"]) for row in reclassified} == {("A", "C", "transfer")}
    assert {tuple(row["target"]) for row in reclassified} == {("B", "C", "transfer")}


def test_adjustment_records_catalog_version_and_window(base):
    rid = base["manifest"]["run_id"]
    adjusted = create(rid, {"catalog_version": "CAT-2026-11-B"})
    assert adjusted["catalog_version"] == "CAT-2026-11-B"
    assert adjusted["window"]["days"] == 7
    assert adjusted["window"]["start"].endswith("+08:00")
    assert "T00:00:00+08:00" in adjusted["window"]["start"]
    assert adjusted["accepted"] == len(fetch(rid)["rows"])


def test_adjustment_retry_is_idempotent(base):
    rid = base["manifest"]["run_id"]
    first = create(rid, {"catalog_version": "CAT-2026-11-B"})
    second = create(rid, {"catalog_version": "CAT-2026-11-B"})
    third = create(rid, {"catalog_version": "CAT-2026-11-B", "params": {"threshold": "5.0"}})
    assert first["id"] == second["id"] == third["id"]
    assert len(listing(rid)) == 1


def test_different_catalog_versions_are_separate_batches(base):
    rid = base["manifest"]["run_id"]
    a = create(rid, {"catalog_version": "CAT-2026-11-A"})
    b = create(rid, {"catalog_version": "CAT-2026-11-B"})
    assert a["id"] != b["id"]
    assert len(listing(rid)) == 2
    assert {item["catalog_version"] for item in listing(rid)} == {"CAT-2026-11-A", "CAT-2026-11-B"}


def test_adjustment_evidence_csv(base):
    rid = base["manifest"]["run_id"]
    adjusted = create(rid, {"catalog_version": "CAT-2026-11-B"})
    text = adjustment_evidence(adjusted)
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    assert rows[0][0] == "调整批次"
    assert any(row[5] == "改挂" and row[8] == "A→C" and row[9] == "B→C" for row in rows)
    assert all(row[0] == adjusted["id"] for row in rows[1:])
    assert all(row[1] == rid for row in rows[1:])


def test_activation_failure_keeps_previous_and_records_event(dirty):
    base_version = registry.active_version()
    with pytest.raises(DomainError, match="存在环"):
        registry.activate("CAT-2026-11-C")
    assert registry.active_version() == base_version
    failed = [event for event in registry.events() if event["status"] == "failed"]
    assert failed and failed[0]["to_version"] == "CAT-2026-11-C"
    assert failed[0]["code"] == "CYCLE"


def test_activation_switches_and_preserves_versions(dirty):
    result = registry.activate("CAT-2026-11-B")
    assert result["from_version"] == "CAT-2026-11-A"
    assert result["version"] == "CAT-2026-11-B"
    assert registry.active_version() == "CAT-2026-11-B"
    active, versions = registry.versions()
    assert {item["version"] for item in versions} == {"CAT-2026-11-A", "CAT-2026-11-B", "CAT-2026-11-C"}
    assert all(item["active"] == (item["version"] == active) for item in versions)
    assert registry.load("CAT-2026-11-A")["catalog"]["AC"] == ["A", "C", "transfer"]


def test_activation_rejects_stale_expected_version(dirty):
    with pytest.raises(DomainError, match="目录版本已切换"):
        registry.activate("CAT-2026-11-B", expected="CAT-2026-11-C")
    assert registry.active_version() == "CAT-2026-11-A"


def test_adjustment_rejects_stale_expected_version(base):
    with pytest.raises(DomainError, match="活动目录版本已切换"):
        create(base["manifest"]["run_id"], {"expected_version": "CAT-2026-11-C"})


def test_unknown_catalog_version(base):
    with pytest.raises(DomainError, match="版本不存在"):
        create(base["manifest"]["run_id"], {"catalog_version": "CAT-9999"})


def test_adjustment_review_is_stale_protected(base):
    rid = base["manifest"]["run_id"]
    adjusted = create(rid, {"catalog_version": "CAT-2026-11-B"})
    assert adjusted["revision"] == 1
    reviewed = review(adjusted["id"], 1, "采用")
    assert reviewed["revision"] == 2 and reviewed["decision"] == "采用"
    with pytest.raises(DomainError, match="重新载入"):
        review(adjusted["id"], 1, "退回")


def test_seed_version_matches_sample_catalog():
    import json
    from app.config import BASE_CATALOG_VERSION, SAMPLE_DIR
    seeded = registry.load(BASE_CATALOG_VERSION)["catalog"]
    sample = json.loads((SAMPLE_DIR / "catalog.json").read_text())
    assert seeded == sample


def test_account_pins_base_catalog_version(base):
    assert base["catalog_version"] == "CAT-2026-11-A"
    registry.activate("CAT-2026-11-B")
    stale = archive({"sample": "dirty"})
    assert stale["manifest"]["run_id"] == base["manifest"]["run_id"]
    assert stale["catalog_version"] == "CAT-2026-11-A"
