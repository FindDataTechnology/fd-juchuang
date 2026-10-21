"""Fountain / FDX 交换层测试（change juchuang-continuity-layer task 1.1-1.6）。

覆盖：
- 1.1 映射表覆盖全部结构化字段，Fountain 不承载字段显式列出
- 1.2 导出文本可被 Fountain 解析器完整解析
- 1.3 导出→导入后场次/角色/台词/转场保持对应
- 1.4 扩展语法进入未识别报告而非静默丢弃
- 1.5 哲思立场标注、场次张力与分镜引用往返后保留
- 1.6 FDX 导入导出与有损字段逐项警告
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fd_juchuang_api import fountain, pipeline
from fd_juchuang_api.screenplay import connect, migrate, router


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_JUCHUANG_DB", str(tmp_path / "jc-fountain.db"))
    monkeypatch.setenv("FD_JUCHUANG_AUTH_DEV_TOKENS", "tok-a=u1,tok-b=u2")
    monkeypatch.setenv("FD__AUTH_DEV_TOKENS", "tok-a=u1,tok-b=u2")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


A = {"Authorization": "Bearer tok-a"}
B = {"Authorization": "Bearer tok-b"}

BASE = "/api/v1/screenplay"


def _seed(client) -> dict:
    """建一个含 2 角色 / 2 场次 / 2 台词（含括号说明、动作、转场、张力）的项目。"""
    project = client.post(f"{BASE}/projects", headers=A, json={
        "title": "洞穴之喻",
        "logline": "一场关于真相与责任的对手戏",
    }).json()
    pid = project["id"]

    chen = client.post(f"{BASE}/projects/{pid}/characters", headers=A, json={
        "name": "陈守拙",
        "appearance": {"年龄感": "四十岁上下", "体型": "清瘦挺拔", "着装": "深灰长衫"},
        "stance": {"position": "真相必须公开，哪怕代价惨重",
                   "reasons": "多年立场", "source": "zhesi:stance-1"},
    }).json()
    lin = client.post(f"{BASE}/projects/{pid}/characters", headers=A, json={
        "name": "林务实",
        "appearance": {"年龄感": "三十岁上下", "体型": "壮实", "着装": "短打"},
        "stance": {"position": "秩序优先，真相可以等待",
                   "reasons": "现实经验", "source": "zhesi:stance-2"},
    }).json()

    s1 = client.post(f"{BASE}/projects/{pid}/scenes", headers=A, json={
        "title": "第一场：对峙",
        "synopsis": "旧友重逢，立场相左",
        "action": "洞穴深处，火光摇曳。\n两人对坐。",
        "transition": "CUT TO:",
        "conflict": {"positions": ["真相必须公开，哪怕代价惨重",
                                   "秩序优先，真相可以等待"],
                     "source": "zhesi:tension-1"},
    }).json()
    s2 = client.post(f"{BASE}/projects/{pid}/scenes", headers=A, json={
        "title": "INT. 洞穴 - 夜",
        "synopsis": "夜谈继续",
        "transition": "FADE OUT.",
    }).json()

    l1 = client.post(f"{BASE}/scenes/{s1['id']}/lines", headers=A, json={
        "speaker_id": chen["id"],
        "text": "纸包不住火。",
        "parenthetical": "低声",
    }).json()
    l2 = client.post(f"{BASE}/scenes/{s1['id']}/lines", headers=A, json={
        "speaker_id": lin["id"],
        "text": "火也会烧掉无辜的人。",
    }).json()
    return {"pid": pid, "chen": chen, "lin": lin,
            "s1": s1, "s2": s2, "l1": l1, "l2": l2}


def _export_tree(client, pid):
    r = client.get(f"{BASE}/projects/{pid}/export", headers=A)
    assert r.status_code == 200, r.text
    return r.json()


def _export_fountain(client, pid):
    r = client.get(f"{BASE}/projects/{pid}/export/fountain", headers=A)
    assert r.status_code == 200, r.text
    return r.text


def _import_fountain(client, pid, text, mode="merge"):
    r = client.post(f"{BASE}/projects/{pid}/import/fountain", headers=A,
                    json={"text": text, "mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


# ---- 1.1 映射表覆盖 ----


def test_field_mapping_covers_every_structured_field(client):
    ids = _seed(client)
    tree = _export_tree(client, ids["pid"])

    # 剧本树出现的每个字段都必须有明确归属（映射或「不承载」）
    assert fountain.FountainFieldMapping.uncovered_fields(tree) == []

    mapping = client.get(f"{BASE}/formats/fountain/mapping", headers=A).json()
    assert mapping["counts"]["mapped"] >= 13
    assert "scenes.transition" in mapping["mapped"]
    assert "scenes.action" in mapping["mapped"]
    assert "lines.parenthetical" in mapping["mapped"]
    # Fountain 无法承载、必须留在结构化层的字段
    assert "characters.stance" in mapping["not_carried"]
    assert "characters.appearance" in mapping["not_carried"]
    assert "scenes.conflict" in mapping["not_carried"]
    assert "storyboard" in mapping["not_carried"]
    assert "versions" in mapping["not_carried"]


# ---- 1.2 序列化可被解析 ----


def test_export_fountain_is_parseable(client):
    ids = _seed(client)
    text = _export_fountain(client, ids["pid"])

    assert text.startswith("Title: 洞穴之喻")
    assert "Logline: 一场关于真相与责任的对手戏" in text
    assert ".第一场：对峙" in text          # 非标准标题用 . 强制
    assert "INT. 洞穴 - 夜" in text         # 标准标题原样
    assert "= 旧友重逢，立场相左" in text
    assert "@陈守拙" in text
    assert "(低声)" in text
    assert ">CUT TO:" in text

    parsed = fountain.parse_from_fountain(text)
    assert parsed["unrecognized"] == []
    assert parsed["warnings"] == []
    assert parsed["title_page"]["title"] == "洞穴之喻"
    assert [s["heading"] for s in parsed["scenes"]] == [
        "第一场：对峙", "INT. 洞穴 - 夜"]
    assert parsed["scenes"][0]["synopsis"] == "旧友重逢，立场相左"
    assert parsed["scenes"][0]["action"] == "洞穴深处，火光摇曳。\n两人对坐。"
    assert parsed["scenes"][0]["transition"] == "CUT TO:"
    assert parsed["scenes"][0]["lines"] == [
        {"speaker": "陈守拙", "parenthetical": "低声", "text": "纸包不住火。"},
        {"speaker": "林务实", "parenthetical": None,
         "text": "火也会烧掉无辜的人。"},
    ]

    # JSON 形态同时给出有损警告与映射表
    body = client.get(f"{BASE}/projects/{ids['pid']}/export/fountain?format=json",
                      headers=A).json()
    assert body["text"] == text
    assert body["format"] == "fountain"
    assert body["mapping"]["counts"]["mapped"] >= 13


# ---- 1.3 往返保持场次/角色/台词/转场 ----


def test_fountain_roundtrip_preserves_structure(client):
    ids = _seed(client)
    pid = ids["pid"]
    before = _export_tree(client, pid)
    text = _export_fountain(client, pid)

    report = _import_fountain(client, pid, text, mode="merge")
    assert report["unrecognized"] == []
    assert report["format"] == "fountain"
    assert report["scenes"]["total"] == 2

    after = _export_tree(client, pid)
    # 场次 id 与顺序保持（分镜引用依赖 id）
    assert [s["id"] for s in after["scenes"]] == [s["id"] for s in before["scenes"]]
    # 场次字段对应
    assert [(s["title"], s["synopsis"], s["action"], s["transition"])
            for s in after["scenes"]] == [
        (s["title"], s["synopsis"], s["action"], s["transition"])
        for s in before["scenes"]
    ]
    # 台词：顺序、说话人、文本、括号说明对应
    for old, new in zip(before["scenes"], after["scenes"]):
        assert [(line["idx"], line["speaker_name"], line["text"],
                 line["parenthetical"]) for line in new["lines"]] == [
            (line["idx"], line["speaker_name"], line["text"],
             line["parenthetical"]) for line in old["lines"]
        ]
    # 角色名与顺序对应（导入按名匹配，不新增）
    assert [c["name"] for c in after["characters"]] == [
        c["name"] for c in before["characters"]]

    # 结构差异为空（元数据另测）
    parsed = fountain.parse_from_fountain(text)
    assert fountain.diff_tree_vs_parsed(
        before, parsed, include_metadata=False) == []


def test_fountain_import_replace_mode_rebuilds_scenes(client):
    ids = _seed(client)
    pid = ids["pid"]
    before = _export_tree(client, pid)
    text = _export_fountain(client, pid)

    report = _import_fountain(client, pid, text, mode="replace")
    assert report["mode"] == "replace"
    # replace 会重建场次：张力与分镜引用无法保留，报告必须明示
    assert report["metadata_preserved"]["conflicts_dropped"] == 1
    assert any("replace" in w["reason"] for w in report["warnings"])

    after = _export_tree(client, pid)
    assert [s["title"] for s in after["scenes"]] == [
        s["title"] for s in before["scenes"]]
    assert [(line["speaker_name"], line["text"]) for s in after["scenes"]
            for line in s["lines"]] == [
        (line["speaker_name"], line["text"]) for s in before["scenes"]
        for line in s["lines"]]
    # 角色卡（含立场）不在 replace 范围内，仍保留
    assert [c["stance"] for c in after["characters"]] == [
        c["stance"] for c in before["characters"]]


# ---- 1.4 未识别元素报告 ----


EXTENDED_SAMPLE = """Title: 扩展样例

