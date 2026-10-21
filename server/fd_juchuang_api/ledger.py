"""叙事连续性台账（change juchuang-continuity-layer task 3.1-3.7）。

三张表（与剧本五表共库；独立迁移账本 ``ledger_migrations``，不碰 personal 的
``user_version``，避免多模块迁移链互相「吃掉」版本号）：

- ``facts``      双时态事实：subject-predicate-object + valid_from/valid_to。
- ``knowledge``  角色知识：character_id + fact_reference + learned_at_episode。
- ``foreshadow`` 伏笔生命周期：planted / reinforced / resolved / abandoned。

不变量（对应 spec「叙事连续性台账」）：

1. 事实过期只写 ``valid_to_episode`` 标记失效，绝不删除——任一集次时点的事实
   集合可回溯（``facts_at_episode``）。时点条件按任务约定为
   ``valid_from <= episode <= valid_to OR valid_to IS NULL``。
2. 知识按 ``character_id`` 隔离：``knowledge_state`` 只返回该角色
   ``learned_at_episode <= episode`` 的知识；上下文组装不会把 B 的秘密塞给 C。
3. 伏笔逾期 = ``current_episode > planned_resolution_episode`` 且仍处于
   planted/reinforced（resolved 与 abandoned 是终态，不再提示逾期）。
4. 空台账降级：所有检测/组装函数在台账为空时返回 ``available=False`` 与
   ``reason``，不抛异常、不阻断创作流程。``pipeline.py`` 的生成前/生成后接缝
   全部包了兜底，台账缺失时的行为与旧流程完全一致。
5. 所有冲突定位到具体场次/台词：``scene_id`` / ``line_id`` / ``line_idx`` /
   ``offset``（台词内字符偏移）。

API 前缀 ``/api/v1/ledger``；鉴权沿用 ``screenplay`` 的 require_user，项目归属
校验经共库 ``projects`` 表（只读），越权与不存在统一 404。
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---- 鉴权接线（与 screenplay.py 同款兜底：personal 尚未交付时用 dev-token） ----

try:  # 正式路径：auth.py 的 require_user（含审计留痕）
    from .auth import require_user
except Exception:  # noqa: BLE001 —— personal 底座未交付时的降级兜底

    def require_user(request: Request) -> str:  # type: ignore[misc]
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token:
            for var in ("FD_JUCHUANG_AUTH_DEV_TOKENS", "FD__AUTH_DEV_TOKENS"):
                for pair in os.environ.get(var, "").split(","):
                    if "=" in pair and pair.split("=", 1)[0].strip() == token:
                        return pair.split("=", 1)[1].strip()
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


# ---- 常量 ----

FORESHADOW_STATUSES = ("planted", "reinforced", "resolved", "abandoned")
OPEN_FORESHADOW_STATUSES = ("planted", "reinforced")
TERMINAL_FORESHADOW_STATUSES = ("resolved", "abandoned")

DEFAULT_TOKEN_BUDGET = 1200
# 中文按 1 字 ≈ 1 token；ASCII 4 字符 ≈ 1 token（保守估算，宁多勿少）
_ASCII_CHARS_PER_TOKEN = 4

EMPTY_LEDGER_REASON = (
    "连续性台账为空：该剧本尚无事实/知识/伏笔记录，连续性检查不可用（创作流程继续）"
)


class LedgerNotFound(LookupError):
    """记录不存在（API 映射 404）。"""


class LedgerConflict(ValueError):
    """状态迁移/失效边界冲突（API 映射 409）。"""


# ---- 存储 / 迁移 ----

MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS ledger_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="0001_facts_knowledge_foreshadow",
        up="""
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY,
            project_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            valid_from_episode INTEGER NOT NULL,
            valid_to_episode INTEGER,
            created_at TEXT NOT NULL,
            CHECK (valid_from_episode >= 1),
            CHECK (valid_to_episode IS NULL OR valid_to_episode >= valid_from_episode)
        );
        CREATE INDEX IF NOT EXISTS idx_facts_project_time
            ON facts(project_id, valid_from_episode, valid_to_episode);

        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY,
            project_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            fact_reference TEXT NOT NULL,
            learned_at_episode INTEGER NOT NULL,
            source_scene_id INTEGER,
            created_at TEXT NOT NULL,
            CHECK (learned_at_episode >= 1)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_character
            ON knowledge(project_id, character_id, learned_at_episode);

        CREATE TABLE IF NOT EXISTS foreshadow (
            id INTEGER PRIMARY KEY,
            project_id TEXT NOT NULL,
            planted_in_episode INTEGER NOT NULL,
            planned_resolution_episode INTEGER,
            current_status TEXT NOT NULL DEFAULT 'planted'
                CHECK (current_status IN ('planted','reinforced','resolved','abandoned')),
            resolution_episode INTEGER,
            resolution_evidence TEXT,
            hint_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reinforced_count INTEGER NOT NULL DEFAULT 0,
            last_reinforced_episode INTEGER,
            updated_at TEXT,
            CHECK (planted_in_episode >= 1),
            CHECK (planned_resolution_episode IS NULL
                   OR planned_resolution_episode >= planted_in_episode)
        );
        CREATE INDEX IF NOT EXISTS idx_foreshadow_project_status
            ON foreshadow(project_id, current_status, planned_resolution_episode);
        """,
        down="""
        DROP INDEX IF EXISTS idx_foreshadow_project_status;
        DROP INDEX IF EXISTS idx_knowledge_character;
        DROP INDEX IF EXISTS idx_facts_project_time;
        DROP TABLE IF EXISTS foreshadow;
        DROP TABLE IF EXISTS knowledge;
        DROP TABLE IF EXISTS facts;
        """,
    ),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """与 screenplay 同库同约定（env FD_JUCHUANG_DB，默认 ~/.fd-juchuang/juchuang.db）。"""
    path = db_path or os.environ.get("FD_JUCHUANG_DB", "")
    if not path:
        path = str(Path.home() / ".fd-juchuang" / "juchuang.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    try:
        rows = conn.execute(
            "SELECT version, applied_at FROM ledger_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(r["version"]): r["applied_at"] for r in rows}


def migrate(conn: sqlite3.Connection) -> int:
    """正向执行未应用的迁移，返回当前已应用版本数（幂等）。"""
    conn.executescript(MIGRATION_TABLE)
    applied = _applied_versions(conn)
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        conn.executescript(migration.up)
        conn.execute(
            "INSERT INTO ledger_migrations (version, name, applied_at) VALUES (?,?,?)",
            (migration.version, migration.name, now()),
        )
        conn.commit()
    return len(_applied_versions(conn))


def rollback(conn: sqlite3.Connection, steps: int = 1) -> list[int]:
    """反向执行最近 ``steps`` 个迁移（3.1 回滚验证），返回被回滚的版本号。"""
    if steps < 1:
        return []
    applied = _applied_versions(conn)
    if not applied:
        return []
    rolled: list[int] = []
    for version in sorted(applied, reverse=True)[:steps]:
        migration = next((m for m in MIGRATIONS if m.version == version), None)
        if migration is None:
            continue
        conn.executescript(migration.down)
        conn.execute("DELETE FROM ledger_migrations WHERE version=?", (version,))
        conn.commit()
        rolled.append(version)
    return rolled


def migration_state(conn: sqlite3.Connection) -> list[dict]:
    applied = _applied_versions(conn)
    return [
        {
            "version": m.version,
            "name": m.name,
            "applied": m.version in applied,
            "applied_at": applied.get(m.version),
        }
        for m in MIGRATIONS
    ]


def _db() -> sqlite3.Connection:
    conn = connect()
    migrate(conn)
    return conn


def _pid(project_id: int | str) -> str:
    return str(project_id).strip()


# ---- 行转换 ----

_STATUS_LABELS = {
    "planted": "已埋设",
    "reinforced": "已强化",
    "resolved": "已回收",
    "abandoned": "已放弃",
}


def _fact_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "project_id": str(row["project_id"]),
        "subject": row["subject"],
        "predicate": row["predicate"],
        "object": row["object"],
        "valid_from_episode": int(row["valid_from_episode"]),
        "valid_to_episode": (
            int(row["valid_to_episode"]) if row["valid_to_episode"] is not None else None
        ),
        "created_at": row["created_at"],
        "signature": f"{row['subject']}|{row['predicate']}|{row['object']}",
        "invalidated": row["valid_to_episode"] is not None,
    }


def _foreshadow_dict(row: sqlite3.Row, current_episode: int | None = None) -> dict:
    planned = row["planned_resolution_episode"]
    status = row["current_status"]
    overdue = (
        current_episode is not None
        and planned is not None
        and status in OPEN_FORESHADOW_STATUSES
        and current_episode > int(planned)
    )
    return {
        "id": int(row["id"]),
        "project_id": str(row["project_id"]),
        "planted_in_episode": int(row["planted_in_episode"]),
        "planned_resolution_episode": int(planned) if planned is not None else None,
        "current_status": status,
        "resolution_episode": (
            int(row["resolution_episode"]) if row["resolution_episode"] is not None else None
        ),
        "resolution_evidence": row["resolution_evidence"],
        "hint_text": row["hint_text"],
        "created_at": row["created_at"],
        "reinforced_count": int(row["reinforced_count"] or 0),
        "last_reinforced_episode": (
            int(row["last_reinforced_episode"])
            if row["last_reinforced_episode"] is not None
            else None
        ),
        "updated_at": row["updated_at"],
        "overdue": bool(overdue),
        "overdue_by": (int(current_episode) - int(planned)) if overdue else None,
    }


def _character_row(conn: sqlite3.Connection, project_id: int | str,
                   character_id: str) -> sqlite3.Row | None:
    """经共库 characters 表解析角色（id 或姓名均可）；剧本表不存在时返回 None。"""
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return None
    raw = str(character_id).strip()
    try:
        if raw.isdigit():
            row = conn.execute(
                "SELECT id, name FROM characters WHERE project_id=? AND id=?",
                (pid, int(raw)),
            ).fetchone()
            if row is not None:
                return row
        return conn.execute(
            "SELECT id, name FROM characters WHERE project_id=? AND name=?",
            (pid, raw),
        ).fetchone()
    except sqlite3.OperationalError:  # 剧本模块尚未迁移
        return None


def _character_name(conn: sqlite3.Connection, project_id: int | str,
                    character_id: str) -> str | None:
    row = _character_row(conn, project_id, character_id)
    return str(row["name"]) if row is not None else None


def _character_aliases(conn: sqlite3.Connection, project_id: int | str,
                       character_id: str) -> set[str]:
    """角色 id 与姓名的别名集合：知识记录可能按 id 或按名字写入。"""
    raw = str(character_id).strip()
    aliases = {raw}
    row = _character_row(conn, project_id, raw)
    if row is not None:
        aliases.add(str(row["id"]))
        aliases.add(str(row["name"]))
    return {a for a in aliases if a}


def _fact_by_id(conn: sqlite3.Connection, fact_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
    return _fact_dict(row) if row is not None else None


def _need_fact(conn: sqlite3.Connection, project_id: int | str, fact_id: int) -> dict:
    fact = _fact_by_id(conn, fact_id)
    if fact is None or fact["project_id"] != _pid(project_id):
        raise LedgerNotFound(f"事实 {fact_id} 不存在")
    return fact


def _resolve_fact_reference(conn: sqlite3.Connection, project_id: int | str,
                            reference: str) -> dict | None:
    ref = str(reference).strip()
    if not ref.isdigit():
        return None
    fact = _fact_by_id(conn, int(ref))
    if fact is None or fact["project_id"] != _pid(project_id):
        return None
    return fact


def _fact_valid_at(fact: dict | None, episode: int) -> bool | None:
    if fact is None:
        return None
    valid_from = int(fact["valid_from_episode"])
    valid_to = fact["valid_to_episode"]
    return valid_from <= episode and (valid_to is None or int(valid_to) >= episode)


# ---- 3.2 时序事实 ----


def create_fact(conn: sqlite3.Connection, project_id: int | str, subject: str,
                predicate: str, object: str, valid_from_episode: int,
                valid_to_episode: int | None = None) -> dict:
    subject, predicate, object = subject.strip(), predicate.strip(), object.strip()
    if not subject or not predicate or not object:
        raise ValueError("fact 的 subject/predicate/object 均须非空")
    if int(valid_from_episode) < 1:
        raise ValueError("valid_from_episode 须 >= 1")
    if valid_to_episode is not None and int(valid_to_episode) < int(valid_from_episode):
        raise LedgerConflict("valid_to_episode 不得早于 valid_from_episode")
    cur = conn.execute(
        "INSERT INTO facts (project_id, subject, predicate, object,"
        " valid_from_episode, valid_to_episode, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (_pid(project_id), subject, predicate, object, int(valid_from_episode),
         int(valid_to_episode) if valid_to_episode is not None else None, now()),
    )
    conn.commit()
    fact = _fact_by_id(conn, int(cur.lastrowid))
    assert fact is not None
    return fact


def list_facts(conn: sqlite3.Connection, project_id: int | str,
               episode: int | None = None, include_invalid: bool = True,
               subject: str | None = None) -> list[dict]:
    """列表 / 时点查询。

    ``episode`` 给定时按 ``valid_from <= episode <= valid_to OR valid_to IS NULL``
    过滤（任务约定的时点语义）；否则返回全部事实（含已失效的历史，除非
    ``include_invalid=False``）。
    """
    clauses = ["project_id=?"]
    values: list[Any] = [_pid(project_id)]
    if episode is not None:
        clauses.append("valid_from_episode <= ?")
        clauses.append("(valid_to_episode IS NULL OR valid_to_episode >= ?)")
        values.extend([int(episode), int(episode)])
    elif not include_invalid:
        clauses.append("valid_to_episode IS NULL")
    if subject is not None:
        clauses.append("subject=?")
        values.append(subject.strip())
    rows = conn.execute(
        f"SELECT * FROM facts WHERE {' AND '.join(clauses)}"
        " ORDER BY valid_from_episode, id",
        values,
    ).fetchall()
    return [_fact_dict(r) for r in rows]


def facts_at_episode(conn: sqlite3.Connection, project_id: int | str,
                     episode: int) -> list[dict]:
    return list_facts(conn, project_id, episode=int(episode), include_invalid=True)


def invalidate_fact(conn: sqlite3.Connection, project_id: int | str, fact_id: int,
                    at_episode: int) -> dict:
    """事实过期 = 标记失效（写 valid_to），不删除；历史查询仍可回溯。"""
    fact = _need_fact(conn, project_id, fact_id)
    if fact["valid_to_episode"] is not None:
        raise LedgerConflict(
            f"事实 {fact_id} 已于第 {fact['valid_to_episode']} 集失效，不可重复失效"
        )
    if int(at_episode) < int(fact["valid_from_episode"]):
        raise LedgerConflict(
            f"失效集次 {at_episode} 早于事实生效集次 {fact['valid_from_episode']}"
        )
    conn.execute("UPDATE facts SET valid_to_episode=? WHERE id=?",
                 (int(at_episode), fact_id))
    conn.commit()
    updated = _need_fact(conn, project_id, fact_id)
    return updated


# ---- 3.3 伏笔生命周期 ----


def create_foreshadow(conn: sqlite3.Connection, project_id: int | str, hint_text: str,
                      planted_in_episode: int, planned_resolution_episode: int | None = None,
                      current_status: str = "planted",
                      resolution_episode: int | None = None,
                      resolution_evidence: str | None = None) -> dict:
    hint_text = hint_text.strip()
    if not hint_text:
        raise ValueError("hint_text 须非空")
    planted = int(planted_in_episode)
    planned = (
        int(planned_resolution_episode)
        if planned_resolution_episode is not None
        else None
    )
    if planted < 1:
        raise ValueError("planted_in_episode 须 >= 1")
    if planned is not None and planned < planted:
        raise LedgerConflict("planned_resolution_episode 不得早于 planted_in_episode")
    if current_status not in FORESHADOW_STATUSES:
        raise ValueError(f"未知伏笔状态：{current_status}")
    if current_status == "resolved" and resolution_episode is None:
        raise ValueError("resolved 伏笔须提供 resolution_episode（回收场次/集次）")
    if resolution_episode is not None and int(resolution_episode) < planted:
        raise LedgerConflict("resolution_episode 不得早于 planted_in_episode")
    cur = conn.execute(
        "INSERT INTO foreshadow (project_id, planted_in_episode,"
        " planned_resolution_episode, current_status, resolution_episode,"
        " resolution_evidence, hint_text, created_at, reinforced_count, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_pid(project_id), planted, planned, current_status,
         int(resolution_episode) if resolution_episode is not None else None,
         resolution_evidence, hint_text, now(), 0, now()),
    )
    conn.commit()
    return get_foreshadow(conn, project_id, int(cur.lastrowid))


def get_foreshadow(conn: sqlite3.Connection, project_id: int | str,
                   foreshadow_id: int, current_episode: int | None = None) -> dict:
    row = conn.execute("SELECT * FROM foreshadow WHERE id=?",
                       (foreshadow_id,)).fetchone()
    if row is None or str(row["project_id"]) != _pid(project_id):
        raise LedgerNotFound(f"伏笔 {foreshadow_id} 不存在")
    return _foreshadow_dict(row, current_episode)


def list_foreshadows(conn: sqlite3.Connection, project_id: int | str,
                     current_episode: int | None = None,
                     status: str | None = None,
                     overdue_only: bool = False) -> list[dict]:
    clauses = ["project_id=?"]
    values: list[Any] = [_pid(project_id)]
    if status is not None:
        if status not in FORESHADOW_STATUSES:
            raise ValueError(f"未知伏笔状态：{status}")
        clauses.append("current_status=?")
        values.append(status)
    rows = conn.execute(
        f"SELECT * FROM foreshadow WHERE {' AND '.join(clauses)}"
        " ORDER BY planted_in_episode, id",
        values,
    ).fetchall()
    items = [_foreshadow_dict(r, current_episode) for r in rows]
    if overdue_only:
        items = [f for f in items if f["overdue"]]
    return items


def overdue_foreshadows(conn: sqlite3.Connection, project_id: int | str,
                        current_episode: int) -> list[dict]:
    """逾期 = 已越过计划回收集次且仍未回收（终态 resolved/abandoned 不再提示）。"""
    rows = conn.execute(
        "SELECT * FROM foreshadow WHERE project_id=?"
        " AND current_status IN ('planted','reinforced')"
        " AND planned_resolution_episode IS NOT NULL"
        " AND planned_resolution_episode < ?"
        " ORDER BY planned_resolution_episode, id",
        (_pid(project_id), int(current_episode)),
    ).fetchall()
    return [_foreshadow_dict(r, int(current_episode)) for r in rows]


def transition_foreshadow(conn: sqlite3.Connection, project_id: int | str,
                          foreshadow_id: int, action: str, episode: int,
                          evidence: str | None = None) -> dict:
    """伏笔状态迁移：reinforce / resolve / abandon（终态不可再迁移）。"""
    current = get_foreshadow(conn, project_id, foreshadow_id)
    status = current["current_status"]
    if status in TERMINAL_FORESHADOW_STATUSES:
        raise LedgerConflict(
            f"伏笔 {foreshadow_id} 已是终态 {status}，不可再迁移为 {action}"
        )
    episode = int(episode)
    if episode < int(current["planted_in_episode"]):
        raise LedgerConflict("迁移集次不得早于伏笔埋设集次")
    if action == "reinforce":
        conn.execute(
            "UPDATE foreshadow SET current_status='reinforced',"
            " reinforced_count=reinforced_count+1, last_reinforced_episode=?,"
            " updated_at=? WHERE id=?",
            (episode, now(), foreshadow_id),
        )
    elif action == "resolve":
        conn.execute(
            "UPDATE foreshadow SET current_status='resolved', resolution_episode=?,"
            " resolution_evidence=?, updated_at=? WHERE id=?",
            (episode, evidence, now(), foreshadow_id),
        )
    elif action == "abandon":
        conn.execute(
            "UPDATE foreshadow SET current_status='abandoned', resolution_episode=?,"
            " resolution_evidence=?, updated_at=? WHERE id=?",
            (episode, evidence, now(), foreshadow_id),
        )
    else:
        raise ValueError(f"未知迁移动作：{action}（支持 reinforce/resolve/abandon）")
    conn.commit()
    return get_foreshadow(conn, project_id, foreshadow_id, current_episode=episode)


# ---- 3.4 角色知识状态与越知检测 ----


def add_knowledge(conn: sqlite3.Connection, project_id: int | str, character_id: str,
                  learned_at_episode: int, fact_id: int | None = None,
                  fact_reference: str | None = None,
                  source_scene_id: int | None = None) -> dict:
    character_id = str(character_id).strip()
    if not character_id:
        raise ValueError("character_id 须非空")
    if int(learned_at_episode) < 1:
        raise ValueError("learned_at_episode 须 >= 1")
    if fact_id is not None:
        fact = _need_fact(conn, project_id, int(fact_id))
        reference = str(fact["id"])
    else:
        reference = (fact_reference or "").strip()
        if not reference:
            raise ValueError("fact_id 与 fact_reference 至少提供一个")
    existing = conn.execute(
        "SELECT * FROM knowledge WHERE project_id=? AND character_id=?"
        " AND fact_reference=? AND learned_at_episode=?",
        (_pid(project_id), character_id, reference, int(learned_at_episode)),
    ).fetchone()
    if existing is not None:
        return _knowledge_dict(conn, existing, episode=int(learned_at_episode))
    cur = conn.execute(
        "INSERT INTO knowledge (project_id, character_id, fact_reference,"
        " learned_at_episode, source_scene_id, created_at) VALUES (?,?,?,?,?,?)",
        (_pid(project_id), character_id, reference, int(learned_at_episode),
         int(source_scene_id) if source_scene_id is not None else None, now()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM knowledge WHERE id=?",
                       (int(cur.lastrowid),)).fetchone()
    return _knowledge_dict(conn, row, episode=int(learned_at_episode))


def _knowledge_dict(conn: sqlite3.Connection, row: sqlite3.Row,
                    episode: int | None = None) -> dict:
    reference = str(row["fact_reference"])
    fact = _resolve_fact_reference(conn, row["project_id"], reference)
    return {
        "id": int(row["id"]),
        "project_id": str(row["project_id"]),
        "character_id": str(row["character_id"]),
        "character_name": _character_name(conn, row["project_id"], row["character_id"]),
        "fact_id": fact["id"] if fact is not None else None,
        "fact_reference": reference,
        "learned_at_episode": int(row["learned_at_episode"]),
        "source_scene_id": (
            int(row["source_scene_id"]) if row["source_scene_id"] is not None else None
        ),
        "created_at": row["created_at"],
        "fact": fact,
        "fact_valid_at_episode": _fact_valid_at(fact, episode) if episode is not None else None,
    }


def knowledge_state(conn: sqlite3.Connection, project_id: int | str, character_id: str,
                    episode: int, include_future: bool = False,
                    strict_valid: bool = False) -> list[dict]:
    """角色在第 ``episode`` 集时点的知识状态（按角色隔离）。

    ``include_future=True`` 时一并返回 ``learned_at_episode > episode`` 的记录
    （越知检测用），否则只返回已获知的知识。
    ``strict_valid=True`` 时过滤掉事实已在该时点失效的知识条目。
    """
    clause = "" if include_future else " AND learned_at_episode <= ?"
    values: list[Any] = [_pid(project_id), str(character_id)]
    if not include_future:
        values.append(int(episode))
    rows = conn.execute(
        "SELECT * FROM knowledge WHERE project_id=? AND character_id=?" + clause
        + " ORDER BY learned_at_episode, id",
        values,
    ).fetchall()
    items = [_knowledge_dict(conn, r, episode=int(episode)) for r in rows]
    if strict_valid:
        items = [i for i in items if i["fact_valid_at_episode"] is not False]
    return items


def known_facts_at(conn: sqlite3.Connection, project_id: int | str, character_id: str,
                   episode: int) -> list[dict]:
    """该角色已获知、且事实在此时点仍成立的事实集合（生成前注入单位）。"""
    return knowledge_state(conn, project_id, character_id, episode, strict_valid=True)


def _violation_meta(scene_id: int | None, line_id: int | None,
                    line_idx: int | None) -> dict:
    return {"scene_id": scene_id, "line_id": line_id, "line_idx": line_idx}


def detect_knowledge_violations(conn: sqlite3.Connection, project_id: int | str,
                                character_id: str, episode: int, text: str,
                                scene_id: int | None = None,
                                line_id: int | None = None,
                                line_idx: int | None = None,
                                strict: bool = False) -> list[dict]:
    """越知 / 时间线矛盾检测（确定性：事实客体文本匹配）。

    - ``knowledge_violation``（block）：角色在 ``learned_at_episode`` 之前使用知识。
    - ``future_fact_mention``（warn）：台词提到该集尚未成立（valid_from 在未来）的事实。
    - ``knowledge_unrecorded``（warn，仅 strict）：事实客体出现但台账无该角色的获知记录。
    """
    text = text or ""
    if not text.strip():
        return []
    aliases = _character_aliases(conn, project_id, character_id)
    display = _character_name(conn, project_id, character_id) or str(character_id)
    rows = conn.execute(
        "SELECT * FROM knowledge WHERE project_id=? ORDER BY learned_at_episode, id",
        (_pid(project_id),),
    ).fetchall()
    knowledge_by_ref: dict[str, list[dict]] = {}
    for row in rows:
        if str(row["character_id"]) not in aliases:
            continue
        knowledge_by_ref.setdefault(str(row["fact_reference"]), []).append(dict(row))

    violations: list[dict] = []
    facts = conn.execute(
        "SELECT * FROM facts WHERE project_id=? ORDER BY valid_from_episode, id",
        (_pid(project_id),),
    ).fetchall()
    for frow in facts:
        fact = _fact_dict(frow)
        if fact["subject"] in aliases:
            continue  # 主体是角色本人：不构成「别人秘密被提前知道」
        object_text = fact["object"].strip()
        if not object_text:
            continue
        offset = text.find(object_text)
        if offset < 0:
            continue
        entries = knowledge_by_ref.get(str(fact["id"]), [])
        learned = min((int(e["learned_at_episode"]) for e in entries), default=None)
        if learned is not None and learned > int(episode):
            violations.append({
                "type": "knowledge_violation",
                "severity": "block",
                "message": (
                    f"{display} 在第{episode}集尚未获知「{object_text}」"
                    f"（第{learned}集才获知），台词提前使用"
                ),
                "character_id": str(character_id),
                "character_name": display,
                "fact_id": fact["id"],
                "fact": fact["signature"],
                "learned_at_episode": learned,
                "episode": int(episode),
                "matched": object_text,
                "offset": offset,
                **_violation_meta(scene_id, line_id, line_idx),
            })
        elif learned is None and int(fact["valid_from_episode"]) > int(episode):
            violations.append({
                "type": "future_fact_mention",
                "severity": "warn",
                "message": (
                    f"台词提到第{fact['valid_from_episode']}集才成立的事实「{object_text}」，"
                    f"当前时点为第{episode}集"
                ),
                "character_id": str(character_id),
                "character_name": display,
                "fact_id": fact["id"],
                "fact": fact["signature"],
                "learned_at_episode": None,
                "episode": int(episode),
                "matched": object_text,
                "offset": offset,
                **_violation_meta(scene_id, line_id, line_idx),
            })
        elif learned is None and strict and int(fact["valid_from_episode"]) <= int(episode):
            violations.append({
                "type": "knowledge_unrecorded",
                "severity": "warn",
                "message": (
                    f"台账无 {display} 对「{object_text}」的获知记录（strict 模式提示补录）"
                ),
                "character_id": str(character_id),
                "character_name": display,
                "fact_id": fact["id"],
                "fact": fact["signature"],
                "learned_at_episode": None,
                "episode": int(episode),
                "matched": object_text,
                "offset": offset,
                **_violation_meta(scene_id, line_id, line_idx),
            })

    # 签名式知识（fact_reference 形如 主体|谓词|客体）：无 facts 行时也能检测
    for reference, entries in knowledge_by_ref.items():
        if reference.isdigit():
            continue
        parts = [p.strip() for p in reference.split("|")]
        object_text = parts[2] if len(parts) >= 3 else ""
        if not object_text:
            continue
        offset = text.find(object_text)
        if offset < 0:
            continue
        learned = min(int(e["learned_at_episode"]) for e in entries)
        if learned > int(episode):
            violations.append({
                "type": "knowledge_violation",
                "severity": "block",
                "message": (
                    f"{display} 在第{episode}集尚未获知「{object_text}」"
                    f"（第{learned}集才获知），台词提前使用"
                ),
                "character_id": str(character_id),
                "character_name": display,
                "fact_id": None,
                "fact": reference,
                "learned_at_episode": learned,
                "episode": int(episode),
                "matched": object_text,
                "offset": offset,
                **_violation_meta(scene_id, line_id, line_idx),
            })
    violations.sort(key=lambda v: (v.get("offset") or 0, v["type"]))
    return violations


# ---- 3.5 生成前上下文组装 ----


def estimate_tokens(text: str) -> int:
    """保守 token 估算：中文 1 字 ≈ 1 token，ASCII 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return other + math.ceil(ascii_chars / _ASCII_CHARS_PER_TOKEN)


