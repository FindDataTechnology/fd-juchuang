"""两条成片通路（change juchuang-video-generation tasks 2.4 / 3.x）。

守四条：
1. 缺图整场拒，不静默跳过镜头；
2. render 不产生上游生成调用（零成本），返回段对齐；
3. i2v 未确认时**只返回预估**，不发起任何付费调用；
4. i2v 车道不可用或首帧未验证时，不发起调用、也不静默退回 render。
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
def scene_with_images(client, monkeypatch):
    """一场 3 镜且都有图的戏。

    图位里存的是**资产 id**；渲染面要**文件路径**，所以这里把解析打桩成
    `path-<id>`——真实解析由 vaas_client.asset_file_path 向 VAAS 查账本。
    """
    monkeypatch.setattr(vaas_client, "asset_file_path", lambda aid: f"path-{aid}")
    pid = client.post("/api/v1/screenplay/projects", json={"title": "P"}).json()["id"]
    c = client.post(f"/api/v1/screenplay/projects/{pid}/characters",
                    json={"name": "阿甲", "appearance": {"年龄感": "青年", "体型": "瘦"}}).json()
    s = client.post(f"/api/v1/screenplay/projects/{pid}/scenes",
                    json={"title": "天台"}).json()
    for t in ("风停了。", "你听见了吗？", "没有。"):
        client.post(f"/api/v1/screenplay/projects/{pid}/scenes/{s['id']}/lines",
                    json={"speaker_id": c["id"], "text": t})
    client.post(f"/api/v1/pipeline/storyboard/{s['id']}")
    conn = pipeline.connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM storyboard WHERE scene_id=?",
                         (s["id"],)).fetchone()[0]
        for i in range(n):
            conn.execute(
                "INSERT INTO shot_image (scene_id, shot_idx, asset_id, idempotency_key)"
                " VALUES (?,?,?,?) ON CONFLICT DO UPDATE SET asset_id=excluded.asset_id",
                (s["id"], i, f"img-{i}", f"k{i}"),
            )
        conn.commit()
    finally:
        conn.close()
    return s["id"], n


def test_missing_image_rejects_whole_scene(client, scene_with_images):
    sid, n = scene_with_images
    conn = pipeline.connect()
    try:
        conn.execute("DELETE FROM shot_image WHERE scene_id=? AND shot_idx=0", (sid,))
        conn.commit()
    finally:
        conn.close()
    r = client.post(f"/api/v1/pipeline/video/{sid}", json={"route": "render"})
    assert r.status_code == 409
    assert 0 in r.json()["detail"]["missing"]


def test_render_makes_no_generation_calls(client, scene_with_images, monkeypatch):
    sid, n = scene_with_images
    gen_calls = []
    monkeypatch.setattr(vaas_client, "generate_image",
                        lambda *a, **k: gen_calls.append(1))
    monkeypatch.setattr(vaas_client, "submit_video", lambda *a, **k: gen_calls.append(2))
    monkeypatch.setattr(vaas_client, "render_carousel",
                        lambda **k: {"id": "vid-1", "slug": "s", "file_paths": ["v.mp4"]})

    r = client.post(f"/api/v1/pipeline/video/{sid}", json={"route": "render"})
    assert r.status_code == 200
    body = r.json()
    assert body["route"] == "render" and body["cost"] == "local"
    assert gen_calls == [], "render 通路不得产生任何上游生成调用"
    assert len(body["segments"]) == n
    assert body["duration_source"] == "audio"


def test_render_uses_deterministic_slug(client, scene_with_images, monkeypatch):
    sid, _ = scene_with_images
    seen = []
    monkeypatch.setattr(vaas_client, "render_carousel",
                        lambda **k: seen.append(k.get("slug")) or {"id": "v", "file_paths": []})
    client.post(f"/api/v1/pipeline/video/{sid}", json={"route": "render"})
    client.post(f"/api/v1/pipeline/video/{sid}", json={"route": "render"})
    assert seen[0] == seen[1] == f"jc-carousel-{sid}"


def test_i2v_requires_confirm_before_any_paid_call(client, scene_with_images, monkeypatch):
    sid, n = scene_with_images
    monkeypatch.setattr(vaas_client, "video_capabilities", lambda: {
        "i2v": {"available": True, "first_frame_supported": True},
        "render": {"available": True},
    })
    calls = []
    monkeypatch.setattr(vaas_client, "submit_video", lambda **k: calls.append(k))

    r = client.post(f"/api/v1/pipeline/video/{sid}", json={"route": "i2v"})
    assert r.status_code == 200
    body = r.json()
    assert body["confirmed"] is False
    assert body["estimated_calls"] == n
    assert body["estimated_seconds"] > 0
    assert calls == [], "未确认不得发起任何付费调用"


def test_i2v_max_calls_is_a_hard_gate(client, scene_with_images, monkeypatch):
    sid, _ = scene_with_images
    monkeypatch.setattr(vaas_client, "video_capabilities", lambda: {
        "i2v": {"available": True, "first_frame_supported": True}})
    calls = []
    monkeypatch.setattr(vaas_client, "submit_video", lambda **k: calls.append(k))
    r = client.post(f"/api/v1/pipeline/video/{sid}",
                    json={"route": "i2v", "confirm": True, "max_calls": 1})
    assert r.status_code == 400
    assert calls == []


def test_i2v_submits_with_first_frame(client, scene_with_images, monkeypatch):
    sid, n = scene_with_images
    monkeypatch.setattr(vaas_client, "video_capabilities", lambda: {
        "i2v": {"available": True, "first_frame_supported": True}})
    got = []

    def _sub(**k):
        got.append(k)
        return {"job_id": f"job-{k['slug']}", "provider_task_id": "t"}
    monkeypatch.setattr(vaas_client, "submit_video", _sub)

    r = client.post(f"/api/v1/pipeline/video/{sid}",
                    json={"route": "i2v", "confirm": True, "shot_idxs": [0]})
    assert r.status_code == 200
    assert len(got) == 1
    assert got[0]["first_frame"] == "img-0", "首帧必须是该镜自己的图"


def test_i2v_unavailable_does_not_fall_back_to_render(client, scene_with_images, monkeypatch):
    sid, _ = scene_with_images
    monkeypatch.setattr(vaas_client, "video_capabilities", lambda: {
        "i2v": {"available": False, "reason": "lane_down"},
        "render": {"available": True}})
    render_calls = []
    monkeypatch.setattr(vaas_client, "render_carousel",
                        lambda **k: render_calls.append(k))
    r = client.post(f"/api/v1/pipeline/video/{sid}",
                    json={"route": "i2v", "confirm": True})
    assert r.status_code == 503
    assert render_calls == [], "i2v 不可用时不得静默改成成片渲染（那是另一个产物）"


def test_i2v_refuses_when_first_frame_unverified(client, scene_with_images, monkeypatch):
    """车道活着但首帧未验证：拒绝，而不是发一个会退化成纯文生视频的任务。"""
    sid, _ = scene_with_images
    monkeypatch.setattr(vaas_client, "video_capabilities", lambda: {
        "i2v": {"available": True, "first_frame_supported": False}})
    calls = []
    monkeypatch.setattr(vaas_client, "submit_video", lambda **k: calls.append(k))
    r = client.post(f"/api/v1/pipeline/video/{sid}",
                    json={"route": "i2v", "confirm": True})
    assert r.status_code == 503
    assert calls == []


def test_video_capabilities_endpoint_splits_routes(client, monkeypatch):
    monkeypatch.setattr(vaas_client, "video_capabilities", lambda: {
        "i2v": {"available": True}, "render": {"available": False, "reason": "unreachable"}})
    r = client.get("/api/v1/pipeline/video/capabilities")
    assert r.json()["i2v"]["available"] is True
    assert r.json()["render"]["reason"] == "unreachable"
