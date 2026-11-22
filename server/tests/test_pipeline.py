"""AI 底料管线离线测试（task 4.1/4.2/4.3 + change juchuang-continuity-layer 2.1-2.4）。

全离线：剧本上游经 pipeline.load_scene_bundle 接缝注入夹具（1 场 6 台词 + 2 角色要素集），
LLM 走 monkeypatch（成功/抛错/坏负载/空响应四态），不打真网关。
覆盖：分镜确定性版字段与切镜规则 → 逐镜 PUT 人在环上编辑 → 三形态提示词
schema 校验（jsonschema 手写 schema + 手写字段断言双保险）→ 角色一致性要素集
原文注入 + character_id → 引擎降级路径 → export 下载体。
2.x：扩展字段（staging/keyframe/model sheet/art bible）的生成、旧行空值读取、
镜头表导出、跨镜头锁定要素一致、编辑持久化与再导出。
"""

from __future__ import annotations

import json
import math
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from fd_juchuang_api import pipeline

try:  # 环境有 jsonschema 则做正式 schema 校验；没有时手写断言仍是硬门槛
    import jsonschema

    HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    HAS_JSONSCHEMA = False

# autouse 夹具替换接缝之前，留存真实适配实现（上游契约测试用）
_REAL_LOAD_SCENE_BUNDLE = pipeline.load_scene_bundle


SCENE_ID = 77  # screenplay 的场次/角色 id 是整数 rowid

LINES = [
    {"idx": 1, "speaker": "沈青梧", "text": "把账拿出来。"},
    {"idx": 2, "speaker": "顾行舟", "text": "什么账，我听不懂你在说什么。"},
    {"idx": 3, "speaker": "沈青梧", "text": "十年前腊月，城南当铺，那一张盖了你私章的借据。"},
    {"idx": 4, "speaker": "顾行舟", "text": "事到如今，你我还分得清谁欠谁吗？"},
    {"idx": 5, "speaker": "沈青梧", "text": "分不清，就让它今夜在此分清。"},
    {"idx": 6, "speaker": "顾行舟", "text": "好，那你听我说完这一段旧事。"},
]
CHARACTERS = [
    {"id": 101, "name": "沈青梧",
     "appearance_elements": ["年龄感：三十五岁上下", "体型：清瘦挺拔",
                             "着装：月白改良旗袍、银襟扣", "画风标签：水墨淡彩、低饱和"]},
    {"id": 102, "name": "顾行舟",
     "appearance_elements": ["年龄感：四十岁上下", "体型：宽肩微驼",
                             "着装：深灰长衫、旧怀表链", "画风标签：油画厚涂、暖褐调"]},
]
BUNDLE = {
    "scene": {"id": SCENE_ID, "heading": "内景·老茶馆·夜",
              "synopsis": "两位旧友为十年前的一笔旧账当面对峙"},
    "lines": LINES,
    "characters": CHARACTERS,
}


def _fake_bundle(scene_id: int) -> dict:
    """镜像真实 load_scene_bundle 契约（归一化后的 bundle 形态）：缺场次 → 404。"""
    if scene_id != SCENE_ID:
        raise HTTPException(status_code=404, detail=f"场次 {scene_id} 不存在")
    return json.loads(json.dumps(BUNDLE, ensure_ascii=False))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_JUCHUANG_DB", str(tmp_path / "pipeline_test.db"))
    monkeypatch.delenv("FD_JUCHUANG_LLM_API_KEY", raising=False)  # 默认确定性引擎
    monkeypatch.setattr(pipeline, "load_scene_bundle", _fake_bundle)


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(pipeline.router)  # 集成到 main.py 的同一行挂载方式
    return TestClient(app)


