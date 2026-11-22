"""执行端点：镜级生成与编辑（change juchuang-image-iteration task 4.4）。

三条契约用测试钉住：
1. 额度上限是**硬门** —— 超了在发起任何付费调用前拒绝，不是截断后继续。
2. 重复点击同键**只调一次上游**（用假 client 计数）。
3. 编辑不可用时**不静默退回生成**（spec 明写禁止）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_juchuang_api import pipeline, vaas_client
from fd_juchuang_api.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_JUCHUANG_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("FD_JUCHUANG_AUTH_DEV_TOKENS", "tok=user_a")
    return TestClient(app, headers={"Authorization": "Bearer tok"})


@pytest.fixture
def ready(client, monkeypatch):
    """一场已转分镜、已出计划的戏。"""
    pid = client.post("/api/v1/screenplay/projects", json={"title": "P"}).json()["id"]
    c = client.post(f"/api/v1/screenplay/projects/{pid}/characters",
                    json={"name": "阿甲", "appearance": {"年龄感": "青年", "体型": "瘦"}}).json()
    s = client.post(f"/api/v1/screenplay/projects/{pid}/scenes",
                    json={"title": "第一场"}).json()
    client.post(f"/api/v1/screenplay/projects/{pid}/scenes/{s['id']}/lines",
                json={"speaker_id": c["id"], "text": "台词一。"})
    client.post(f"/api/v1/pipeline/storyboard/{s['id']}")
    client.post(f"/api/v1/pipeline/prompts/{s['id']}")
    return s["id"]


def _fake_generate(calls):
    def _g(prompt, slug=None, size=None):
        calls.append(prompt)
        return {"id": f"asset-{len(calls)}", "slug": slug or f"s{len(calls)}",
                "file_paths": [f"images/x/{len(calls)}.png"]}
    return _g


def test_max_calls_is_a_hard_gate(client, ready, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(vaas_client, "generate_image", _fake_generate(calls))

    r = client.post(f"/api/v1/pipeline/generate/{ready}", json={"max_calls": 1})
    assert r.status_code == 400
    assert "超过上限" in r.json()["detail"]
    assert calls == [], "超限时不得发起任何付费调用"


def test_generate_respects_shot_subset(client, ready, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(vaas_client, "generate_image", _fake_generate(calls))
    r = client.post(f"/api/v1/pipeline/generate/{ready}",
                    json={"shot_idxs": [0], "max_calls": 5})
    assert r.status_code == 200
    assert len(r.json()["generated"]) == 1
    assert len(calls) == 1


def test_repeat_generate_records_once_per_shot(client, ready, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(vaas_client, "generate_image", _fake_generate(calls))
    client.post(f"/api/v1/pipeline/generate/{ready}", json={"shot_idxs": [0], "max_calls": 5})
    client.post(f"/api/v1/pipeline/generate/{ready}", json={"shot_idxs": [0], "max_calls": 5})
    # 镜位表按 (scene, shot) 唯一 —— 第二次覆盖同一行，不新增镜像
    conn = pipeline.connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM shot_image WHERE scene_id=?",
                         (ready,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_partial_failure_keeps_successes(client, ready, monkeypatch):
    seq = iter(["ok", "boom"])

    def _g(prompt, slug=None, size=None):
        if next(seq) == "boom":
            raise vaas_client.Unavailable("lane_down", "lane is down")
        return {"id": "asset-ok", "slug": slug, "file_paths": ["p.png"]}
    monkeypatch.setattr(vaas_client, "generate_image", _g)

    r = client.post(f"/api/v1/pipeline/generate/{ready}",
                    json={"shot_idxs": [0, 1], "max_calls": 5})
    body = r.json()
    assert len(body["generated"]) == 1
    assert body["failures"][0]["category"] == "lane_down"


def test_edit_without_existing_image_is_409(client, ready):
    r = client.post(f"/api/v1/pipeline/shots/{ready}/0/edit",
                    json={"instruction": "改蓝"})
    assert r.status_code == 409


def test_edit_uses_current_shot_image_as_source(client, ready, monkeypatch):
    monkeypatch.setattr(vaas_client, "generate_image", _fake_generate([]))
    client.post(f"/api/v1/pipeline/generate/{ready}", json={"shot_idxs": [0], "max_calls": 5})

    got = {}

    def _e(source, instruction, slug=None, mask=None, references=None, n=1):
        got["source"] = source
        got["instruction"] = instruction
        return {"id": "edited-1", "file_paths": ["e.png"], "warnings": []}
    monkeypatch.setattr(vaas_client, "edit_image", _e)

    r = client.post(f"/api/v1/pipeline/shots/{ready}/0/edit",
                    json={"instruction": "改成蓝色"})
    assert r.status_code == 200
    assert got["source"] == "asset-1"      # 该镜生成的图
    assert r.json()["source_asset_id"] == "asset-1"


def test_edit_unavailable_does_not_fall_back_to_generate(client, ready, monkeypatch):
    """spec 明写：编辑不可用时 MUST NOT 静默回退为一次文生图调用。"""
    monkeypatch.setattr(vaas_client, "generate_image", _fake_generate([]))
    client.post(f"/api/v1/pipeline/generate/{ready}", json={"shot_idxs": [0], "max_calls": 5})

    gen_calls: list[str] = []
    monkeypatch.setattr(vaas_client, "generate_image", _fake_generate(gen_calls))

    def _down(*a, **k):
        raise vaas_client.Unavailable("lane_down", "edit lane down")
    monkeypatch.setattr(vaas_client, "edit_image", _down)

    r = client.post(f"/api/v1/pipeline/shots/{ready}/0/edit", json={"instruction": "x"})
    assert r.status_code == 503
    assert gen_calls == [], "编辑失败不得退回生成"


def test_capabilities_endpoint_splits_generate_and_edit(client, monkeypatch):
    monkeypatch.setattr(vaas_client, "capabilities", lambda: {
        vaas_client.GENERATE: {"available": True, "generator": "g"},
        vaas_client.EDIT: {"available": False, "reason": "lane_down", "message": "down"},
    })
    r = client.get("/api/v1/pipeline/generate/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["generate"]["available"] is True
    assert body["edit"]["available"] is False
    assert body["edit"]["reason"] == "lane_down"
