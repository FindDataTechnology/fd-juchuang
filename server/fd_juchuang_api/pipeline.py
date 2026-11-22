"""AI 底料管线（task 4.1/4.2/4.3）：剧本 → 分镜 → 三形态提示词。

三级结构化产物（design D3），LLM 只做级间转换、人在环上可逐级修改：
  剧本（screenplay 模块的场次/台词/角色要素集）
    → 分镜 storyboard（镜头表：每镜一行，确定性引擎必做，LLM 仅润色 visual/action）
    → 提示词 promptset（image / video / narrative 三形态，schema_version 版本化）

- 确定性底座：无 FD_JUCHUANG_LLM_API_KEY 时全流程可用（按台词行切镜，句长定景别，
  字数定时长）；LLM 为可选增强，任何失败回退确定性版并标 engine。
- 角色一致性（design D4）：含某角色的镜头，image/video 提示词自动注入该角色
  appearance 要素集原文 + character_id，跨镜头同角色要素集严格一致。
- 分镜字段扩展（change juchuang-continuity-layer task 2.1-2.3）：镜头行除景别/
  画面/动作/台词/时长外，还含 staging（走位/俯视图）、keyframe（关键帧描述）、
  model_sheet_refs（角色 model sheet 引用）、art_bible_refs（场景/道具 art bible
  引用）。四个字段都落在 shot JSON 内，旧行缺键时按 None / [] 读取；迁移只补键，
  不改既有字段。提示词对引用资产注入锁定要素：同一角色/场景跨镜头注入原文一致。

存储：共库 FD_JUCHUANG_DB（默认 juchuang.db），本模块自建两张表：
  storyboard(id, scene_id INTEGER, idx, shot JSON, created_at)
  promptset(id, storyboard_id INTEGER, form, payload JSON, schema_version, created_at)
「镜头表」无独立实体：一场戏的当前分镜 = storyboard 中该 scene_id 的全部行，
因此 storyboard_id 即 scene_id（POST 重转分镜 = 整场替换，人工编辑走 PUT）。
场次/角色 id 沿用 screenplay 的整数 rowid（其 0001 迁移已注明与本模块对齐），
幂等 DDL（CREATE … IF NOT EXISTS）同为两模块共库约定，不碰 user_version。

上游集成（剧本数据经 screenplay 的读函数，task 2.1/2.2 交付）：
load_scene_bundle 用 screenplay.connect/_project_tree 组装
场次（title/synopsis）+ 台词（idx/speaker_name/text）+ 角色要素集（appearance dict）。
并行开发期 screenplay 未落地时返回 503；测试/联调可替换 load_scene_bundle 接缝。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import ledger, llm

router = APIRouter(prefix="/api/v1/pipeline", tags=["pipeline"])

# ---------------------------------------------------------------- 常量

SHOT_SIZES = ["全景", "中景", "近景", "特写"]
CAMERA_MOVES = ["固定机位", "缓慢跟拍", "手持轻晃", "正反打"]
STYLE_ANCHOR = "电影感写实画风，柔和自然光影，35mm 胶片质感，构图克制"
PROMPT_SCHEMA_VERSION = "1"
# 第四形态：`generation` 是可执行的图片生成计划，与三种提示词并列
# （change juchuang-image-iteration）。它是「怎么生成」而不是「说什么」，
# 所以必须有幂等键与模式，让重跑不重复计费。
PROMPT_FORMS = ("image", "video", "narrative", "generation")
HINT_FORMS = ("image", "video", "narrative")  # 四种里只有这三种是提示词文本
# 中文台词语速约 3 字/秒；定场/收尾镜的默认时长（秒）
CHARS_PER_SEC = 3
ESTABLISH_SEC = 4
ENDING_SEC = 3

# 分镜镜头的扩展字段（旧行缺键时按 None / [] 读取，见 _normalize_shot）
SHOT_TEXT_FIELDS = ("staging", "keyframe")
SHOT_REF_FIELDS = ("model_sheet_refs", "art_bible_refs")


# ---------------------------------------------------------------- 存储（共库自建表）


def _db_path() -> str:
    return os.environ.get("FD_JUCHUANG_DB", "juchuang.db")


def connect() -> sqlite3.Connection:
    path = _db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS storyboard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id INTEGER NOT NULL,
            idx INTEGER NOT NULL,
            shot TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (scene_id, idx)
        );
        -- change juchuang-image-iteration：镜级图位。一镜一张「当前」图，
        -- 重生成 = 覆盖；历史留在 VAAS 侧（父子血缘），这里只存当前 + 幂等键。
        CREATE TABLE IF NOT EXISTS shot_image (
            scene_id INTEGER NOT NULL,
            shot_idx INTEGER NOT NULL,
            asset_id TEXT NOT NULL,
            slug TEXT,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (scene_id, shot_idx)
        );
        CREATE TABLE IF NOT EXISTS promptset (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storyboard_id INTEGER NOT NULL,
            form TEXT NOT NULL,
            payload TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (storyboard_id, form)
        );
        """
    )
    _upgrade_shot_schema(conn)
    conn.commit()


# ---------------------------------------------------------------- 分镜 JSON 字段迁移


def _norm_text(value: Any) -> str | None:
    """空串/空白/非字符串一律归一为 None（“未设置”的统一表示）。"""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _norm_refs(raw: Any) -> list[str]:
    """引用列表归一：字符串化、去空白、去重且保持顺序；非列表值视为空。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        if item is None:
            continue
        value = str(item).strip()
        if value and value not in out:
            out.append(value)
    return out


def _normalize_shot(raw: Any, idx: int) -> dict:
    """旧行/脏行归一：补齐扩展字段并修正类型，不改动既有字段。

    旧版 storyboard 行只有 idx/shot_size/visual/action/line_text/line_speaker/
    duration_sec/engine；缺失的 staging/keyframe 读作 None，引用列表读作 []。
    """
    shot = dict(raw) if isinstance(raw, dict) else {}
    if not isinstance(shot.get("idx"), int):
        shot["idx"] = idx
    for key in SHOT_TEXT_FIELDS:
        shot[key] = _norm_text(shot.get(key))
    for key in SHOT_REF_FIELDS:
        shot[key] = _norm_refs(shot.get(key))
    return shot


def _upgrade_shot_schema(conn: sqlite3.Connection) -> None:
    """幂等迁移：给既有镜头行补齐扩展字段（只补键，不改内容；可无限重放）。"""
    changed: list[tuple[str, int]] = []
    for row in conn.execute("SELECT id, idx, shot FROM storyboard").fetchall():
        try:
            raw = json.loads(row["shot"])
        except (json.JSONDecodeError, TypeError):
            continue  # 坏行不阻塞迁移，读取侧 _normalize_shot 仍会兜底
        shot = _normalize_shot(raw, row["idx"])
        if shot != raw:
            changed.append((json.dumps(shot, ensure_ascii=False), row["id"]))
    if changed:
        conn.executemany("UPDATE storyboard SET shot = ? WHERE id = ?", changed)


def _row_shot(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "scene_id": row["scene_id"], "idx": row["idx"],
            "shot": _normalize_shot(json.loads(row["shot"]), row["idx"]),
            "created_at": row["created_at"]}


def _load_storyboard(conn: sqlite3.Connection, storyboard_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM storyboard WHERE scene_id = ? ORDER BY idx", (storyboard_id,)
    ).fetchall()
    return [_row_shot(r) for r in rows]


# ---------------------------------------------------------------- 上游剧本读取（接缝）


def _norm_elements(raw: Any) -> list[str]:
    """要素集归一为字符串列表，保留原文（角色一致性的注入单位）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        return [f"{k}：{v}" for k, v in raw.items()]
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                out.extend(f"{k}：{v}" for k, v in item.items())
            else:
                out.append(str(item))
        return out
    return [str(raw)]


