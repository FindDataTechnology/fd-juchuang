"""叙事连续性台账（change juchuang-continuity-layer task 3.1-3.7）离线测试。

全离线：临时 SQLite 共库（screenplay 五表 + ledger 三表），不依赖网络与 LLM。
覆盖：迁移正向/回滚 → 双时态事实与按时点查询 → 伏笔生命周期与逾期 → 角色知识
隔离与越知检测 → 生成前上下文（时点/角色视角/token 预算）→ 生成后确定性检查
（实体名/知识时点/伏笔状态，含台词定位）→ 空台账降级 → pipeline 接缝。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_juchuang_api import ledger, pipeline, screenplay

A = {"Authorization": "Bearer tok-a"}
B = {"Authorization": "Bearer tok-b"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ledger-test.db")
    monkeypatch.setenv("FD_JUCHUANG_DB", db_path)
    monkeypatch.setenv("FD_JUCHUANG_AUTH_DEV_TOKENS", "tok-a=u1,tok-b=u2")
    monkeypatch.setenv("FD__AUTH_DEV_TOKENS", "tok-a=u1,tok-b=u2")
    return db_path


@pytest.fixture()
def client(env):
    app = FastAPI()
    app.include_router(ledger.router)
    return TestClient(app)


def _conn():
    conn = ledger.connect()
    ledger.migrate(conn)
    return conn


def _setup_project(pid: int = 1, user: str = "u1",
                   names: tuple[str, ...] = ("陈守拙", "林务实", "苏未晚"),
                   with_scene: bool = False) -> dict:
    """建 screenplay 项目/角色（可含一场一台词），返回 id 映射。"""
    conn = screenplay.connect()
    try:
        screenplay.migrate(conn)
        conn.execute(
            "INSERT INTO projects (id, user_id, title, logline, created_at)"
            " VALUES (?,?,?,?,?)", (pid, user, f"项目{pid}", None, "2026-01-01"))
        ids = {}
        for name in names:
            cur = conn.execute(
                "INSERT INTO characters (project_id, name, appearance, stance, created_at)"
                " VALUES (?,?,?,?,?)",
                (pid, name, '{"年龄感":"中年","着装":"深色"}', None, "2026-01-01"),
            )
            ids[name] = int(cur.lastrowid)
        out = {"project_id": pid, "characters": ids}
        if with_scene:
            cur = conn.execute(
                "INSERT INTO scenes (project_id, idx, title, synopsis, created_at)"
                " VALUES (?,?,?,?,?)", (pid, 1, "第一场：对峙", "旧友重逢", "2026-01-01"))
            scene_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO lines (scene_id, idx, speaker_id, text) VALUES (?,?,?,?)",
                (scene_id, 1, ids[names[1]], "密钥在哪？"))
            out["scene_id"] = scene_id
        conn.commit()
    finally:
        conn.close()
    return out


def _add_scene(pid: int, title: str, idx: int = 1) -> int:
    conn = screenplay.connect()
    try:
        screenplay.migrate(conn)
        cur = conn.execute(
            "INSERT INTO scenes (project_id, idx, title, synopsis, created_at)"
            " VALUES (?,?,?,?,?)", (pid, idx, title, "场景", "2026-01-01"))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


# ---------------------------------------------------------------- 3.1 迁移


def test_migration_forward_and_rollback(env):
    conn = _conn()
    try:
        assert {"facts", "knowledge", "foreshadow", "ledger_migrations"} <= _tables(conn)
        state = ledger.migration_state(conn)
        assert len(state) == 1 and state[0]["applied"] is True

        ledger.create_fact(conn, 1, "陈守拙", "拥有", "密钥", 3)
        assert ledger.rollback(conn) == [1]
        tables = _tables(conn)
        assert not ({"facts", "knowledge", "foreshadow"} & tables)
        assert "ledger_migrations" in tables
        assert ledger.migration_state(conn)[0]["applied"] is False

        # 回滚后可再次正向执行
        assert ledger.migrate(conn) == 1
        assert {"facts", "knowledge", "foreshadow"} <= _tables(conn)
        assert ledger.migrate(conn) == 1  # 幂等重放
    finally:
        conn.close()


# ---------------------------------------------------------------- 3.2 时序事实


def test_fact_timepoint_query_excludes_future_facts(env):
    conn = _conn()
    try:
        early = ledger.create_fact(conn, 1, "陈守拙", "拥有", "密钥", 3)
        future = ledger.create_fact(conn, 1, "林务实", "得到", "密令", 7)

        at5 = ledger.facts_at_episode(conn, 1, 5)
        assert [f["id"] for f in at5] == [early["id"]]
        assert "密令" not in {f["object"] for f in at5}

        at7 = ledger.facts_at_episode(conn, 1, 7)
        assert {f["id"] for f in at7} == {early["id"], future["id"]}

        # 过期标记失效而非删除：历史仍可查
        invalidated = ledger.invalidate_fact(conn, 1, early["id"], 8)
        assert invalidated["valid_to_episode"] == 8
        assert invalidated["invalidated"] is True
        history = ledger.list_facts(conn, 1)
        assert {f["id"] for f in history} == {early["id"], future["id"]}
        assert ledger.facts_at_episode(conn, 1, 7)[0]["valid_to_episode"] == 8
        assert [f["id"] for f in ledger.facts_at_episode(conn, 1, 9)] == [future["id"]]

        with pytest.raises(ledger.LedgerConflict):
            ledger.invalidate_fact(conn, 1, early["id"], 9)  # 不可重复失效
        with pytest.raises(ledger.LedgerConflict):
            ledger.create_fact(conn, 1, "甲", "在", "乙", 5, valid_to_episode=4)
    finally:
        conn.close()
        # 清理临时库文件
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.environ["FD_JUCHUANG_DB"] + suffix)
            except OSError:
                pass


def test_fact_api_timepoint_and_invalidate(client):
    _setup_project()
    r = client.post("/api/v1/ledger/projects/1/facts", headers=A, json={
        "subject": "陈守拙", "predicate": "拥有", "object": "密钥",
        "valid_from_episode": 3})
    assert r.status_code == 201, r.text
    fact_id = r.json()["id"]
    client.post("/api/v1/ledger/projects/1/facts", headers=A, json={
        "subject": "林务实", "predicate": "得到", "object": "密令",
        "valid_from_episode": 7})

    at5 = client.get("/api/v1/ledger/projects/1/facts/timepoint?episode=5",
                     headers=A).json()
    assert [f["id"] for f in at5] == [fact_id]

    r = client.post(f"/api/v1/ledger/projects/1/facts/{fact_id}/invalidate",
                    headers=A, json={"at_episode": 8})
    assert r.status_code == 200 and r.json()["valid_to_episode"] == 8
    assert client.get("/api/v1/ledger/projects/1/facts", headers=A).json()[0][
        "invalidated"] is True
    # 重复失效 409；未知事实 404；越权 404
    assert client.post(f"/api/v1/ledger/projects/1/facts/{fact_id}/invalidate",
                       headers=A, json={"at_episode": 9}).status_code == 409
    assert client.get("/api/v1/ledger/projects/1/facts/9999",
                      headers=A).status_code == 404
    assert client.get("/api/v1/ledger/projects/1/facts",
                      headers=B).status_code == 404


# ---------------------------------------------------------------- 3.3 伏笔生命周期


def test_foreshadow_lifecycle_and_overdue(env):
    conn = _conn()
    try:
        fs = ledger.create_foreshadow(conn, 1, "门后的脚步声", 2, 6)
        assert fs["current_status"] == "planted"

        at7 = ledger.list_foreshadows(conn, 1, current_episode=7)
        assert at7[0]["overdue"] is True and at7[0]["overdue_by"] == 1
        assert [f["id"] for f in ledger.overdue_foreshadows(conn, 1, 7)] == [fs["id"]]

        reinforced = ledger.transition_foreshadow(conn, 1, fs["id"], "reinforce", 4)
        assert reinforced["current_status"] == "reinforced"
        assert reinforced["reinforced_count"] == 1
        assert reinforced["last_reinforced_episode"] == 4

        resolved = ledger.transition_foreshadow(
            conn, 1, fs["id"], "resolve", 6, evidence="门后只是风，伏笔解除")
        assert resolved["current_status"] == "resolved"
        assert resolved["resolution_episode"] == 6
        assert resolved["resolution_evidence"].startswith("门后")
        assert ledger.overdue_foreshadows(conn, 1, 9) == []

        with pytest.raises(ledger.LedgerConflict):  # 终态不可再迁移
            ledger.transition_foreshadow(conn, 1, fs["id"], "abandon", 8)

        fs2 = ledger.create_foreshadow(conn, 1, "旧怀表", 1, 2)
        abandoned = ledger.transition_foreshadow(
            conn, 1, fs2["id"], "abandon", 5, evidence="支线删除")
        assert abandoned["current_status"] == "abandoned"
        assert ledger.overdue_foreshadows(conn, 1, 9) == []  # 放弃不计逾期
    finally:
        conn.close()
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.environ["FD_JUCHUANG_DB"] + suffix)
            except OSError:
                pass


def test_foreshadow_api_lifecycle_and_overdue(client):
    _setup_project()
    r = client.post("/api/v1/ledger/projects/1/foreshadows", headers=A, json={
        "hint_text": "门后的脚步声", "planted_in_episode": 2,
        "planned_resolution_episode": 6})
    assert r.status_code == 201, r.text
    fs_id = r.json()["id"]

    overdue = client.get("/api/v1/ledger/projects/1/foreshadows/overdue?episode=7",
                         headers=A).json()
    assert [f["id"] for f in overdue] == [fs_id]
    assert overdue[0]["overdue_by"] == 1

    r = client.post(f"/api/v1/ledger/projects/1/foreshadows/{fs_id}/reinforce",
                    headers=A, json={"episode": 4})
    assert r.status_code == 200 and r.json()["current_status"] == "reinforced"
    r = client.post(f"/api/v1/ledger/projects/1/foreshadows/{fs_id}/resolve",
                    headers=A, json={"episode": 6, "evidence": "门后只是风"})
    assert r.status_code == 200 and r.json()["current_status"] == "resolved"
    assert client.get("/api/v1/ledger/projects/1/foreshadows/overdue?episode=9",
                      headers=A).json() == []
    assert client.post(f"/api/v1/ledger/projects/1/foreshadows/{fs_id}/abandon",
                       headers=A, json={"episode": 8}).status_code == 409


# ---------------------------------------------------------------- 3.4 角色知识 / 越知


def test_knowledge_isolation_and_violation(env):
    ids = _setup_project()
    lin = ids["characters"]["林务实"]
    chen = ids["characters"]["陈守拙"]
    su = ids["characters"]["苏未晚"]
    conn = _conn()
    try:
        fact = ledger.create_fact(conn, 1, "陈守拙", "拥有", "密钥", 3)
        record = ledger.add_knowledge(
            conn, 1, str(lin), learned_at_episode=4, fact_id=fact["id"],
            source_scene_id=11)
        assert record["fact_id"] == fact["id"]
        assert record["character_name"] == "林务实"

        assert ledger.knowledge_state(conn, 1, str(lin), 3) == []
        at4 = ledger.knowledge_state(conn, 1, str(lin), 4)
        assert len(at4) == 1 and at4[0]["fact"]["object"] == "密钥"
        # 知识按角色隔离：陈守拙/苏未晚在第 5 集都没有这条知识
        assert ledger.knowledge_state(conn, 1, str(chen), 5) == []
        assert ledger.knowledge_state(conn, 1, str(su), 5) == []

        # 越知：林务实第 3 集就说出「密钥」，但其第 4 集才获知
        violations = ledger.detect_knowledge_violations(
            conn, 1, str(lin), 3, "密钥我一直带在身上。",
            scene_id=11, line_id=101, line_idx=2)
        assert len(violations) == 1
        v = violations[0]
        assert v["type"] == "knowledge_violation" and v["severity"] == "block"
        assert v["learned_at_episode"] == 4 and v["episode"] == 3
        assert v["matched"] == "密钥" and v["offset"] == 0
        assert (v["scene_id"], v["line_id"], v["line_idx"]) == (11, 101, 2)

        # 获知时点之后不再冲突
        assert ledger.detect_knowledge_violations(
            conn, 1, str(lin), 4, "密钥我一直带在身上。") == []

        # 未来事实：第 2 集提到第 3 集才成立的事实 → 时间线告警
        future = ledger.detect_knowledge_violations(
            conn, 1, str(su), 2, "那把密钥在哪？")
        assert [x["type"] for x in future] == ["future_fact_mention"]
        assert future[0]["severity"] == "warn"

        # 签名式知识（无 facts 行）同样可检测
        ledger.add_knowledge(
            conn, 1, "苏未晚", learned_at_episode=6,
            fact_reference="顾行舟|知道|账本藏在钟里")
        sig = ledger.detect_knowledge_violations(
            conn, 1, "苏未晚", 5, "账本藏在钟里，你忘了吗？")
        assert len(sig) == 1 and sig[0]["type"] == "knowledge_violation"
        assert sig[0]["fact_id"] is None
    finally:
        conn.close()
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.environ["FD_JUCHUANG_DB"] + suffix)
            except OSError:
                pass


def test_knowledge_api_state_and_check(client):
    ids = _setup_project()
    r = client.post("/api/v1/ledger/projects/1/facts", headers=A, json={
        "subject": "陈守拙", "predicate": "拥有", "object": "密钥",
        "valid_from_episode": 3})
    fact_id = r.json()["id"]
    r = client.post("/api/v1/ledger/projects/1/knowledge", headers=A, json={
        "character_id": str(ids["characters"]["林务实"]),
        "fact_id": fact_id, "learned_at_episode": 4, "source_scene_id": 7})
    assert r.status_code == 201, r.text

    assert client.get(
        "/api/v1/ledger/projects/1/knowledge/state"
        f"?character_id={ids['characters']['林务实']}&episode=3",
        headers=A).json() == []
    state = client.get(
        "/api/v1/ledger/projects/1/knowledge/state"
        f"?character_id={ids['characters']['林务实']}&episode=4",
        headers=A).json()
    assert len(state) == 1 and state[0]["fact"]["object"] == "密钥"

    violations = client.post("/api/v1/ledger/projects/1/knowledge/check",
                             headers=A, json={
                                 "character_id": str(ids["characters"]["林务实"]),
                                 "episode": 3, "text": "密钥给我。",
                                 "scene_id": 5, "line_idx": 1}).json()
    assert violations[0]["type"] == "knowledge_violation"
    assert violations[0]["scene_id"] == 5


# ---------------------------------------------------------------- 3.5 上下文组装


def test_context_assembly_timepoint_and_character_isolation(env):
    ids = _setup_project()
    lin = ids["characters"]["林务实"]
    chen = ids["characters"]["陈守拙"]
    conn = _conn()
    try:
        fact3 = ledger.create_fact(conn, 1, "陈守拙", "拥有", "密钥", 3)
        ledger.create_fact(conn, 1, "林务实", "得到", "密令", 7)
        ledger.add_knowledge(conn, 1, str(lin), 4, fact_id=fact3["id"])

        ctx = ledger.assemble_context(
            conn, 1, 5, character_ids=[str(lin)], token_budget=1000)
        assert ctx["available"] is True and ctx["enabled"] is True
        signatures = {f["signature"] for f in ctx["facts"]}
        assert signatures == {fact3["signature"]}
        assert "密令" not in ctx["text"]  # 第 7 集才成立的事实不注入
        assert "密钥" in ctx["text"]
        assert "第4集获知" in ctx["text"]
        assert ctx["characters"][0]["character_name"] == "林务实"
        assert [f["object"] for f in ctx["characters"][0]["known_facts"]] == ["密钥"]

        # 角色视角隔离：为陈守拙组装时不包含林务实的知识
        ctx_chen = ledger.assemble_context(
            conn, 1, 5, character_ids=[str(chen)], token_budget=1000)
        assert ctx_chen["characters"][0]["knowledge"] == []
        assert "林务实 于第4集获知" not in ctx_chen["text"]
    finally:
        conn.close()
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.environ["FD_JUCHUANG_DB"] + suffix)
            except OSError:
                pass


def test_context_token_budget_keeps_character_knowledge(env):
    ids = _setup_project()
    lin = ids["characters"]["林务实"]
    conn = _conn()
    try:
        fact = ledger.create_fact(conn, 1, "陈守拙", "拥有", "密钥", 3)
        ledger.add_knowledge(conn, 1, str(lin), 4, fact_id=fact["id"])
        for i in range(40):
            ledger.create_fact(conn, 1, f"路人{i}", "持有", f"物品{i}", 1)

        ctx = ledger.assemble_context(
            conn, 1, 5, character_ids=[str(lin)], token_budget=60)
        assert ctx["available"] is True
        assert ctx["truncated"]["facts_dropped"] > 0
        assert ctx["truncated"]["knowledge_dropped"] == 0
        # 超限优先保留角色知识
        assert "于第4集获知" in ctx["text"]
        assert ctx["characters"][0]["knowledge"]
        assert ctx["token_estimate"] >= 1
    finally:
        conn.close()
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.environ["FD_JUCHUANG_DB"] + suffix)
            except OSError:
                pass


# ---------------------------------------------------------------- 3.6 生成后检查


def test_post_generation_deterministic_checks(env):
    ids = _setup_project()
    lin = ids["characters"]["林务实"]
    conn = _conn()
    try:
        fact = ledger.create_fact(conn, 1, "陈守拙", "拥有", "密钥", 3)
        ledger.add_knowledge(conn, 1, str(lin), 4, fact_id=fact["id"])
        resolved = ledger.create_foreshadow(conn, 1, "门后的脚步声", 2, 6)
        ledger.transition_foreshadow(conn, 1, resolved["id"], "resolve", 6,
                                     evidence="门后只是风")
        ledger.create_foreshadow(conn, 1, "旧怀表", 1, 2)  # 第 3 集起逾期

        lines = [
            {"scene_id": 11, "line_id": 101, "idx": 1,
             "speaker": "林务实", "text": "密钥我一直带在身上。"},
            {"scene_id": 11, "line_id": 102, "idx": 2,
             "speaker": "陌生人", "text": "谁在那儿？"},
        ]
        result = ledger.check_generated(
            conn, 1, 3, lines,
            known_characters=["陈守拙", "林务实", "苏未晚"])
        assert result["available"] is True and result["blocked"] is True
        types = {v["type"] for v in result["violations"]}
        assert {"knowledge_violation", "unknown_entity", "foreshadow_overdue"} <= types

        leak = next(v for v in result["violations"]
                    if v["type"] == "knowledge_violation")
        assert leak["severity"] == "block"
        assert (leak["scene_id"], leak["line_id"], leak["line_idx"]) == (11, 101, 1)
        assert leak["offset"] is not None and leak["matched"] == "密钥"
        assert result["checks"]["knowledge"] == "violated"
        assert result["checks"]["entities"] == "violated"
        assert result["counts"]["block"] == 1 and result["counts"]["warn"] >= 2

        # 终态伏笔再次被提及 + 逾期提示（第 7 集）
        later = ledger.check_generated(
            conn, 1, 7,
            [{"scene_id": 12, "line_id": 201, "idx": 1, "speaker": "陈守拙",
              "text": "门后的脚步声又响了。"}],
            known_characters=["陈守拙", "林务实", "苏未晚"])
        later_types = {v["type"] for v in later["violations"]}
        assert "foreshadow_status_conflict" in later_types
        assert "foreshadow_overdue" in later_types
        assert later["checks"]["foreshadow"] == "violated"

        # 过早提及：第 5 集提到第 7 集才埋设的伏笔
        ledger.create_foreshadow(conn, 1, "墙上新添的血字", 7, 9)
        early = ledger.check_generated(
            conn, 1, 5,
            [{"scene_id": 13, "line_id": 301, "idx": 1, "speaker": "林务实",
              "text": "墙上新添的血字是什么意思？"}],
            known_characters=["林务实"])
        assert any(v["type"] == "foreshadow_early_mention"
                   for v in early["violations"])
    finally:
        conn.close()
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.environ["FD_JUCHUANG_DB"] + suffix)
            except OSError:
                pass


def test_check_api_locations(client):
    _setup_project()
    r = client.post("/api/v1/ledger/projects/1/facts", headers=A, json={
        "subject": "陈守拙", "predicate": "拥有", "object": "密钥",
        "valid_from_episode": 3})
    fact_id = r.json()["id"]
    client.post("/api/v1/ledger/projects/1/knowledge", headers=A, json={
        "character_id": "林务实", "fact_id": fact_id, "learned_at_episode": 4})

    r = client.post("/api/v1/ledger/projects/1/check", headers=A, json={
        "episode": 3,
        "lines": [{"scene_id": 21, "line_id": 5, "idx": 3,
                   "speaker": "林务实", "text": "密钥给我。"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["blocked"] is True
    v = body["violations"][0]
    assert (v["scene_id"], v["line_id"], v["line_idx"]) == (21, 5, 3)


# ---------------------------------------------------------------- 3.7 空台账降级


def test_empty_ledger_degrades_without_blocking(env):
    ids = _setup_project(with_scene=True)
    conn = _conn()
    try:
        status = ledger.ledger_status(conn, 1)
        assert status["enabled"] is False and status["available"] is False
        assert "台账为空" in status["reason"]

        ctx = ledger.assemble_context(conn, 1, 1)
        assert ctx["available"] is False
        assert "不可用" in ctx["reason"] and ctx["text"] == ""

        result = ledger.check_generated(
            conn, 1, 1, [{"speaker": "任意", "text": "任意台词"}])
        assert result["available"] is False and result["blocked"] is False
        assert result["violations"] == []

        pre = ledger.pre_generation_context_for_scene(ids["scene_id"], episode=1)
        assert pre["available"] is False and pre["reason"]
        post = ledger.post_generation_check_for_scene(
            ids["scene_id"],
            [{"idx": 1, "line_speaker": "林务实", "line_text": "密钥在哪？"}])
        assert post["available"] is False and post["blocked"] is False
    finally:
        conn.close()


def test_empty_ledger_pipeline_still_generates(env, monkeypatch):
    ids = _setup_project(with_scene=True)
    scene_id = ids["scene_id"]
    bundle = {
        "scene": {"id": scene_id, "heading": "第一场：对峙", "synopsis": "旧友重逢"},
        "lines": [{"idx": 1, "speaker": "林务实", "text": "密钥在哪？"}],
        "characters": [{"id": ids["characters"]["林务实"], "name": "林务实",
                        "appearance_elements": []}],
    }
    monkeypatch.setattr(pipeline, "load_scene_bundle",
                        lambda sid: bundle if sid == scene_id else (_ for _ in ()).throw(
                            pipeline.HTTPException(status_code=404, detail="场次不存在")))
    app = FastAPI()
    app.include_router(pipeline.router)
    client = TestClient(app)

    r = client.post(f"/api/v1/pipeline/storyboard/{scene_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shot_count"] >= 2  # 创作流程继续
    assert body["continuity"]["available"] is False
    assert body["continuity"]["episode"] == 1
    assert body["continuity"]["reason"]
    assert body["continuity"]["post"]["blocked"] is False


def test_pipeline_hooks_block_knowledge_leak(env, monkeypatch):
    ids = _setup_project(with_scene=True)
    scene_id = ids["scene_id"]
    lin = ids["characters"]["林务实"]
    conn = _conn()
    try:
        fact = ledger.create_fact(conn, 1, "陈守拙", "拥有", "密钥", 3)
        ledger.add_knowledge(conn, 1, str(lin), 4, fact_id=fact["id"])
    finally:
        conn.close()

    bundle = {
        "scene": {"id": scene_id, "heading": "第一场：对峙", "synopsis": "旧友重逢"},
        "lines": [{"idx": 1, "speaker": "林务实", "text": "密钥我一直带在身上。"}],
        "characters": [{"id": lin, "name": "林务实", "appearance_elements": []}],
    }
    monkeypatch.setattr(pipeline, "load_scene_bundle",
                        lambda sid: bundle if sid == scene_id else (_ for _ in ()).throw(
                            pipeline.HTTPException(status_code=404, detail="场次不存在")))
    app = FastAPI()
    app.include_router(pipeline.router)
    client = TestClient(app)

    r = client.post(f"/api/v1/pipeline/storyboard/{scene_id}?episode=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["continuity"]["available"] is True
    assert body["continuity"]["pre"]["available"] is True
    assert body["continuity"]["post"]["blocked"] is True
    assert body["continuity"]["post"]["checks"]["knowledge"] == "violated"
    # 生成产物仍然保存返回（拦截=标记，不吞掉已生成内容）
    assert body["shot_count"] >= 2


# ---------------------------------------------------------------- API 基础


def test_api_status_and_auth(client):
    _setup_project()
    status = client.get("/api/v1/ledger/projects/1/status", headers=A).json()
    assert status["enabled"] is False and status["available"] is False
    assert client.get("/api/v1/ledger/projects/999/status",
                      headers=A).status_code == 404
    assert client.get("/api/v1/ledger/projects/1/status",
                      headers=B).status_code == 404
    assert client.get("/api/v1/ledger/projects/1/status").status_code == 401


def test_api_context_and_validation(client):
    _setup_project()
    r = client.post("/api/v1/ledger/projects/1/facts", headers=A, json={
        "subject": "陈守拙", "predicate": "拥有", "object": "密钥",
        "valid_from_episode": 3})
    assert r.status_code == 201
    ctx = client.get("/api/v1/ledger/projects/1/context?episode=5",
                     headers=A).json()
    assert ctx["available"] is True and ctx["episode"] == 5
    assert [f["object"] for f in ctx["facts"]] == ["密钥"]

    # 校验：空字段 / 非法集次 / 未知字段 → 422
    assert client.post("/api/v1/ledger/projects/1/facts", headers=A, json={
        "subject": "", "predicate": "拥有", "object": "密钥",
        "valid_from_episode": 3}).status_code == 422
    assert client.post("/api/v1/ledger/projects/1/facts", headers=A, json={
        "subject": "甲", "predicate": "在", "object": "乙",
        "valid_from_episode": 0}).status_code == 422
    assert client.post("/api/v1/ledger/projects/1/facts", headers=A, json={
        "subject": "甲", "predicate": "在", "object": "乙",
        "valid_from_episode": 3, "extra": 1}).status_code == 422