def _make_storyboard(client: TestClient) -> dict:
    resp = client.post(f"/api/v1/pipeline/storyboard/{SCENE_ID}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- 4.1 剧本 → 分镜


def test_storyboard_deterministic(client: TestClient) -> None:
    body = _make_storyboard(client)
    assert body["engine"] == "deterministic"
    shots = body["shots"]
    # 6 句台词 + 开场定场镜 + 结尾镜 = 8 镜
    assert body["shot_count"] == 8 and len(shots) == 8
    fields = {"idx", "shot_size", "visual", "action", "line_text",
              "line_speaker", "duration_sec", "engine",
              "staging", "keyframe", "model_sheet_refs", "art_bible_refs"}
    for shot in shots:
        assert fields <= set(shot), f"缺字段: {fields - set(shot)}"
        assert shot["shot_size"] in pipeline.SHOT_SIZES
        assert shot["duration_sec"] > 0 and shot["visual"] and shot["action"]
        assert shot["engine"] == "deterministic"
    # 开场定场镜 / 结尾镜
    assert shots[0]["idx"] == 0 and shots[0]["shot_size"] == "全景"
    assert shots[0]["line_text"] is None and shots[0]["line_speaker"] is None
    assert shots[7]["idx"] == 7 and shots[7]["line_text"] is None
    # 每句一镜：文本/说话人对位，景别按句长轮换，时长按字数
    for pos, line in enumerate(LINES, start=1):
        shot = shots[pos]
        assert shot["idx"] == pos
        assert shot["line_text"] == line["text"]
        assert shot["line_speaker"] == line["speaker"]
        assert shot["shot_size"] == pipeline.SHOT_SIZES[len(line["text"]) % 4]
        assert shot["duration_sec"] == max(2, math.ceil(len(line["text"]) / 3))
    # GET 镜头表（同一场）
    resp = client.get(f"/api/v1/pipeline/storyboard/{SCENE_ID}")
    assert resp.status_code == 200 and resp.json()["shot_count"] == 8


def test_storyboard_scene_missing(client: TestClient) -> None:
    assert client.post("/api/v1/pipeline/storyboard/99999").status_code == 404
    assert client.get("/api/v1/pipeline/storyboard/99999").status_code == 404


def test_put_shot_edit(client: TestClient) -> None:
    _make_storyboard(client)
    resp = client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/3",
                      json={"visual": "茶馆一角，烛火压低了半个画面"})
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["idx"] == 3 and updated["scene_id"] == SCENE_ID
    assert updated["shot"]["visual"] == "茶馆一角，烛火压低了半个画面"
    assert updated["shot"]["line_text"] == LINES[2]["text"]  # 未提交字段不动
    # 改一格生效，其余镜头不受影响（GET 列表按 idx 排序，idx=3 即第 4 行）
    rows = client.get(f"/api/v1/pipeline/storyboard/{SCENE_ID}").json()["shots"]
    assert rows[3]["visual"] == "茶馆一角，烛火压低了半个画面"
    assert rows[1]["visual"] != "茶馆一角，烛火压低了半个画面"
    # 景别编辑同样生效
    resp = client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/3",
                      json={"shot_size": "特写", "duration_sec": 7})
    assert resp.status_code == 200
    assert resp.json()["shot"]["shot_size"] == "特写"
    assert resp.json()["shot"]["duration_sec"] == 7
    # 边界：不存在的 idx → 404；非法景别 → 422；空 patch → 400；未知字段 → 422
    assert client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/99",
                      json={"visual": "x"}).status_code == 404
    assert client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/1",
                      json={"shot_size": "大远景"}).status_code == 422
    assert client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/1",
                      json={}).status_code == 400
    assert client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/1",
                      json={"visualize": "x"}).status_code == 422


# ---------------------------------------------------------------- 4.2/4.3 分镜 → 三形态提示词


