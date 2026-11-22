"""资产绑定：把引用字符串升级为真资产（change juchuang-image-iteration task 2.3）。

关键约束（spec）：
- 绑定是**附加信息**，原有 appearance / art_bible 字符串字段 MUST 原样保留；
- 导出 MUST NOT 依赖绑定 —— 没有绑定也要能完整导出。

第二条容易被写成「顺手把导出改成读 asset_id」，那就是把可选能力变成了硬依赖。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_juchuang_api.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_JUCHUANG_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("FD_JUCHUANG_AUTH_DEV_TOKENS", "tok=user_a")
    return TestClient(app, headers={"Authorization": "Bearer tok"})


def _project_with_character(client) -> tuple[int, int]:
    p = client.post("/api/v1/screenplay/projects",
                    json={"title": "绑定测试"}).json()
    c = client.post(f"/api/v1/screenplay/projects/{p['id']}/characters",
                    json={"name": "阿甲",
                          "appearance": {"年龄感": "青年", "体型": "瘦"}}).json()
    return p["id"], c["id"]


def test_bind_character_asset(client):
    pid, cid = _project_with_character(client)
    r = client.post(f"/api/v1/screenplay/projects/{pid}/characters/{cid}/asset",
                    json={"asset_id": "a-1", "asset_slug": "model-sheet-a"})
    assert r.status_code == 200
    body = r.json()
    assert body["asset_id"] == "a-1"
    assert body["asset_slug"] == "model-sheet-a"


def test_binding_is_idempotent(client):
    pid, cid = _project_with_character(client)
    url = f"/api/v1/screenplay/projects/{pid}/characters/{cid}/asset"
    first = client.post(url, json={"asset_id": "a-1"}).json()
    second = client.post(url, json={"asset_id": "a-1"}).json()
    assert first["asset_id"] == second["asset_id"] == "a-1"


def test_binding_does_not_touch_appearance_strings(client):
    """spec「资产绑定不破坏导出」的核心：绑定不能覆盖原字符串要素集。"""
    pid, cid = _project_with_character(client)
    before = client.get(f"/api/v1/screenplay/projects/{pid}/characters/{cid}").json()
    client.post(f"/api/v1/screenplay/projects/{pid}/characters/{cid}/asset",
                json={"asset_id": "a-1"})
    after = client.get(f"/api/v1/screenplay/projects/{pid}/characters/{cid}").json()
    assert after["appearance"] == before["appearance"]
    assert after["name"] == before["name"]


def test_export_works_without_any_binding(client):
    """没有绑定也必须能完整导出 —— 这是回归门槛，防止把绑定做成硬依赖。"""
    pid, _ = _project_with_character(client)
    r = client.get(f"/api/v1/screenplay/projects/{pid}/characters")
    assert r.status_code == 200
    assert r.json()[0]["asset_id"] is None


def test_unknown_character_is_404(client):
    pid, _ = _project_with_character(client)
    r = client.post(f"/api/v1/screenplay/projects/{pid}/characters/9999/asset",
                    json={"asset_id": "x"})
    assert r.status_code == 404


def test_bind_scene_asset(client):
    pid, _ = _project_with_character(client)
    s = client.post(f"/api/v1/screenplay/projects/{pid}/scenes",
                    json={"title": "第一场"}).json()
    r = client.post(f"/api/v1/screenplay/projects/{pid}/scenes/{s['id']}/asset",
                    json={"asset_id": "scene-1"})
    assert r.status_code == 200
    assert r.json()["asset_id"] == "scene-1"
    assert r.json()["title"] == "第一场"
