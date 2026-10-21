"""剧作质量门（change juchuang-continuity-layer task 4.1-4.4）离线测试。

全离线：临时 SQLite 共库（剧本五表 + 台账三表 + 质量两表），不依赖网络与 LLM。
覆盖：六维 rubric 与定位字段 → 分维评分与问题清单 → 连续性维引用台账冲突事实
→ 空台账降级 → 合规红线阻断与策略配置 → 评估结果持久化/API 隔离。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_juchuang_api import ledger, quality, screenplay

A = {"Authorization": "Bearer tok-a"}
B = {"Authorization": "Bearer tok-b"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "quality-test.db")
    monkeypatch.setenv("FD_JUCHUANG_DB", db_path)
    monkeypatch.setenv("FD_JUCHUANG_AUTH_DEV_TOKENS", "tok-a=u1,tok-b=u2")
    monkeypatch.setenv("FD__AUTH_DEV_TOKENS", "tok-a=u1,tok-b=u2")
    return db_path


@pytest.fixture()
def client(env):
    app = FastAPI()
    app.include_router(quality.router)
    return TestClient(app)


def _conn():
    conn = quality.connect()
    screenplay.migrate(conn)
    ledger.migrate(conn)
    quality.migrate(conn)
    return conn


def _setup_project(pid: int = 1, user: str = "u1",
                   names: tuple[str, ...] = ("陈守拙", "林务实", "苏未晚")) -> dict:
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
        conn.commit()
    finally:
        conn.close()
    return {"project_id": pid, "characters": ids}


def _add_scene(pid: int, idx: int, title: str,
               lines: list[tuple[str, str, str | None]],
               characters: dict[str, int], *,
               synopsis: str = "", action: str = "",
               transition: str | None = None,
               conflict: str | None = None) -> int:
    conn = screenplay.connect()
    try:
        screenplay.migrate(conn)
        cur = conn.execute(
            "INSERT INTO scenes (project_id, idx, title, synopsis, action,"
            " transition, conflict, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, idx, title, synopsis, action, transition,
             conflict, "2026-01-01"),
        )
        scene_id = int(cur.lastrowid)
        for line_idx, (speaker, text, parenthetical) in enumerate(lines, start=1):
            conn.execute(
                "INSERT INTO lines (scene_id, idx, speaker_id, text, parenthetical)"
                " VALUES (?,?,?,?,?)",
                (scene_id, line_idx, characters[speaker], text, parenthetical),
            )
        conn.commit()
        return scene_id
    finally:
        conn.close()


def _standard_script(pid: int = 1) -> dict:
    """三场、有冲突、有情绪节拍、格式规范的剧本（无红线）。"""
    ids = _setup_project(pid)
    _add_scene(
        pid, 1, "第一场：茶馆对峙",
        [
            ("陈守拙", "把账拿出来。", None),
            ("林务实", "什么账，我听不懂你在说什么。", None),
            ("陈守拙", "你心里清楚！", None),
        ],
        ids["characters"], synopsis="旧友重逢，暗流涌动。",
        action="茶馆内，烛火压低了半个画面。", transition="CUT TO",
        conflict=json.dumps({"description": "信任与怀疑"}, ensure_ascii=False),
    )
    _add_scene(
        pid, 2, "第二场：雨夜追踪",
        [
            ("林务实", "他往东边跑了！", None),
            ("苏未晚", "别追了，前面是死路。", None),
            ("林务实", "那也要追！", None),
        ],
        ids["characters"], synopsis="林务实追查账本下落。",
        action="雨夜长街，脚步声由远及近。",
        conflict=json.dumps({"description": "追与逃"}, ensure_ascii=False),
    )
    _add_scene(
        pid, 3, "第三场：真相前夜",
        [
            ("苏未晚", "账本一直在我手里。", None),
            ("陈守拙", "你竟然骗了我这么久？", None),
            ("苏未晚", "你还想知道什么……", None),
        ],
        ids["characters"], synopsis="苏未晚摊牌。",
        action="灯下，两人对坐。", transition="FADE OUT",
        conflict=json.dumps({"description": "坦白与背叛"}, ensure_ascii=False),
    )
    return ids


def _problems(assessment: dict, dimension: str | None = None) -> list[dict]:
    dimensions = [dimension] if dimension else list(quality.DIMENSIONS)
    return [
        problem
        for key in dimensions
        for problem in assessment["dimensions"][key]["problems"]
    ]


# ---------------------------------------------------------------- 4.1 rubric


def test_rubric_has_six_dimensions_each_with_rules_and_location_scope(env):
    payload = quality.rubric_payload()
    assert [d["key"] for d in payload["dimensions"]] == list(quality.DIMENSIONS)
    assert set(payload["default_weights"]) == set(quality.DIMENSIONS)
    assert payload["redline_rules"], "合规红线清单不能为空"

    for dimension in payload["dimensions"]:
        rules = dimension["rules"]
        assert rules, f"{dimension['key']} 必须有规则"
        for rule in rules:
            assert rule["dimension"] == dimension["key"]
            assert rule["title"] and rule["description"]
            assert rule["location_scope"] in {"episode", "scene", "line"}
            assert rule["severity"] in {"warning", "error", "critical"}

    # 六维都必须能定位到场次或台词（spec：每维均有规则与定位字段）
    scopes = {
        d["key"]: {rule["location_scope"] for rule in d["rules"]}
        for d in payload["dimensions"]
    }
    for key in quality.DIMENSIONS:
        assert scopes[key] & {"scene", "line"}, f"{key} 缺少场次/台词定位规则"


def test_rubric_api(client):
    r = client.get("/api/v1/quality/rubric")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["dimensions"]) == 6
    assert body["required_problem_location_fields"][:3] == [
        "scene_id", "line_id", "line_idx",
    ]


def test_assessment_returns_six_scored_dimensions_with_supporting_problems(env):
    ids = _standard_script()
    conn = _conn()
    try:
        result = quality.assess_episode(conn, ids["project_id"], episode=3,
                                        persist=False)
    finally:
        conn.close()

    assert set(result["dimensions"]) == set(quality.DIMENSIONS)
    assert result["dimension_order"] == list(quality.DIMENSIONS)
    assert result["stats"]["scene_count"] == 3

    # 每维都有规则项、规则执行结果与可展开的问题清单
    for key in quality.DIMENSIONS:
        dimension = result["dimensions"][key]
        assert dimension["dimension"] == key
        assert dimension["label"]
        assert dimension["rules_applied"], f"{key} 必须列出规则项"
        assert dimension["rule_results"], f"{key} 必须列出规则执行结果"
        if dimension["available"]:
            assert isinstance(dimension["score"], float)
            assert 0 <= dimension["score"] <= 10
        else:
            assert dimension["score"] is None and dimension["reason"]

    # 每条问题都带维度/严重度/定位/依据/规则
    for problem in _problems(result):
        assert problem["dimension"] in quality.DIMENSIONS
        assert problem["severity"] in {"warning", "error", "critical"}
        assert problem["message"]
        assert problem["evidence"]
        assert problem["rule_id"] in quality.RULE_INDEX
        location = problem["location"]
        for field in ("scene_id", "line_id", "line_idx", "character"):
            assert field in location
        assert location["scope"] in {"episode", "scene", "line"}

    # 总分 = 可用维度加权平均（不以总分替代分维）
    available = {
        key: value for key, value in result["dimensions"].items()
        if value["available"] and value["score"] is not None
    }
    total_weight = sum(value["weight"] for value in available.values())
    expected = round(
        sum(value["score"] * value["weight"] for value in available.values())
        / total_weight,
        2,
    )
    assert result["overall_score"] == expected
    assert result["overall_formula"] == "weighted_average(available_dimensions)"


def test_problem_locations_point_to_specific_scene_and_line(env):
    ids = _setup_project()
    long_line = "这是一句刻意写得很长很长的台词" * 10  # 150 字，触发台词长度规则
    scene_id = _add_scene(
        1, 1, "对峙",  # 缺少「第 N 场」前缀 → 格式问题
        [
            ("陈守拙", long_line, None),
            ("林务实", "说重点。", None),
            ("苏未晚", "我也想知道。", None),
        ],
        ids["characters"],
        action="**雨夜**",  # Markdown 标记 → 格式问题
    )
    conn = _conn()
    try:
        result = quality.assess_episode(conn, 1, episode=1, persist=False)
    finally:
        conn.close()

    length_problem = next(
        p for p in _problems(result, "dialogue")
        if p["rule_id"] == "dialogue.line_length"
    )
    assert length_problem["location"]["scene_id"] == scene_id
    assert length_problem["location"]["line_id"] is not None
    assert length_problem["location"]["line_idx"] == 1
    assert length_problem["location"]["character"] == "陈守拙"
    assert length_problem["location"]["scope"] == "line"
    assert long_line[:20] in length_problem["evidence"]

    heading_problem = next(
        p for p in _problems(result, "format")
        if p["rule_id"] == "format.scene_heading"
    )
    assert heading_problem["location"]["scene_id"] == scene_id
    assert heading_problem["location"]["scope"] == "scene"

    markup_problem = next(
        p for p in _problems(result, "format")
        if p["rule_id"] == "format.action_markup"
    )
    assert markup_problem["location"]["scene_id"] == scene_id
    assert "**" in markup_problem["evidence"]


def test_rule_penalty_cap_keeps_score_reproducible(env):
    """同一规则命中多次时按 cap 截断：10 句超长台词 → 扣 2.4 分，而非无限扣。"""
    ids = _setup_project()
    long_line = "这是一句刻意写得很长很长的台词" * 10
    _add_scene(
        1, 1, "第一场：长夜",
        [
            ("陈守拙", long_line, None),
            ("林务实", long_line, None),
            ("陈守拙", long_line, None),
            ("林务实", long_line, None),
            ("陈守拙", long_line, None),
            ("林务实", long_line, None),
            ("陈守拙", long_line, None),
            ("林务实", long_line, None),
            ("陈守拙", long_line, None),
            ("林务实", long_line, None),
        ],
        ids["characters"],
        conflict=json.dumps({"description": "对峙"}, ensure_ascii=False),
    )
    conn = _conn()
    try:
        result = quality.assess_episode(conn, 1, episode=1, persist=False)
    finally:
        conn.close()

    dialogue = result["dimensions"]["dialogue"]
    assert dialogue["problem_count"] >= 10
    assert dialogue["score"] == round(10 - 2.4, 2)


# ---------------------------------------------------------------- 4.2 连续性


def test_continuity_references_ledger_conflict_facts(env):
    ids = _setup_project()
    scene_id = _add_scene(
        1, 1, "第一场：摊牌",
        [
            ("林务实", "密钥到底在哪？", None),
            ("陈守拙", "你竟然知道密钥！", None),
        ],
        ids["characters"],
        conflict=json.dumps({"description": "审问与隐瞒"}, ensure_ascii=False),
    )
    conn = _conn()
    try:
        fact = ledger.create_fact(conn, 1, "陈守拙", "拥有", "密钥", 3)
        ledger.add_knowledge(conn, 1, "林务实", learned_at_episode=5,
                             fact_id=fact["id"])
        foreshadow = ledger.create_foreshadow(
            conn, 1, "染血的玉佩", planted_in_episode=1,
            planned_resolution_episode=2,
        )
        result = quality.assess_episode(conn, 1, episode=3, persist=False)
    finally:
        conn.close()

    continuity = result["dimensions"]["continuity"]
    assert continuity["available"] is True
    assert continuity["score"] is not None and continuity["score"] < 10

    knowledge = next(
        p for p in continuity["problems"]
        if p["rule_id"] == "continuity.knowledge_timeline"
    )
    assert knowledge["severity"] == "error"
    assert knowledge["location"]["scene_id"] == scene_id
    assert knowledge["location"]["line_id"] is not None
    assert knowledge["location"]["line_idx"] == 1
    assert knowledge["location"]["character"] == "林务实"

    ref = knowledge["ledger_refs"][0]
    assert ref["kind"] == "fact"
    assert ref["fact_id"] == fact["id"]
    assert ref["signature"] == fact["signature"] == "陈守拙|拥有|密钥"
    assert ref["violation_type"] == "knowledge_violation"
    # evidence 必须给出可读的台账冲突事实引用
    assert str(fact["id"]) in knowledge["evidence"]
    assert fact["signature"] in knowledge["evidence"]

    overdue = next(
        p for p in continuity["problems"]
        if p["rule_id"] == "continuity.foreshadow_overdue"
    )
    assert overdue["ledger_refs"][0]["foreshadow_id"] == foreshadow["id"]
    assert overdue["ledger_refs"][0]["signature"] == "染血的玉佩"


def test_empty_ledger_degrades_continuity_without_blocking(env):
    _standard_script()
    conn = _conn()
    try:
        result = quality.assess_episode(conn, 1, episode=3, persist=False)
    finally:
        conn.close()

    continuity = result["dimensions"]["continuity"]
    assert continuity["available"] is False
    assert continuity["score"] is None
    assert "台账为空" in continuity["reason"]
    assert all(
        r["status"] == "skipped" and r["reason"] == continuity["reason"]
        for r in continuity["rule_results"] if r["rule_id"] != "continuity.ledger_available"
    )

    # 其余五维照常评分，整体不被阻断
    for key in quality.DIMENSIONS:
        if key != "continuity":
            assert result["dimensions"][key]["available"] is True
            assert result["dimensions"][key]["score"] is not None
    assert result["overall_score"] is not None
    assert result["blocked"] is False
    assert result["stats"]["unavailable_dimensions"] == ["continuity"]
    assert result["stats"]["weights_renormalized"] is True


# ---------------------------------------------------------------- 4.3 合规红线


def test_compliance_redline_blocks_delivery_and_lists_violations(env):
    ids = _setup_project()
    _add_scene(
        1, 1, "第一场：计划",
        [
            ("陈守拙", "制造炸弹的步骤我全知道。", None),
            ("林务实", "你疯了！", None),
        ],
        ids["characters"],
        conflict=json.dumps({"description": "疯狂与阻止"}, ensure_ascii=False),
    )
    conn = _conn()
    try:
        result = quality.assess_episode(conn, 1, episode=1, persist=False)
        gate = quality.delivery_gate(conn, 1, episode=1)
    finally:
        conn.close()

    assert result["redline_hit"] is True
    assert result["blocked"] is True
    assert result["block_reasons"]
    assert result["violation_details"], "命中红线时必须列出命中项"
    violation = result["violation_details"][0]
    assert violation["rule_id"] == "compliance.violence_instruction"
    assert violation["red_line"] is True
    assert violation["severity"] == "critical"
    assert "制造炸弹" in violation["evidence"]
    assert violation["location"]["scene_id"] is not None

    assert gate["delivered"] is False
    assert gate["blocked"] is True
    assert len(gate["violations"]) == len(result["violation_details"])


def test_policy_can_disable_redline_and_blocking(client, env):
    ids = _setup_project()
    _add_scene(
        1, 1, "第一场：计划",
        [("陈守拙", "制造炸弹的步骤我全知道。", None)],
        ids["characters"],
        conflict=json.dumps({"description": "疯狂"}, ensure_ascii=False),
    )

    # 默认策略：阻断
    r = client.post("/api/v1/quality/projects/1/assess", headers=A,
                    json={"episode": 1})
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] is True

    # 禁用该条红线：评估不再命中，也不再阻断
    r = client.put("/api/v1/quality/projects/1/policy", headers=A, json={
        "disabled_redlines": ["compliance.violence_instruction"],
    })
    assert r.status_code == 200, r.text
    assert r.json()["disabled_redlines"] == ["compliance.violence_instruction"]

    r = client.post("/api/v1/quality/projects/1/assess", headers=A,
                    json={"episode": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["redline_hit"] is False
    assert body["blocked"] is False
    assert body["dimensions"]["compliance"]["problems"] == []

    # 重新启用红线但关闭阻断：仍标记命中，交付放行（策略可配置）
    r = client.put("/api/v1/quality/projects/1/policy", headers=A, json={
        "disabled_redlines": [],
        "block_on_redline": False,
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/v1/quality/projects/1/assess", headers=A,
                    json={"episode": 1})
    body = r.json()
    assert body["redline_hit"] is True
    assert body["blocked"] is False

    r = client.post("/api/v1/quality/projects/1/deliver", headers=A,
                    json={"episode": 1})
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is True
    assert r.json()["violations"], "即使放行也要列出命中项供复核"

    # 未知红线/规则 → 422
    r = client.put("/api/v1/quality/projects/1/policy", headers=A, json={
        "disabled_redlines": ["compliance.not_a_rule"],
    })
    assert r.status_code == 422


def test_deliver_endpoint_returns_409_with_violations(client, env):
    ids = _setup_project()
    _add_scene(
        1, 1, "第一场：计划",
        [("陈守拙", "他详细讲解了制造炸弹的步骤。", None)],
        ids["characters"],
        conflict=json.dumps({"description": "危险"}, ensure_ascii=False),
    )

    r = client.post("/api/v1/quality/projects/1/deliver", headers=A,
                    json={"episode": 1, "target": "fountain"})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["delivered"] is False
    assert body["blocked"] is True
    assert body["target"] == "fountain"
    assert body["violations"]
    assert body["violations"][0]["rule_id"] == "compliance.violence_instruction"


def test_compliance_redline_also_scans_scene_text(env):
    ids = _setup_project()
    _add_scene(
        1, 1, "第一场：暗室",
        [("陈守拙", "把那东西收好。", None)],
        ids["characters"],
        action="桌上摊着制造炸弹的配方。",
        conflict=json.dumps({"description": "秘密"}, ensure_ascii=False),
    )
    conn = _conn()
    try:
        result = quality.assess_episode(conn, 1, episode=1, persist=False)
    finally:
        conn.close()

    assert result["blocked"] is True
    violation = result["violation_details"][0]
    assert violation["location"]["scope"] == "scene"
    assert "制造炸弹" in violation["evidence"]


# ---------------------------------------------------------------- 评估结果 / API


def test_assessment_api_persistence_and_user_isolation(client, env):
    _standard_script()

    r = client.post("/api/v1/quality/projects/1/assess", headers=A,
                    json={"episode": 3, "episode_id": "s1e3"})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["id"] is not None
    assert first["episode_id"] == "s1e3"
    assert set(first["dimensions"]) == set(quality.DIMENSIONS)

    r = client.get("/api/v1/quality/projects/1/assessments", headers=A)
    assert r.status_code == 200, r.text
    assert [item["id"] for item in r.json()] == [first["id"]]

    r = client.get("/api/v1/quality/projects/1/assessments/latest", headers=A)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == first["id"]
    # 结果里仍带完整问题清单与规则执行情况（可展开复核）
    assert r.json()["dimensions"]["rhythm"]["rule_results"]

    r = client.get(f"/api/v1/quality/projects/1/assessments/{first['id']}",
                   headers=A)
    assert r.status_code == 200 and r.json()["id"] == first["id"]

    # persist=False 不落库
    r = client.post("/api/v1/quality/projects/1/assess", headers=A,
                    json={"episode": 3, "persist": False})
    assert r.status_code == 200 and r.json()["id"] is None
    r = client.get("/api/v1/quality/projects/1/assessments", headers=A)
    assert [item["id"] for item in r.json()] == [first["id"]]

    # 越权/未认证
    r = client.get("/api/v1/quality/projects/1/assessments/latest", headers=B)
    assert r.status_code == 404
    r = client.get("/api/v1/quality/projects/1/assessments/latest")
    assert r.status_code == 401


def test_assessment_history_survives_new_connection(env):
    _standard_script()
    conn = _conn()
    try:
        first = quality.assess_episode(conn, 1, episode=3)
    finally:
        conn.close()

    conn = _conn()
    try:
        latest = quality.latest_assessment(conn, 1)
        listed = quality.list_assessments(conn, 1)
    finally:
        conn.close()
    assert latest["id"] == first["id"]
    assert len(listed) == 1
    assert listed[0]["episode_id"] == "ep-3"


def test_migration_forward_and_rollback(env):
    conn = _conn()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"quality_assessments", "quality_policies",
                "quality_migrations"} <= tables
        assert quality.rollback(conn) == [1]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "quality_assessments" not in tables
        assert "quality_migrations" in tables
        assert quality.migrate(conn) == 1
        assert quality.migrate(conn) == 1  # 幂等
    finally:
        conn.close()