IMAGE_SCHEMA = {
    "type": "object",
    "required": ["form", "schema_version", "storyboard_id", "style_anchor", "shots"],
    "properties": {
        "form": {"const": "image"},
        "schema_version": {"const": "1"},
        "storyboard_id": {"type": "integer"},
        "style_anchor": {"type": "string", "minLength": 1},
        "shots": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "required": ["idx", "shot_size", "prompt", "character_id",
                         "staging", "keyframe", "model_sheet_refs", "art_bible_refs",
                         "model_sheets", "art_bible"],
            "properties": {
                "idx": {"type": "integer"},
                "shot_size": {"enum": ["全景", "中景", "近景", "特写"]},
                "prompt": {"type": "string", "minLength": 1},
                "character_id": {"type": ["integer", "null"]},
                "character_name": {"type": ["string", "null"]},
                "character_elements": {"type": "array", "items": {"type": "string"}},
                "staging": {"type": ["string", "null"]},
                "keyframe": {"type": ["string", "null"]},
                "model_sheet_refs": {"type": "array", "items": {"type": "string"}},
                "art_bible_refs": {"type": "array", "items": {"type": "string"}},
                "model_sheets": {"type": "array", "items": {
                    "type": "object",
                    "required": ["ref", "id", "name", "elements"],
                    "properties": {
                        "ref": {"type": "string"},
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "elements": {"type": "array", "items": {"type": "string"}},
                    }}},
                "art_bible": {"type": "array", "items": {
                    "type": "object",
                    "required": ["ref", "kind", "name", "elements"],
                    "properties": {
                        "ref": {"type": "string"},
                        "kind": {"enum": ["scene", "prop"]},
                        "name": {"type": "string"},
                        "elements": {"type": "array", "items": {"type": "string"}},
                    }}},
            }}},
    },
}
VIDEO_SCHEMA = {
    "type": "object",
    "required": ["form", "schema_version", "storyboard_id", "shots"],
    "properties": {
        "form": {"const": "video"},
        "schema_version": {"const": "1"},
        "storyboard_id": {"type": "integer"},
        "shots": {"type": "array", "minItems": 1, "items": {
            "type": "object",
            "required": ["idx", "shot", "duration_sec", "camera_move", "character_id",
                         "staging", "keyframe", "model_sheet_refs", "art_bible_refs",
                         "model_sheets", "art_bible"],
            "properties": {
                "idx": {"type": "integer"},
                "shot": {"type": "string", "minLength": 1},
                "duration_sec": {"type": "number", "exclusiveMinimum": 0},
                "camera_move": {"type": "string", "minLength": 1},
                "character_id": {"type": ["integer", "null"]},
                "staging": {"type": ["string", "null"]},
                "keyframe": {"type": ["string", "null"]},
                "model_sheet_refs": {"type": "array", "items": {"type": "string"}},
                "art_bible_refs": {"type": "array", "items": {"type": "string"}},
                "model_sheets": {"type": "array"},
                "art_bible": {"type": "array"},
            }}},
    },
}
NARRATIVE_SCHEMA = {
    "type": "object",
    "required": ["form", "schema_version", "storyboard_id", "text"],
    "properties": {
        "form": {"const": "narrative"},
        "schema_version": {"const": "1"},
        "storyboard_id": {"type": "integer"},
        "text": {"type": "string", "minLength": 1},
        "word_count": {"type": "integer"},
    },
}


def _make_promptsets(client: TestClient) -> dict[int, dict]:
    resp = client.post(f"/api/v1/pipeline/prompts/{SCENE_ID}")
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["schema_version"] == "1"
    # change juchuang-image-iteration：新增第四形态 `generation`（可执行生成计划）。
    # 断言从「恰好三形态」改为「三种提示词形态必须在 + generation 必须在」——
    # 前者会在每次加形态时误报，后者才是在保护「三形态不丢」这个行为。
    forms = {p["form"] for p in created["promptsets"]}
    assert {"image", "video", "narrative", "generation"} <= forms
    out = {}
    for p in created["promptsets"]:
        got = client.get(f"/api/v1/pipeline/prompts/{p['id']}")
        assert got.status_code == 200, got.text
        row = got.json()
        assert row["form"] == p["form"] and row["schema_version"] == "1"
        out[p["form"]] = row
    return out


def test_prompts_three_forms_schema(client: TestClient) -> None:
    # 未转分镜先导提示词 → 404
    assert client.post(f"/api/v1/pipeline/prompts/{SCENE_ID}").status_code == 404
    _make_storyboard(client)
    sets = _make_promptsets(client)

    image, video, narrative = sets["image"]["payload"], sets["video"]["payload"], sets["narrative"]["payload"]
    if HAS_JSONSCHEMA:
        jsonschema.validate(image, IMAGE_SCHEMA)
        jsonschema.validate(video, VIDEO_SCHEMA)
        jsonschema.validate(narrative, NARRATIVE_SCHEMA)

    # 手写字段断言（schema 校验之外的第二道门槛）
    assert image["form"] == "image" and image["style_anchor"]
    assert len(image["shots"]) == 8
    assert all(s["prompt"] for s in image["shots"])
    assert video["form"] == "video" and len(video["shots"]) == 8
    for s in video["shots"]:
        assert s["shot"] and s["camera_move"] and s["duration_sec"] > 0
    assert narrative["form"] == "narrative" and narrative["text"]
    assert narrative["word_count"] == len(narrative["text"])
    # 旁白覆盖整场：场刊标题与六句台词全部在场
    for needle in [BUNDLE["scene"]["heading"]] + [line["text"] for line in LINES]:
        assert needle in narrative["text"]


