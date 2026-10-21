"""端到端与集成测试（change juchuang-continuity-layer task 5.1 / 5.2 / 5.3）。

覆盖三层：

1. **5.1 全链**：子进程跑 ``scripts/e2e_juchuang.py``（临时库、离线、确定性），
   断言脚本退出码与 JSON 报告；报告必须包含 Fountain 往返、台账、分镜生成后检查、
   质量门与导出等关键 check id。
2. **边界与性能**：畸形导入（坏 FDX / 空白 Fountain）、空项目评估、缺失角色引用、
   大剧本（20 场 × 30 台词）的分镜与质量评估时延上界。
3. **集成面**：所有 router 挂在 ``main.app`` 上（鉴权、项目归属、共享 SQLite），
   与部署形态一致，而不是单模块拼装。

5.2（旧剧本回归）与 5.3（无哲思模式）的细粒度断言在
``tests/test_regression_compat.py``；本文件的 e2e 报告同时覆盖二者的端到端路径。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fd_juchuang_api import ledger, pipeline, quality, screenplay

A = {"Authorization": "Bearer tok-a"}
BASE = "/api/v1"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "e2e_juchuang.py"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "e2e-integration.db"
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


# ---------------------------------------------------------------- 5.1 E2E 脚本


def test_e2e_script_offline_passes(tmp_path):
    """5.1：一条命令跑通全链；JSON 报告可作为验收证据。"""
    report_path = tmp_path / "e2e-report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(tmp_path / "e2e.db"),
            "--json-report",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, (
        f"e2e 脚本失败\nstdout:\n{result.stdout[-6000:]}\nstderr:\n{result.stderr[-3000:]}"
    )
    assert "E2E PASS" in result.stdout
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["passed"] is True and payload["error"] is None
    assert payload["mode"] == "offline"
    assert payload["duration_sec"] >= 0

    check_ids = {item["id"] for item in payload["checks"]}
    required = {
        # 5.1 主链
        "fountain.export",
        "fountain.import",
        "fountain.roundtrip",
        "ledger.timepoint",
        "ledger.precontext",
        "storyboard.generate",
        "storyboard.fields",
        "storyboard.postcheck_block",
        "storyboard.prompts",
        "quality.rubric",
        "quality.continuity_located",
        "quality.redline_block",
        "quality.deliver_409",
        "export.project",
        "export.fountain",
        "export.fdx",
        "export.storyboard",
        "export.promptset",
        # 5.2 旧剧本回归
        "legacy.ledger.empty",
        "legacy.storyboard.degraded",
        "legacy.quality.degraded",
        "legacy.storyboard.legacy_row",
        # 5.3 无哲思模式
        "zhesi.link_standalone",
        "zhesi.ledger.independent",
        "zhesi.quality.independent",
    }
    assert required <= check_ids, f"e2e 报告缺少检查项：{sorted(required - check_ids)}"
    assert len(payload["checks"]) >= 80


# ---------------------------------------------------------------- 边界输入


def _create_minimal_project(client: TestClient, title: str = "边界样本") -> int:
    project = client.post(
        f"{BASE}/screenplay/projects", headers=A,
        json={"title": title, "logline": "边界测试"},
    ).json()
    return int(project["id"])


def test_malformed_exchange_inputs_are_rejected_without_500(client):
    pid = _create_minimal_project(client)

    bad_fdx = client.post(
        f"{BASE}/screenplay/projects/{pid}/import/fdx", headers=A,
        json={"text": "<not-valid-xml"},
    )
    assert bad_fdx.status_code == 422, bad_fdx.text

    whitespace_fountain = client.post(
        f"{BASE}/screenplay/projects/{pid}/import/fountain", headers=A,
        json={"text": "   \n\n   "},
    )
    # 解析器对空文档不应崩溃：要么明确 422，要么返回 0 场次的导入报告
    assert whitespace_fountain.status_code in (200, 422), whitespace_fountain.text
    if whitespace_fountain.status_code == 200:
        assert whitespace_fountain.json()["scenes"]["total"] == 0

    invalid_episode = client.post(
        f"{BASE}/quality/projects/{pid}/assess", headers=A, json={"episode": 0},
    )
    assert invalid_episode.status_code == 422


def test_empty_project_assessment_is_graceful(client):
    """空项目（无场次/无台账）：质量门返回结构化结果，不抛 500。"""
    pid = _create_minimal_project(client, "空项目")
    response = client.post(
        f"{BASE}/quality/projects/{pid}/assess", headers=A, json={},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stats"]["scene_count"] == 0
    assert body["dimensions"]["continuity"]["available"] is False
    assert body["dimensions"]["continuity"]["score"] is None
    assert body["blocked"] is False

    board = client.get(f"{BASE}/pipeline/storyboard/99999", headers=A)
    assert board.status_code == 404


def test_missing_character_reference_degrades_to_problem_not_crash(env, client):
    """台词引用了不存在的角色：生成与评估照常，质量门定位到问题。"""
    conn = screenplay.connect()
    try:
        screenplay.migrate(conn)
        conn.execute("PRAGMA foreign_keys=OFF")  # 构造脏数据：台词指向已不存在的角色
        cur = conn.execute(
            "INSERT INTO projects (id, user_id, title, logline, created_at)"
            " VALUES (NULL,'u1','缺失角色样本',NULL,'2026-01-01')",
        )
        pid = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO scenes (project_id, idx, title, synopsis, action,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (pid, 1, "第一场：残页", "台词引用了不存在的角色", "灯下。", "2026-01-01"),
        )
        scene_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO lines (scene_id, idx, speaker_id, text) VALUES (?,?,?,?)",
            (scene_id, 1, 999999, "这句台词没有主人。"),
        )
        conn.commit()
    finally:
        conn.close()

    board = client.post(f"{BASE}/pipeline/storyboard/{scene_id}", headers=A)
    assert board.status_code == 200, board.text
    assert board.json()["shot_count"] == 3
    assert board.json()["continuity"]["available"] is False

    assessed = client.post(
        f"{BASE}/quality/projects/{pid}/assess", headers=A, json={"episode": 1},
    )
    assert assessed.status_code == 200, assessed.text
    problems = assessed.json()["dimensions"]["format"]["problems"]
    speaker_problems = [
        p for p in problems if p["rule_id"] == "format.speaker_present"
    ]
    assert speaker_problems, "缺失说话人必须在格式维产出问题"
    location = speaker_problems[0]["location"]
    assert location["scene_id"] == scene_id and location["line_idx"] == 1


# ---------------------------------------------------------------- 性能上界


def _seed_large_script(scene_count: int = 20, lines_per_scene: int = 30) -> dict:
    """直接建库（比逐条 API 快两个数量级），只测读取/评估/生成路径。"""
    conn = screenplay.connect()
    try:
        screenplay.migrate(conn)
        cur = conn.execute(
            "INSERT INTO projects (id, user_id, title, logline, created_at)"
            " VALUES (NULL,'u1','大剧本','性能样本','2026-01-01')",
        )
        pid = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO characters (project_id, name, appearance, stance, created_at)"
            " VALUES (?,?,?,?,?)",
            (pid, "主角", '{"年龄感":"青年","着装":"长衫"}', None, "2026-01-01"),
        )
        cid = int(cur.lastrowid)
        scene_ids: list[int] = []
        for idx in range(1, scene_count + 1):
            cur = conn.execute(
                "INSERT INTO scenes (project_id, idx, title, synopsis, action,"
                " transition, conflict, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    pid, idx, f"第{idx}场：雨夜追索", f"第{idx}场梗概",
                    "雨声压低了整条街。", "CUT TO:",
                    json.dumps(
                        {"positions": ["追", "逃"], "source": "性能样本"},
                        ensure_ascii=False,
                    ),
                    "2026-01-01",
                ),
            )
            scene_id = int(cur.lastrowid)
            scene_ids.append(scene_id)
            conn.executemany(
                "INSERT INTO lines (scene_id, idx, speaker_id, text)"
                " VALUES (?,?,?,?)",
                [
                    (
                        scene_id,
                        number,
                        cid,
                        f"第{idx}场第{number}句：账本还在你手里，你终于肯说了吗？",
                    )
                    for number in range(1, lines_per_scene + 1)
                ],
            )
        conn.commit()
    finally:
        conn.close()
    return {"project_id": pid, "scene_ids": scene_ids,
            "scene_count": scene_count, "line_count": scene_count * lines_per_scene}


@pytest.mark.parametrize("scene_count,lines_per_scene", [(20, 30)])
def test_large_script_performance_bounds(env, scene_count, lines_per_scene):
    """大剧本（600 台词）：分镜与质量评估都必须亚十秒（本地实测 << 1s）。"""
    seeded = _seed_large_script(scene_count, lines_per_scene)

    started = time.perf_counter()
    board = pipeline.create_storyboard(seeded["scene_ids"][9], episode=10)
    board_seconds = time.perf_counter() - started
    assert board["shot_count"] == lines_per_scene + 2
    assert board["continuity"]["available"] is False  # 空台账

    conn = quality.connect()
    try:
        quality.migrate(conn)
        ledger.migrate(conn)
        started = time.perf_counter()
        assessment = quality.assess_episode(
            conn, seeded["project_id"], episode=10, persist=False,
        )
        assess_seconds = time.perf_counter() - started
    finally:
        conn.close()

    assert assessment["stats"]["scene_count"] == scene_count
    assert assessment["stats"]["line_count"] == seeded["line_count"]
    assert assessment["dimensions"]["continuity"]["available"] is False
    assert assessment["dimensions"]["format"]["score"] is not None

    assert board_seconds < 10.0, f"分镜过慢：{board_seconds:.2f}s"
    assert assess_seconds < 10.0, f"质量评估过慢：{assess_seconds:.2f}s"
