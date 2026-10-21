"""剧创端到端验收脚本（change juchuang-continuity-layer task 5.1-5.3）。

一条链路跑完「Fountain 导入 → 连续性台账 → 生成分镜 → 生成后检查 → 质量门 →
导出」，并显式验证两条降级路径。脚本默认**离线**运行：临时 SQLite 共库 +
FastAPI TestClient，不依赖网络、LLM 或哲思；也可以 ``--base URL --token T``
对已部署服务跑同一套断言。

覆盖矩阵（每个 check id 对应一条可验收断言）：

5.1 完整链路
  e2e.project.create / e2e.character.create / e2e.scene.create / e2e.line.create
  fountain.export / fountain.import / fountain.roundtrip
  ledger.fact.create / ledger.timepoint / ledger.knowledge / ledger.foreshadow
  ledger.overdue / ledger.precontext
  storyboard.generate / storyboard.fields / storyboard.precheck
  storyboard.postcheck_block / storyboard.edit / storyboard.prompts
  quality.rubric / quality.assess / quality.dimensions / quality.continuity_located
  quality.evidence / quality.redline_block / quality.deliver_409
  export.project / export.fountain / export.fdx / export.storyboard / export.promptset

5.2 旧剧本回归（无台账、无新字段）
  legacy.create / legacy.ledger.empty / legacy.storyboard.ok
  legacy.storyboard.degraded / legacy.quality.degraded / legacy.fountain.ok
  legacy.storyboard.legacy_row / legacy.storyboard.legacy_export

5.3 无哲思模式
  zhesi.link_standalone / zhesi.ledger.independent / zhesi.quality.independent

用法::

    python3 scripts/e2e_juchuang.py                 # 离线全链（CI 用）
    python3 scripts/e2e_juchuang.py --json-report /tmp/e2e.json
    python3 scripts/e2e_juchuang.py --base http://127.0.0.1:8016 --token "$TOK"

退出码 0 表示全部断言通过；非 0 表示首个失败点（同时打印已完成/失败清单）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:  # 允许 `python3 scripts/e2e_juchuang.py`
    sys.path.insert(0, str(SERVER_DIR))

AUTH_TOKEN = "tok-e2e"
AUTH_USER = "e2e-user"
AUTH_HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}"}
API = "/api/v1"


class E2EFailure(AssertionError):
    """断言失败：携带 check id 与细节，供报告与退出码使用。"""

    def __init__(self, check_id: str, detail: str = ""):
        super().__init__(f"{check_id}: {detail}" if detail else check_id)
        self.check_id = check_id
        self.detail = detail


class Report:
    """收集断言结果并即时打印（CI 日志可直接定位失败点）。"""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, check_id: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.checks.append({"id": check_id, "ok": ok, "detail": detail})
        if ok:
            print(f"[PASS] {check_id}", flush=True)
        else:
            suffix = f" — {detail}" if detail else ""
            print(f"[FAIL] {check_id}{suffix}", flush=True)
        return ok

    def expect(self, check_id: str, ok: bool, detail: str = "") -> None:
        if not self.check(check_id, ok, detail):
            raise E2EFailure(check_id, detail)

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]


# ---------------------------------------------------------------- 传输层


class OfflineClient:
    """进程内 TestClient：覆盖路由、鉴权、SQLite 与全部业务模块。"""

    mode = "offline"

    def __init__(self, db_path: str) -> None:
        os.environ["FD_JUCHUANG_DB"] = db_path
        os.environ["FD_JUCHUANG_AUTH_DEV_TOKENS"] = f"{AUTH_TOKEN}={AUTH_USER}"
        os.environ["FD__AUTH_DEV_TOKENS"] = f"{AUTH_TOKEN}={AUTH_USER}"
        os.environ.pop("FD_JUCHUANG_LLM_API_KEY", None)  # 确定性引擎
        os.environ["FD_JUCHUANG_ZHESI_BASE"] = "http://127.0.0.1:1"  # 不可达
        from fastapi.testclient import TestClient

        from fd_juchuang_api.main import app

        self._client = TestClient(app)
        self.db_path = db_path

    def call(self, method: str, path: str, body=None, params=None, timeout=120):
        response = self._client.request(
            method, path, json=body, params=params, headers=AUTH_HEADERS
        )
        ctype = response.headers.get("content-type", "")
        if "application/json" in ctype:
            return response.status_code, response.json()
        return response.status_code, response.text

    def raw_db(self) -> sqlite3.Connection:
        """仅离线模式开放：用于构造「旧版数据行」回归样本。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