def test_character_consistency_injection(client: TestClient) -> None:
    _make_storyboard(client)
    sets = _make_promptsets(client)
    image = sets["image"]["payload"]
    video = sets["video"]["payload"]

    by_pos = {pos: line for pos, line in enumerate(LINES, start=1)}
    prompts_of: dict[int, list[str]] = {101: [], 102: []}
    for pos, line in by_pos.items():
        shot = image["shots"][pos]
        char = next(c for c in CHARACTERS if c["name"] == line["speaker"])
        # character_id 字段 + 要素集原文逐条注入
        assert shot["character_id"] == char["id"]
        assert shot["character_name"] == char["name"]
        for element in char["appearance_elements"]:
            assert element in shot["prompt"], f"要素集原文缺失: {element}"
        assert shot["prompt"].startswith(pipeline.STYLE_ANCHOR)  # 风格锚点
        prompts_of[char["id"]].append(shot["prompt"])
        # video 形态同样注入（spec：各镜头提示词注入同一要素集）
        vshot = video["shots"][pos]
        assert vshot["character_id"] == char["id"]
        assert all(e in vshot["shot"] for e in char["appearance_elements"])
    # 定场/收尾镜无角色 → character_id 为空且不注入任何要素
    for shot in (image["shots"][0], image["shots"][7]):
        assert shot["character_id"] is None and shot["character_elements"] == []
    # 跨镜头一致性：同一角色的注入段（要素锁定部分）逐字相同
    for cid, prompts in prompts_of.items():
        char = next(c for c in CHARACTERS if c["id"] == cid)
        lock = f"人物锁定——{char['name']}：{'；'.join(char['appearance_elements'])}"
        for prompt in prompts:
            assert lock in prompt


def test_edit_flows_downstream(client: TestClient) -> None:
    """人在环上：分镜编辑后的画面进入下一级提示词。"""
    _make_storyboard(client)
    marker = "编辑后的画面：柜台上一盏将尽的油灯"
    assert client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/2",
                      json={"visual": marker}).status_code == 200
    sets = _make_promptsets(client)
    image = sets["image"]["payload"]
    assert marker in image["shots"][2]["prompt"]
    assert marker in sets["video"]["payload"]["shots"][2]["shot"]


# ---------------------------------------------------------------- 2.1 分镜字段扩展 / 旧数据

LEGACY_SHOT = {
    "idx": 1,
    "shot_size": "中景",
    "visual": "旧版画面",
    "action": "旧版动作",
    "line_text": "旧版台词",
    "line_speaker": "沈青梧",
    "duration_sec": 4,
    "engine": "deterministic",
}


def test_legacy_shot_reads_defaults_and_migrates(client: TestClient) -> None:
    """旧行（无扩展字段）读取不报错：staging/keyframe 为 null、引用为 []。"""
    _make_storyboard(client)
    # 绕过生成与迁移，直接落一行旧 schema，再用原始连接读取（只靠读取侧归一）
    raw_conn = sqlite3.connect(pipeline._db_path())
    raw_conn.row_factory = sqlite3.Row
    try:
        with raw_conn:
            raw_conn.execute(
                "UPDATE storyboard SET shot=? WHERE scene_id=? AND idx=1",
                (json.dumps(LEGACY_SHOT, ensure_ascii=False), SCENE_ID))
        rows = pipeline._load_storyboard(raw_conn, SCENE_ID)
    finally:
        raw_conn.close()
    shot = rows[1]["shot"]
    assert shot["staging"] is None and shot["keyframe"] is None
    assert shot["model_sheet_refs"] == [] and shot["art_bible_refs"] == []
    # 旧字段原样保留
    assert shot["line_text"] == LEGACY_SHOT["line_text"]
    assert shot["visual"] == LEGACY_SHOT["visual"]

    # 端点读取同样不报错，且返回补齐后的默认值
    got = client.get(f"/api/v1/pipeline/storyboard/{SCENE_ID}").json()["shots"]
    assert got[1]["staging"] is None
    assert got[1]["model_sheet_refs"] == [] and got[1]["art_bible_refs"] == []

    # connect() 的幂等迁移把扩展字段持久化进 shot JSON
    conn = pipeline.connect()
    try:
        stored = json.loads(conn.execute(
            "SELECT shot FROM storyboard WHERE scene_id=? AND idx=1",
            (SCENE_ID,)).fetchone()["shot"])
    finally:
        conn.close()
    for key in ("staging", "keyframe", "model_sheet_refs", "art_bible_refs"):
        assert key in stored
    assert stored["staging"] is None and stored["art_bible_refs"] == []