def _pick(mapping: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _scene_art_bible_elements(scene: dict, heading: str, synopsis: str) -> list[str]:
    """场景 art bible 锁定要素：显式 art_bible 优先，否则由场次信息确定性生成。

    同一场次的所有镜头共享同一组要素，保证跨镜头场景/道具外观一致（task 2.3）。
    """
    explicit = _norm_elements(
        _pick(scene, "art_bible", "art_bible_elements", "visual_bible"))
    if explicit:
        return explicit
    elements = [f"环境：{heading}"]
    if synopsis:
        elements.append(f"氛围：{synopsis}")
    action = _pick(scene, "action", "action_text", default="")
    if isinstance(action, str) and action.strip():
        elements.append(f"场景描述：{action.strip()}")
    return elements


def _normalize_props(bundle: Any) -> list[dict]:
    """道具 art bible（可选）：id/name/锁定要素；上游无道具表时为空列表。"""
    props_raw = _pick(bundle, "props", "art_bible_props", "set_pieces", default=[]) or []
    props: list[dict] = []
    for item in props_raw:
        pid = _pick(item, "id", "prop_id")
        if pid is None:
            continue
        props.append({
            "id": str(pid),
            "name": str(_pick(item, "name", "prop_name", "title",
                              default=f"道具 {pid}")).strip(),
            "elements": _norm_elements(
                _pick(item, "art_bible_elements", "art_bible", "elements",
                      "appearance")),
        })
    return props


def _normalize_bundle(bundle: Any, scene_id: int) -> dict:
    """容忍少量键名别名的归一化（id 一律整型 rowid，沿用 screenplay 契约）。"""
    scene = _pick(bundle, "scene", default={}) or {}
    lines_raw = _pick(bundle, "lines", "dialogues", "speeches", default=[]) or []
    chars_raw = _pick(bundle, "characters", "cast", "roles", default=[]) or []

    lines = []
    for pos, item in enumerate(lines_raw):
        text = _pick(item, "text", "content", "line", "line_text", default="")
        speaker = _pick(item, "speaker", "speaker_name", "character",
                        "character_name", "name", default="")
        idx = _pick(item, "idx", "order", "sequence", default=pos + 1)
        if str(text).strip():
            lines.append({"idx": int(idx), "speaker": str(speaker).strip(),
                          "text": str(text).strip()})

    characters = []
    for item in chars_raw:
        cid = _pick(item, "id", "character_id")
        name = _pick(item, "name", "character_name", default="")
        if cid is None or not str(name).strip():
            continue
        appearance = _norm_elements(
            _pick(item, "appearance_elements", "appearance", "elements",
                  "elements_set", "style_elements"))
        model_sheet = _norm_elements(
            _pick(item, "model_sheet_elements", "model_sheet", "sheet_elements"))
        characters.append({
            "id": int(cid),
            "name": str(name).strip(),
            "appearance_elements": appearance,
            # model sheet 锁定要素：显式给出则用显式版，否则与角色要素集同源
            "model_sheet_elements": model_sheet or list(appearance),
        })

    heading = str(_pick(scene, "heading", "scene_heading", "title",
                        "name", default=f"场次 {scene_id}"))
    synopsis = str(_pick(scene, "synopsis", "summary", "description", default=""))

    return {
        "scene": {
            "id": int(_pick(scene, "id", default=scene_id)),
            "heading": heading,
            "synopsis": synopsis,
            "art_bible_elements": _scene_art_bible_elements(scene, heading, synopsis),
        },
        "lines": lines,
        "characters": characters,
        "props": _normalize_props(bundle),
    }


def load_scene_bundle(scene_id: int) -> dict:
    """经 screenplay 读函数取场次/台词/角色要素集（task 2.1/2.2 交付）。

    用 screenplay.connect()（共库连接）+ _project_tree（剧本树读取函数）
    组装本场 bundle；场次不存在 → 404；screenplay 未落地 → 503。
    测试与联调可直接替换本函数（模块级接缝，端点在请求时才解析名字）。
    """
    try:
        from . import screenplay
    except ImportError as exc:  # 并行开发期：上游模块尚未落地
        raise HTTPException(
            status_code=503,
            detail=f"剧本模块（screenplay）未就绪，无法读取场次 {scene_id}: {exc}",
        ) from exc
    conn = screenplay.connect()
    try:
        screenplay.migrate(conn)
        row = conn.execute(
            "SELECT id, project_id FROM scenes WHERE id = ?", (scene_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"场次 {scene_id} 不存在")
        tree = screenplay._project_tree(conn, row["project_id"])  # noqa: SLF001 —— screenplay 的剧本树读取函数（集成点）
    finally:
        conn.close()
    for scene in tree.get("scenes", []):
        if scene.get("id") == scene_id:
            return _normalize_bundle({
                "scene": scene,
                "lines": scene.get("lines", []),
                "characters": tree.get("characters", []),
            }, scene_id)
    raise HTTPException(status_code=404, detail=f"场次 {scene_id} 不存在")


# ---------------------------------------------------------------- 4.1 剧本 → 分镜（确定性 + LLM 润色）


def _scene_ref(bundle: dict) -> str | None:
    sid = (bundle.get("scene") or {}).get("id")
    return str(sid) if sid is not None else None


def _character_refs(speaker: str, by_name: dict[str, dict]) -> list[str]:
    """镜头归属角色的 model sheet 引用（无台词/旁白镜为空）。"""
    char = by_name.get(speaker) if speaker else None
    return [str(char["id"])] if char else []


def _deterministic_shots(bundle: dict) -> list[dict]:
    scene = bundle["scene"]
    heading, synopsis = scene["heading"], scene["synopsis"]
    setting = f"{heading}；{synopsis}" if synopsis else heading
    scene_ref = _scene_ref(bundle)
    art_refs = [scene_ref] if scene_ref else []
    by_name = {c["name"]: c for c in bundle.get("characters") or []}

    shots: list[dict] = [{
        "idx": 0,
        "shot_size": "全景",
        "visual": f"{setting}（定场）",
        "action": "镜头缓推，交代环境与人物站位",
        "staging": f"俯视图：{heading}的空间关系全景铺陈，人物入口与主光方向标在画面边缘",
        "keyframe": f"关键帧：{setting}的空镜定场，画面先立住空间与光影",
        "model_sheet_refs": [],
        "art_bible_refs": list(art_refs),
        "line_text": None,
        "line_speaker": None,
        "duration_sec": ESTABLISH_SEC,
        "engine": "deterministic",
    }]
    for pos, line in enumerate(bundle["lines"], start=1):
        text, speaker = line["text"], line["speaker"]
        size = SHOT_SIZES[len(text) % 4]  # 按句长轮换景别
        excerpt = text[:16] + "…" if len(text) > 16 else text
        side = "左" if pos % 2 else "右"
        counter = "右" if side == "左" else "左"
        if speaker:
            staging = (f"俯视图：{speaker}位于画面{side}侧，对话对象在{counter}侧，"
                       f"二人沿{size}的对话轴相对站位")
            keyframe = f"关键帧：{speaker}在{size}中的定格，表情与手势正对「{excerpt}」"
        else:
            staging = "俯视图：空镜，机位沿场景纵深缓慢推移"
            keyframe = f"关键帧：{size}空镜，画面随旁白停在「{excerpt}」"
        shots.append({
            "idx": pos,
            "shot_size": size,
            "visual": f"{setting}；{speaker or '旁白'}（{size}）：{excerpt}",
            "action": f"{speaker or '旁白'}开口，神情与手势随语义起伏" if speaker
                      else "画面随旁白语流缓缓推移",
            "staging": staging,
            "keyframe": keyframe,
            "model_sheet_refs": _character_refs(speaker, by_name),
            "art_bible_refs": list(art_refs),
            "line_text": text,
            "line_speaker": speaker or None,
            "duration_sec": max(2, math.ceil(len(text) / CHARS_PER_SEC)),
            "engine": "deterministic",
        })
    shots.append({
        "idx": len(shots),
        "shot_size": "全景",
        "visual": f"{setting}，对话落定，全场归于静默（收尾）",
        "action": "镜头缓缓拉远，收束全场",
        "staging": "俯视图：人物沿对话轴收拢至画面中央，机位退向后景",
        "keyframe": "关键帧：对话落定后的静默瞬间，画面留白收束",
        "model_sheet_refs": [],
        "art_bible_refs": list(art_refs),
        "line_text": None,
        "line_speaker": None,
        "duration_sec": ENDING_SEC,
        "engine": "deterministic",
    })
    return shots


def _loads_lenient(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):  # 剥掉 markdown 代码围栏
        text = text.strip("`").strip()
        if "\n" in text:
            text = text.split("\n", 1)[1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _polish_with_llm(shots: list[dict], bundle: dict,
                     continuity_text: str | None = None) -> list[dict]:
    """LLM 仅润色 visual/action（一次性整场调用）；任何失败抛 LLMError 由调用方降级。

    ``continuity_text`` 为 task 3.5 的生成前上下文（时点有效事实/角色知识/伏笔），
    仅作为约束注入，不参与镜头归属与字段锁定。
    """
    scene = bundle["scene"]
    material = [
        {"idx": s["idx"], "shot_size": s["shot_size"], "visual": s["visual"],
         "action": s["action"], "line_speaker": s["line_speaker"],
         "line_text": s["line_text"]}
        for s in shots
    ]
    system = (
        "你是电影分镜师。对给定镜头表逐镜润色画面描述(visual)与动作(action)，"
        "使其更具镜头感与可视性；不得改变景别、台词、时长与镜头归属。"
        + (
            "参考连续性台账：不得让角色使用其尚不知晓的信息，不得提前使用未来才成立的事实。"
            if continuity_text else ""
        )
        + '只输出 JSON 数组：[{"idx": int, "visual": str, "action": str}, ...]，不要多余文字。'
    )
    payload: dict[str, Any] = {"scene": scene, "shots": material}
    if continuity_text:
        payload["continuity"] = continuity_text
    content = llm.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        temperature=0.4,
        max_tokens=1600,
    )
    if not content:
        raise llm.LLMError("LLM 网关返回空响应")
    try:
        data = _loads_lenient(content)
    except (json.JSONDecodeError, ValueError) as exc:
        raise llm.LLMError(f"LLM 润色负载不是合法 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise llm.LLMError("LLM 润色负载不是 JSON 数组")
    by_idx = {s["idx"]: dict(s) for s in shots}
    known = set(by_idx)
    for item in data:
        if not isinstance(item, dict) or "idx" not in item:
            continue
        idx = item["idx"]
        if not isinstance(idx, int) or idx not in known:
            continue
        for key in ("visual", "action"):
            value = item.get(key)
            if isinstance(value, str) and value.strip() and len(value) <= 2000:
                by_idx[idx][key] = value.strip()
    return [by_idx[s["idx"]] for s in shots]


def build_storyboard(bundle: dict,
                     continuity_text: str | None = None) -> tuple[list[dict], str]:
    """确定性版必做；.llm 有 KEY 时尝试 LLM 润色，超时/失败回退并标 engine。"""
    shots = _deterministic_shots(bundle)
    engine = "deterministic"
    if llm.is_configured():
        try:
            shots = _polish_with_llm(shots, bundle, continuity_text=continuity_text)
            engine = "llm"
        except llm.LLMError:
            shots = _deterministic_shots(bundle)
            engine = "deterministic"
    for shot in shots:
        shot["engine"] = engine
    return shots, engine


class ShotPatch(BaseModel):
    """逐镜人工编辑（人在环上）：至少改一格，景别限定四档。

    扩展字段：staging/keyframe 用空串或 null 清空；model_sheet_refs/
    art_bible_refs 用 [] 清空。引用 ID 统一字符串化并去重。
    """
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    shot_size: str | None = Field(default=None, pattern="^(全景|中景|近景|特写)$")
    visual: str | None = Field(default=None, min_length=1, max_length=2000)
    action: str | None = Field(default=None, min_length=1, max_length=2000)
    line_text: str | None = Field(default=None, max_length=2000)
    line_speaker: str | None = Field(default=None, max_length=200)
    duration_sec: float | None = Field(default=None, gt=0, le=600)
    staging: str | None = Field(default=None, max_length=2000)
    keyframe: str | None = Field(default=None, max_length=2000)
    model_sheet_refs: list[str] | None = Field(default=None, max_length=64)
    art_bible_refs: list[str] | None = Field(default=None, max_length=64)

    @field_validator("model_sheet_refs", "art_bible_refs")
    @classmethod
    def _normalize_refs(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _norm_refs(value)


@router.post("/storyboard/{scene_id}")
def create_storyboard(scene_id: int,
                      episode: int | None = Query(default=None, ge=1)) -> dict:
    """场次 → 镜头表（重转 = 整场替换；产出后逐镜 PUT 编辑）。

    连续性台账接缝（change juchuang-continuity-layer task 3.5-3.7）：
    - 生成前按 ``episode``（缺省取场次 idx）组装时点上下文，注入 LLM 润色提示；
    - 生成后对镜头台词做确定性检查（实体名/知识时点/伏笔状态）；
    - 台账为空或不可用时两段都返回 ``available=False``，生成照常完成。
    """
    bundle = load_scene_bundle(scene_id)
    pre = ledger.pre_generation_context_for_scene(scene_id, episode=episode)
    continuity_text = pre.get("text") if pre.get("available") else None
    shots, engine = build_storyboard(bundle, continuity_text=continuity_text)
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM storyboard WHERE scene_id = ?", (scene_id,))
            conn.executemany(
                "INSERT INTO storyboard (scene_id, idx, shot) VALUES (?, ?, ?)",
                [(scene_id, s["idx"], json.dumps(s, ensure_ascii=False)) for s in shots],
            )
        rows = _load_storyboard(conn, scene_id)
    finally:
        conn.close()
    post = ledger.post_generation_check_for_scene(
        scene_id, shots, episode=pre.get("episode") or episode)
    available = bool(pre.get("available") or post.get("available"))
    return {
        "scene_id": scene_id,
        "storyboard_id": scene_id,
        "engine": engine,
        "shot_count": len(rows),
        "shots": [r["shot"] for r in rows],
        "continuity": {
            "available": available,
            "enabled": bool(pre.get("enabled") or post.get("enabled")),
            "reason": None if available else (
                pre.get("reason") or post.get("reason") or ledger.EMPTY_LEDGER_REASON
            ),
            "episode": pre.get("episode", episode),
            "pre": pre,
            "post": post,
        },
    }


@router.get("/storyboard/{storyboard_id}")
def get_storyboard(storyboard_id: int) -> dict:
    conn = connect()
    try:
        rows = _load_storyboard(conn, storyboard_id)
    finally:
        conn.close()
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"场次 {storyboard_id} 尚无镜头表，先 POST 转分镜")
    return {"scene_id": storyboard_id, "storyboard_id": storyboard_id,
            "shot_count": len(rows), "shots": [r["shot"] for r in rows]}


@router.get("/storyboard/{storyboard_id}/export")
def export_storyboard(storyboard_id: int) -> Response:
    """镜头表纯 JSON 下载体：staging/keyframe/model_sheet/art_bible 一并导出。"""
    conn = connect()
    try:
        rows = _load_storyboard(conn, storyboard_id)
    finally:
        conn.close()
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"场次 {storyboard_id} 尚无镜头表，先 POST 转分镜")
    payload = {
        "scene_id": storyboard_id,
        "storyboard_id": storyboard_id,
        "shot_count": len(rows),
        "shots": [r["shot"] for r in rows],
    }
    filename = f"storyboard-{storyboard_id}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.put("/storyboard/{storyboard_id}/{idx}")
def update_shot(storyboard_id: int, idx: int, patch: ShotPatch) -> dict:
    """逐镜编辑一格（merge 语义：只覆盖提交的字段）。

    扩展字段支持清空：staging/keyframe 提交空串或 null、引用列表提交 []。
    """
    fields = patch.model_dump(exclude_unset=True)
    for key in SHOT_TEXT_FIELDS:
        if key in fields:
            fields[key] = _norm_text(fields[key])
    for key in SHOT_REF_FIELDS:
        if key in fields and fields[key] is not None:
            fields[key] = _norm_refs(fields[key])
    # 旧字段维持原语义：提交 null 视为「不修改」，不能用于清空
    for key in ("shot_size", "visual", "action", "line_text", "line_speaker",
                "duration_sec"):
        if fields.get(key) is None:
            fields.pop(key, None)
    if not fields:
        raise HTTPException(status_code=400, detail="至少提交一个要编辑的字段")
    if "duration_sec" in fields:
        fields["duration_sec"] = int(fields["duration_sec"]) if float(
            fields["duration_sec"]).is_integer() else fields["duration_sec"]
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM storyboard WHERE scene_id = ? AND idx = ?",
            (storyboard_id, idx),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"镜头表 {storyboard_id} 不存在 idx={idx} 的镜头",
            )
        shot = json.loads(row["shot"])
        shot.update(fields)
        shot["idx"] = idx  # 镜头序号不可经编辑漂移
        with conn:
            conn.execute(
                "UPDATE storyboard SET shot = ? WHERE id = ?",
                (json.dumps(shot, ensure_ascii=False), row["id"]),
            )
        updated = conn.execute("SELECT * FROM storyboard WHERE id = ?",
                               (row["id"],)).fetchone()
        return _row_shot(updated)
    finally:
        conn.close()


# ---------------------------------------------------------------- 4.2/4.3 分镜 → 三形态提示词


def _characters_by_name(bundle: dict) -> dict[str, dict]:
    return {c["name"]: c for c in bundle["characters"]}


def _characters_by_id(bundle: dict) -> dict[str, dict]:
    return {str(c["id"]): c for c in bundle["characters"]}


def _shot_character(shot: dict, chars: dict[str, dict]) -> dict | None:
    """镜头归属角色：按台词说话人对名（含要素集的角色卡即一致性锚点）。"""
    speaker = (shot.get("line_speaker") or "").strip()
    return chars.get(speaker) if speaker else None


def _element_text(elements: list[str]) -> str:
    return "；".join(elements)


def _character_lock_segment(char: dict) -> str:
    """角色锁定段：跨镜头逐字一致（spec：同一组外观/风格要素）。"""
    return f"人物锁定——{char['name']}：{_element_text(char['appearance_elements'])}"


def _model_sheet_elements(char: dict) -> list[str]:
    """model sheet 锁定要素：显式 model sheet 优先，否则与角色要素集同源。"""
    return list(char.get("model_sheet_elements")
                or char.get("appearance_elements") or [])


def _resolve_model_sheets(shot: dict, chars_by_id: dict[str, dict],
                          char: dict | None) -> list[dict]:
    """按 model_sheet_refs 解析角色 model sheet；旧行无引用时沿用说话人角色卡。"""
    refs = _norm_refs(shot.get("model_sheet_refs"))
    if not refs and char is not None:
        refs = [str(char["id"])]
    out: list[dict] = []
    seen: set[str] = set()
    for ref in refs:
        target = chars_by_id.get(ref)
        if target is None or ref in seen:
            continue
        seen.add(ref)
        out.append({
            "ref": ref,
            "id": target["id"],
            "name": target["name"],
            "elements": _model_sheet_elements(target),
        })
    return out


def _scene_art_bible_entry(bundle: dict) -> dict:
    """场景 art bible 条目：整场共享的锁定要素（旧 bundle 由场次信息兜底）。"""
    scene = bundle["scene"]
    elements = list(scene.get("art_bible_elements") or [])
    if not elements:
        elements = _scene_art_bible_elements(
            scene, scene["heading"], scene.get("synopsis") or "")
    return {
        "ref": str(scene["id"]),
        "kind": "scene",
        "name": scene["heading"],
        "elements": elements,
    }


def _resolve_art_bible(shot: dict, bundle: dict) -> list[dict]:
    """按 art_bible_refs 解析场景/道具 art bible；未知引用忽略，空引用场景兜底。

    refs 指向场次 id → 场景条目；指向 props[].id → 道具条目；其余忽略（不报错），
    全部未解析时退回场景 art bible，保证旧数据仍注入同一组场景要素。
    """
    scene_entry = _scene_art_bible_entry(bundle)
    props = {p["id"]: p for p in bundle.get("props") or []}
    refs = _norm_refs(shot.get("art_bible_refs"))
    if not refs:
        return [scene_entry]
    out: list[dict] = []
    seen: set[str] = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        if ref == scene_entry["ref"]:
            out.append(scene_entry)
        elif ref in props:
            prop = props[ref]
            out.append({"ref": ref, "kind": "prop", "name": prop["name"],
                        "elements": list(prop["elements"])})
    return out or [scene_entry]


def _shot_locks(shot: dict, bundle: dict, char: dict | None,
                chars_by_id: dict[str, dict]) -> tuple[list[str], list[dict], list[dict]]:
    """生成镜头的锁定段，并返回结构化 model_sheets / art_bible（供导出）。"""
    segments: list[str] = []
    if char:
        segments.append(_character_lock_segment(char))
    model_sheets = _resolve_model_sheets(shot, chars_by_id, char)
    for sheet in model_sheets:
        if (char is not None and str(sheet["id"]) == str(char["id"])
                and sheet["elements"] == list(char["appearance_elements"])):
            continue  # 与人物锁定段同一组要素，不重复注入
        segments.append(f"model_sheet〔{sheet['ref']}·{sheet['name']}〕锁定："
                        f"{_element_text(sheet['elements'])}")
    art_bible = _resolve_art_bible(shot, bundle)
    for entry in art_bible:
        kind = "场景" if entry["kind"] == "scene" else "道具"
        segments.append(f"art_bible〔{kind} {entry['ref']}·{entry['name']}〕锁定："
                        f"{_element_text(entry['elements'])}")
    return segments, model_sheets, art_bible


def _shot_extras(shot: dict, model_sheets: list[dict], art_bible: list[dict]) -> dict:
    """导出用扩展字段（旧行缺键时 staging/keyframe 为 null、引用为 []）。"""
    return {
        "staging": _norm_text(shot.get("staging")),
        "keyframe": _norm_text(shot.get("keyframe")),
        "model_sheet_refs": _norm_refs(shot.get("model_sheet_refs")),
        "art_bible_refs": _norm_refs(shot.get("art_bible_refs")),
        "model_sheets": model_sheets,
        "art_bible": art_bible,
    }


def _build_image_payload(storyboard_id: int, bundle: dict, shots: list[dict]) -> dict:
    chars = _characters_by_name(bundle)
    chars_by_id = _characters_by_id(bundle)
    out = []
    for shot in shots:
        char = _shot_character(shot, chars)
        locks, model_sheets, art_bible = _shot_locks(shot, bundle, char, chars_by_id)
        parts = [STYLE_ANCHOR, shot["visual"]]
        staging = _norm_text(shot.get("staging"))
        keyframe = _norm_text(shot.get("keyframe"))
        if staging:
            parts.append(f"走位：{staging}")
        if keyframe:
            parts.append(f"关键帧：{keyframe}")
        parts.extend(locks)
        out.append({
            "idx": shot["idx"],
            "shot_size": shot["shot_size"],
            "prompt": "｜".join(parts),
            "character_id": char["id"] if char else None,
            "character_name": char["name"] if char else None,
            "character_elements": list(char["appearance_elements"]) if char else [],
            **_shot_extras(shot, model_sheets, art_bible),
        })
    return {
        "form": "image",
        "schema_version": PROMPT_SCHEMA_VERSION,
        "storyboard_id": storyboard_id,
        "scene": bundle["scene"],
        "style_anchor": STYLE_ANCHOR,
        "shots": out,
    }


def _build_video_payload(storyboard_id: int, bundle: dict, shots: list[dict]) -> dict:
    chars = _characters_by_name(bundle)
    chars_by_id = _characters_by_id(bundle)
    out = []
    for shot in shots:
        char = _shot_character(shot, chars)
        if shot["idx"] == 0:
            move = "缓推定场"
        elif shot["idx"] == len(shots) - 1:
            move = "缓慢拉远"
        else:
            move = CAMERA_MOVES[shot["idx"] % len(CAMERA_MOVES)]
        locks, model_sheets, art_bible = _shot_locks(shot, bundle, char, chars_by_id)
        parts = [shot["visual"], shot["action"]]
        staging = _norm_text(shot.get("staging"))
        keyframe = _norm_text(shot.get("keyframe"))
        if staging:
            parts.append(f"走位：{staging}")
        if keyframe:
            parts.append(f"关键帧：{keyframe}")
        parts.extend(locks)
        out.append({
            "idx": shot["idx"],
            "shot": "；".join(parts),
            "duration_sec": shot["duration_sec"],
            "camera_move": move,
            "character_id": char["id"] if char else None,
            "character_name": char["name"] if char else None,
            "character_elements": list(char["appearance_elements"]) if char else [],
            **_shot_extras(shot, model_sheets, art_bible),
        })
    return {
        "form": "video",
        "schema_version": PROMPT_SCHEMA_VERSION,
        "storyboard_id": storyboard_id,
        "scene": bundle["scene"],
        "shots": out,
    }


def _build_narrative_payload(storyboard_id: int, bundle: dict) -> dict:
    scene = bundle["scene"]
    parts: list[str] = [f"{scene['heading']}。"]
    if scene["synopsis"]:
        parts.append(f"{scene['synopsis']}。")
    for line in bundle["lines"]:
        speaker = f"{line['speaker']}道" if line["speaker"] else "旁白起"
        parts.append(f"{speaker}：「{line['text']}」")
    parts.append("本场收束。")
    text = "".join(parts)
    return {
        "form": "narrative",
        "schema_version": PROMPT_SCHEMA_VERSION,
        "storyboard_id": storyboard_id,
        "scene": scene,
        "text": text,
        "word_count": len(text),
    }


def _idempotency_key(*parts: Any) -> str:
    """跨进程稳定的内容派生键（不用内置 hash：它按进程加盐）。"""
    import hashlib

    raw = "\u0000".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _reference_ids(bundle: dict, shot: dict, chars_by_id: dict) -> list[str]:
    """这一镜要引用的**已绑定**资产 id（model sheet + 场景 art bible）。

    只收已绑定的：没绑定就不给假 id，让下游按纯文本生成。
    """
    ids: list[str] = []
    char = _shot_character(shot, _characters_by_name(bundle))
    if char and char.get("asset_id"):
        ids.append(str(char["asset_id"]))
    scene = bundle.get("scene") or {}
    if scene.get("asset_id"):
        ids.append(str(scene["asset_id"]))
    return ids


def _build_generation_payload(storyboard_id: int, bundle: dict,
                              shots: list[dict]) -> dict:
    """镜头表 → 可执行生成计划（第四形态）。

    每条含：镜头序号、提示词、模式（generate|edit）、引用资产、源资产（编辑时）、
    幂等键。**键里必须含源资产 id** —— 否则「对 A 图改外套」与「对 B 图改外套」
    会撞键返回同一张（design D4，最容易漏的一处）。
    """
    chars_by_id = _characters_by_id(bundle)
    image_payload = _build_image_payload(storyboard_id, bundle, shots)
    prompts = {p["idx"]: p["prompt"] for p in image_payload["shots"]}
    # 「这一镜当前是哪张图」来自图位记录（镜级产物），不是 shot JSON ——
    # shot 是剧本侧结构，图是生成侧结果，两者归属不同层。
    current = _current_shot_images(os.environ.get("FD_JUCHUANG_DB", "juchuang.db"),
                                   storyboard_id)

    plan = []
    for shot in shots:
        refs = _reference_ids(bundle, shot, chars_by_id)
        prompt = prompts.get(shot["idx"], shot.get("visual") or "")
        source = current.get(shot["idx"])
        mode = "edit" if source else "generate"
        key = _idempotency_key(
            storyboard_id, shot["idx"], prompt, mode, source,
            ",".join(sorted(refs)),
        )
        entry = {
            "shot_idx": shot["idx"],
            "prompt": prompt,
            "mode": mode,
            "reference_asset_ids": refs,
            "idempotency_key": key,
        }
        if source:
            entry["source_asset_id"] = source
        plan.append(entry)
    return {
        "form": "generation",
        "schema_version": PROMPT_SCHEMA_VERSION,
        "storyboard_id": storyboard_id,
        "scene": bundle["scene"],
        "shots": plan,
    }


def _current_shot_images(db_path: str, storyboard_id: int) -> dict[int, str]:
    """{shot_idx: 当前图资产 id}；无记录时返回 {}（计划里即 mode=generate）。"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT shot_idx, asset_id FROM shot_image WHERE scene_id = ?",
            (storyboard_id,),
        ).fetchall()
        conn.close()
        return {int(r["shot_idx"]): r["asset_id"] for r in rows if r["asset_id"]}
    except sqlite3.Error:
        return {}


def _build_payloads(storyboard_id: int, bundle: dict, shots: list[dict]) -> dict[str, dict]:
    return {
        "image": _build_image_payload(storyboard_id, bundle, shots),
        "video": _build_video_payload(storyboard_id, bundle, shots),
        "narrative": _build_narrative_payload(storyboard_id, bundle),
        "generation": _build_generation_payload(storyboard_id, bundle, shots),
    }


@router.post("/prompts/{storyboard_id}")
def create_promptsets(storyboard_id: int) -> dict:
    """镜头表 → 三形态提示词（重导出 = 替换该镜头表旧 promptset）。"""
    conn = connect()
    try:
        rows = _load_storyboard(conn, storyboard_id)
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"镜头表 {storyboard_id} 不存在，先 POST /pipeline/storyboard/{storyboard_id}",
            )
        bundle = load_scene_bundle(storyboard_id)
        payloads = _build_payloads(storyboard_id, bundle, [r["shot"] for r in rows])
        with conn:
            conn.execute("DELETE FROM promptset WHERE storyboard_id = ?",
                         (storyboard_id,))
            for form in PROMPT_FORMS:
                conn.execute(
                    "INSERT INTO promptset (storyboard_id, form, payload, schema_version)"
                    " VALUES (?, ?, ?, ?)",
                    (storyboard_id, form,
                     json.dumps(payloads[form], ensure_ascii=False),
                     PROMPT_SCHEMA_VERSION),
                )
        created = conn.execute(
            "SELECT id, form, created_at FROM promptset WHERE storyboard_id = ?"
            " ORDER BY form", (storyboard_id,)
        ).fetchall()
    finally:
        conn.close()
    return {
        "storyboard_id": storyboard_id,
        "schema_version": PROMPT_SCHEMA_VERSION,
        "promptsets": [{"id": r["id"], "form": r["form"],
                        "created_at": r["created_at"]} for r in created],
    }


def _get_promptset_row(conn: sqlite3.Connection, promptset_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM promptset WHERE id = ?",
                       (promptset_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"提示词集 {promptset_id} 不存在")
    return row


@router.get("/prompts/{promptset_id}")
def get_promptset(promptset_id: int) -> dict:
    """按形态查询：一个 promptset 即一种形态（form 字段）。"""
    conn = connect()
    try:
        row = _get_promptset_row(conn, promptset_id)
    finally:
        conn.close()
    return {
        "id": row["id"],
        "storyboard_id": row["storyboard_id"],
        "form": row["form"],
        "schema_version": row["schema_version"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }


@router.get("/prompts/{promptset_id}/export")
def export_promptset(
    promptset_id: int,
    form: str | None = Query(default=None, pattern="^(image|video|narrative)$"),
) -> Response:
    """纯 JSON 下载体（payload 本体，Content-Disposition 附件）。"""
    conn = connect()
    try:
        row = _get_promptset_row(conn, promptset_id)
    finally:
        conn.close()
    if form is not None and form != row["form"]:
        raise HTTPException(
            status_code=409,
            detail=f"提示词集 {promptset_id} 是 {row['form']} 形态，与请求的 {form} 不符",
        )
    filename = f"promptset-{promptset_id}-{row['form']}.json"
    return Response(
        content=row["payload"],
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- 生成能力与执行（change juchuang-image-iteration）


@router.get("/generate/capabilities")
def generate_capabilities() -> dict:
    """出图能力的可用性，**按能力分别判定**（spec「降级独立模式」）。

    文生图可用而编辑不可用是合法状态。前端据此分别禁用按钮，
    不因一个车道抖动就把整块出图关掉。
    """
    from . import vaas_client

    caps = vaas_client.capabilities()
    return {
        "configured": vaas_client.configured(),
        "generate": caps[vaas_client.GENERATE],
        "edit": caps[vaas_client.EDIT],
    }


# ---------------------------------------------------------------- 4.x 执行：生成与编辑


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_idxs: list[int] | None = None      # 省略 = 全场
    max_calls: int = Field(default=20, ge=1, le=100)


def _plan_for(storyboard_id: int, conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT payload FROM promptset WHERE storyboard_id=? AND form='generation'",
        (storyboard_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"镜头表 {storyboard_id} 还没有生成计划，先 POST /pipeline/prompts/{storyboard_id}",
        )
    return json.loads(row["payload"])


def _record_shot_image(conn: sqlite3.Connection, scene_id: int, shot_idx: int,
                       asset_id: str, slug: str | None, key: str) -> None:
    conn.execute(
        "INSERT INTO shot_image (scene_id, shot_idx, asset_id, slug, idempotency_key)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(scene_id, shot_idx) DO UPDATE SET"
        " asset_id=excluded.asset_id, slug=excluded.slug,"
        " idempotency_key=excluded.idempotency_key, created_at=datetime('now')",
        (scene_id, shot_idx, asset_id, slug, key),
    )


@router.post("/generate/{storyboard_id}")
def execute_generation(storyboard_id: int, body: GenerateRequest | None = None) -> dict:
    """按计划逐镜出图（task 4.1）。

    上限是**硬门**：超了在发起任何付费调用前拒绝（spec「逐镜调用需先确认额度」）。
    部分失败保留已成功的，逐镜报错，不做整体回滚。
    """
    from . import vaas_client

    body = body or GenerateRequest()
    conn = connect()
    try:
        plan = _plan_for(storyboard_id, conn)
        all_shots = plan["shots"]
        wanted = set(body.shot_idxs) if body.shot_idxs else None
        targets = [s for s in all_shots if wanted is None or s["shot_idx"] in wanted]

        if len(targets) > body.max_calls:
            raise HTTPException(
                status_code=400,
                detail=f"本次要生成 {len(targets)} 镜，超过上限 {body.max_calls}；"
                       f"请缩小范围或调高 max_calls（每一镜都是一次付费调用）",
            )

        results, failures = [], []
        for shot in targets:
            try:
                out = vaas_client.generate_image(
                    prompt=shot["prompt"], slug=shot.get("slug"),
                )
            except vaas_client.Unavailable as e:
                failures.append({"shot_idx": shot["shot_idx"],
                                 "category": e.category, "message": str(e)})
                continue
            asset_id = out.get("id") or ""
            _record_shot_image(conn, storyboard_id, shot["shot_idx"], asset_id,
                               out.get("slug"), shot["idempotency_key"])
            results.append({"shot_idx": shot["shot_idx"], "asset_id": asset_id,
                            "file_paths": out.get("file_paths") or [],
                            "download_url": out.get("download_url")})
        conn.commit()
    finally:
        conn.close()
    return {"storyboard_id": storyboard_id, "generated": results,
            "failures": failures, "requested": len(targets)}


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1)
    mask: str | None = None


@router.post("/shots/{storyboard_id}/{shot_idx}/edit")
def execute_edit(storyboard_id: int, shot_idx: int,
                 body: EditRequest) -> dict:
    """对某一镜当前图做精细化修改（task 4.2）。

    「改这一镜」只动这一镜：源必须是该镜已生成的图，否则没有可改的东西。
    编辑不可用时**不静默退回生成**（spec 明写禁止）——报告类别，让前端禁按钮。
    """
    from . import vaas_client

    conn = connect()
    try:
        row = conn.execute(
            "SELECT asset_id, idempotency_key FROM shot_image"
            " WHERE scene_id=? AND shot_idx=?", (storyboard_id, shot_idx),
        ).fetchone()
        if row is None or not row["asset_id"]:
            raise HTTPException(
                status_code=409,
                detail=f"第 {shot_idx} 镜还没有图，先执行生成再修改",
            )
        source = row["asset_id"]
        # 编辑键与生成键必须不同（含源资产 + 指令），否则「改 A」会命中「生成 A」
        key = _idempotency_key(storyboard_id, shot_idx, body.instruction,
                              "edit", source, "")
        try:
            out = vaas_client.edit_image(
                source=source, instruction=body.instruction, mask=body.mask,
            )
        except vaas_client.Unavailable as e:
            raise HTTPException(status_code=503,
                                detail=f"{e.category}: {e}") from e
        new_id = out.get("id") or ""
        _record_shot_image(conn, storyboard_id, shot_idx, new_id,
                           out.get("slug"), key)
        conn.commit()
    finally:
        conn.close()
    return {"storyboard_id": storyboard_id, "shot_idx": shot_idx,
            "asset_id": new_id, "source_asset_id": source,
            "file_paths": out.get("file_paths") or [],
            "download_url": out.get("download_url"),
            "warnings": out.get("warnings") or []}


# ---------------------------------------------------------------- 视频：两条成片通路


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: str = Field(pattern="^(i2v|render)$")
    shot_idxs: list[int] | None = None
    max_calls: int = Field(default=20, ge=1, le=100)
    confirm: bool = False          # i2v 必须先确认（按次付费）


@router.get("/video/capabilities")
def video_capabilities() -> dict:
    """两条成片通路**分别**判定（spec：互不代表）。"""
    from . import vaas_client
    return vaas_client.video_capabilities()


def _shot_images(conn: sqlite3.Connection, scene_id: int) -> dict[int, str]:
    """{镜号: 资产 id}。资产 id 是**账本**标识，不是文件路径——两者用途不同：
    - 编辑 / 图生视频的首帧：传资产 id（VAAS 自己会解析）；
    - 成片渲染的图清单：VAAS 渲染面只认**文件路径**，不认账本 id。
      2026-11-22 实测：传 id 会报 `carousel image not found: <uuid>`。
    所以调用方按需取：`_shot_image_paths` 供渲染，本函数供生成/编辑。
    """
    rows = conn.execute(
        "SELECT shot_idx, asset_id FROM shot_image WHERE scene_id=?", (scene_id,)
    ).fetchall()
    return {int(r["shot_idx"]): r["asset_id"] for r in rows if r["asset_id"]}


def _shot_image_paths(images: dict[int, str]) -> dict[int, str]:
    """资产 id → 渲染面可用的文件路径（downloads 相对路径，VAAS 侧可解析）。"""
    from . import vaas_client

    out: dict[int, str] = {}
    for idx, aid in images.items():
        path = vaas_client.asset_file_path(aid)
        if path:
            out[idx] = path
    return out


def _shots_for(scene_id: int, conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT idx, shot FROM storyboard WHERE scene_id=? ORDER BY idx", (scene_id,)
    ).fetchall()
    out = []
    for r in rows:
        shot = json.loads(r["shot"])
        shot.setdefault("idx", r["idx"])
        out.append(shot)
    return out


@router.post("/video/{storyboard_id}")
def create_video(storyboard_id: int, body: VideoRequest) -> dict:
    """分镜 → 成片。两条路各自可算成本、各自可降级（design D1）。"""
    from . import storyboard_render, vaas_client

    conn = connect()
    try:
        shots = _shots_for(storyboard_id, conn)
        if not shots:
            raise HTTPException(status_code=404,
                                detail=f"镜头表 {storyboard_id} 不存在")
        images = _shot_images(conn, storyboard_id)
        # 渲染面只认文件路径；账本 id 只给生成/编辑用（见 _shot_image_paths 注释）。
        # 取不到路径的镜**不放进图清单**，由 compile_carousel 按「缺图」报出来——
        # 静默少一段是最糟的失败形态。
        from . import vaas_client as _vc
        render_paths = {}
        for idx, aid in images.items():
            p = _vc.asset_file_path(aid)
            if p:
                render_paths[idx] = p
        plan = storyboard_render.compile_carousel({"shots": shots}, render_paths)
    finally:
        conn.close()

    if plan["missing"]:
        raise HTTPException(
            status_code=409,
            detail={"message": f"这些镜头还没有图：{plan['missing']}",
                    "missing": plan["missing"]},
        )

    if body.route == "render":
        return _video_render(storyboard_id, plan)
    return _video_i2v(storyboard_id, plan, shots, images, body)


def _video_render(storyboard_id: int, plan: dict) -> dict:
    from . import vaas_client

    slug = f"jc-carousel-{storyboard_id}"
    try:
        out = vaas_client.render_carousel(
            script=plan["script"],
            inputs={"images": plan["images"]},
            slug=slug,
        )
    except vaas_client.Unavailable as e:
        raise HTTPException(status_code=503, detail=f"{e.category}: {e}") from e
    # render_video 回的是 `file_path`（单数）——与 generate_* 的 `file_paths` 不同形状。
    # 2026-11-22 实测：按 file_paths 取会拿到空数组，看起来像「渲染没出片」。
    fpath = out.get("file_path")
    return {
        "storyboard_id": storyboard_id,
        "route": "render",
        "cost": "local",                     # 零上游成本
        "asset_id": out.get("id"),
        "slug": out.get("slug") or slug,
        "file_paths": [fpath] if fpath else [],
        "download_url": out.get("download_url"),
        "segments": plan["segments"],        # 哪一段对应哪一镜
        "duration_source": plan["duration_source"],
        "note": "段时长以音频为准，不按镜头 duration_sec 强制定长",
    }


def _video_i2v(storyboard_id: int, plan: dict, shots: list[dict],
               images: dict[int, str], body: VideoRequest) -> dict:
    from . import vaas_client

    caps = vaas_client.video_capabilities()
    if not caps["i2v"].get("available"):
        # 不可用时**不发起任何调用**，也不静默退回 render（那是另一个产物）
        raise HTTPException(
            status_code=503,
            detail={"message": "图生视频不可用", "reason": caps["i2v"].get("reason"),
                    "detail": caps["i2v"].get("message")},
        )
    if not caps["i2v"].get("first_frame_supported", False):
        raise HTTPException(
            status_code=503,
            detail={"message": "该车道未验证支持首帧，图生视频会退化成纯文生视频",
                    "reason": "first_frame_unverified"},
        )

    wanted = set(body.shot_idxs) if body.shot_idxs else None
    targets = [s for s in shots if wanted is None or s["idx"] in wanted]
    if len(targets) > body.max_calls:
        raise HTTPException(
            status_code=400,
            detail=f"本次要生成 {len(targets)} 镜，超过上限 {body.max_calls}"
                   f"（每镜一次付费调用）",
        )
    if not body.confirm:
        # 发起前必须让人看到次数与时长（spec「逐镜调用需先确认额度」）
        return {
            "storyboard_id": storyboard_id, "route": "i2v", "confirmed": False,
            "estimated_calls": len(targets),
            "estimated_seconds": sum(int(s.get("duration_sec") or 3) for s in targets),
            "cost": "per_second",
            "note": "确认后再提交（confirm=true）",
        }

    submitted, failures = [], []
    for shot in targets:
        idx = shot["idx"]
        prompt = (shot.get("visual") or shot.get("action") or shot.get("line") or "").strip() \
                 or f"第 {idx} 镜"
        try:
            out = vaas_client.submit_video(
                prompt=prompt,
                first_frame=images.get(idx),
                duration=int(shot.get("duration_sec") or 3),
                slug=f"jc-i2v-{storyboard_id}-{idx}",
            )
        except vaas_client.Unavailable as e:
            failures.append({"shot_idx": idx, "category": e.category, "message": str(e)})
            continue
        submitted.append({"shot_idx": idx, "job_id": out.get("job_id"),
                          "provider_task_id": out.get("provider_task_id")})
    return {
        "storyboard_id": storyboard_id, "route": "i2v", "confirmed": True,
        "submitted": submitted, "failures": failures,
        "requested": len(targets),
        "note": "视频是异步的：用 job_id 查 video_job_status",
    }