def _ledger_counts(conn: sqlite3.Connection, project_id: int | str) -> dict:
    pid = _pid(project_id)
    facts = int(conn.execute("SELECT COUNT(*) FROM facts WHERE project_id=?",
                             (pid,)).fetchone()[0])
    facts_current = int(conn.execute(
        "SELECT COUNT(*) FROM facts WHERE project_id=? AND valid_to_episode IS NULL",
        (pid,)).fetchone()[0])
    knowledge = int(conn.execute("SELECT COUNT(*) FROM knowledge WHERE project_id=?",
                                 (pid,)).fetchone()[0])
    foreshadows = int(conn.execute(
        "SELECT COUNT(*) FROM foreshadow WHERE project_id=?", (pid,)).fetchone()[0])
    foreshadows_open = int(conn.execute(
        "SELECT COUNT(*) FROM foreshadow WHERE project_id=?"
        " AND current_status IN ('planted','reinforced')", (pid,)).fetchone()[0])
    return {
        "facts": facts,
        "facts_current": facts_current,
        "knowledge": knowledge,
        "foreshadows": foreshadows,
        "foreshadows_open": foreshadows_open,
        "total": facts + knowledge + foreshadows,
    }


def ledger_status(conn: sqlite3.Connection, project_id: int | str) -> dict:
    counts = _ledger_counts(conn, project_id)
    enabled = counts["total"] > 0
    return {
        "project_id": _pid(project_id),
        "enabled": enabled,
        "available": enabled,
        "reason": None if enabled else EMPTY_LEDGER_REASON,
        "counts": counts,
    }