def test_normalize_shot_handles_dirty_rows() -> None:
    assert pipeline._normalize_shot(None, 4)["idx"] == 4
    shot = pipeline._normalize_shot(
        {"idx": "2", "staging": "  ", "keyframe": 3,
         "model_sheet_refs": "101", "art_bible_refs": [None, " 77 ", 77]}, 9)
    assert shot["idx"] == 9  # 非法 idx 用行 idx 兜底
    assert shot["staging"] is None and shot["keyframe"] is None
    assert shot["model_sheet_refs"] == ["101"]
    assert shot["art_bible_refs"] == ["77"]  # 字符串化 + 去重


# ---------------------------------------------------------------- 2.2 生成与导出含全部字段


def test_extended_fields_generated_and_exported(client: TestClient) -> None:
    body = _make_storyboard(client)
    required = {"staging", "keyframe", "model_sheet_refs", "art_bible_refs"}
    for shot in body["shots"]:
        assert required <= set(shot), f"缺字段: {required - set(shot)}"
        assert shot["staging"].startswith("俯视图：")
        assert shot["keyframe"].startswith("关键帧：")
        assert shot["art_bible_refs"] == [str(SCENE_ID)]
    # 有台词镜引用说话人角色；定场/收尾镜无角色引用
    for pos, line in enumerate(LINES, start=1):
        char = next(c for c in CHARACTERS if c["name"] == line["speaker"])
        assert body["shots"][pos]["model_sheet_refs"] == [str(char["id"])]
    assert body["shots"][0]["model_sheet_refs"] == []
    assert body["shots"][-1]["model_sheet_refs"] == []

    # 镜头表 JSON 导出：全部字段随行导出，不丢弃
    resp = client.get(f"/api/v1/pipeline/storyboard/{SCENE_ID}/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert f'filename="storyboard-{SCENE_ID}.json"' in resp.headers["content-disposition"]
    exported = json.loads(resp.text)
    assert exported["shot_count"] == 8 and exported["storyboard_id"] == SCENE_ID
    assert exported["shots"] == body["shots"]
    for shot in exported["shots"]:
        assert required <= set(shot)
    # 未生成镜头表 → 导出 404
    assert client.get("/api/v1/pipeline/storyboard/424242/export").status_code == 404

    # 三形态提示词中 image/video 每镜同样携带扩展字段
    sets = _make_promptsets(client)
    for form in ("image", "video"):
        for shot in sets[form]["payload"]["shots"]:
            assert required <= set(shot)
            assert shot["staging"] and shot["keyframe"]
            assert shot["model_sheet_refs"] is not None
            assert shot["art_bible"]


# ---------------------------------------------------------------- 2.3 提示词注入锁定要素


def test_model_sheet_and_art_bible_consistency(client: TestClient) -> None:
    """同一角色多镜头注入同一组外观要素；场景 art bible 跨镜一致。"""
    _make_storyboard(client)
    sets = _make_promptsets(client)
    image, video = sets["image"]["payload"], sets["video"]["payload"]

    variants: dict[int, set[tuple[str, ...]]] = {}
    for pos, line in enumerate(LINES, start=1):
        char = next(c for c in CHARACTERS if c["name"] == line["speaker"])
        lock = f"人物锁定——{char['name']}：{'；'.join(char['appearance_elements'])}"
        for shot, text_key in ((image["shots"][pos], "prompt"),
                               (video["shots"][pos], "shot")):
            assert shot["model_sheet_refs"] == [str(char["id"])]
            assert [m["id"] for m in shot["model_sheets"]] == [char["id"]]
            assert shot["model_sheets"][0]["elements"] == char["appearance_elements"]
            assert lock in shot[text_key]
            variants.setdefault(char["id"], set()).add(
                tuple(shot["model_sheets"][0]["elements"]))
        variants.setdefault(char["id"], set()).add(tuple(char["appearance_elements"]))
    # 同一角色跨镜头只有一种外观要素组合（逐字一致）
    for cid, combos in variants.items():
        assert len(combos) == 1, f"角色 {cid} 要素集不一致: {combos}"

    # 场景 art bible：每镜同一组要素，并注入提示词
    scene_bible = image["shots"][0]["art_bible"]
    assert scene_bible[0]["kind"] == "scene" and scene_bible[0]["ref"] == str(SCENE_ID)
    assert scene_bible[0]["elements"][0] == f"环境：{BUNDLE['scene']['heading']}"
    for shot in image["shots"]:
        assert shot["art_bible"] == scene_bible
        assert f"art_bible〔场景 {SCENE_ID}·{BUNDLE['scene']['heading']}〕锁定：" in shot["prompt"]
    for shot in video["shots"]:
        assert shot["art_bible"] == scene_bible
        assert f"art_bible〔场景 {SCENE_ID}·{BUNDLE['scene']['heading']}〕锁定：" in shot["shot"]


def test_extra_character_and_prop_refs_injected() -> None:
    """引用多个角色/道具：非主角色追加 model sheet 段，道具注入 art bible 段。"""
    bundle = pipeline._normalize_bundle({
        "scene": {"id": SCENE_ID, "heading": "内景·老茶馆·夜", "synopsis": "对峙"},
        "lines": [],
        "characters": [
            {"id": 101, "name": "沈青梧",
             "appearance": {"年龄感": "三十五岁上下", "着装": "月白旗袍"}},
            {"id": 102, "name": "顾行舟",
             "appearance": {"年龄感": "四十岁上下", "体型": "宽肩微驼"}},
        ],
        "props": [{"id": "p1", "name": "借据",
                   "art_bible": {"纸张": "泛黄毛边纸", "字迹": "墨色偏褐"}}],
    }, SCENE_ID)
    shot = {
        "idx": 1, "shot_size": "中景", "visual": "两人对峙", "action": "递出借据",
        "line_text": "把账拿出来。", "line_speaker": "沈青梧", "duration_sec": 3,
        "staging": "俯视图：二人对坐", "keyframe": "关键帧：借据入画",
        "model_sheet_refs": ["101", "102"],
        "art_bible_refs": [str(SCENE_ID), "p1"],
    }
    payload = pipeline._build_image_payload(SCENE_ID, bundle, [shot])
    got = payload["shots"][0]
    assert [m["id"] for m in got["model_sheets"]] == [101, 102]
    # 主角色（101）与人物锁定段同要素 → 不重复；102 追加 model sheet 锁定段
    assert got["prompt"].count("人物锁定——沈青梧") == 1
    assert ("model_sheet〔102·顾行舟〕锁定："
            "年龄感：四十岁上下；体型：宽肩微驼") in got["prompt"]
    assert [(a["kind"], a["ref"]) for a in got["art_bible"]] == [
        ("scene", str(SCENE_ID)), ("prop", "p1")]
    assert ("art_bible〔道具 p1·借据〕锁定："
            "纸张：泛黄毛边纸；字迹：墨色偏褐") in got["prompt"]
    # 未知引用不报错：只剩场景 art bible 兜底
    fallback = pipeline._build_image_payload(SCENE_ID, bundle, [{
        **shot, "art_bible_refs": ["no-such-prop"]}])["shots"][0]
    assert [a["ref"] for a in fallback["art_bible"]] == [str(SCENE_ID)]


def test_bundle_normalizes_art_bible_and_model_sheet() -> None:
    bundle = pipeline._normalize_bundle({
        "scene": {"id": 5, "heading": "外景·渡口", "synopsis": "雾起", "action": "船靠岸",
                  "art_bible": {"色调": "冷灰", "光位": "左上逆光"}},
        "characters": [{"id": 1, "name": "林远",
                        "appearance": {"年龄感": "中年", "着装": "旧呢大衣"},
                        "model_sheet": {"正面": "国字脸", "服装": "旧呢大衣"}}],
        "props": [{"id": "p1", "name": "旧船桨",
                   "art_bible": {"材质": "榆木", "磨损": "桨柄开裂"}}],
    }, 5)
    assert bundle["scene"]["art_bible_elements"] == ["色调：冷灰", "光位：左上逆光"]
    assert bundle["characters"][0]["model_sheet_elements"] == [
        "正面：国字脸", "服装：旧呢大衣"]
    assert bundle["props"] == [{
        "id": "p1", "name": "旧船桨",
        "elements": ["材质：榆木", "磨损：桨柄开裂"]}]


# ---------------------------------------------------------------- 2.4 扩展字段编辑 / 持久化 / 再导出


def test_edit_extended_fields_persist_and_reexport(client: TestClient) -> None:
    _make_storyboard(client)
    patch = {
        "staging": "俯视图：长桌两端对坐，机位在桌尾俯拍",
        "keyframe": "关键帧：烛火压暗半张脸，手指按住借据一角",
        "model_sheet_refs": ["101", "102"],
        "art_bible_refs": [str(SCENE_ID), "prop-lamp"],
    }
    resp = client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/2", json=patch)
    assert resp.status_code == 200, resp.text
    shot = resp.json()["shot"]
    for key, value in patch.items():
        assert shot[key] == value

    # GET 持久化
    rows = client.get(f"/api/v1/pipeline/storyboard/{SCENE_ID}").json()["shots"]
    assert rows[2]["staging"] == patch["staging"]
    assert rows[2]["keyframe"] == patch["keyframe"]
    assert rows[2]["model_sheet_refs"] == ["101", "102"]
    assert rows[2]["art_bible_refs"] == [str(SCENE_ID), "prop-lamp"]

    # 镜头表再次导出含编辑值
    exported = json.loads(client.get(
        f"/api/v1/pipeline/storyboard/{SCENE_ID}/export").text)
    assert exported["shots"][2]["staging"] == patch["staging"]
    assert exported["shots"][2]["art_bible_refs"] == [str(SCENE_ID), "prop-lamp"]

    # 再用编辑后的分镜生成提示词：走位/关键帧与额外角色引用进入提示词
    sets = _make_promptsets(client)
    image_shot = sets["image"]["payload"]["shots"][2]
    assert patch["staging"] in image_shot["prompt"]
    assert patch["keyframe"] in image_shot["prompt"]
    assert [m["id"] for m in image_shot["model_sheets"]] == [101, 102]
    # idx=2 的说话人是顾行舟（102）：主角色段之外追加沈青梧（101）的 model sheet
    assert "model_sheet〔101·沈青梧〕" in image_shot["prompt"]
    assert patch["staging"] in sets["video"]["payload"]["shots"][2]["shot"]

    # 清空：空串/None/空列表都归一为“未设置”
    resp = client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/2", json={
        "staging": "", "keyframe": None,
        "model_sheet_refs": [], "art_bible_refs": [],
    })
    assert resp.status_code == 200, resp.text
    shot = resp.json()["shot"]
    assert shot["staging"] is None and shot["keyframe"] is None
    assert shot["model_sheet_refs"] == [] and shot["art_bible_refs"] == []

    # 只提交扩展字段也是有效编辑
    resp = client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/2",
                      json={"staging": "只改走位"})
    assert resp.status_code == 200
    assert resp.json()["shot"]["staging"] == "只改走位"

    # 非法引用类型 / 未知字段 → 422
    assert client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/2",
                      json={"model_sheet_refs": 101}).status_code == 422
    assert client.put(f"/api/v1/pipeline/storyboard/{SCENE_ID}/2",
                      json={"stagingx": "x"}).status_code == 422