# 第一幕

INT. 洞穴 - 夜

= 场次提要

[[这是一条注释]]

>居中字幕<

~歌词一句

@陈守拙
纸包不住火。

@林务实^
火也会烧掉无辜的人。

CUT TO:

===

/* 废弃段落
第二行 */
"""


def test_unrecognized_elements_are_reported_not_dropped():
    parsed = fountain.parse_from_fountain(EXTENDED_SAMPLE)
    kinds = {item["kind"] for item in parsed["unrecognized"]}
    assert {"section", "note", "centered", "lyrics", "dual_dialogue",
            "page_break", "boneyard"} <= kinds

    # 每条都有定位（行号）与原因，且扩展语法的原文保留在报告里
    for item in parsed["unrecognized"]:
        assert item["reason"]
        assert item["text"]
        assert item["line"] is None or item["line"] >= 1
    assert any("居中字幕" in item["text"] for item in parsed["unrecognized"])
    assert any("废弃段落" in item["text"] for item in parsed["unrecognized"])

    # 可承载的对白仍然完整导入
    assert parsed["scenes"][0]["synopsis"] == "场次提要"
    assert [line["speaker"] for line in parsed["scenes"][0]["lines"]] == [
        "陈守拙", "林务实"]
    assert parsed["scenes"][0]["transition"] == "CUT TO:"


def test_unrecognized_elements_surface_through_import_api(client):
    project = client.post(f"{BASE}/projects", headers=A,
                          json={"title": "扩展导入"}).json()
    report = _import_fountain(client, project["id"], EXTENDED_SAMPLE)

    kinds = {item["kind"] for item in report["unrecognized"]}
    assert {"section", "note", "centered", "lyrics", "dual_dialogue",
            "page_break", "boneyard"} <= kinds
    assert report["unrecognized_summary"]["section"] == 1
    assert report["scenes"]["total"] == 1

    # 对白没有被静默丢弃
    scenes = client.get(f"{BASE}/projects/{project['id']}/scenes", headers=A).json()
    lines = client.get(f"{BASE}/scenes/{scenes[0]['id']}/lines", headers=A).json()
    assert [line["text"] for line in lines] == [
        "纸包不住火。", "火也会烧掉无辜的人。"]


def test_external_fountain_import_maps_action_and_transitions(client):
    project = client.post(f"{BASE}/projects", headers=A,
                          json={"title": "外部剧本"}).json()
    pid = project["id"]
    external = """Title: 外部剧本