class LiveClient:
    """对已部署服务跑同一套断言（需要用户令牌与可写数据库）。"""

    mode = "live"

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body=None, params=None, timeout=120):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                raw = response.read()
                ctype = response.headers.get("content-type", "")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
            ctype = exc.headers.get("content-type", "")
        if "application/json" in ctype:
            return status, json.loads(raw or b"null")
        return status, raw.decode("utf-8", "replace")

    def raw_db(self) -> sqlite3.Connection:  # pragma: no cover - live 模式不走
        raise RuntimeError("live 模式不能直接操作数据库")


# ---------------------------------------------------------------- 断言辅助


def _ensure(report: Report, check_id: str, code: int, body, allowed=(200, 201)):
    report.expect(
        check_id,
        code in allowed,
        "" if code in allowed else f"HTTP {code}: {str(body)[:400]}",
    )
    return body


def _ids(items: list[dict]) -> set[int]:
    return {int(item["id"]) for item in items}


def _line_texts(tree: dict) -> list[str]:
    return [
        str(line.get("text"))
        for scene in tree.get("scenes") or []
        for line in scene.get("lines") or []
    ]


# ---------------------------------------------------------------- 5.1 主链


def create_sample_project(client, report: Report) -> dict:
    code, project = client.call(
        "POST", f"{API}/screenplay/projects",
        {"title": "旧账与旧友", "logline": "一张借据牵出十年前的旧账"},
    )
    _ensure(report, "e2e.project.create", code, project)
    pid = int(project["id"])

    characters: dict[str, int] = {}
    specs = [
        ("沈青梧", {"年龄感": "三十五岁上下", "着装": "月白旗袍", "画风": "水墨淡彩"},
         {"position": "旧账必须算清", "reasons": "家族旧怨"}),
        ("顾行舟", {"年龄感": "四十岁上下", "着装": "深灰长衫", "画风": "油画厚涂"},
         {"position": "旧账早已两清", "reasons": "自保"}),
    ]
    for name, appearance, stance in specs:
        code, character = client.call(
            "POST", f"{API}/screenplay/projects/{pid}/characters",
            {"name": name, "appearance": appearance, "stance": stance},
        )
        _ensure(report, "e2e.character.create", code, character)
        characters[name] = int(character["id"])

    code, scene1 = client.call(
        "POST", f"{API}/screenplay/projects/{pid}/scenes",
        {
            "title": "第一场：茶馆对峙",
            "synopsis": "旧友重逢，旧账与借据被摆上台面",
            "action": "茶馆内，烛火压低，雨声敲窗。",
            "transition": "CUT TO:",
            "conflict": {
                "positions": ["旧账必须算清", "旧账早已两清"],
                "source": "手工设定",
            },
        },
    )
    _ensure(report, "e2e.scene.create", code, scene1)
    code, scene2 = client.call(
        "POST", f"{API}/screenplay/projects/{pid}/scenes",
        {
            "title": "第二场：雨夜旧宅",
            "synopsis": "顾行舟夜访旧宅，怀表仍在走",
            "action": "雨夜，旧宅门廊，怀表滴答。",
            "transition": "FADE OUT.",
            "conflict": {
                "positions": ["追问真相", "守住秘密"],
                "source": "手工设定",
            },
        },
    )
    _ensure(report, "e2e.scene.create", code, scene2)

    lines1 = [
        ("沈青梧", "把账拿出来。"),
        ("顾行舟", "什么账，我听不懂你在说什么。"),
        ("沈青梧", "十年前腊月，城南当铺的借据，还认得吗？"),
        ("顾行舟", "城南当铺的借据，我早就烧了。"),
        ("沈青梧", "烧得掉纸，烧不掉印。"),
        ("顾行舟", "那你就去告。"),
    ]
    lines2 = [
        ("顾行舟", "旧怀表还在走。"),
        ("沈青梧", "表在，人也在。"),
        ("顾行舟", "你终于还是来了。"),
    ]
    for scene_id, lines in ((int(scene1["id"]), lines1), (int(scene2["id"]), lines2)):
        for speaker, text in lines:
            code, line = client.call(
                "POST", f"{API}/screenplay/scenes/{scene_id}/lines",
                {"speaker_id": characters[speaker], "text": text},
            )
            _ensure(report, "e2e.line.create", code, line)

    print("     样例项目：2 场 / 9 台词 / 2 角色（含要素集与立场卡）", flush=True)
    return {
        "project_id": pid,
        "characters": characters,
        "scene1": int(scene1["id"]),
        "scene2": int(scene2["id"]),
    }