# ---------------------------------------------------------------- 引擎降级 / LLM 润色


@pytest.mark.parametrize("mode", ["raise", "garbage", "none"])
def test_engine_fallback(client: TestClient, monkeypatch, mode: str) -> None:
    """KEY 在但网关抛错/坏负载/空响应 → 回退确定性版并标 engine。"""
    monkeypatch.setattr(pipeline.llm, "is_configured", lambda: True)

    def fake_chat(*_a, **_k):
        if mode == "raise":
            raise pipeline.llm.LLMError("网关超时（测试注入）")
        if mode == "garbage":
            return "抱歉，这不是 JSON"
        return None

    monkeypatch.setattr(pipeline.llm, "chat", fake_chat)
    body = _make_storyboard(client)
    assert body["engine"] == "deterministic"
    assert all(s["engine"] == "deterministic" for s in body["shots"])
    for shot in body["shots"]:  # 降级版字段完整可用
        assert shot["visual"] and shot["action"]


def test_engine_llm_polish(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(pipeline.llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        pipeline.llm, "chat",
        lambda *a, **k: json.dumps(
            [{"idx": 1, "visual": "LLM 润色画面", "action": "LLM 润色动作"}],
            ensure_ascii=False),
    )
    body = _make_storyboard(client)
    assert body["engine"] == "llm"
    assert body["shots"][1]["visual"] == "LLM 润色画面"
    assert body["shots"][1]["action"] == "LLM 润色动作"
    # 景别/台词/时长仍由确定性层锁定（LLM 只润色 visual/action）
    assert body["shots"][1]["shot_size"] == pipeline.SHOT_SIZES[len(LINES[0]["text"]) % 4]
    assert body["shots"][1]["line_text"] == LINES[0]["text"]
    assert body["shots"][2]["visual"].startswith("内景·老茶馆·夜")  # 未润色镜保持原样


# ---------------------------------------------------------------- export 下载体


def test_export_download(client: TestClient) -> None:
    _make_storyboard(client)
    sets = _make_promptsets(client)
    for form, row in sets.items():
        resp = client.get(f"/api/v1/pipeline/prompts/{row['id']}/export")
        assert resp.status_code == 200
        disp = resp.headers["content-disposition"]
        assert disp.startswith("attachment;")
        assert f'filename="promptset-{row["id"]}-{form}.json"' in disp
        assert resp.headers["content-type"].startswith("application/json")
        assert json.loads(resp.text) == row["payload"]  # 纯 payload 本体
    # form 参数与实际形态不符 → 409；非法 form → 422；不存在 → 404
    image_id = sets["image"]["id"]
    assert client.get(f"/api/v1/pipeline/prompts/{image_id}/export?form=video").status_code == 409
    assert client.get(f"/api/v1/pipeline/prompts/{image_id}/export?form=other").status_code == 422
    assert client.get("/api/v1/pipeline/prompts/424242").status_code == 404


# ---------------------------------------------------------------- 上游适配（真实 screenplay 契约）


def test_load_scene_bundle_against_real_screenplay(monkeypatch) -> None:
    """适配层对真实 screenplay 五表：直插共库 → 真实 load_scene_bundle 组装正确。"""
    from fd_juchuang_api import screenplay

    monkeypatch.setattr(pipeline, "load_scene_bundle", _REAL_LOAD_SCENE_BUNDLE)
    conn = screenplay.connect()
    try:
        screenplay.migrate(conn)
        pid, cid, sid = 1, 1, 1
        conn.execute(
            "INSERT INTO projects (id, user_id, title, logline, created_at)"
            " VALUES (?,?,?,?,?)", (pid, "u1", "旧账", None, "2026-01-01"))
        conn.execute(
            "INSERT INTO characters (id, project_id, name, appearance, stance,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (cid, pid, "沈青梧",
             json.dumps({"年龄感": "三十五岁上下", "着装": "月白旗袍"}, ensure_ascii=False),
             None, "2026-01-01"))
        conn.execute(
            "INSERT INTO scenes (id, project_id, idx, title, synopsis, conflict,"
            " created_at) VALUES (?,?,?,?,?,?,?)",
            (sid, pid, 1, "内景·老茶馆·夜", "对峙", None, "2026-01-01"))
        conn.execute(
            "INSERT INTO lines (id, scene_id, idx, speaker_id, text)"
            " VALUES (?,?,?,?,?)", (1, sid, 1, cid, "把账拿出来。"))
        conn.commit()
    finally:
        conn.close()

    bundle = _REAL_LOAD_SCENE_BUNDLE(sid)
    assert bundle["scene"]["id"] == sid
    assert bundle["scene"]["heading"] == "内景·老茶馆·夜"
    assert bundle["scene"]["synopsis"] == "对峙"
    assert bundle["lines"] == [{"idx": 1, "speaker": "沈青梧", "text": "把账拿出来。"}]
    assert bundle["characters"] == [{
        "id": cid, "name": "沈青梧",
        "appearance_elements": ["年龄感：三十五岁上下", "着装：月白旗袍"],
        # model sheet 未显式给出 → 与角色要素集同源（task 2.1/2.3）
        "model_sheet_elements": ["年龄感：三十五岁上下", "着装：月白旗袍"]}]
    with pytest.raises(HTTPException) as err:
        _REAL_LOAD_SCENE_BUNDLE(424242)
    assert err.value.status_code == 404