def _render_knowledge_section(episode: int, characters: list[dict]) -> str:
    lines: list[str] = []
    for item in characters:
        knowledge = item["knowledge"]
        if not knowledge:
            continue
        label = item["character_name"] or item["character_id"]
        for entry in knowledge:
            fact = entry.get("fact")
            detail = (
                f"{fact['subject']} {fact['predicate']} {fact['object']}"
                if fact is not None else entry["fact_reference"]
            )
            lines.append(
                f"- {label} 于第{entry['learned_at_episode']}集获知：{detail}"
            )
    if not lines:
        return ""
    return "角色知识（按角色隔离，只注入各自已知）：\n" + "\n".join(lines)


def _render_fact_line(fact: dict) -> str:
    suffix = (
        f"第{fact['valid_from_episode']}集起"
        if fact["valid_to_episode"] is None
        else f"第{fact['valid_from_episode']}–{fact['valid_to_episode']}集"
    )
    return f"- {fact['subject']} {fact['predicate']} {fact['object']}（{suffix}）"


def _render_foreshadow_line(fs: dict) -> str:
    plan = (
        f"计划第{fs['planned_resolution_episode']}集回收"
        if fs["planned_resolution_episode"] is not None
        else "未定回收集次"
    )
    state = _STATUS_LABELS.get(fs["current_status"], fs["current_status"])
    overdue = f"，已逾期 {fs['overdue_by']} 集" if fs["overdue"] else ""
    return (
        f"- 「{fs['hint_text']}」（第{fs['planted_in_episode']}集埋设，"
        f"{plan}，{state}{overdue}）"
    )