def fountain_roundtrip(client, report: Report, sample: dict) -> None:
    pid = sample["project_id"]
    code, exported = client.call(
        "GET", f"{API}/screenplay/projects/{pid}/export/fountain",
        params={"format": "json"},
    )
    _ensure(report, "fountain.export", code, exported)
    text = exported.get("text") or ""
    report.expect(
        "fountain.roundtrip",
        all(token in text for token in ("Title:", "第一场：茶馆对峙", "城南当铺的借据")),
        "Fountain 文本缺少标题页/场次/台词",
    )

    code, imported = client.call(
        "POST", f"{API}/screenplay/projects/{pid}/import/fountain",
        {"text": text, "mode": "merge"},
    )
    _ensure(report, "fountain.import", code, imported)
    report.expect(
        "fountain.roundtrip",
        imported["scenes"]["total"] == 2
        and len(imported["scenes"]["updated"]) == 2
        and len(imported["characters"]["matched"]) >= 2,
        f"往返场次/角色不对应：scenes={imported['scenes']} chars={imported['characters']}",
    )
    preserved = imported.get("metadata_preserved") or {}
    report.expect(
        "fountain.roundtrip",
        preserved.get("stances") == 2 and preserved.get("conflicts") == 2,
        f"Fountain 往返丢失元数据：{preserved}",
    )

    code, tree = client.call("GET", f"{API}/screenplay/projects/{pid}/export")
    _ensure(report, "fountain.roundtrip", code, tree)
    expected = [
        "把账拿出来。",
        "什么账，我听不懂你在说什么。",
        "十年前腊月，城南当铺的借据，还认得吗？",
        "城南当铺的借据，我早就烧了。",
        "烧得掉纸，烧不掉印。",
        "那你就去告。",
        "旧怀表还在走。",
        "表在，人也在。",
        "你终于还是来了。",
    ]
    report.expect(
        "fountain.roundtrip",
        _line_texts(tree) == expected,
        f"往返后台词不一致：{_line_texts(tree)}",
    )
    print("     Fountain 导出→导入：场次/角色/台词/元数据保持对应", flush=True)


def seed_ledger(client, report: Report, sample: dict) -> dict:
    pid = sample["project_id"]
    code, fact_loan = client.call(
        "POST", f"{API}/ledger/projects/{pid}/facts",
        {
            "subject": "沈青梧",
            "predicate": "持有",
            "object": "城南当铺的借据",
            "valid_from_episode": 1,
        },
    )
    _ensure(report, "ledger.fact.create", code, fact_loan)
    code, fact_deed = client.call(
        "POST", f"{API}/ledger/projects/{pid}/facts",
        {
            "subject": "沈青梧",
            "predicate": "持有",
            "object": "田契",
            "valid_from_episode": 2,
        },
    )
    _ensure(report, "ledger.fact.create", code, fact_deed)

    code, timepoint = client.call(
        "GET", f"{API}/ledger/projects/{pid}/facts/timepoint",
        params={"episode": 1},
    )
    _ensure(report, "ledger.timepoint", code, timepoint)
    ids = _ids(timepoint)
    report.expect(
        "ledger.timepoint",
        fact_loan["id"] in ids and fact_deed["id"] not in ids,
        f"第 1 集时点返回了未来事实：{[f['object'] for f in timepoint]}",
    )

    code, known = client.call(
        "POST", f"{API}/ledger/projects/{pid}/knowledge",
        {
            "character_id": str(sample["characters"]["顾行舟"]),
            "fact_id": fact_loan["id"],
            "learned_at_episode": 3,
        },
    )
    _ensure(report, "ledger.knowledge", code, known)
    code, known2 = client.call(
        "POST", f"{API}/ledger/projects/{pid}/knowledge",
        {
            "character_id": str(sample["characters"]["沈青梧"]),
            "fact_id": fact_deed["id"],
            "learned_at_episode": 2,
        },
    )
    _ensure(report, "ledger.knowledge", code, known2)

    code, foreshadow = client.call(
        "POST", f"{API}/ledger/projects/{pid}/foreshadows",
        {
            "hint_text": "旧怀表",
            "planted_in_episode": 1,
            "planned_resolution_episode": 1,
        },
    )
    _ensure(report, "ledger.foreshadow", code, foreshadow)
    code, overdue = client.call(
        "GET", f"{API}/ledger/projects/{pid}/foreshadows/overdue",
        params={"episode": 2},
    )
    _ensure(report, "ledger.overdue", code, overdue)
    report.expect(
        "ledger.overdue",
        any(item["id"] == foreshadow["id"] for item in overdue),
        "逾期伏笔未提示",
    )

    code, status = client.call("GET", f"{API}/ledger/projects/{pid}/status")
    _ensure(report, "ledger.knowledge", code, status)
    report.expect(
        "ledger.knowledge",
        status["available"] is True and status["counts"]["facts"] == 2,
        f"台账状态异常：{status}",
    )

    code, context = client.call(
        "GET", f"{API}/ledger/projects/{pid}/context",
        params={
            "episode": 1,
            "character_id": [
                str(sample["characters"]["沈青梧"]),
                str(sample["characters"]["顾行舟"]),
            ],
        },
    )
    _ensure(report, "ledger.precontext", code, context)
    context_fact_ids = _ids(context.get("facts") or [])
    characters = {c["character_name"]: c for c in context.get("characters") or []}
    report.expect(
        "ledger.precontext",
        fact_loan["id"] in context_fact_ids
        and fact_deed["id"] not in context_fact_ids
        and "田契" not in (context.get("text") or ""),
        f"生成前上下文包含未来事实：{context.get('text')}",
    )
    report.expect(
        "ledger.precontext",
        characters.get("顾行舟", {}).get("future_knowledge_count") == 1,
        f"角色未来知识计数异常：{characters.get('顾行舟')}",
    )
    print("     台账：双时态事实 / 角色知识隔离 / 伏笔逾期 / 生成前上下文", flush=True)
    return {
        "fact_loan": fact_loan["id"],
        "fact_deed": fact_deed["id"],
        "foreshadow": foreshadow["id"],
    }


