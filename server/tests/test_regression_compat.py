"""回归与降级测试（change juchuang-continuity-layer task 5.2 / 5.3）。

5.2 旧剧本回归：没有台账、没有分镜扩展字段、没有新场次元数据的剧本，必须
继续可创建、可转分镜、可导出；空台账只让连续性检查明示不可用，不阻断创作。

5.3 无哲思模式：哲思契约不可达时，``/kernel/link`` 降级为 standalone；
连续性台账（事实/知识/伏笔/上下文）与质量门（六维评估）完全本地运行，
不依赖哲思，也不因降级而失效。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fd_juchuang_api import fountain, ledger, pipeline, quality, zhesi_client
from fd_juchuang_api.zhesi_client import Unavailable

A = {"Authorization": "Bearer tok-a"}
BASE = "/api/v1"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "regression-compat.db"
    monkeypatch.setenv("FD_JUCHUANG_DB", str(db_path))
    monkeypatch.setenv("FD_JUCHUANG_AUTH_DEV_TOKENS", "tok-a=u1,tok-b=u2")
    monkeypatch.setenv("FD__AUTH_DEV_TOKENS", "tok-a=u1,tok-b=u2")
    monkeypatch.delenv("FD_JUCHUANG_LLM_API_KEY", raising=False)
    monkeypatch.setenv("FD_JUCHUANG_ZHESI_BASE", "http://127.0.0.1:1")
    return str(db_path)


@pytest.fixture()
def client(env):
    from fd_juchuang_api.main import app

    return TestClient(app)


def _seed_minimal_project(client: TestClient, title: str = "旧剧本") -> dict:
    """只有最小五表字段的旧剧本：场次无 synopsis/action/transition/conflict。"""
    project = client.post(
        f"{BASE}/screenplay/projects", headers=A,
        json={"title": title},
    )
    assert project.status_code == 201, project.text
    pid = int(project.json()["id"])

    character = client.post(
        f"{BASE}/screenplay/projects/{pid}/characters", headers=A,
        json={"name": "老角色", "appearance": {"年龄感": "中年", "着装": "旧棉袄"}},
    )
    assert character.status_code == 201, character.text
    cid = int(character.json()["id"])

    scene = client.post(
        f"{BASE}/screenplay/projects/{pid}/scenes", headers=A,
        json={"title": "旧戏"},
    )
    assert scene.status_code == 201, scene.text
    sid = int(scene.json()["id"])

    line = client.post(
        f"{BASE}/screenplay/scenes/{sid}/lines", headers=A,
        json={"speaker_id": cid, "text": "旧戏照旧唱。"},
    )
    assert line.status_code == 201, line.text
    return {"project_id": pid, "character_id": cid, "scene_id": sid}


# ---------------------------------------------------------------- 5.2 旧剧本回归


def test_legacy_screenplay_creation_flow_not_blocked(client):
    """旧剧本全流程（创建 → 分镜 → 导出）不因新模块缺数据而阻断。"""
    ids = _seed_minimal_project(client)

    status = client.get(
        f"{BASE}/ledger/projects/{ids['project_id']}/status", headers=A,
    )
    assert status.status_code == 200, status.text
    assert status.json()["available"] is False
    assert "台账为空" in status.json()["reason"]

    board = client.post(
        f"{BASE}/pipeline/storyboard/{ids['scene_id']}", headers=A,
    )
    assert board.status_code == 200, board.text
    body = board.json()
    assert body["shot_count"] == 3  # 定场 + 1 台词 + 收尾
    assert body["continuity"]["available"] is False
    assert body["continuity"]["reason"]
    assert body["shots"][1]["line_text"] == "旧戏照旧唱。"
    # 新字段仍然产出（生成器负责补齐），旧剧本不需要预先迁移
    assert {"staging", "keyframe", "model_sheet_refs", "art_bible_refs"} <= set(
        body["shots"][1]
    )

    export = client.get(
        f"{BASE}/screenplay/projects/{ids['project_id']}/export/fountain",
        headers=A, params={"format": "json"},
    )
    assert export.status_code == 200, export.text
    assert "旧戏" in export.json()["text"]


def test_legacy_quality_gate_degrades_continuity_only(client):
    """空台账：连续性维 unavailable，其余五维照常评分，交付不阻断。"""
    ids = _seed_minimal_project(client, "旧剧本质量门")
    response = client.post(
        f"{BASE}/quality/projects/{ids['project_id']}/assess", headers=A,
        json={"episode": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    continuity = body["dimensions"]["continuity"]
    assert continuity["available"] is False
    assert continuity["score"] is None
    assert continuity["reason"]
    assert continuity["problems"] == []
    assert all(
        result["status"] == "skipped"
        for result in continuity["rule_results"]
        if result["rule_id"] != "continuity.ledger_available"
    )

    for key in quality.DIMENSIONS:
        if key == "continuity":
            continue
        assert body["dimensions"][key]["available"] is True
        assert body["dimensions"][key]["score"] is not None
    assert body["overall_score"] is not None
    assert body["blocked"] is False
    assert body["stats"]["unavailable_dimensions"] == ["continuity"]


def test_legacy_storyboard_rows_without_new_fields(env, client):
    """旧版 storyboard 行（JSON 无 staging/keyframe/refs）读取补空值、可编辑可导出。"""
    ids = _seed_minimal_project(client, "旧分镜行")
    assert client.post(
        f"{BASE}/pipeline/storyboard/{ids['scene_id']}", headers=A,
    ).status_code == 200

    conn = pipeline.connect()
    try:
        conn.execute("DELETE FROM storyboard WHERE scene_id=?", (ids["scene_id"],))
        legacy_shot = {
            "idx": 0,
            "shot_size": "全景",
            "visual": "旧版画面",
            "action": "旧版动作",
            "line_text": None,
            "line_speaker": None,
            "duration_sec": 4,
            "engine": "deterministic",
        }
        conn.execute(
            "INSERT INTO storyboard (scene_id, idx, shot) VALUES (?,?,?)",
            (ids["scene_id"], 0, json.dumps(legacy_shot, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()

    fetched = client.get(f"{BASE}/pipeline/storyboard/{ids['scene_id']}", headers=A)
    assert fetched.status_code == 200, fetched.text
    shot = fetched.json()["shots"][0]
    assert shot["staging"] is None
    assert shot["keyframe"] is None
    assert shot["model_sheet_refs"] == []
    assert shot["art_bible_refs"] == []

    edited = client.put(
        f"{BASE}/pipeline/storyboard/{ids['scene_id']}/0", headers=A,
        json={"staging": "俯视图：补写走位", "model_sheet_refs": ["m1"]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["shot"]["staging"] == "俯视图：补写走位"
    assert edited.json()["shot"]["model_sheet_refs"] == ["m1"]

    exported = client.get(
        f"{BASE}/pipeline/storyboard/{ids['scene_id']}/export", headers=A,
    )
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["shots"][0]["staging"] == "俯视图：补写走位"
    assert "keyframe" in payload["shots"][0]


def test_legacy_version_snapshot_still_works(client):
    """版本快照/回滚不依赖台账或扩展字段（旧剧本审阅路径）。"""
    ids = _seed_minimal_project(client, "旧版本树")
    saved = client.post(
        f"{BASE}/screenplay/projects/{ids['project_id']}/versions", headers=A,
    )
    assert saved.status_code == 201, saved.text
    version_id = saved.json()["id"]

    tree = client.get(
        f"{BASE}/screenplay/projects/{ids['project_id']}/versions/{version_id}",
        headers=A,
    )
    assert tree.status_code == 200
    snapshot = tree.json()["snapshot"]
    assert snapshot["scenes"][0]["lines"][0]["text"] == "旧戏照旧唱。"

    restored = client.post(
        f"{BASE}/screenplay/projects/{ids['project_id']}/restore/{version_id}",
        headers=A,
    )
    assert restored.status_code == 200, restored.text


# ---------------------------------------------------------------- 5.3 无哲思模式


def test_zhesi_unavailable_keeps_ledger_and_quality_operational(client, monkeypatch):
    """哲思不可达：/kernel/link 降级，台账与质量门仍完全可用。"""
    ids = _seed_minimal_project(client, "无哲思样本")
    pid = ids["project_id"]

    def _unreachable(token: str):
        raise Unavailable("simulated: connection refused")

    monkeypatch.setattr(zhesi_client, "fetch_cards", _unreachable)

    link = client.post(f"{BASE}/kernel/link", headers=A, json={})
    assert link.status_code == 200, link.text
    assert link.json()["mode"] == "standalone"
    assert "unavailable" in link.json()["reason"]

    status = client.get(f"{BASE}/kernel/status", headers=A)
    assert status.status_code == 200
    assert status.json()["mode"] == "standalone"

    # 降级后台账照常写入/查询：事实 → 知识 → 上下文
    fact = client.post(
        f"{BASE}/ledger/projects/{pid}/facts", headers=A,
        json={
            "subject": "老角色",
            "predicate": "藏着",
            "object": "一张旧当票",
            "valid_from_episode": 1,
        },
    )
    assert fact.status_code == 201, fact.text
    fact_id = fact.json()["id"]

    knowledge = client.post(
        f"{BASE}/ledger/projects/{pid}/knowledge", headers=A,
        json={
            "character_id": str(ids["character_id"]),
            "fact_id": fact_id,
            "learned_at_episode": 1,
        },
    )
    assert knowledge.status_code == 201, knowledge.text

    context = client.get(
        f"{BASE}/ledger/projects/{pid}/context", headers=A,
        params={"episode": 1, "character_id": [str(ids["character_id"])]},
    )
    assert context.status_code == 200, context.text
    assert context.json()["available"] is True
    assert any(item["object"] == "一张旧当票" for item in context.json()["facts"])
    assert "一张旧当票" in context.json()["text"]

    # 若质量门偷偷依赖哲思，这里会因 fetch_cards 被换成炸弹而 500
    def _must_not_be_called(token: str):
        raise AssertionError("质量门不得依赖哲思")

    monkeypatch.setattr(zhesi_client, "fetch_cards", _must_not_be_called)

    assessment = client.post(
        f"{BASE}/quality/projects/{pid}/assess", headers=A, json={"episode": 1},
    )
    assert assessment.status_code == 200, assessment.text
    body = assessment.json()
    assert body["dimensions"]["continuity"]["available"] is True
    assert body["dimensions"]["continuity"]["score"] is not None
    assert body["overall_score"] is not None


def test_ledger_and_quality_modules_have_no_zhesi_dependency():
    """静态依赖面：台账/质量门/管线模块的 import 图里没有哲思客户端。"""
    import ast
    from pathlib import Path

    def imported_names(path: str) -> set[str]:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[-1])
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name.split(".")[-1] for alias in node.names)
        return names

    for module in (ledger, quality, pipeline, fountain):
        imports = imported_names(module.__file__)
        assert "zhesi_client" not in imports, f"{module.__name__} 不应导入哲思客户端"
        assert "kernel" not in imports, f"{module.__name__} 不应导入哲思内核路由"