def _empty_context(episode: int | None, token_budget: int, reason: str) -> dict:
    return {
        "available": False,
        "enabled": False,
        "reason": reason,
        "episode": episode,
        "characters": [],
        "facts": [],
        "foreshadows": [],
        "overdue": [],
        "text": "",
        "token_budget": token_budget,
        "token_estimate": 0,
        "truncated": {
            "facts_dropped": 0,
            "foreshadows_dropped": 0,
            "knowledge_dropped": 0,
            "budget_exceeded": False,
        },
        "budget_exceeded": False,
    }


def assemble_context(conn: sqlite3.Connection, project_id: int | str, episode: int,
                     character_ids: list[str] | None = None,
                     token_budget: int = DEFAULT_TOKEN_BUDGET) -> dict:
    """生成前上下文：时点 + 角色视角 + token 预算。

    固定顺序：角色知识 → 有效事实 → 未回收伏笔。超预算时先丢低相关事实，
    再丢伏笔；角色知识永不丢弃（spec：超限时优先保留角色知识）。
    """
    episode = int(episode)
    budget = max(1, int(token_budget))
    counts = _ledger_counts(conn, project_id)
    if counts["total"] == 0:
        return _empty_context(episode, budget, EMPTY_LEDGER_REASON)

    facts = facts_at_episode(conn, project_id, episode)
    foreshadows = [
        _foreshadow_dict(r, episode)
        for r in conn.execute(
            "SELECT * FROM foreshadow WHERE project_id=?"
            " AND planted_in_episode <= ?"
            " AND current_status IN ('planted','reinforced')"
            " ORDER BY planted_in_episode, id",
            (_pid(project_id), episode),
        ).fetchall()
    ]
    overdue = overdue_foreshadows(conn, project_id, episode)

    if character_ids is None:
        ids = [
            str(r["character_id"])
            for r in conn.execute(
                "SELECT DISTINCT character_id FROM knowledge WHERE project_id=?"
                " ORDER BY character_id",
                (_pid(project_id),),
            ).fetchall()
        ]
    else:
        ids = [str(c) for c in character_ids]

    characters: list[dict] = []
    for cid in ids:
        known = knowledge_state(conn, project_id, cid, episode, strict_valid=True)
        future = knowledge_state(conn, project_id, cid, episode, include_future=True)
        future_count = sum(
            1 for k in future if int(k["learned_at_episode"]) > episode
        )
        characters.append({
            "character_id": cid,
            "character_name": _character_name(conn, project_id, cid),
            "knowledge": known,
            "known_facts": [k["fact"] for k in known if k.get("fact") is not None],
            "future_knowledge_count": future_count,
        })

    knowledge_text = _render_knowledge_section(episode, characters)
    base_tokens = estimate_tokens(knowledge_text)
    character_names = {
        (c["character_name"] or "") for c in characters if c["character_name"]
    } | {c["character_id"] for c in characters}

    # 事实相关性：角色相关 / 与伏笔线索相关的事实优先保留
    def relevance(fact: dict) -> int:
        score = 0
        if fact["subject"] in character_names:
            score += 2
        for fs in foreshadows:
            hint = fs["hint_text"]
            if hint and (hint in fact["object"] or fact["object"] in hint):
                score += 1
                break
        return score

    fact_lines = [(f, _render_fact_line(f)) for f in facts]
    fs_lines = [(f, _render_foreshadow_line(f)) for f in foreshadows]
    fact_costs = [estimate_tokens(line) + 1 for _, line in fact_lines]
    fs_costs = [estimate_tokens(line) + 1 for _, line in fs_lines]
    remaining = base_tokens + sum(fact_costs) + sum(fs_costs)

    dropped_facts: set[int] = set()
    drop_order = sorted(
        range(len(fact_lines)),
        key=lambda i: (
            relevance(fact_lines[i][0]),
            fact_lines[i][0]["valid_from_episode"],
            fact_lines[i][0]["id"],
        ),
    )
    while remaining > budget and drop_order:
        index = drop_order.pop(0)
        if index in dropped_facts:
            continue
        dropped_facts.add(index)
        remaining -= fact_costs[index]

    dropped_fs: set[int] = set()
    fs_drop_order = list(range(len(fs_lines)))
    while remaining > budget and fs_drop_order:
        index = fs_drop_order.pop()  # 从计划回收最晚/最后登记的开始丢
        dropped_fs.add(index)
        remaining -= fs_costs[index]

    kept_facts = [f for i, (f, _) in enumerate(fact_lines) if i not in dropped_facts]
    kept_fs = [f for i, (f, _) in enumerate(fs_lines) if i not in dropped_fs]

    sections: list[str] = []
    if knowledge_text:
        sections.append(knowledge_text)
    if kept_facts:
        sections.append(
            "本集有效事实（不含未来事实）：\n"
            + "\n".join(_render_fact_line(f) for f in kept_facts)
        )
    if kept_fs:
        sections.append(
            "未回收伏笔：\n" + "\n".join(_render_foreshadow_line(f) for f in kept_fs)
        )
    header = f"【连续性台账 · 第{episode}集时点】"
    text = "\n".join([header, *sections]) if sections else header
    budget_exceeded = base_tokens > budget

    return {
        "available": True,
        "enabled": True,
        "reason": None,
        "episode": episode,
        "characters": characters,
        "facts": kept_facts,
        "foreshadows": kept_fs,
        "overdue": overdue,
        "text": text,
        "token_budget": budget,
        "token_estimate": estimate_tokens(text),
        "truncated": {
            "facts_dropped": len(dropped_facts),
            "foreshadows_dropped": len(dropped_fs),
            "knowledge_dropped": 0,
            "budget_exceeded": budget_exceeded,
        },
        "budget_exceeded": budget_exceeded,
    }


