"""计量目录版本注册表。

目录版本只增不改：启用新版本只移动指针，旧版本与其历史始终保留；
启用失败在事务中回滚，活动版本保持原值，失败尝试另行记入审计事件。
"""
import json
import time
from app.config import SAMPLE_DIR
from app.errors import reject
from app.store import connect

SEED_PATH = SAMPLE_DIR / "catalog_versions.json"
DEFAULT_VERSION = "CAT-2026-11-A"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _ensure(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS catalogs ("
        "version TEXT PRIMARY KEY,"
        "label TEXT NOT NULL,"
        "status TEXT NOT NULL,"
        "note TEXT NOT NULL DEFAULT '',"
        "body TEXT NOT NULL,"
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS catalog_pointer ("
        "singleton INTEGER PRIMARY KEY CHECK (singleton=1),"
        "version TEXT NOT NULL,"
        "updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS catalog_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "at TEXT NOT NULL,"
        "from_version TEXT,"
        "to_version TEXT,"
        "status TEXT NOT NULL,"
        "code TEXT,"
        "detail TEXT)"
    )
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    for item in seed["versions"]:
        conn.execute(
            "INSERT OR IGNORE INTO catalogs(version,label,status,note,body,created_at) VALUES(?,?,?,?,?,?)",
            (item["version"], item["label"], item["status"], item.get("note", ""),
             json.dumps(item["catalog"], ensure_ascii=False, sort_keys=True), _now()),
        )
    conn.execute(
        "INSERT OR IGNORE INTO catalog_pointer(singleton,version,updated_at) VALUES(1,?,?)",
        (DEFAULT_VERSION, _now()),
    )


def _row_to_version(row, active):
    return {
        "version": row["version"],
        "label": row["label"],
        "status": row["status"],
        "note": row["note"],
        "active": row["version"] == active,
        "catalog": json.loads(row["body"]),
    }


def versions():
    with connect() as conn:
        _ensure(conn)
        active = conn.execute("SELECT version FROM catalog_pointer WHERE singleton=1").fetchone()["version"]
        rows = conn.execute("SELECT * FROM catalogs ORDER BY rowid").fetchall()
        return active, [_row_to_version(row, active) for row in rows]


def active_version():
    with connect() as conn:
        _ensure(conn)
        return conn.execute("SELECT version FROM catalog_pointer WHERE singleton=1").fetchone()["version"]


def load(version=None):
    version = version or active_version()
    with connect() as conn:
        _ensure(conn)
        active = conn.execute("SELECT version FROM catalog_pointer WHERE singleton=1").fetchone()["version"]
        row = conn.execute("SELECT * FROM catalogs WHERE version=?", (version,)).fetchone()
        if row is None:
            reject("CATALOG_VERSION", "计量目录版本不存在")
        return _row_to_version(row, active)


def events(limit=20):
    with connect() as conn:
        _ensure(conn)
        rows = conn.execute("SELECT * FROM catalog_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def _record_failed_event(version, from_version, code, detail):
    # 失败尝试在独立事务中留痕，不回写目录与指针。
    try:
        with connect() as conn:
            _ensure(conn)
            conn.execute(
                "INSERT INTO catalog_events(at,from_version,to_version,status,code,detail) VALUES(?,?,?,?,?,?)",
                (_now(), from_version, version, "failed", code, detail),
            )
    except Exception:
        pass


def activate(version, expected=None):
    """启用目录版本：校验在事务外完成，切换在 BEGIN IMMEDIATE 内完成。

    任何失败都记入 catalog_events（status=failed），活动指针保持原值。
    """
    from app.modules.m3_model.rules import validate_catalog
    from app.errors import DomainError

    with connect() as conn:
        _ensure(conn)
        current = conn.execute("SELECT version FROM catalog_pointer WHERE singleton=1").fetchone()["version"]
        row = conn.execute("SELECT * FROM catalogs WHERE version=?", (version,)).fetchone()

    if row is None:
        _record_failed_event(version, current, "CATALOG_VERSION", "计量目录版本不存在")
        reject("CATALOG_VERSION", "计量目录版本不存在")
    if expected is not None and expected != current:
        _record_failed_event(version, current, "STALE_VERSION", "目录版本已切换，请重新载入")
        reject("STALE_VERSION", "目录版本已切换，请重新载入")

    zones = None
    try:
        zones = validate_catalog(json.loads(row["body"]))
    except DomainError as exc:
        _record_failed_event(version, current, exc.code, str(exc))
        raise

    with connect() as conn:
        _ensure(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT version FROM catalog_pointer WHERE singleton=1").fetchone()["version"]
        if expected is not None and expected != current:
            conn.rollback()
            _record_failed_event(version, current, "STALE_VERSION", "目录版本已切换，请重新载入")
            reject("STALE_VERSION", "目录版本已切换，请重新载入")
        if version == current:
            conn.rollback()
            return {"from_version": current, "version": version, "zones": zones, "changed": False}
        conn.execute(
            "UPDATE catalog_pointer SET version=?,updated_at=? WHERE singleton=1",
            (version, _now()),
        )
        conn.execute(
            "INSERT INTO catalog_events(at,from_version,to_version,status,code,detail) VALUES(?,?,?,?,?,?)",
            (_now(), current, version, "success", None, "已启用"),
        )
        return {"from_version": current, "version": version, "zones": zones, "changed": True}