def storyboard_step(client, report: Report, sample: dict) -> dict:
    scene1 = sample["scene1"]
    code, board1 = client.call(
        "POST", f"{API}/pipeline/storyboard/{scene1}", params={"episode": 1},
    )
    _ensure(report, "storyboard.generate", code, board1)
    shots = board1.get("shots") or []
    report.expect(
        "storyboard.generate",
        board1.get("shot_count") == 8 and len(shots) == 8,
        f"镜头数异常：{board1.get('shot_count')}",
    )
    required = {"staging", "keyframe", "model_sheet_refs", "art_bible_refs"}
    report.expect(
        "storyboard.fields",
        all(required <= set(shot) for shot in shots),
        "扩展字段缺失",
    )

    continuity = board1.get("continuity") or {}
    report.expect(
        "storyboard.precheck",
        continuity.get("available") is True
        and continuity.get("pre", {}).get("available") is True
        and "田契" not in (continuity.get("pre", {}).get("text") or ""),
        f"生成前上下文未接入台账：{str(continuity)[:300]}",
    )
    post = continuity.get("post") or {}
    violations = post.get("violations") or []
    knowledge = [v for v in violations if v.get("type") == "knowledge_violation"]
    report.expect(
        "storyboard.postcheck_block",
        post.get("blocked") is True
        and any(v.get("matched") == "城南当铺的借据" for v in knowledge),
        f"生成后越知未拦截：{violations}",
    )
    report.expect(
        "storyboard.postcheck_block",
        bool(knowledge) and knowledge[0].get("line_idx") is not None,
        f"越知未定位到台词：{knowledge}",
    )

    # 场景 2 在第 2 集：无越知，但伏笔逾期以 warn 呈现（不阻断）
    code, board2 = client.call(
        "POST", f"{API}/pipeline/storyboard/{sample['scene2']}", params={"episode": 2},
    )
    _ensure(report, "storyboard.generate", code, board2)
    post2 = (board2.get("continuity") or {}).get("post") or {}
    report.expect(
        "storyboard.postcheck_block",
        post2.get("blocked") is False
        and any(v.get("type") == "foreshadow_overdue" for v in post2.get("violations") or []),
        f"伏笔逾期未以提示呈现：{post2}",
    )

    # 人在环上编辑扩展字段并复读
    code, updated = client.call(
        "PUT", f"{API}/pipeline/storyboard/{scene1}/4",
        {
            "staging": "俯视图：顾行舟在画面右侧，沈青梧在左侧，二人沿桌沿对峙",
            "keyframe": "关键帧：顾行舟说完『烧了』后的半秒静默，手指压住桌角",
            "model_sheet_refs": [str(sample["characters"]["顾行舟"])],
            "art_bible_refs": [str(scene1)],
        },
    )
    _ensure(report, "storyboard.edit", code, updated)
    code, fetched = client.call("GET", f"{API}/pipeline/storyboard/{scene1}")
    _ensure(report, "storyboard.edit", code, fetched)
    shot4 = next((s for s in fetched["shots"] if s["idx"] == 4), None)
    report.expect(
        "storyboard.edit",
        shot4 is not None
        and shot4.get("staging", "").startswith("俯视图")
        and shot4.get("model_sheet_refs") == [str(sample["characters"]["顾行舟"])],
        f"分镜编辑未持久化：{shot4}",
    )

    code, prompts = client.call("POST", f"{API}/pipeline/prompts/{scene1}")
    _ensure(report, "storyboard.prompts", code, prompts)
    forms = {item["form"] for item in prompts.get("promptsets") or []}
    report.expect(
        "storyboard.prompts",
        forms == {"image", "video", "narrative"},
        f"三形态提示词缺失：{forms}",
    )
    image_promptset = next(
        item for item in prompts["promptsets"] if item["form"] == "image"
    )
    code, payload = client.call(
        "GET", f"{API}/pipeline/prompts/{image_promptset['id']}"
    )
    _ensure(report, "storyboard.prompts", code, payload)
    image_text = json.dumps(payload.get("payload") or {}, ensure_ascii=False)
    report.expect(
        "storyboard.prompts",
        "月白旗袍" in image_text and "走位：" in image_text and "关键帧：" in image_text,
        "提示词未注入角色要素/走位/关键帧",
    )
    print("     分镜：扩展字段 / 生成前上下文 / 生成后拦截 / 逐镜编辑 / 三形态提示词", flush=True)
    return {"board1": board1, "prompts": prompts, "image_promptset": image_promptset}


