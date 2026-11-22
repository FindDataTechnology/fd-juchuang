"""VAAS 客户端的降级契约（change juchuang-image-iteration task 1.4）。

核心主张：**不配置也能上线**。VAAS 缺失是可用性分类，不是异常——
剧创的剧本/分镜/提示词/导出全流程 MUST 不受影响。

另一个主张：**按能力分别判定**。文生图可用而编辑不可用是合法状态，
两者各自呈现，不互相牵连（spec「降级独立模式」）。
"""
from __future__ import annotations

import pytest

from fd_juchuang_api import vaas_client


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("VAAS_MCP_URL", "VAAS_SERVICE_TOKEN", "VAAS_SERVICE_IDENTITY"):
        monkeypatch.delenv(k, raising=False)


def test_unconfigured_is_a_category_not_a_crash():
    assert vaas_client.configured() is False
    caps = vaas_client.capabilities()
    assert caps["generate"]["available"] is False
    assert caps["generate"]["reason"] == "unconfigured"
    assert caps["edit"]["reason"] == "unconfigured"
    # 有可行动的说明，而不是一个裸错误
    assert "VAAS_MCP_URL" in caps["generate"]["message"]


def test_unconfigured_raises_with_category(monkeypatch):
    with pytest.raises(vaas_client.Unavailable) as e:
        vaas_client.generate_image("anything")
    assert e.value.category == "unconfigured"


def test_partial_config_still_counts_as_unconfigured(monkeypatch):
    """只配一半也是未配置——半配比全缺更危险，它会通过 presence 检查然后 401。"""
    monkeypatch.setenv("VAAS_MCP_URL", "http://x/mcp")
    assert vaas_client.configured() is False
    monkeypatch.setenv("VAAS_SERVICE_TOKEN", "tok")
    assert vaas_client.configured() is True


def test_unreachable_is_distinct_from_unconfigured(monkeypatch):
    """两类必须分开：一个要改配置，一个要查网络。"""
    monkeypatch.setenv("VAAS_MCP_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("VAAS_SERVICE_TOKEN", "tok")
    caps = vaas_client.capabilities()
    assert caps["generate"]["reason"] == "unreachable"


def test_capabilities_split_generate_from_edit(monkeypatch):
    """一个车道坏不代表另一个坏 —— 这是本 change 最容易做错的地方。"""
    monkeypatch.setenv("VAAS_MCP_URL", "http://x/mcp")
    monkeypatch.setenv("VAAS_SERVICE_TOKEN", "tok")

    fake = {
        "items": [
            {"name": "seedream-grok", "driver": "generate_image",
             "lane": {"health": "active", "reason_class": "none"}},
            {"name": "edit-grok", "driver": "edit_image",
             "lane": {"health": "down", "reason_class": "upstream_blocked"}},
        ]
    }
    monkeypatch.setattr(vaas_client, "_call_tool", lambda *a, **k: fake)
    caps = vaas_client.capabilities()
    assert caps["generate"]["available"] is True
    assert caps["generate"]["generator"] == "seedream-grok"
    assert caps["edit"]["available"] is False
    assert caps["edit"]["reason"] == "lane_down"


def test_both_down_reports_both(monkeypatch):
    monkeypatch.setenv("VAAS_MCP_URL", "http://x/mcp")
    monkeypatch.setenv("VAAS_SERVICE_TOKEN", "tok")
    fake = {"items": [
        {"name": "s", "driver": "generate_image", "lane": {"health": "down", "reason_class": "upstream_blocked"}},
        {"name": "e", "driver": "edit_image", "lane": {"health": "down", "reason_class": "upstream_blocked"}},
    ]}
    monkeypatch.setattr(vaas_client, "_call_tool", lambda *a, **k: fake)
    caps = vaas_client.capabilities()
    assert caps["generate"]["reason"] == "lane_down"
    assert caps["edit"]["reason"] == "lane_down"