# ---- 3.6 生成后确定性检查 ----


def _as_line_dict(line: Any) -> dict:
    if isinstance(line, dict):
        return line
    dump = getattr(line, "model_dump", None)
    if callable(dump):
        return dump()
    return dict(line)


def _empty_check(episode: int | None, reason: str) -> dict:
    return {
        "available": False,
        "enabled": False,
        "reason": reason,
        "episode": episode,
        "violations": [],
        "blocked": False,
        "counts": {"block": 0, "warn": 0},
        "checks": {
            "entities": "skipped",
            "knowledge": "skipped",
            "foreshadow": "skipped",
        },
    }


def check_generated(conn: sqlite3.Connection, project_id: int | str, episode: int,
                    lines: list[Any], known_characters: list[str] | None = None,
                    strict_knowledge: bool = False) -> dict:
    """生成后确定性检查：实体名、知识时点、伏笔状态，全部定位到台词。"""
    episode = int(episode)
    counts = _ledger_counts(conn, project_id)
    if counts["total"] == 0:
        return _empty_check(episode, EMPTY_LEDGER_REASON)

    normalized = [_as_line_dict(line) for line in (lines or [])]
    violations: list[dict] = []

    # 1) 实体名比对（角色表）
    if known_characters is not None:
        known = {str(n).strip() for n in known_characters if str(n).strip()}
    else:
        try:
            known = {
                str(r["name"]).strip()
                for r in conn.execute(
                    "SELECT name FROM characters WHERE project_id=?",
                    (int(project_id),),
                ).fetchall()
            }
        except (sqlite3.OperationalError, TypeError, ValueError):
            known = set()

    entity_violated = False
    for position, line in enumerate(normalized):
        text = str(line.get("text") or "")
        speaker = str(line.get("speaker") or "").strip()
        line_meta = _violation_meta(
            line.get("scene_id"), line.get("line_id"),
            line.get("idx") if line.get("idx") is not None else position,
        )
        if known and speaker and speaker not in known:
            entity_violated = True
            offset = text.find(speaker)
            violations.append({
                "type": "unknown_entity",
                "severity": "warn",
                "message": f"实体名「{speaker}」不在角色表中",
                "character_id": None,
                "character_name": speaker,
                "fact_id": None,
                "fact": None,
                "learned_at_episode": None,
                "episode": episode,
                "matched": speaker,
                "offset": offset if offset >= 0 else None,
                **line_meta,
            })

    # 2) 知识时点 / 时间线矛盾
    knowledge_violated = False
    if counts["facts"] + counts["knowledge"] > 0:
        for position, line in enumerate(normalized):
            text = str(line.get("text") or "")
            speaker = str(line.get("speaker") or "").strip()
            if not speaker or not text:
                continue
            found = detect_knowledge_violations(
                conn, project_id, speaker, episode, text,
                scene_id=line.get("scene_id"),
                line_id=line.get("line_id"),
                line_idx=line.get("idx") if line.get("idx") is not None else position,
                strict=strict_knowledge,
            )
            if found:
                knowledge_violated = True
                violations.extend(found)

    # 3) 伏笔状态：已回收/已放弃的线索再次出现、过早提及、逾期未回收
    foreshadow_violated = False
    foreshadows = list_foreshadows(conn, project_id, current_episode=episode)
    for position, line in enumerate(normalized):
        text = str(line.get("text") or "")
        if not text:
            continue
        for fs in foreshadows:
            hint = (fs["hint_text"] or "").strip()
            if not hint:
                continue
            offset = text.find(hint)
            if offset < 0:
                continue
            if fs["current_status"] in TERMINAL_FORESHADOW_STATUSES:
                foreshadow_violated = True
                violations.append({
                    "type": "foreshadow_status_conflict",
                    "severity": "warn",
                    "message": (
                        f"伏笔「{hint}」已是终态"
                        f"{_STATUS_LABELS.get(fs['current_status'], fs['current_status'])}，"
                        "台词再次提及但未重新埋设"
                    ),
                    "character_id": None,
                    "character_name": None,
                    "fact_id": None,
                    "fact": None,
                    "learned_at_episode": None,
                    "episode": episode,
                    "matched": hint,
                    "offset": offset,
                    "foreshadow_id": fs["id"],
                    "hint_text": hint,
                    "status": fs["current_status"],
                    **_violation_meta(
                        line.get("scene_id"), line.get("line_id"),
                        line.get("idx") if line.get("idx") is not None else position,
                    ),
                })
            elif int(fs["planted_in_episode"]) > episode:
                foreshadow_violated = True
                violations.append({
                    "type": "foreshadow_early_mention",
                    "severity": "warn",
                    "message": (
                        f"伏笔「{hint}」计划第{fs['planted_in_episode']}集才埋设，"
                        f"当前为第{episode}集"
                    ),
                    "character_id": None,
                    "character_name": None,
                    "fact_id": None,
                    "fact": None,
                    "learned_at_episode": None,
                    "episode": episode,
                    "matched": hint,
                    "offset": offset,
                    "foreshadow_id": fs["id"],
                    "hint_text": hint,
                    "status": fs["current_status"],
                    **_violation_meta(
                        line.get("scene_id"), line.get("line_id"),
                        line.get("idx") if line.get("idx") is not None else position,
                    ),
                })

    for fs in overdue_foreshadows(conn, project_id, episode):
        foreshadow_violated = True
        violations.append({
            "type": "foreshadow_overdue",
            "severity": "warn",
            "message": (
                f"伏笔「{fs['hint_text']}」计划第{fs['planned_resolution_episode']}集回收，"
                f"当前第{episode}集仍未回收（逾期 {fs['overdue_by']} 集）"
            ),
            "character_id": None,
            "character_name": None,
            "fact_id": None,
            "fact": None,
            "learned_at_episode": None,
            "episode": episode,
            "matched": None,
            "offset": None,
            "foreshadow_id": fs["id"],
            "hint_text": fs["hint_text"],
            "status": fs["current_status"],
            "scene_id": None,
            "line_id": None,
            "line_idx": None,
        })

    violations.sort(
        key=lambda v: (
            1 if v.get("line_idx") is None and v.get("line_id") is None else 0,
            v.get("scene_id") or 0,
            v.get("line_idx") or 0,
            v.get("offset") or 0,
        )
    )
    block_count = sum(1 for v in violations if v["severity"] == "block")
    warn_count = sum(1 for v in violations if v["severity"] == "warn")
    return {
        "available": True,
        "enabled": True,
        "reason": None,
        "episode": episode,
        "violations": violations,
        "blocked": block_count > 0,
        "counts": {"block": block_count, "warn": warn_count},
        "checks": {
            "entities": (
                "skipped" if not known
                else ("violated" if entity_violated else "ok")
            ),
            "knowledge": (
                "skipped" if counts["facts"] + counts["knowledge"] == 0
                else ("violated" if knowledge_violated else "ok")
            ),
            "foreshadow": (
                "skipped" if counts["foreshadows"] == 0
                else ("violated" if foreshadow_violated else "ok")
            ),
        },
    }


