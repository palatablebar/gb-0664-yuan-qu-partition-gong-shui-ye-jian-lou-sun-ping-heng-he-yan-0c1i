"""调整重算：在原账之外生成可复核的修正批次，原账零改动。"""
import csv
import io
import json
import time
from decimal import Decimal, InvalidOperation

from app import catalog as registry
from app.config import BASE_CATALOG_VERSION, RULE_VERSION
from app.errors import reject
from app.modules.m4_calculate.rules import balances
from app.modules.m7_batches.rules import csv_evidence
from app.store import connect, fetch
from .rules import catalog_digest, compare, identity, inside, project, totals, window


def _ensure(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS adjustments ("
        "id TEXT PRIMARY KEY,"
        "base_run TEXT NOT NULL,"
        "base_sha256 TEXT NOT NULL,"
        "catalog_version TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "revision INTEGER NOT NULL DEFAULT 1,"
        "decision TEXT NOT NULL DEFAULT '待复核',"
        "body TEXT NOT NULL)"
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(adjustments)")}
    if "revision" not in columns:
        conn.execute("ALTER TABLE adjustments ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    if "decision" not in columns:
        conn.execute("ALTER TABLE adjustments ADD COLUMN decision TEXT NOT NULL DEFAULT '待复核'")


def _threshold(params):
    raw = (params or {}).get("threshold", "5")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError):
        reject("THRESHOLD", "阈值须为非负数")
    if not value.is_finite() or value < 0:
        reject("THRESHOLD", "阈值须为非负数")
    return format(value.normalize(), "f")


def _save(adjustment):
    with connect() as conn:
        _ensure(conn)
        conn.execute(
            "INSERT OR IGNORE INTO adjustments(id,base_run,base_sha256,catalog_version,created_at,body) VALUES(?,?,?,?,?,?)",
            (
                adjustment["id"],
                adjustment["base_run"],
                adjustment["base_sha256"],
                adjustment["catalog_version"],
                adjustment["created_at"],
                json.dumps(adjustment, ensure_ascii=False),
            ),
        )
    return load(adjustment["id"])


def load(adjustment_id):
    with connect() as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT body,revision,decision FROM adjustments WHERE id=?", (adjustment_id,)
        ).fetchone()
    if row is None:
        reject("NOT_FOUND", "调整批次不存在")
    body = json.loads(row["body"])
    body["revision"] = row["revision"]
    body["decision"] = row["decision"]
    return body


def review(adjustment_id, revision, decision):
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        reject("VERSION", "修订号须为正整数")
    if decision not in ("采用", "退回"):
        reject("DECISION", "调整结论无效")
    with connect() as conn:
        _ensure(conn)
        changed = conn.execute(
            "UPDATE adjustments SET revision=revision+1,decision=? WHERE id=? AND revision=?",
            (decision, adjustment_id, revision),
        ).rowcount
        if not changed:
            exists = conn.execute("SELECT 1 FROM adjustments WHERE id=?", (adjustment_id,)).fetchone()
            if not exists:
                reject("NOT_FOUND", "调整批次不存在")
            reject("STALE_VERSION", "调整批次已更新，请重新载入")
    return load(adjustment_id)


def listing(base_run=None):
    with connect() as conn:
        _ensure(conn)
        if base_run:
            rows = conn.execute(
                "SELECT body,revision,decision FROM adjustments WHERE base_run=? ORDER BY rowid DESC", (base_run,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT body,revision,decision FROM adjustments ORDER BY rowid DESC LIMIT 100").fetchall()
    items = []
    for row in rows:
        body = json.loads(row["body"])
        items.append({
            "id": body["id"],
            "base_run": body["base_run"],
            "catalog_version": body["catalog_version"],
            "catalog_label": body["catalog_label"],
            "window": body["window"],
            "created_at": body["created_at"],
            "revision": row["revision"],
            "decision": row["decision"],
            "affected": [x["zone"] for x in body["zones"] if x["affected"]],
            "totals": body["totals"],
        })
    return items


def _project_rows(base_rows, catalog):
    projected, evidence, orphans = [], [], []
    for row in base_rows:
        result = project(row, catalog)
        if result is None:
            orphans.append({
                "line": row["line"],
                "record_id": row["record_id"],
                "meter": row["meter"],
                "code": "REFERENCE",
                "detail": "表号不在目标目录中，未参与修正计算",
            })
            continue
        new_row, recorded, target = result
        projected.append(new_row)
        if recorded != target:
            evidence.append({
                "line": row["line"],
                "record_id": row["record_id"],
                "meter": row["meter"],
                "value_m3": row["value_m3"],
                "recorded": recorded,
                "target": target,
            })
    return projected, evidence, orphans


def create(base_run_id, options=None):
    options = options or {}
    params = options.get("params", {})
    if not isinstance(params, dict) or set(params) - {"threshold"}:
        reject("PARAMETER", "未知计算参数")
    if set(options) - {"catalog_version", "params", "expected_version"}:
        reject("PARAMETER", "存在未知调整参数")
    threshold = _threshold(params)

    base = fetch(base_run_id)
    version = options.get("catalog_version") or registry.active_version()
    record = registry.load(version)
    from app.modules.m3_model.rules import validate_catalog
    zones_new = validate_catalog(record["catalog"])

    expected = options.get("expected_version")
    if expected is not None and expected != registry.active_version():
        reject("STALE_VERSION", "活动目录版本已切换，请重新载入")

    span = window(base["rows"])
    window_rows = [row for row in base["rows"] if inside(row, span)]
    if not window_rows:
        reject("EMPTY_WINDOW", "最近七天没有可重算的采信行")

    projected, reclassified, orphans = _project_rows(window_rows, record["catalog"])
    if orphans and not projected:
        reject("REFERENCE", "目标目录无法归属任何采信行")

    zone_names = sorted(set(base["zones"]) | set(zones_new))
    original_results = balances(window_rows, zone_names)
    adjusted_results = balances(projected, zone_names)
    zones = compare(original_results, adjusted_results, threshold)
    original_night = balances(window_rows, zone_names, True)
    adjusted_night = balances(projected, zone_names, True)
    night_zones = compare(original_night, adjusted_night, threshold)
    # 窗口覆盖原账全部采信行时，窗口内原值应与原账一致；若原账跨出七天，则如实标注差异。
    account_matches = (
        len(window_rows) == len(base["rows"])
        and {item["zone"]: str(item["residual"]) for item in base["results"]}
        == {item["zone"]: str(item["residual"]) for item in original_results}
    )

    def summarize(rows, results):
        amounts = totals(rows)
        residual = sum((Decimal(x["residual"]) for x in results), Decimal(0))
        amounts["residual"] = str(residual)
        amounts["balanced"] = Decimal(amounts["supply"]) == (
            Decimal(amounts["sales"]) + Decimal(amounts["authorized"]) + residual
        )
        return amounts

    original_totals = summarize(window_rows, original_results)
    adjusted_totals = summarize(projected, adjusted_results)

    prefix = identity(
        base["manifest"]["sha256"],
        version,
        catalog_digest(record["catalog"]),
        span,
        {"threshold": threshold},
    )
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    adjustment = {
        "id": prefix,
        "created_at": now,
        "rule_version": RULE_VERSION,
        "base_run": base_run_id,
        "base_sha256": base["manifest"]["sha256"],
        "base_revision": base["revision"],
        "base_decision": base["decision"],
        "base_created_at": base["manifest"]["created_at"],
        "base_catalog_version": base.get("catalog_version", BASE_CATALOG_VERSION),
        "catalog_version": version,
        "catalog_label": record["label"],
        "catalog_active": record["active"],
        "catalog_status": record["status"],
        "catalog_sha256": catalog_digest(record["catalog"]),
        "window": span,
        "threshold": threshold,
        "accepted": len(window_rows),
        "input_count": base["input_count"],
        "window_matches_account": account_matches,
        "base_input_count": base["input_count"],
        "zones": zones,
        "night_zones": night_zones,
        "reclassified": reclassified,
        "orphans": orphans,
        "totals": {
            "original": original_totals,
            "adjusted": adjusted_totals,
        },
        "original": {
            "results": original_results,
            "night_results": original_night,
            "reconciliation": base["reconciliation"],
        },
        "adjusted": {
            "results": adjusted_results,
            "night_results": adjusted_night,
        },
        "base_evidence": f"/api/runs/{base_run_id}/evidence.csv",
        "applied_to_account": False,
        "note": "原账保留原编号与原值；本批次仅为可复核修正，未回写历史账。",
    }
    return _save(adjustment)


def adjustment_evidence(adjustment):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["调整批次", "原账运行号", "目录版本", "窗口起", "窗口止", "类别", "来源行", "计量记录", "原挂", "新挂", "计量类型", "水量 / m³", "残差 / m³", "差额 / m³", "结论"])
    for zone in adjustment["zones"]:
        writer.writerow([
            adjustment["id"], adjustment["base_run"], adjustment["catalog_version"],
            adjustment["window"]["start_utc"], adjustment["window"]["end_utc"],
            "分区", "", zone["zone"], "", "", "",
            "", zone["adjusted"]["residual"], zone["delta_residual"], zone["adjusted"]["status"],
        ])
    for row in adjustment["reclassified"]:
        recorded = "→".join(row["recorded"][:2])
        target = "→".join(row["target"][:2])
        writer.writerow([
            adjustment["id"], adjustment["base_run"], adjustment["catalog_version"],
            adjustment["window"]["start_utc"], adjustment["window"]["end_utc"],
            "改挂", row["line"], row["record_id"], recorded, target, row["target"][2],
            row["value_m3"], "", "", "",
        ])
    for row in adjustment["orphans"]:
        writer.writerow([
            adjustment["id"], adjustment["base_run"], adjustment["catalog_version"],
            adjustment["window"]["start_utc"], adjustment["window"]["end_utc"],
            "未归属", row["line"], row["record_id"], "", "", "",
            "", "", "", row["detail"],
        ])
    return "﻿" + buffer.getvalue()


__all__ = ["create", "load", "listing", "adjustment_evidence", "csv_evidence"]
