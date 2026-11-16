"""调整重算的纯规则：时间窗、目录投影、差额与批次号。

重算不修改原账：它只在原账已采信的行上，用目标计量目录重新归属计量边，
并把原挂与新挂的差异逐条留作证据。
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import RULE_VERSION
from app.errors import reject

LOCAL = timezone(timedelta(hours=8))
WINDOW_DAYS = 7


def _local_date(value):
    return datetime.fromisoformat(value).astimezone(LOCAL).date()


def window(rows):
    """以台账最新采信读数为锚点的最近七个自然日，[start, end) 按 UTC 表示。"""
    stamps = [row["at"] for row in rows]
    if not stamps:
        reject("EMPTY_WINDOW", "没有可用于重算的采信行")
    anchor = max(_local_date(stamp) for stamp in stamps)
    start_date = anchor - timedelta(days=WINDOW_DAYS - 1)
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=LOCAL)
    end = datetime(anchor.year, anchor.month, anchor.day, tzinfo=LOCAL) + timedelta(days=1)
    return {
        "anchor": anchor.isoformat(),
        "days": WINDOW_DAYS,
        "timezone": "UTC+08:00",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_utc": start.astimezone(timezone.utc).isoformat(),
        "end_utc": end.astimezone(timezone.utc).isoformat(),
    }


def inside(row, span):
    at = datetime.fromisoformat(row["at"])
    return datetime.fromisoformat(span["start_utc"]) <= at < datetime.fromisoformat(span["end_utc"])


def project(row, catalog):
    """按目录返回 (投影行, 原挂, 新挂)；表不在目录中返回 None。"""
    link = catalog.get(row["meter"])
    if link is None:
        return None
    recorded = [row["from_zone"], row["to_zone"], row["kind"]]
    projected = {**row, "from_zone": link[0], "to_zone": link[1], "kind": link[2]}
    return projected, recorded, list(link)


def totals(rows):
    def kind_sum(kind):
        return sum((Decimal(x["value_m3"]) for x in rows if x["kind"] == kind), Decimal(0))

    return {
        "supply": str(kind_sum("supply")),
        "sales": str(kind_sum("sale")),
        "authorized": str(kind_sum("authorized")),
    }


def compare(original, adjusted, threshold="5"):
    from app.modules.m6_reconcile.rules import classify

    origin = {item["zone"]: item for item in original}
    rows = []
    for item in adjusted:
        before = origin.get(item["zone"], {})
        item["status"] = classify(item, threshold)
        if before:
            before["status"] = classify(before, threshold)
        delta = Decimal(item["residual"]) - Decimal(before.get("residual", "0"))
        fields = ("inflow", "outflow", "sale", "authorized", "uncertainty", "residual")
        affected = any(before.get(key, "0") != item.get(key) for key in fields)
        rows.append({
            "zone": item["zone"],
            "original": {key: before.get(key) for key in fields + ("lower", "upper", "status")},
            "adjusted": {key: item.get(key) for key in fields + ("lower", "upper", "status")},
            "delta_residual": str(delta),
            "affected": affected,
            "status_changed": before.get("status") != item.get("status"),
        })
    return rows


def catalog_digest(catalog):
    material = json.dumps(catalog, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def identity(base, catalog_version, catalog_sha256, span, params):
    material = json.dumps(
        {
            "base_sha256": base,
            "catalog_version": catalog_version,
            "catalog_sha256": catalog_sha256,
            "window": [span["start_utc"], span["end_utc"]],
            "params": params,
            "rule": RULE_VERSION,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "ADJ-" + hashlib.sha256(material.encode()).hexdigest()[:16]