def quality_step(client, report: Report, sample: dict, ledger_ids: dict) -> dict:
    code, rubric = client.call("GET", f"{API}/quality/rubric")
    _ensure(report, "quality.rubric", code, rubric)
    dimensions = [item["key"] for item in rubric.get("dimensions") or []]
    report.expect(
        "quality.rubric",
        dimensions == ["rhythm", "emotional_payoff", "dialogue", "format",
                       "continuity", "compliance"]
        and all(item.get("rules") for item in rubric["dimensions"]),
        f"六维 rubric 不完整：{dimensions}",
    )

    code, assessment = client.call(
        "POST", f"{API}/quality/projects/{sample['project_id']}/assess",
        {"episode": 1},
    )
    _ensure(report, "quality.assess", code, assessment)
    dims = assessment.get("dimensions") or {}
    report.expect(
        "quality.dimensions",
        set(dims) == set(dimensions),
        f"评估维度不齐：{sorted(dims)}",
    )
    report.expect(
        "quality.dimensions",
        all(dim.get("rule_results") is not None and "problems" in dim
            for dim in dims.values()),
        "维度缺少规则执行结果或问题清单（分数不可复核）",
    )

    continuity = dims["continuity"]
    report.expect(
        "quality.continuity_located",
        continuity.get("available") is True and continuity.get("score") is not None,
        f"连续性维不可用：{continuity.get('reason')}",
    )
    knowledge_problems = [
        p for p in continuity.get("problems") or []
        if p.get("rule_id") == "continuity.knowledge_timeline"
    ]
    report.expect(
        "quality.continuity_located",
        bool(knowledge_problems)
        and knowledge_problems[0]["location"]["scene_id"] == sample["scene1"]
        and knowledge_problems[0]["location"]["line_idx"] == 4,
        f"越知问题未定位到具体场次/台词：{knowledge_problems[:1]}",
    )
    refs = knowledge_problems[0].get("ledger_refs") or []
    report.expect(
        "quality.evidence",
        any(ref.get("kind") == "fact" and ref.get("fact_id") == ledger_ids["fact_loan"]
            for ref in refs),
        f"问题项未引用台账冲突事实：{refs}",
    )
    report.expect(
        "quality.evidence",
        "沈青梧|持有|城南当铺的借据" in (knowledge_problems[0].get("evidence") or ""),
        "问题依据缺少可读台账事实",
    )
    report.expect(
        "quality.assess",
        assessment.get("blocked") is False and assessment.get("overall_score") is not None,
        "正常剧本不应被质量门阻断",
    )

    # 合规红线：独立项目，命中即阻断并逐条列出
    code, red_project = client.call(
        "POST", f"{API}/screenplay/projects",
        {"title": "红线样例", "logline": "仅用于合规红线验收"},
    )
    _ensure(report, "quality.redline_block", code, red_project)
    red_pid = int(red_project["id"])
    code, red_character = client.call(
        "POST", f"{API}/screenplay/projects/{red_pid}/characters",
        {"name": "样例角色", "appearance": {"年龄感": "未知", "着装": "便装"}},
    )
    _ensure(report, "quality.redline_block", code, red_character)
    code, red_scene = client.call(
        "POST", f"{API}/screenplay/projects/{red_pid}/scenes",
        {"title": "第一场：计划", "synopsis": "红线检测样例",
         "conflict": {"positions": ["阻止", "实施"], "source": "测试"}},
    )
    _ensure(report, "quality.redline_block", code, red_scene)
    code, _ = client.call(
        "POST", f"{API}/screenplay/scenes/{red_scene['id']}/lines",
        {"speaker_id": red_character["id"], "text": "制造炸弹的步骤我全知道。"},
    )
    _ensure(report, "quality.redline_block", code, _)
    code, red_assessment = client.call(
        "POST", f"{API}/quality/projects/{red_pid}/assess", {"episode": 1},
    )
    _ensure(report, "quality.redline_block", code, red_assessment)
    violations = red_assessment.get("violation_details") or []
    report.expect(
        "quality.redline_block",
        red_assessment.get("blocked") is True
        and red_assessment.get("redline_hit") is True
        and violations
        and violations[0].get("rule_id") == "compliance.violence_instruction",
        f"合规红线未阻断：{red_assessment.get('block_reasons')}",
    )
    report.expect(
        "quality.redline_block",
        violations[0]["location"].get("scene_id") == int(red_scene["id"]),
        "红线命中项未定位到场次",
    )
    code, delivery = client.call(
        "POST", f"{API}/quality/projects/{red_pid}/deliver", {"episode": 1}
    )
    _ensure(report, "quality.deliver_409", code, delivery, allowed=(409,))
    report.expect(
        "quality.deliver_409",
        delivery.get("delivered") is False and delivery.get("violations"),
        f"交付门未阻断：{delivery}",
    )
    print("     质量门：六维分列 / 问题定位 / 台账事实引用 / 红线阻断与交付 409", flush=True)
    return {"assessment": assessment, "red": red_assessment}