Author: 某人

INT. COFFEE SHOP - DAY

Action line.

@ALICE
(whispering)
Hello.

BOB
Hi.

CUT TO:
"""
    report = _import_fountain(client, pid, external)
    assert report["scenes"]["created"][0]["title"] == "INT. COFFEE SHOP - DAY"
    # 标题页 Author 不能落库，但必须被列出
    assert any(item["kind"] == "title_page_field"
               for item in report["unrecognized"])

    scenes = client.get(f"{BASE}/projects/{pid}/scenes", headers=A).json()
    assert scenes[0]["action"] == "Action line."
    assert scenes[0]["transition"] == "CUT TO:"
    lines = client.get(f"{BASE}/scenes/{scenes[0]['id']}/lines", headers=A).json()
    assert [(line["idx"], line["parenthetical"], line["text"]) for line in lines] == [
        (1, "whispering", "Hello."), (2, None, "Hi.")]
    characters = client.get(f"{BASE}/projects/{pid}/characters", headers=A).json()
    assert {c["name"] for c in characters} == {"ALICE", "BOB"}


def test_cjk_external_fountain_character_cues():
    parsed = fountain.parse_from_fountain(
        "INT. 洞穴 - 夜\n\n"
        "陈守拙\n纸包不住火。\n\n"
        "林务实\n火也会烧掉无辜的人。\n"
    )
    assert [line["speaker"] for line in parsed["scenes"][0]["lines"]] == [
        "陈守拙", "林务实"]


def test_import_matches_by_position_when_heading_changed(client):
    """外部工具改写场景标题时，按位置兜底保住场次 id、conflict 与分镜引用。"""
    ids = _seed(client)
    pid = ids["pid"]
    text = _export_fountain(client, pid).replace("第一场：对峙", "第一场：新的标题")

    report = _import_fountain(client, pid, text, mode="merge")
    assert report["scenes"]["updated"][0]["id"] == ids["s1"]["id"]

    after = _export_tree(client, pid)
    assert after["scenes"][0]["id"] == ids["s1"]["id"]
    assert after["scenes"][0]["title"] == "第一场：新的标题"
    assert after["scenes"][0]["conflict"] == ids["s1"]["conflict"]


# ---- 1.5 元数据往返保留 ----


def test_roundtrip_preserves_stance_conflict_and_storyboard(client):
    ids = _seed(client)
    pid = ids["pid"]

    # 分镜引用：pipeline 的 storyboard 以 scene_id 关联
    conn = pipeline.connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO storyboard (scene_id, idx, shot) VALUES (?,?,?)",
                (ids["s1"]["id"], 0,
                 json.dumps({"idx": 0, "visual": "定场"}, ensure_ascii=False)),
            )
    finally:
        conn.close()

    before = _export_tree(client, pid)
    text = _export_fountain(client, pid)
    report = _import_fountain(client, pid, text, mode="merge")

    # 报告显式给出保留数量
    assert report["metadata_preserved"]["stances"] == 2
    assert report["metadata_preserved"]["conflicts"] == 1
    assert report["metadata_preserved"]["storyboard_refs"] == 1
    assert report["characters"]["created"] == []

    after = _export_tree(client, pid)
    # 哲思立场标注 / 角色要素集未被导入覆盖
    assert [c["stance"] for c in after["characters"]] == [
        c["stance"] for c in before["characters"]]
    assert [c["appearance"] for c in after["characters"]] == [
        c["appearance"] for c in before["characters"]]
    # 场次张力标注保留
    assert after["scenes"][0]["conflict"] == before["scenes"][0]["conflict"]
    # 分镜引用仍指向同一场次
    conn = pipeline.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM storyboard WHERE scene_id=?",
            (ids["s1"]["id"],)).fetchone()[0] == 1
    finally:
        conn.close()


# ---- 1.6 FDX 导入导出与有损警告 ----


def test_fdx_export_reports_lossy_fields(client):
    ids = _seed(client)
    body = client.get(f"{BASE}/projects/{ids['pid']}/export/fdx?format=json",
                      headers=A).json()
    assert body["format"] == "fdx"
    warning_fields = {w["field"] for w in body["warnings"]}
    assert {"characters[].appearance", "characters[].stance",
            "scenes[].synopsis", "scenes[].conflict", "*.id"} <= warning_fields

    parsed = fountain.parse_from_fdx(body["text"])
    assert parsed["unrecognized"] == []
    assert parsed["capabilities"] == ["action", "transition", "parenthetical"]

    before = _export_tree(client, ids["pid"])
    diffs = fountain.diff_tree_vs_parsed(before, parsed)
    paths = {diff["path"] for diff in diffs}
    # 往返差异逐项列出
    assert any(path.endswith(".synopsis") for path in paths)
    assert any(path.endswith(".conflict") for path in paths)
    assert any(".stance" in path for path in paths)
    assert any(".appearance" in path for path in paths)
    # 结构字段（场次/动作/台词/转场）无差异
    structural = [diff for diff in diffs
                  if not any(key in diff["path"]
                             for key in ("synopsis", "conflict", "stance", "appearance"))]
    assert structural == []


def test_fdx_import_merges_without_overwriting_metadata(client):
    ids = _seed(client)
    pid = ids["pid"]
    before = _export_tree(client, pid)
    fdx = client.get(f"{BASE}/projects/{pid}/export/fdx?format=json",
                     headers=A).json()["text"]

    r = client.post(f"{BASE}/projects/{pid}/import/fdx", headers=A,
                    json={"text": fdx, "mode": "merge"})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["format"] == "fdx"
    assert report["unrecognized"] == []
    assert report["metadata_preserved"]["stances"] == 2

    after = _export_tree(client, pid)
    # FDX 不承载 synopsis/conflict/stance/appearance，导入必须保留原值
    assert after["scenes"][0]["synopsis"] == before["scenes"][0]["synopsis"]
    assert after["scenes"][0]["conflict"] == before["scenes"][0]["conflict"]
    assert [c["stance"] for c in after["characters"]] == [
        c["stance"] for c in before["characters"]]
    # FDX 承载的结构字段保持一致
    for old, new in zip(before["scenes"], after["scenes"]):
        assert new["title"] == old["title"]
        assert new["action"] == old["action"]
        assert new["transition"] == old["transition"]
        assert [(line["speaker_name"], line["text"], line["parenthetical"])
                for line in new["lines"]] == [
            (line["speaker_name"], line["text"], line["parenthetical"])
            for line in old["lines"]]


def test_fdx_invalid_xml_returns_422(client):
    ids = _seed(client)
    r = client.post(f"{BASE}/projects/{ids['pid']}/import/fdx", headers=A,
                    json={"text": "not xml"})
    assert r.status_code == 422


# ---- 边界与隔离 ----


def test_empty_and_title_only_documents():
    parsed = fountain.parse_from_fountain("")
    assert parsed["scenes"] == []
    assert parsed["unrecognized"] == []

    text = fountain.serialize_to_fountain({
        "project": {"title": "空项目"},
        "characters": [],
        "scenes": [],
    })
    assert text == "Title: 空项目\n"
    parsed = fountain.parse_from_fountain(text)
    assert parsed["title_page"]["title"] == "空项目"
    assert parsed["scenes"] == []


def test_exchange_endpoints_are_user_isolated(client):
    ids = _seed(client)
    pid = ids["pid"]
    for method, path, payload in (
        ("get", f"{BASE}/projects/{pid}/export/fountain", None),
        ("get", f"{BASE}/projects/{pid}/export/fdx", None),
        ("post", f"{BASE}/projects/{pid}/import/fountain", {"text": "Title: x"}),
        ("post", f"{BASE}/projects/{pid}/import/fdx", {"text": "x"}),
    ):
        kwargs = {"headers": B}
        if payload is not None:
            kwargs["json"] = payload
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)


def test_migrate_adds_exchange_columns_to_legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
            logline TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE characters (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, name TEXT NOT NULL,
            appearance TEXT NOT NULL, stance TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE scenes (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, idx INTEGER NOT NULL,
            title TEXT NOT NULL, synopsis TEXT, conflict TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE lines (
            id INTEGER PRIMARY KEY, scene_id INTEGER NOT NULL, idx INTEGER NOT NULL,
            speaker_id INTEGER NOT NULL, text TEXT NOT NULL
        );
        CREATE TABLE versions (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.close()

    monkeypatch.setenv("FD_JUCHUANG_DB", str(path))
    conn = connect()
    try:
        migrate(conn)
        scene_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scenes)")}
        line_cols = {row["name"] for row in conn.execute("PRAGMA table_info(lines)")}
        assert {"action", "transition"} <= scene_cols
        assert "parenthetical" in line_cols
        # 旧数据以空值读取，不报错
        conn.execute(
            "INSERT INTO projects (id, user_id, title, created_at) VALUES (1,'u','旧剧本','t')"
        )
        conn.execute(
            "INSERT INTO scenes (id, project_id, idx, title, created_at)"
            " VALUES (1,1,1,'旧场次','t')"
        )
        row = conn.execute("SELECT * FROM scenes WHERE id=1").fetchone()
        assert row["action"] is None and row["transition"] is None
    finally:
        conn.close()