# ---- pipeline 集成接缝（3.5 / 3.6 / 3.7） ----


def _scene_meta(conn: sqlite3.Connection, scene_id: int) -> dict | None:
    try:
        row = conn.execute(
            "SELECT id, project_id, idx, title FROM scenes WHERE id=?", (scene_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "idx": int(row["idx"]) if row["idx"] is not None else 1,
        "title": row["title"],
    }


def pre_generation_context_for_scene(scene_id: int, episode: int | None = None,
                                     token_budget: int = DEFAULT_TOKEN_BUDGET,
                                     character_ids: list[str] | None = None) -> dict:
    """pipeline 生成前接缝：按场次找项目/角色，组装上下文；任何失败都降级不阻断。"""
    conn: sqlite3.Connection | None = None
    try:
        conn = connect()
        migrate(conn)
        meta = _scene_meta(conn, scene_id)
        if meta is None:
            return _empty_context(
                episode, int(token_budget),
                f"场次 {scene_id} 不存在或剧本库未就绪，连续性上下文不可用（创作流程继续）",
            )
        ep = int(episode) if episode is not None else meta["idx"]
        chars = character_ids
        if chars is None:
            try:
                chars = [
                    str(r["speaker_id"])
                    for r in conn.execute(
                        "SELECT DISTINCT speaker_id FROM lines WHERE scene_id=?"
                        " ORDER BY speaker_id",
                        (scene_id,),
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                chars = None
        return assemble_context(
            conn, meta["project_id"], ep, character_ids=chars,
            token_budget=token_budget,
        )
    except Exception as exc:  # noqa: BLE001 —— 台账不可用绝不阻断生成
        return _empty_context(
            episode, int(token_budget),
            f"连续性台账不可用：{exc}（创作流程继续）",
        )
    finally:
        if conn is not None:
            conn.close()


def post_generation_check_for_scene(scene_id: int, shots: list[dict],
                                    episode: int | None = None,
                                    strict_knowledge: bool = False) -> dict:
    """pipeline 生成后接缝：把镜头里的台词逐条做确定性检查；失败降级不阻断。"""
    conn: sqlite3.Connection | None = None
    try:
        conn = connect()
        migrate(conn)
        meta = _scene_meta(conn, scene_id)
        if meta is None:
            return _empty_check(
                episode,
                f"场次 {scene_id} 不存在或剧本库未就绪，连续性检查不可用（创作流程继续）",
            )
        ep = int(episode) if episode is not None else meta["idx"]
        lines: list[dict] = []
        for shot in shots or []:
            text = str(shot.get("line_text") or "").strip()
            if not text:
                continue
            lines.append({
                "scene_id": scene_id,
                "line_id": None,
                "idx": shot.get("idx"),
                "speaker": str(shot.get("line_speaker") or "").strip(),
                "text": text,
            })
        try:
            known = [
                str(r["name"])
                for r in conn.execute(
                    "SELECT name FROM characters WHERE project_id=? ORDER BY id",
                    (meta["project_id"],),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            known = []
        return check_generated(
            conn, meta["project_id"], ep, lines,
            known_characters=known or None,
            strict_knowledge=strict_knowledge,
        )
    except Exception as exc:  # noqa: BLE001 —— 检查不可用绝不阻断生成
        return _empty_check(episode, f"连续性检查不可用：{exc}（创作流程继续）")
    finally:
        if conn is not None:
            conn.close()


# ---- API 模型 ----


class FactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=200)
    object: str = Field(min_length=1, max_length=500)
    valid_from_episode: int = Field(ge=1)
    valid_to_episode: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _range(self) -> "FactIn":
        if self.valid_to_episode is not None and self.valid_to_episode < self.valid_from_episode:
            raise ValueError("valid_to_episode 不得早于 valid_from_episode")
        return self


class FactOut(BaseModel):
    id: int
    project_id: str
    subject: str
    predicate: str
    object: str
    valid_from_episode: int
    valid_to_episode: int | None = None
    created_at: str
    signature: str
    invalidated: bool


class InvalidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at_episode: int = Field(ge=1)


class ForeshadowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hint_text: str = Field(min_length=1, max_length=1000)
    planted_in_episode: int = Field(ge=1)
    planned_resolution_episode: int | None = Field(default=None, ge=1)
    current_status: str = Field(
        default="planted", pattern="^(planted|reinforced|resolved|abandoned)$"
    )
    resolution_episode: int | None = Field(default=None, ge=1)
    resolution_evidence: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _range(self) -> "ForeshadowIn":
        if (
            self.planned_resolution_episode is not None
            and self.planned_resolution_episode < self.planted_in_episode
        ):
            raise ValueError("planned_resolution_episode 不得早于 planted_in_episode")
        if self.current_status == "resolved" and self.resolution_episode is None:
            raise ValueError("resolved 伏笔须提供 resolution_episode")
        return self


class ForeshadowOut(BaseModel):
    id: int
    project_id: str
    planted_in_episode: int
    planned_resolution_episode: int | None = None
    current_status: str
    resolution_episode: int | None = None
    resolution_evidence: str | None = None
    hint_text: str
    created_at: str
    reinforced_count: int = 0
    last_reinforced_episode: int | None = None
    updated_at: str | None = None
    overdue: bool = False
    overdue_by: int | None = None


class ReinforceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: int = Field(ge=1)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: int = Field(ge=1)
    evidence: str | None = Field(default=None, max_length=2000)


class AbandonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class KnowledgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1, max_length=200)
    fact_id: int | None = None
    fact_reference: str | None = Field(default=None, max_length=500)
    learned_at_episode: int = Field(ge=1)
    source_scene_id: int | None = None

    @model_validator(mode="after")
    def _ref(self) -> "KnowledgeIn":
        if self.fact_id is None and not (self.fact_reference or "").strip():
            raise ValueError("fact_id 与 fact_reference 至少提供一个")
        return self


class KnowledgeOut(BaseModel):
    id: int
    project_id: str
    character_id: str
    character_name: str | None = None
    fact_id: int | None = None
    fact_reference: str
    learned_at_episode: int
    source_scene_id: int | None = None
    created_at: str
    fact: FactOut | None = None
    fact_valid_at_episode: bool | None = None


class KnowledgeCheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1, max_length=200)
    episode: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4000)
    scene_id: int | None = None
    line_id: int | None = None
    line_idx: int | None = None
    strict: bool = False


class ViolationOut(BaseModel):
    type: str
    severity: Literal["block", "warn"]
    message: str
    scene_id: int | None = None
    line_id: int | None = None
    line_idx: int | None = None
    character_id: str | None = None
    character_name: str | None = None
    fact_id: int | None = None
    fact: str | None = None
    learned_at_episode: int | None = None
    episode: int | None = None
    matched: str | None = None
    offset: int | None = None
    foreshadow_id: int | None = None
    hint_text: str | None = None
    status: str | None = None


class ContextCharacterOut(BaseModel):
    character_id: str
    character_name: str | None = None
    knowledge: list[KnowledgeOut] = Field(default_factory=list)
    known_facts: list[FactOut] = Field(default_factory=list)
    future_knowledge_count: int = 0


class ContextOut(BaseModel):
    available: bool
    enabled: bool
    reason: str | None = None
    episode: int | None = None
    characters: list[ContextCharacterOut] = Field(default_factory=list)
    facts: list[FactOut] = Field(default_factory=list)
    foreshadows: list[ForeshadowOut] = Field(default_factory=list)
    overdue: list[ForeshadowOut] = Field(default_factory=list)
    text: str = ""
    token_budget: int
    token_estimate: int
    truncated: dict[str, Any] = Field(default_factory=dict)
    budget_exceeded: bool = False


class CheckLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: int | None = None
    line_id: int | None = None
    idx: int | None = None
    speaker: str | None = Field(default=None, max_length=200)
    text: str = Field(default="", max_length=4000)


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: int = Field(ge=1)
    lines: list[CheckLineIn] = Field(default_factory=list)
    known_characters: list[str] | None = None
    strict_knowledge: bool = False


class CheckOut(BaseModel):
    available: bool
    enabled: bool
    reason: str | None = None
    episode: int | None = None
    violations: list[ViolationOut] = Field(default_factory=list)
    blocked: bool = False
    counts: dict[str, int] = Field(default_factory=dict)
    checks: dict[str, str] = Field(default_factory=dict)


class StatusOut(BaseModel):
    project_id: str
    enabled: bool
    available: bool
    reason: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)