def export_step(client, report: Report, sample: dict, storyboard: dict) -> None:
    pid = sample["project_id"]
    code, tree = client.call("GET", f"{API}/screenplay/projects/{pid}/export")
    _ensure(report, "export.project", code, tree)
    report.expect(
        "export.project",
        len(tree.get("scenes") or []) == 2 and len(tree.get("characters") or []) == 2,
        "项目导出结构不完整",
    )

    code, fountain = client.call(
        "GET", f"{API}/screenplay/projects/{pid}/export/fountain",
        params={"format": "json"},
    )
    _ensure(report, "export.fountain", code, fountain)
    report.expect(
        "export.fountain",
        fountain.get("format") == "fountain" and "Title:" in (fountain.get("text") or ""),
        "Fountain 导出体异常",
    )

    code, fdx = client.call(
        "GET", f"{API}/screenplay/projects/{pid}/export/fdx",
        params={"format": "json"},
    )
    _ensure(report, "export.fdx", code, fdx)
    report.expect(
        "export.fdx",
        fdx.get("format") == "fdx" and "<?xml" in (fdx.get("text") or ""),
        "FDX 导出体异常",
    )

    code, board_export = client.call(
        "GET", f"{API}/pipeline/storyboard/{sample['scene1']}/export"
    )
    _ensure(report, "export.storyboard", code, board_export)
    payload = json.loads(board_export) if isinstance(board_export, str) else board_export
    report.expect(
        "export.storyboard",
        payload.get("shot_count") == 8
        and all({"staging", "keyframe", "model_sheet_refs", "art_bible_refs"} <= set(s)
                for s in payload.get("shots") or []),
        "镜头表导出丢失扩展字段",
    )

    promptset_id = storyboard["image_promptset"]["id"]
    code, prompt_export = client.call(
        "GET", f"{API}/pipeline/prompts/{promptset_id}/export"
    )
    _ensure(report, "export.promptset", code, prompt_export)
    prompt_payload = (
        json.loads(prompt_export) if isinstance(prompt_export, str) else prompt_export
    )
    report.expect(
        "export.promptset",
        prompt_payload.get("form") == "image"
        and "月白旗袍" in json.dumps(prompt_payload, ensure_ascii=False),
        "提示词导出体异常",
    )
    print("     导出：项目 JSON / Fountain / FDX / 镜头表 / 提示词集", flush=True)


# ---------------------------------------------------------------- 5.2 旧剧本回归


