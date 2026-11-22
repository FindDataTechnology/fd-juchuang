"""生成计划的幂等键（change juchuang-image-iteration task 3.3）。

design D4 点名这是最容易漏的一处：**键里必须含源资产 id**。
漏了的话，「对 A 图改外套」与「对 B 图改外套」会撞键返回同一张——
用户看到的是一张自己没编辑过的图，而系统认为缓存命中。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_juchuang_api import pipeline
from fd_juchuang_api.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_JUCHUANG_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("FD_JUCHUANG_AUTH_DEV_TOKENS", "tok=user_a")
    return TestClient(app, headers={"Authorization": "Bearer tok"})


def _key(**over):
    base = dict(storyboard_id=1, shot_idx=1, prompt="p", mode="generate",
                source_asset_id=None, refs="")
    base.update(over)
    return pipeline._idempotency_key(
        base["storyboard_id"], base["shot_idx"], base["prompt"],
        base["mode"], base["source_asset_id"], base["refs"],
    )


def test_same_inputs_same_key():
    assert _key() == _key()


def test_changing_prompt_changes_key():
    assert _key(prompt="a") != _key(prompt="b")


def test_changing_mode_changes_key():
    assert _key(mode="generate") != _key(mode="edit")


def test_source_asset_is_part_of_the_key():
    """最关键的一条：同指令、不同源图 MUST 不同键。"""
    assert _key(mode="edit", source_asset_id="A") != _key(mode="edit", source_asset_id="B")


def test_reference_assets_are_part_of_the_key():
    assert _key(refs="r1") != _key(refs="r1,r2")


def test_key_is_stable_across_processes():
    """内置 hash() 按进程加盐，用它做键会在重启后全部失效。"""
    import subprocess
    import sys

    code = ("import sys;sys.path.insert(0,'.');"
            "from fd_juchuang_api import pipeline;"
            "print(pipeline._idempotency_key(1,2,'p','generate',None,''))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(__import__("pathlib").Path(__file__).parent.parent))
    assert out.stdout.strip() == _key(storyboard_id=1, shot_idx=2, prompt="p"), out.stderr