# ---- API 路由 ----


router = APIRouter(prefix="/api/v1/ledger", tags=["ledger"])


def _need_project(conn: sqlite3.Connection, user_id: str, pid: int) -> str:
    """项目归属校验（共库 projects 表只读）；越权与不存在统一 404。"""
    try:
        from . import screenplay

        screenplay.migrate(conn)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"剧本模块未就绪：{exc}") from exc
    row = conn.execute(
        "SELECT id FROM projects WHERE id=? AND user_id=?", (pid, user_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return str(pid)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LedgerNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LedgerConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/projects/{pid}/status", response_model=StatusOut)
def api_status(pid: int, user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return ledger_status(conn, pid)
    finally:
        conn.close()


# ---- facts ----


@router.post("/projects/{pid}/facts", status_code=201, response_model=FactOut)
def api_create_fact(pid: int, body: FactIn,
                    user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(create_fact, conn, pid, body.subject, body.predicate,
                     body.object, body.valid_from_episode, body.valid_to_episode)
    finally:
        conn.close()


@router.get("/projects/{pid}/facts", response_model=list[FactOut])
def api_list_facts(pid: int,
                   episode: int | None = Query(default=None, ge=1),
                   include_invalid: bool = Query(default=True),
                   subject: str | None = Query(default=None),
                   user_id: str = Depends(require_user)) -> list[dict]:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return list_facts(conn, pid, episode=episode,
                          include_invalid=include_invalid, subject=subject)
    finally:
        conn.close()


@router.get("/projects/{pid}/facts/timepoint", response_model=list[FactOut])
def api_facts_at_episode(pid: int, episode: int = Query(ge=1),
                         user_id: str = Depends(require_user)) -> list[dict]:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return facts_at_episode(conn, pid, episode)
    finally:
        conn.close()


@router.post("/projects/{pid}/facts/{fact_id}/invalidate", response_model=FactOut)
def api_invalidate_fact(pid: int, fact_id: int, body: InvalidateIn,
                        user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(invalidate_fact, conn, pid, fact_id, body.at_episode)
    finally:
        conn.close()


@router.get("/projects/{pid}/facts/{fact_id}", response_model=FactOut)
def api_get_fact(pid: int, fact_id: int,
                 user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(_need_fact, conn, pid, fact_id)
    finally:
        conn.close()


# ---- foreshadow ----


@router.post("/projects/{pid}/foreshadows", status_code=201,
             response_model=ForeshadowOut)
def api_create_foreshadow(pid: int, body: ForeshadowIn,
                          user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(
            create_foreshadow, conn, pid, body.hint_text, body.planted_in_episode,
            body.planned_resolution_episode, body.current_status,
            body.resolution_episode, body.resolution_evidence,
        )
    finally:
        conn.close()


@router.get("/projects/{pid}/foreshadows", response_model=list[ForeshadowOut])
def api_list_foreshadows(pid: int,
                         episode: int | None = Query(default=None, ge=1),
                         status: str | None = Query(default=None),
                         overdue_only: bool = Query(default=False),
                         user_id: str = Depends(require_user)) -> list[dict]:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(list_foreshadows, conn, pid, episode, status, overdue_only)
    finally:
        conn.close()


@router.get("/projects/{pid}/foreshadows/overdue",
            response_model=list[ForeshadowOut])
def api_overdue_foreshadows(pid: int, episode: int = Query(ge=1),
                            user_id: str = Depends(require_user)) -> list[dict]:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return overdue_foreshadows(conn, pid, episode)
    finally:
        conn.close()


@router.get("/projects/{pid}/foreshadows/{foreshadow_id}",
            response_model=ForeshadowOut)
def api_get_foreshadow(pid: int, foreshadow_id: int,
                       episode: int | None = Query(default=None, ge=1),
                       user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(get_foreshadow, conn, pid, foreshadow_id, episode)
    finally:
        conn.close()


@router.post("/projects/{pid}/foreshadows/{foreshadow_id}/reinforce",
             response_model=ForeshadowOut)
def api_reinforce_foreshadow(pid: int, foreshadow_id: int, body: ReinforceIn,
                             user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(transition_foreshadow, conn, pid, foreshadow_id,
                     "reinforce", body.episode)
    finally:
        conn.close()


@router.post("/projects/{pid}/foreshadows/{foreshadow_id}/resolve",
             response_model=ForeshadowOut)
def api_resolve_foreshadow(pid: int, foreshadow_id: int, body: ResolveIn,
                           user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(transition_foreshadow, conn, pid, foreshadow_id,
                     "resolve", body.episode, body.evidence)
    finally:
        conn.close()


@router.post("/projects/{pid}/foreshadows/{foreshadow_id}/abandon",
             response_model=ForeshadowOut)
def api_abandon_foreshadow(pid: int, foreshadow_id: int, body: AbandonIn,
                           user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(transition_foreshadow, conn, pid, foreshadow_id,
                     "abandon", body.episode, body.reason)
    finally:
        conn.close()


# ---- knowledge ----


@router.post("/projects/{pid}/knowledge", status_code=201,
             response_model=KnowledgeOut)
def api_add_knowledge(pid: int, body: KnowledgeIn,
                      user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(
            add_knowledge, conn, pid, body.character_id, body.learned_at_episode,
            body.fact_id, body.fact_reference, body.source_scene_id,
        )
    finally:
        conn.close()


@router.get("/projects/{pid}/knowledge", response_model=list[KnowledgeOut])
def api_list_knowledge(pid: int,
                       character_id: str = Query(min_length=1),
                       episode: int = Query(ge=1),
                       include_future: bool = Query(default=False),
                       user_id: str = Depends(require_user)) -> list[dict]:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return knowledge_state(conn, pid, character_id, episode,
                               include_future=include_future)
    finally:
        conn.close()


@router.get("/projects/{pid}/knowledge/state", response_model=list[KnowledgeOut])
def api_knowledge_state(pid: int,
                        character_id: str = Query(min_length=1),
                        episode: int = Query(ge=1),
                        user_id: str = Depends(require_user)) -> list[dict]:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return known_facts_at(conn, pid, character_id, episode)
    finally:
        conn.close()


@router.post("/projects/{pid}/knowledge/check", response_model=list[ViolationOut])
def api_check_knowledge(pid: int, body: KnowledgeCheckIn,
                        user_id: str = Depends(require_user)) -> list[dict]:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return detect_knowledge_violations(
            conn, pid, body.character_id, body.episode, body.text,
            scene_id=body.scene_id, line_id=body.line_id,
            line_idx=body.line_idx, strict=body.strict,
        )
    finally:
        conn.close()


# ---- 生成前上下文 / 生成后检查 ----


@router.get("/projects/{pid}/context", response_model=ContextOut)
def api_context(pid: int,
                episode: int = Query(ge=1),
                character_id: list[str] | None = Query(default=None),
                token_budget: int = Query(default=DEFAULT_TOKEN_BUDGET,
                                          ge=1, le=20000),
                user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return assemble_context(conn, pid, episode,
                                character_ids=character_id,
                                token_budget=token_budget)
    finally:
        conn.close()


@router.post("/projects/{pid}/check", response_model=CheckOut)
def api_check_generated(pid: int, body: CheckIn,
                        user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return check_generated(
            conn, pid, body.episode, body.lines,
            known_characters=body.known_characters,
            strict_knowledge=body.strict_knowledge,
        )
    finally:
        conn.close()