def legacy_regression(client, report: Report, offline: bool) -> None:
    code, project = client.call(
        "POST", f"{API}/screenplay/projects",
        {"title": "旧剧本（无台账）", "logline": "回归样本"},
    )
    _ensure(report, "legacy.create", code, project)
    pid = int(project["id"])
    code, character = client.call(
        "POST", f"{API}/screenplay/projects/{pid}/characters",
        {"name": "老角色", "appearance": {"年龄感": "中年", "着装": "旧棉袄"}},
    )
    _ensure(report, "legacy.create", code, character)
    # 旧剧本没有 synopsis/action/transition/conflict —— 只保留标题与台词
    code, scene = client.call(
        "POST", f"{API}/screenplay/projects/{pid}/scenes", {"title": "旧戏"}
    )
    _ensure(report, "legacy.create", code, scene)
    code, _ = client.call(
        "POST", f"{API}/screenplay/scenes/{scene['id']}/lines",
        {"speaker_id": character["id"], "text": "旧戏照旧唱。"},
    )
    _ensure(report, "legacy.create", code, _)

    code, status = client.call("GET", f"{API}/ledger/projects/{pid}/status")
    _ensure(report, "legacy.ledger.empty", code, status)
    report.expect(
        "legacy.ledger.empty",
        status.get("available") is False and status.get("reason"),
        f"空台账未明示不可用：{status}",
    )

    code, board = client.call("POST", f"{API}/pipeline/storyboard/{scene['id']}")
    _ensure(report, "legacy.storyboard.ok", code, board)
    continuity = board.get("continuity") or {}
    report.expect(
        "legacy.storyboard.degraded",
        continuity.get("available") is False and continuity.get("reason"),
        f"无台账时生成未降级明示：{continuity}",
    )
    report.expect(
        "legacy.storyboard.ok",
        board.get("shot_count") == 3
        and all({"staging", "keyframe", "model_sheet_refs", "art_bible_refs"} <= set(s)
                for s in board.get("shots") or []),
        "旧剧本分镜未生成或字段缺失",
    )

    code, assessment = client.call(
        "POST", f"{API}/quality/projects/{pid}/assess", {"episode": 1},
    )
    _ensure(report, "legacy.quality.degraded", code, assessment)
    legacy_continuity = assessment["dimensions"]["continuity"]
    report.expect(
        "legacy.quality.degraded",
        legacy_continuity.get("available") is False
        and legacy_continuity.get("score") is None
        and legacy_continuity.get("reason")
        and assessment.get("blocked") is False,
        f"质量门未优雅降级：{legacy_continuity}",
    )
    report.expect(
        "legacy.quality.degraded",
        assessment["dimensions"]["format"].get("score") is not None,
        "其余维度未照常评分",
    )

    code, fountain = client.call(
        "GET", f"{API}/screenplay/projects/{pid}/export/fountain",
        params={"format": "json"},
    )
    _ensure(report, "legacy.fountain.ok", code, fountain)
    report.expect(
        "legacy.fountain.ok",
        "旧戏" in (fountain.get("text") or ""),
        "旧剧本 Fountain 导出失败",
    )

    if offline:
        # 旧版镜头行：JSON 中完全没有扩展字段，读取侧必须补 None/[] 而非报错
        conn = client.raw_db()
        try:
            conn.execute("DELETE FROM storyboard WHERE scene_id=?", (scene["id"],))
            legacy_shot = {
                "idx": 0,
                "shot_size": "全景",
                "visual": "旧版镜头画面",
                "action": "旧版镜头动作",
                "line_text": None,
                "line_speaker": None,
                "duration_sec": 4,
                "engine": "deterministic",
            }
            conn.execute(
                "INSERT INTO storyboard (scene_id, idx, shot) VALUES (?,?,?)",
                (scene["id"], 0, json.dumps(legacy_shot, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()

        code, legacy_board = client.call(
            "GET", f"{API}/pipeline/storyboard/{scene['id']}"
        )
        _ensure(report, "legacy.storyboard.legacy_row", code, legacy_board)
        shot = legacy_board["shots"][0]
        report.expect(
            "legacy.storyboard.legacy_row",
            shot.get("staging") is None
            and shot.get("keyframe") is None
            and shot.get("model_sheet_refs") == []
            and shot.get("art_bible_refs") == [],
            f"旧行扩展字段未按空值读取：{shot}",
        )

        code, edit = client.call(
            "PUT", f"{API}/pipeline/storyboard/{scene['id']}/0",
            {"staging": "俯视图：旧行补写走位"},
        )
        _ensure(report, "legacy.storyboard.legacy_row", code, edit)
        code, exported = client.call(
            "GET", f"{API}/pipeline/storyboard/{scene['id']}/export"
        )
        _ensure(report, "legacy.storyboard.legacy_export", code, exported)
        exported_payload = json.loads(exported) if isinstance(exported, str) else exported
        report.expect(
            "legacy.storyboard.legacy_export",
            exported_payload["shots"][0].get("staging") == "俯视图：旧行补写走位"
            and "keyframe" in exported_payload["shots"][0],
            "旧行编辑后导出异常",
        )
    else:
        report.check(
            "legacy.storyboard.legacy_row", True,
            "live 模式跳过直接写库的旧行注入（离线用例覆盖）",
        )
        report.check(
            "legacy.storyboard.legacy_export", True,
            "live 模式跳过直接写库的旧行注入（离线用例覆盖）",
        )
    print("     回归：旧剧本无台账/无新字段时创作与导出不阻断，质量门优雅降级", flush=True)


# ---------------------------------------------------------------- 5.3 无哲思模式


def non_zhesi_step(client, report: Report, sample: dict, expect_standalone: bool) -> None:
    code, link = client.call("POST", f"{API}/kernel/link", {})
    _ensure(report, "zhesi.link_standalone", code, link)
    mode = link.get("mode")
    if expect_standalone:
        report.expect(
            "zhesi.link_standalone",
            mode == "standalone" and bool(link.get("reason")),
            f"哲思不可达时未降级：{link}",
        )
    else:
        report.check("zhesi.link_standalone", mode in ("linked", "standalone"),
                     f"mode={mode}")

    # 台账独立：无哲思模式仍可写入与查询
    pid = sample["project_id"]
    code, fact = client.call(
        "POST", f"{API}/ledger/projects/{pid}/facts",
        {
            "subject": "沈青梧",
            "predicate": "保存",
            "object": "旧当票副联",
            "valid_from_episode": 2,
        },
    )
    _ensure(report, "zhesi.ledger.independent", code, fact)
    code, timepoint = client.call(
        "GET", f"{API}/ledger/projects/{pid}/facts/timepoint",
        params={"episode": 2},
    )
    _ensure(report, "zhesi.ledger.independent", code, timepoint)
    report.expect(
        "zhesi.ledger.independent",
        any(item["object"] == "旧当票副联" for item in timepoint),
        "无哲思模式下台账写入/查询失败",
    )

    # 质量门独立：无哲思模式仍可评估，且连续性维可用
    code, assessment = client.call(
        "POST", f"{API}/quality/projects/{pid}/assess", {"episode": 2},
    )
    _ensure(report, "zhesi.quality.independent", code, assessment)
    report.expect(
        "zhesi.quality.independent",
        assessment["dimensions"]["continuity"].get("available") is True
        and assessment["overall_score"] is not None,
        f"无哲思模式下质量门不可用：{assessment['dimensions']['continuity']}",
    )
    print(f"     无哲思模式（mode={mode}）：台账与质量门独立可用", flush=True)


# ---------------------------------------------------------------- 入口


def _prepare_offline_db(db_path: str | None) -> tuple[str, tempfile.TemporaryDirectory | None]:
    if db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return db_path, None
    temp = tempfile.TemporaryDirectory(prefix="juchuang-e2e-")
    return str(Path(temp.name) / "e2e.db"), temp


def run(client, report: Report, offline: bool, expect_standalone: bool) -> None:
    sample = create_sample_project(client, report)
    fountain_roundtrip(client, report, sample)
    ledger_ids = seed_ledger(client, report, sample)
    storyboard = storyboard_step(client, report, sample)
    quality_step(client, report, sample, ledger_ids)
    export_step(client, report, sample, storyboard)
    # 5.2 旧剧本回归
    legacy_regression(client, report, offline=offline)
    # 5.3 无哲思模式（放在最后：先证明完整链路，再证明降级路径）
    non_zhesi_step(client, report, sample, expect_standalone=expect_standalone)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="剧创端到端验收（Fountain/台账/分镜/质量门/降级）"
    )
    parser.add_argument("--base", help="已部署服务地址；省略则离线 TestClient 模式")
    parser.add_argument("--token", help="live 模式用户令牌（Bearer）")
    parser.add_argument("--db", help="离线模式 SQLite 路径（默认临时文件）")
    parser.add_argument("--json-report", help="把断言清单写入 JSON 文件")
    parser.add_argument(
        "--expect-standalone", action="store_true",
        help="断言哲思不可达（离线模式默认开启）",
    )
    parser.add_argument(
        "--keep-db", action="store_true", help="离线模式保留临时数据库用于排查",
    )
    args = parser.parse_args(argv)

    offline = not args.base
    if not offline and not args.token:
        parser.error("--base 需要同时提供 --token")

    temp: tempfile.TemporaryDirectory | None = None
    db_path: str | None = None
    if offline:
        db_path, temp = _prepare_offline_db(args.db)

    report = Report()
    started = time.time()
    error: str | None = None
    try:
        if offline:
            assert db_path is not None
            client = OfflineClient(db_path)
        else:
            client = LiveClient(args.base, args.token)
        run(
            client, report, offline=offline,
            expect_standalone=args.expect_standalone or offline,
        )
    except E2EFailure as exc:
        error = f"首个失败断言：{exc.check_id}" + (f" — {exc.detail}" if exc.detail else "")
    except Exception as exc:  # noqa: BLE001 —— 脚本要把异常收敛成退出码
        error = f"{type(exc).__name__}: {exc}"
    finally:
        duration = round(time.time() - started, 2)
        passed = sum(1 for c in report.checks if c["ok"])
        failed = report.failed
        print("", flush=True)
        print(
            f"E2E {'PASS' if not failed and error is None else 'FAIL'}: "
            f"{passed}/{len(report.checks)} 断言通过，耗时 {duration}s",
            flush=True,
        )
        if db_path:
            print(f"数据库：{db_path}", flush=True)
        if error:
            print(f"错误：{error}", flush=True)
        if args.json_report:
            Path(args.json_report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_report).write_text(
                json.dumps(
                    {
                        "mode": "offline" if offline else "live",
                        "db": db_path,
                        "duration_sec": duration,
                        "passed": not failed and error is None,
                        "checks": report.checks,
                        "error": error,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        if temp is not None and not args.keep_db:
            temp.cleanup()

    return 0 if not failed and error is None else 1


if __name__ == "__main__":
    sys.exit(main())
