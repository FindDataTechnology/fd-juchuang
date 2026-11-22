"""Fountain / FDX 交换层（change juchuang-continuity-layer task 1.1-1.6）。

设计原则（design D1「结构化 JSON 为真源，Fountain 为交换投影」）：

- 结构化剧本树（``screenplay._project_tree`` 的形状）是唯一真源；
- Fountain / FDX 是投影：导出可能丢字段，导入按名/序合并；
- Fountain 不承载的元数据（角色 stance、场次 conflict、分镜引用、版本树）
  永远留在结构化层，导入不得覆盖；
- 解析器无法映射的元素进入 ``unrecognized`` 清单，绝不静默丢弃。

本模块只做纯格式转换，不碰数据库；落库合并在 ``screenplay.py`` 完成。

Fountain 语法覆盖（对齐 fountain-js 的行为）：
场景标题、动作、角色 cue、括号说明、对白、转场、标题页、synopsis（``=``）、
注释（``[[ ]]``）、章节（``#``）、居中（``>…<``）、歌词（``~``）、分页（``===``）、
boneyard（``/* */``）、双人对白（``^``）、场景编号（``#1#``）。
其中结构化模型无对应字段的元素会进入 ``unrecognized`` 报告。

FDX（Final Draft XML）为有损交换格式：场次/角色/对白/转场可映射，
角色要素集、立场、场次 synopsis/conflict、内部主键不可映射，
导出返回 ``warnings``，往返差异由 :func:`diff_tree_vs_parsed` 逐项列出。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 语法常量

#: Fountain 标题页标准键（小写归一；空格转下划线）
_TITLE_KEYS = {
    "title", "credit", "author", "authors", "source", "draft_date", "date",
    "contact", "copyright", "notes", "logline", "revision", "header",
}

_TITLE_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]{0,30}):\s*(.*)$")
_SCENE_HEADING_RE = re.compile(
    r"^(?:INT|EXT|EST|INT\.?/EXT\.?|EXT\.?/INT\.?|I/E)[\.\s]", re.IGNORECASE
)
_SCENE_NUMBER_RE = re.compile(r"\s*#([^#]+)#\s*$")
_PAREN_RE = re.compile(r"^\((.+)\)$")
_CHAR_EXT_RE = re.compile(
    r"\s*\((V\.?O\.?|O\.?S\.?|CONT'D|CONTINUED|OFF SCREEN|PRE-?LAP|FILTERED|MUFFLED)\)\s*$",
    re.IGNORECASE,
)
_NOTES_RE = re.compile(r"\[\[(.*?)\]\]")
_PAGE_BREAK_RE = re.compile(r"^={3,}\s*$")
_CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


# ---------------------------------------------------------------- 通用工具


def _norm_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm_compare(value: Any) -> str:
    """比较用归一：去首尾空白并把连续空白压成单空格（保留文本语义）。"""
    return " ".join(_norm_text(value).split())


def _scene_heading_parts(text: str) -> tuple[str, str | None] | None:
    """识别场景标题，返回 (标题文本, 场景编号)；非场景标题返回 None。"""
    s = text.strip()
    if s.startswith(".") and not s.startswith(".."):
        body = s[1:].strip()
    elif _SCENE_HEADING_RE.match(s):
        body = s
    else:
        return None
    m = _SCENE_NUMBER_RE.search(body)
    if m:
        return body[:m.start()].strip(), m.group(1).strip()
    return body, None


def _is_transition_text(text: str) -> bool:
    """Fountain 转场：``>`` 强制，或大写且以 TO: / FADE … 结尾。"""
    s = text.strip()
    if not s:
        return False
    if s.startswith(">"):
        # ``>居中文本<`` 是居中元素，不是转场
        return not (s.endswith("<") and len(s) > 2)
    upper = s.upper()
    if not any(ch.isalpha() for ch in s):
        return False
    if upper.endswith("TO:"):
        return True
    return bool(re.match(
        r"^(FADE (IN|OUT)|FADE TO BLACK|CUT TO BLACK|SMASH CUT|MATCH CUT|DISSOLVE|WIPE|IRIS)",
        upper,
    )) and upper.endswith((".", ":"))


def _looks_like_block_starter(text: str) -> bool:
    """行是否开启一个新的 Fountain 块（用于动作段落/角色 cue 的前瞻）。"""
    s = text.strip()
    if not s:
        return False
    if s.startswith("/*") or _PAGE_BREAK_RE.match(s):
        return True
    if s.startswith("#") or s.startswith("=") or s.startswith("~"):
        return True
    if _scene_heading_parts(s) is not None:
        return True
    if re.match(r"^>.*<$", s):
        return True
    if _is_transition_text(s):
        return True
    return False


def _heading_for(title: str) -> str:
    """把内部场次标题写成 Fountain 场景标题（必要时用 ``.`` 强制）。"""
    t = title.strip()
    if not t:
        return "."
    if _SCENE_HEADING_RE.match(t):
        return t
    if t.startswith("."):
        # ``..`` 会被解析为转义动作；用 ``. `` 保留标题里的前导点
        return ". " + t
    return "." + t


def _append_paragraph(existing: str | None, text: str) -> str | None:
    t = text.strip()
    if not t:
        return existing
    return f"{existing}\n\n{t}" if existing else t


# ---------------------------------------------------------------- task 1.1 映射表


class FountainFieldMapping:
    """内部结构化模型 ↔ Fountain 的字段映射表。

    ``MAPPED`` 覆盖全部可交换字段；``NOT_CARRIED`` 明确列出 Fountain 无法承载、
    必须留在结构化层的字段；``FOUNTAIN_ONLY`` 列出 Fountain 有而内部模型无的
    元素（导入时进入 ``unrecognized`` 报告）。
    """

    MAPPED: dict[str, dict[str, str]] = {
        "project.title": {
            "fountain": "Title Page: Title",
            "note": "项目标题",
        },
        "project.logline": {
            "fountain": "Title Page: Logline",
            "note": "标题页自定义键（Fountain 标题页允许任意键）",
        },
        "characters.name": {
            "fountain": "Character cue（导出统一 @姓名 强制 cue）",
            "note": "导入按角色名匹配，已有角色卡不覆盖",
        },
        "scenes.idx": {
            "fountain": "场次顺序",
            "note": "导出按 idx 排序；导入按顺序还原",
        },
        "scenes.title": {
            "fountain": "Scene Heading（INT./EXT. 原样，否则 .强制标题）",
            "note": "场景标题",
        },
        "scenes.synopsis": {
            "fountain": "Synopsis（= 行）",
            "note": "场次提要",
        },
        "scenes.action": {
            "fountain": "Action",
            "note": "动作/描述段落（保留段落间空行）",
        },
        "scenes.transition": {
            "fountain": "Transition（> 强制转场）",
            "note": "场次末尾转场；多行逐行写出",
        },
        "lines.idx": {
            "fountain": "对白顺序",
            "note": "导入按出现顺序重建 idx",
        },
        "lines.speaker_id": {
            "fountain": "Character cue",
            "note": "经角色名解析为 speaker_id",
        },
        "lines.speaker_name": {
            "fountain": "Character cue",
            "note": "导出时写角色名（@姓名）",
        },
        "lines.text": {
            "fountain": "Dialogue",
            "note": "对白文本",
        },
        "lines.parenthetical": {
            "fountain": "Parenthetical",
            "note": "括号说明",
        },
    }

    NOT_CARRIED: dict[str, str] = {
        "project.id": "内部主键；导出不写，导入不覆盖",
        "project.user_id": "归属信息；不导出",
        "project.created_at": "时间戳；不导出",
        "characters.id": "内部主键；导入按姓名匹配",
        "characters.project_id": "归属信息；不导出",
        "characters.appearance": "角色要素集；Fountain 无对应元素，保留在结构化层",
        "characters.stance": "哲思立场标注；Fountain 不承载，导入不覆盖",
        "characters.created_at": "时间戳；不导出",
        # change juchuang-image-iteration：生成侧资产绑定。Fountain 是剧本交换格式，
        # 资产 id 属于生成面，跨格式携带会变成另一格式凭空多出的字段。
        "characters.asset_id": "生成侧资产绑定（model sheet）；Fountain 不承载",
        "characters.asset_slug": "生成侧资产绑定（可读 slug）；Fountain 不承载",
        "scenes.id": "内部主键；导入按标题/顺序匹配",
        "scenes.project_id": "归属信息；不导出",
        "scenes.conflict": "场次张力标注；Fountain 不承载，导入不覆盖",
        "scenes.created_at": "时间戳；不导出",
        "scenes.asset_id": "生成侧资产绑定（场景 art bible）；Fountain 不承载",
        "scenes.asset_slug": "生成侧资产绑定（可读 slug）；Fountain 不承载",
        "lines.id": "内部主键；导入重建",
        "lines.scene_id": "归属信息；不导出",
        "versions": "版本树不进入交换格式",
        "storyboard": "分镜引用（pipeline.storyboard）；不导出，导入按场次 id 保留",
    }

    FOUNTAIN_ONLY: dict[str, str] = {
        "title_page.author/credit/source/draft date/contact": "标题页附加字段，导入列入未识别清单",
        "section": "# 章节标记",
        "note": "[[ 注释 ]]",
        "centered": "> 居中文本 <",
        "lyrics": "~ 歌词",
        "page_break": "=== 分页符",
        "boneyard": "/* 废弃块 */",
        "dual_dialogue": "^ 双人对白标记",
        "scene_number": "#1# 场景编号（导入映射为场次顺序，不单独落库）",
    }

    @classmethod
    def field_names(cls, project_tree: dict) -> set[str]:
        """枚举剧本树中出现的全部字段路径（不含容器键 ``lines``）。"""
        fields: set[str] = set()
        for key in (project_tree.get("project") or {}):
            fields.add(f"project.{key}")
        for character in project_tree.get("characters") or []:
            for key in character:
                fields.add(f"characters.{key}")
        for scene in project_tree.get("scenes") or []:
            for key in scene:
                if key != "lines":
                    fields.add(f"scenes.{key}")
            for line in scene.get("lines") or []:
                for key in line:
                    fields.add(f"lines.{key}")
        return fields

    @classmethod
    def uncovered_fields(cls, project_tree: dict) -> list[str]:
        """返回既未映射也未列入 NOT_CARRIED 的字段（task 1.1 的覆盖校验）。"""
        known = set(cls.MAPPED) | set(cls.NOT_CARRIED)
        return sorted(cls.field_names(project_tree) - known)

    @classmethod
    def as_dict(cls) -> dict:
        return {
            "mapped": dict(cls.MAPPED),
            "not_carried": dict(cls.NOT_CARRIED),
            "fountain_only": dict(cls.FOUNTAIN_ONLY),
            "counts": {
                "mapped": len(cls.MAPPED),
                "not_carried": len(cls.NOT_CARRIED),
                "fountain_only": len(cls.FOUNTAIN_ONLY),
            },
        }


# ---------------------------------------------------------------- task 1.4 未识别报告


class ReportUnrecognizedElements:
    """收集解析时无法映射到结构化模型的元素。

    条目形状：``{"kind", "text", "line", "reason"}``（可带额外字段）。
    解析器所有「识别但无对应字段」的分支都必须调用 :meth:`add`，
    以保证 task 1.4 的「列出而非静默丢弃」。
    """

    def __init__(self) -> None:
        self._items: list[dict] = []

    def add(self, kind: str, text: Any, line: int | None = None,
            reason: str | None = None, **extra: Any) -> None:
        item = {
            "kind": kind,
            "text": "" if text is None else str(text),
            "line": line,
            "reason": reason or "结构化模型无对应字段",
        }
        item.update(extra)
        self._items.append(item)

    def extend(self, items: list[dict]) -> None:
        self._items.extend(dict(item) for item in items)

    def as_list(self) -> list[dict]:
        return [dict(item) for item in self._items]

    def summary(self) -> dict[str, int]:
        return dict(Counter(item["kind"] for item in self._items))

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)


# ---------------------------------------------------------------- 导出结果


@dataclass
class FountainExport:
    text: str
    warnings: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "format": "fountain",
            "text": self.text,
            "warnings": self.warnings,
            "mapping": FountainFieldMapping.as_dict(),
        }


@dataclass
class FDXExport:
    text: str
    warnings: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "format": "fdx",
            "text": self.text,
            "warnings": self.warnings,
            "mapping": FountainFieldMapping.as_dict(),
        }


# ---------------------------------------------------------------- task 1.2 Fountain 序列化


def serialize_to_fountain(project_tree: dict) -> str:
    """把结构化剧本树投影为 Fountain 文本。"""
    return serialize_with_report(project_tree).text


def serialize_with_report(project_tree: dict) -> FountainExport:
    """导出 Fountain，并返回有损项警告（无说话人、空行对白等）。"""
    warnings: list[dict] = []
    project = project_tree.get("project") or {}
    title = _norm_text(project.get("title"))
    logline = _norm_text(project.get("logline"))

    title_page = [f"Title: {title}"]
    if logline:
        title_page.append(f"Logline: {logline}")
    document_blocks: list[str] = ["\n".join(title_page)]

    for scene in project_tree.get("scenes") or []:
        scene_blocks: list[str] = []

        scene_title = _norm_text(scene.get("title"))
        if scene_title.startswith("."):
            warnings.append({
                "field": "scenes[].title",
                "reason": "标题以 . 开头；导出用 '. ' 转义，往返后标题保持",
                "text": scene_title,
            })
        # 场景标题先行（Fountain 标准布局），synopsis/action 随后归属本场
        scene_blocks.append(_heading_for(scene_title))

        synopsis = _norm_text(scene.get("synopsis"))
        if synopsis:
            # 连续 = 行构成一个提要块，避免与下一场的提要混淆
            scene_blocks.append("\n".join(
                f"= {line.strip()}" for line in synopsis.splitlines()))

        action = _norm_text(scene.get("action"))
        if action:
            for line in action.splitlines():
                if line.strip() and _looks_like_block_starter(line):
                    warnings.append({
                        "field": "scenes[].action",
                        "reason": "动作段落中的行会被 Fountain 解析为独立元素，往返后可能拆块",
                        "text": line.strip(),
                    })
            scene_blocks.append(action)

        for line in scene.get("lines") or []:
            speaker = _norm_text(line.get("speaker_name"))
            text = _norm_text(line.get("text"))
            if not text:
                if line.get("parenthetical"):
                    warnings.append({
                        "field": "lines[].text",
                        "reason": "只有括号说明没有对白文本，Fountain 无法单独表达",
                        "line_id": line.get("id"),
                    })
                continue
            if not speaker:
                warnings.append({
                    "field": "lines[].speaker_name",
                    "reason": "台词没有说话人，Fountain 无法表达，已跳过",
                    "line_id": line.get("id"),
                })
                continue
            if re.search(r"\n\s*\n", text):
                warnings.append({
                    "field": "lines[].text",
                    "reason": "对白含空行，Fountain 无法表达，导出时压缩为单换行",
                    "line_id": line.get("id"),
                })
                text = re.sub(r"\n\s*\n+", "\n", text)
            for raw_line in text.splitlines():
                if _PAREN_RE.match(raw_line.strip()):
                    warnings.append({
                        "field": "lines[].text",
                        "reason": "对白行形如括号说明，导入时会被解析为 parenthetical",
                        "line_id": line.get("id"),
                    })
                    break

            parenthetical = _norm_text(line.get("parenthetical"))
            cue_lines = ["@" + speaker]
            if parenthetical:
                cue_lines.append(f"({parenthetical})")
            cue_lines.append(text)
            scene_blocks.append("\n".join(cue_lines))

        transition = _norm_text(scene.get("transition"))
        if transition:
            scene_blocks.extend(f">{line.strip()}" for line in transition.splitlines()
                                if line.strip())

        if scene_blocks:
            document_blocks.append("\n\n".join(scene_blocks))

    text = "\n\n".join(block for block in document_blocks if block.strip()) + "\n"
    return FountainExport(text=text, warnings=warnings)


# ---------------------------------------------------------------- task 1.3 Fountain 解析


@dataclass
class _ParsedLine:
    speaker: str
    text: str = ""
    parenthetical: str | None = None
    dual: bool = False
    line_no: int = 0


@dataclass
class _ParsedScene:
    heading: str = ""
    scene_number: str | None = None
    synopsis_parts: list[str] = field(default_factory=list)
    action_paras: list[str] = field(default_factory=list)
    lines: list[_ParsedLine] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    line_no: int = 0


class _FountainParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.title_page: dict[str, str] = {}
        self.scenes: list[_ParsedScene] = []
        self.report = ReportUnrecognizedElements()
        self.warnings: list[dict] = []

    # -- 标题页 --------------------------------------------------------

    def _parse_title_page(self, lines: list[str]) -> int:
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            return i
        first = _TITLE_LINE_RE.match(lines[i].strip())
        if not first or first.group(1).strip().lower().replace(" ", "_") not in _TITLE_KEYS:
            return 0

        last_key: str | None = None
        while i < len(lines) and lines[i].strip():
            line = lines[i].strip()
            match = _TITLE_LINE_RE.match(line)
            key = (match.group(1).strip().lower().replace(" ", "_")
                   if match else None)
            if match and key in _TITLE_KEYS:
                self.title_page[key] = match.group(2).strip()
                last_key = key
            elif last_key:
                self.title_page[last_key] = (
                    self.title_page[last_key] + "\n" + line
                ).strip()
            else:
                self.report.add("title_page_field", line, i + 1,
                                "标题页字段不进入结构化模型")
            i += 1
        return i

    # -- 角色 cue ------------------------------------------------------

    @staticmethod
    def _clean_character(text: str) -> tuple[str, str | None, bool]:
        core = text.strip()
        if core.startswith("@"):
            core = core[1:].strip()
        dual = core.endswith("^")
        if dual:
            core = core[:-1].rstrip()
        extension = None
        match = _CHAR_EXT_RE.search(core)
        if match:
            extension = match.group(0).strip()
            core = core[:match.start()].strip()
        return core, extension, dual

    @staticmethod
    def _looks_like_name(core: str) -> bool:
        if not core or len(core) > 60:
            return False
        if not any(ch.isalpha() for ch in core):
            return False
        if any(ch.islower() for ch in core):
            return False
        if core.endswith(("。", "！", "？", "；", "，", ".", "!", "?", ";", ",")):
            return False
        # CJK 无大小写，用长度限制降低把动作行误判为角色 cue 的概率
        if _CJK_RE.search(core) and len(core) > 12:
            return False
        return True

    def _is_character_cue(self, text: str, lines: list[str], i: int) -> bool:
        s = text.strip()
        forced = s.startswith("@")
        core, _extension, _dual = self._clean_character(s)
        if not core:
            return False
        if not forced and not self._looks_like_name(core):
            return False
        j = i + 1
        if j >= len(lines) or not lines[j].strip():
            return False
        return not _looks_like_block_starter(lines[j].strip())

    # -- 主解析 --------------------------------------------------------

    @staticmethod
    def _add_action(scene: _ParsedScene, text: str) -> None:
        clean = text.strip()
        if clean:
            scene.action_paras.append(clean)

    def parse(self) -> dict:
        lines = self.text.splitlines()
        i = self._parse_title_page(lines)
        for key, value in self.title_page.items():
            if key not in ("title", "logline"):
                self.report.add("title_page_field", f"{key}: {value}", None,
                                "标题页字段不进入结构化模型")
        if not self.title_page:
            self.warnings.append({
                "field": "project.title",
                "reason": "未检测到 Fountain 标题页，导入不会修改项目标题",
            })

        current_scene: _ParsedScene | None = None
        current_line: _ParsedLine | None = None
        pending_synopsis: list[str] = []
        scene_has_content = False
        in_dialogue = False

        def ensure_scene(line_no: int) -> _ParsedScene:
            nonlocal current_scene
            if current_scene is None:
                current_scene = _ParsedScene(heading="", line_no=line_no)
                self.scenes.append(current_scene)
                self.warnings.append({
                    "field": "scenes[].title",
                    "reason": "内容出现在首个场次标题之前，已放入无名场次",
                    "line": line_no,
                })
            return current_scene

        while i < len(lines):
            raw = lines[i]
            s = raw.strip()
            line_no = i + 1

            if not s:
                in_dialogue = False
                current_line = None
                i += 1
                continue

            # boneyard（可跨行）
            if s.startswith("/*"):
                buffer = [raw]
                while "*/" not in buffer[-1] and i + 1 < len(lines):
                    i += 1
                    buffer.append(lines[i])
                self.report.add("boneyard", "\n".join(buffer), line_no,
                                "Fountain 废弃块（/* */）不进入结构化模型")
                in_dialogue = False
                current_line = None
                i += 1
                continue

            if _PAGE_BREAK_RE.match(s):
                self.report.add("page_break", s, line_no,
                                "分页符（===）不进入结构化模型")
                in_dialogue = False
                current_line = None
                i += 1
                continue

            # 对白内：直到空行前都属于对白（Fountain 语义）
            if in_dialogue and current_line is not None:
                scene_has_content = True
                clean, notes = self._extract_notes(s)
                for note in notes:
                    self.report.add("note", note, line_no,
                                    "Fountain 注释（[[ ]]）不进入结构化模型")
                if not clean:
                    i += 1
                    continue
                parenthetical = _PAREN_RE.match(clean)
                if parenthetical:
                    if current_line.text:
                        current_line = _ParsedLine(
                            speaker=current_line.speaker, line_no=line_no)
                        ensure_scene(line_no).lines.append(current_line)
                    current_line.parenthetical = parenthetical.group(1).strip()
                else:
                    current_line.text = (
                        current_line.text + "\n" + clean
                        if current_line.text else clean
                    )
                i += 1
                continue

            # 章节
            if s.startswith("#") and not _SCENE_NUMBER_RE.fullmatch(s):
                self.report.add("section", s, line_no,
                                "章节标记（#）不进入结构化模型")
                i += 1
                continue

            # synopsis：连续 = 行组成一个提要块。归属判定：
            # - 文档开头无场次 → 暂存给下一场；
            # - 后面紧跟场景标题，且本场已有内容或已有提要 → 属于下一场；
            # - 否则属于当前场（标题后紧邻的提要、场末提要）。
            if s.startswith("="):
                group = [s[1:].strip()]
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    if nxt.startswith("="):
                        group.append(nxt[1:].strip())
                        j += 1
                        continue
                    if not nxt:
                        k = j + 1
                        while k < len(lines) and not lines[k].strip():
                            k += 1
                        if k < len(lines) and lines[k].strip().startswith("="):
                            j = k
                            continue
                    break
                next_is_heading = (
                    j < len(lines)
                    and _scene_heading_parts(lines[j].strip()) is not None
                )
                if current_scene is None:
                    pending_synopsis.extend(group)
                elif next_is_heading and (scene_has_content
                                         or current_scene.synopsis_parts):
                    pending_synopsis.extend(group)
                else:
                    current_scene.synopsis_parts.extend(group)
                i = j
                continue

            # 场景标题
            heading = _scene_heading_parts(s)
            if heading is not None:
                new_scene = _ParsedScene(
                    heading=heading[0], scene_number=heading[1], line_no=line_no)
                if pending_synopsis:
                    new_scene.synopsis_parts.extend(pending_synopsis)
                    pending_synopsis = []
                self.scenes.append(new_scene)
                current_scene = new_scene
                current_line = None
                scene_has_content = False
                in_dialogue = False
                i += 1
                continue

            # 居中
            centered = re.match(r"^>(.*)<$", s)
            if centered:
                self.report.add("centered", centered.group(1).strip(), line_no,
                                "居中文本（>…<）不进入结构化模型")
                i += 1
                continue

            # 转场
            if _is_transition_text(s):
                ensure_scene(line_no).transitions.append(
                    s[1:].strip() if s.startswith(">") else s)
                scene_has_content = True
                i += 1
                continue

            # 歌词
            if s.startswith("~"):
                self.report.add("lyrics", s[1:].strip(), line_no,
                                "歌词（~）不进入结构化模型")
                i += 1
                continue

            # 行内注释
            clean, notes = self._extract_notes(s)
            for note in notes:
                self.report.add("note", note, line_no,
                                "Fountain 注释（[[ ]]）不进入结构化模型")
            if not clean:
                i += 1
                continue
            s = clean

            # 强制动作
            if s.startswith("!"):
                self._add_action(ensure_scene(line_no), s[1:].strip())
                scene_has_content = True
                i += 1
                continue
            if s.startswith(".."):
                self._add_action(ensure_scene(line_no), s[1:])
                scene_has_content = True
                i += 1
                continue

            # 角色 cue
            if self._is_character_cue(s, lines, i):
                name, extension, dual = self._clean_character(s)
                scene = ensure_scene(line_no)
                current_line = _ParsedLine(
                    speaker=name, dual=dual, line_no=line_no)
                scene.lines.append(current_line)
                scene_has_content = True
                in_dialogue = True
                if extension:
                    self.report.add("character_extension", extension, line_no,
                                    "角色扩展标记（V.O./O.S./CONT'D 等）不进入结构化模型")
                if dual:
                    self.report.add("dual_dialogue", s, line_no,
                                    "双人对白标记（^）不进入结构化模型（对白文本保留）")
                i += 1
                continue

            # 动作段落：连续非空行，遇到空行或新块起始为止
            paragraph = [s]
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt or _looks_like_block_starter(nxt):
                    break
                clean_n, notes_n = self._extract_notes(nxt)
                for note in notes_n:
                    self.report.add("note", note, i + 1,
                                    "Fountain 注释（[[ ]]）不进入结构化模型")
                if clean_n:
                    paragraph.append(clean_n)
                i += 1
            self._add_action(ensure_scene(line_no), "\n".join(paragraph))
            scene_has_content = True

        # 文档末尾仍未归属的 synopsis：挂到最后一场（没有场次则建无名场次）
        if pending_synopsis:
            if current_scene is not None:
                current_scene.synopsis_parts.extend(pending_synopsis)
            else:
                implicit = ensure_scene(1)
                implicit.synopsis_parts.extend(pending_synopsis)

        # 丢弃没有文本的角色 cue，并报告
        for scene in self.scenes:
            kept: list[_ParsedLine] = []
            for line in scene.lines:
                if line.text.strip():
                    kept.append(line)
                else:
                    self.warnings.append({
                        "field": "lines[].text",
                        "reason": "角色 cue 后没有对白文本，已忽略",
                        "speaker": line.speaker,
                        "line": line.line_no,
                    })
            scene.lines = kept

        return self._document()

    @staticmethod
    def _extract_notes(text: str) -> tuple[str, list[str]]:
        notes = [match.group(1).strip() for match in _NOTES_RE.finditer(text)]
        return _NOTES_RE.sub("", text).strip(), notes

    def _document(self) -> dict:
        scenes = []
        for scene in self.scenes:
            scenes.append({
                "heading": scene.heading,
                "scene_number": scene.scene_number,
                "synopsis": "\n".join(p for p in scene.synopsis_parts if p) or None,
                "action": "\n\n".join(p for p in scene.action_paras if p) or None,
                "transition": "\n".join(scene.transitions) or None,
                "transitions": list(scene.transitions),
                "lines": [
                    {
                        "speaker": line.speaker,
                        "parenthetical": line.parenthetical,
                        "text": line.text.strip(),
                    }
                    for line in scene.lines
                ],
            })
        return {
            "format": "fountain",
            "capabilities": ["synopsis", "action", "transition", "parenthetical"],
            "title_page": dict(self.title_page),
            "scenes": scenes,
            "unrecognized": self.report.as_list(),
            "unrecognized_summary": self.report.summary(),
            "warnings": list(self.warnings),
            "stats": {
                "scene_count": len(scenes),
                "line_count": sum(len(scene["lines"]) for scene in scenes),
                "character_count": len({
                    line["speaker"]
                    for scene in scenes for line in scene["lines"]
                }),
                "transition_count": sum(
                    len(scene["transitions"]) for scene in scenes),
            },
        }


def parse_from_fountain(fountain_text: str) -> dict:
    """解析 Fountain 文本为结构化中间表示（不落库）。

    返回 ``title_page`` / ``scenes`` / ``unrecognized`` / ``warnings`` / ``stats``；
    ``scenes[].lines`` 以角色名（而非 id）表示，供 ``screenplay`` 合并时解析。
    """
    return _FountainParser(fountain_text or "").parse()


# ---------------------------------------------------------------- task 1.6 FDX


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""


def _fdx_text(element: ET.Element) -> str:
    texts = [node for node in element.iter() if _tag(node) == "Text"]
    if texts:
        return "".join("".join(node.itertext()) for node in texts)
    return "".join(element.itertext())


def _fdx_paragraph(parent: ET.Element, ptype: str, text: str,
                   **attrs: str) -> ET.Element:
    paragraph = ET.SubElement(parent, "Paragraph", {"Type": ptype, **attrs})
    node = ET.SubElement(paragraph, "Text")
    node.text = text
    return paragraph


def serialize_to_fdx(project_tree: dict) -> FDXExport:
    """把结构化剧本树投影为 Final Draft XML，并列出有损字段。"""
    warnings: list[dict] = []
    project = project_tree.get("project") or {}
    title = _norm_text(project.get("title"))
    logline = _norm_text(project.get("logline"))
    characters = project_tree.get("characters") or []
    scenes = project_tree.get("scenes") or []

    root = ET.Element("FinalDraft", {
        "DocumentType": "Script",
        "Template": "No",
        "Version": "1",
    })
    title_page = ET.SubElement(root, "TitlePage")
    title_content = ET.SubElement(title_page, "Content")
    _fdx_paragraph(title_content, "General", f"Title: {title}")
    if logline:
        _fdx_paragraph(title_content, "General", f"Logline: {logline}")

    content = ET.SubElement(root, "Content")
    for scene in scenes:
        scene_title = _norm_text(scene.get("title")) or "未命名场次"
        if not _norm_text(scene.get("title")):
            warnings.append({
                "field": "scenes[].title",
                "reason": "空场次标题在 FDX 中写为「未命名场次」，往返后标题会变化",
                "scene_id": scene.get("id"),
            })
        _fdx_paragraph(content, "Scene Heading", scene_title)
        action = _norm_text(scene.get("action"))
        if action:
            _fdx_paragraph(content, "Action", action)
        for line in scene.get("lines") or []:
            speaker = _norm_text(line.get("speaker_name"))
            text = _norm_text(line.get("text"))
            if not text:
                continue
            if not speaker:
                warnings.append({
                    "field": "lines[].speaker_name",
                    "reason": "台词没有说话人，FDX 无法表达，已跳过",
                    "line_id": line.get("id"),
                })
                continue
            _fdx_paragraph(content, "Character", speaker)
            parenthetical = _norm_text(line.get("parenthetical"))
            if parenthetical:
                _fdx_paragraph(content, "Parenthetical", f"({parenthetical})")
            _fdx_paragraph(content, "Dialogue", text)
        transition = _norm_text(scene.get("transition"))
        if transition:
            for line in transition.splitlines():
                if line.strip():
                    _fdx_paragraph(content, "Transition", line.strip())

    ET.indent(root, space="  ")
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"no\"?>\n"
           + ET.tostring(root, encoding="unicode"))

    appearance_count = sum(1 for c in characters if c.get("appearance"))
    if appearance_count:
        warnings.append({
            "field": "characters[].appearance",
            "reason": "FDX 无角色要素集字段；保留在结构化层",
            "count": appearance_count,
        })
    stance_count = sum(1 for c in characters if c.get("stance"))
    if stance_count:
        warnings.append({
            "field": "characters[].stance",
            "reason": "FDX 无哲思立场标注；保留在结构化层",
            "count": stance_count,
        })
    synopsis_count = sum(1 for s in scenes if s.get("synopsis"))
    if synopsis_count:
        warnings.append({
            "field": "scenes[].synopsis",
            "reason": "FDX 无 synopsis 元素；保留在结构化层",
            "count": synopsis_count,
        })
    conflict_count = sum(1 for s in scenes if s.get("conflict"))
    if conflict_count:
        warnings.append({
            "field": "scenes[].conflict",
            "reason": "FDX 无场次张力标注；保留在结构化层",
            "count": conflict_count,
        })
    warnings.append({
        "field": "*.id",
        "reason": "FDX 不承载内部主键；导入按场次标题/顺序与角色名匹配",
        "count": len(characters) + len(scenes),
    })
    return FDXExport(text=xml, warnings=warnings)


class _FDXParser:
    def __init__(self, root: ET.Element) -> None:
        self.root = root
        self.report = ReportUnrecognizedElements()
        self.warnings: list[dict] = []
        self.scenes: list[dict] = []
        self.current_scene: dict | None = None
        self.current_line: dict | None = None
        self.title_page = self._parse_title_page()

    def _parse_title_page(self) -> dict[str, str]:
        title_page = next((el for el in self.root if _tag(el) == "TitlePage"), None)
        if title_page is None:
            return {}
        lines = [
            _fdx_text(node).strip()
            for node in title_page.iter()
            if _tag(node) == "Paragraph" and _fdx_text(node).strip()
        ]
        out: dict[str, str] = {}
        for index, line in enumerate(lines):
            match = _TITLE_LINE_RE.match(line)
            if match:
                key = match.group(1).strip().lower().replace(" ", "_")
                if key in _TITLE_KEYS:
                    out[key] = match.group(2).strip()
                    continue
            if index == 0 and "title" not in out:
                out["title"] = line
                continue
            self.report.add("title_page", line, None,
                            "FDX 标题页附加行不进入结构化模型")
        return out

    def _ensure_scene(self) -> dict:
        if self.current_scene is None:
            self.current_scene = {
                "heading": "",
                "scene_number": None,
                "synopsis": None,
                "action": None,
                "transition": None,
                "transitions": [],
                "lines": [],
            }
            self.scenes.append(self.current_scene)
            self.warnings.append({
                "field": "scenes[].title",
                "reason": "内容出现在首个 Scene Heading 之前，已放入无名场次",
            })
        return self.current_scene

    def _handle_paragraph(self, paragraph: ET.Element) -> None:
        ptype = (paragraph.get("Type") or "General").strip()
        key = ptype.lower()
        text = _fdx_text(paragraph).strip()

        if key == "scene heading":
            self.current_scene = {
                "heading": text,
                "scene_number": paragraph.get("Number"),
                "synopsis": None,
                "action": None,
                "transition": None,
                "transitions": [],
                "lines": [],
            }
            self.scenes.append(self.current_scene)
            self.current_line = None
        elif key in ("action", "general"):
            scene = self._ensure_scene()
            scene["action"] = _append_paragraph(scene["action"], text)
        elif key == "character":
            scene = self._ensure_scene()
            self.current_line = {"speaker": text, "parenthetical": None, "text": ""}
            scene["lines"].append(self.current_line)
        elif key == "parenthetical":
            if self.current_line is None:
                self.report.add("fdx_paragraph", f"{ptype}: {text}", None,
                                "括号说明缺少角色段")
            else:
                self.current_line["parenthetical"] = text.strip().strip("()").strip()
        elif key == "dialogue":
            if self.current_line is None:
                self.report.add("fdx_paragraph", f"{ptype}: {text}", None,
                                "对白缺少角色段")
            else:
                self.current_line["text"] = (
                    self.current_line["text"] + "\n" + text
                    if self.current_line["text"] else text
                )
        elif key == "transition":
            scene = self._ensure_scene()
            scene["transitions"].append(text)
            scene["transition"] = "\n".join(scene["transitions"])
        else:
            self.report.add("fdx_paragraph_type", f"{ptype}: {text}", None,
                            "FDX 段落类型不进入结构化模型")
            if text and key == "shot":
                scene = self._ensure_scene()
                scene["action"] = _append_paragraph(scene["action"], text)

    def parse(self) -> dict:
        content = next((el for el in self.root if _tag(el) == "Content"), None)
        if content is None:
            raise ValueError("FDX 缺少 Content 元素")
        for element in content:
            tag = _tag(element)
            if tag == "Paragraph":
                self._handle_paragraph(element)
            elif tag == "DualDialogue":
                summary = " / ".join(
                    _fdx_text(paragraph).strip()
                    for paragraph in element.iter()
                    if _tag(paragraph) == "Paragraph"
                    and _fdx_text(paragraph).strip()
                )
                self.report.add("dual_dialogue", summary or _fdx_text(element),
                                None,
                                "FDX 双人对白容器不进入结构化模型（对白文本保留）")
                for paragraph in element.iter():
                    if _tag(paragraph) == "Paragraph":
                        self._handle_paragraph(paragraph)
            else:
                self.report.add("fdx_element", tag, None,
                                "FDX 元素不进入结构化模型")

        for scene in self.scenes:
            scene["lines"] = [
                line for line in scene["lines"] if line["text"].strip()
            ]
            if not scene["transitions"]:
                scene["transition"] = None
        return {
            "format": "fdx",
            "capabilities": ["action", "transition", "parenthetical"],
            "title_page": dict(self.title_page),
            "scenes": self.scenes,
            "unrecognized": self.report.as_list(),
            "unrecognized_summary": self.report.summary(),
            "warnings": list(self.warnings),
            "stats": {
                "scene_count": len(self.scenes),
                "line_count": sum(len(scene["lines"]) for scene in self.scenes),
                "character_count": len({
                    line["speaker"]
                    for scene in self.scenes for line in scene["lines"]
                }),
                "transition_count": sum(
                    len(scene["transitions"]) for scene in self.scenes),
            },
        }


def parse_from_fdx(xml_text: str) -> dict:
    """解析 Final Draft XML 为结构化中间表示（不落库）。"""
    if not (xml_text or "").strip():
        raise ValueError("FDX 内容为空")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"FDX XML 解析失败：{exc}") from exc
    if _tag(root) != "FinalDraft":
        raise ValueError("不是 Final Draft XML：根元素应为 FinalDraft")
    return _FDXParser(root).parse()


# ---------------------------------------------------------------- 往返差异（task 1.6）


def diff_tree_vs_parsed(project_tree: dict, parsed: dict, *,
                        include_metadata: bool = True) -> list[dict]:
    """逐项列出结构化树与交换格式解析结果之间的差异。

    ``include_metadata=True`` 时把 Fountain/FDX 不承载的元数据
    （stance/appearance/conflict）也列为差异，用于 task 1.6 的有损报告；
    只比较结构字段时传 ``False``（Fountain 往返应返回空列表）。
    """
    differences: list[dict] = []
    project = project_tree.get("project") or {}
    title_page = parsed.get("title_page") or {}

    if _norm_compare(project.get("title")) != _norm_compare(title_page.get("title")):
        differences.append({
            "path": "project.title",
            "before": project.get("title"),
            "after": title_page.get("title"),
            "reason": "标题页 Title 不同或缺失",
        })
    if _norm_compare(project.get("logline")) != _norm_compare(title_page.get("logline")):
        differences.append({
            "path": "project.logline",
            "before": project.get("logline"),
            "after": title_page.get("logline"),
            "reason": "标题页 Logline 不同或缺失",
        })

    scenes = project_tree.get("scenes") or []
    parsed_scenes = parsed.get("scenes") or []
    if len(scenes) != len(parsed_scenes):
        differences.append({
            "path": "scenes",
            "before": len(scenes),
            "after": len(parsed_scenes),
            "reason": "场次数量不同",
        })

    for index, (scene, parsed_scene) in enumerate(zip(scenes, parsed_scenes)):
        path = f"scenes[{index}]"
        if _norm_compare(scene.get("title")) != _norm_compare(parsed_scene.get("heading")):
            differences.append({
                "path": f"{path}.title",
                "before": scene.get("title"),
                "after": parsed_scene.get("heading"),
                "reason": "场景标题不同",
            })
        if _norm_compare(scene.get("synopsis")) != _norm_compare(parsed_scene.get("synopsis")):
            differences.append({
                "path": f"{path}.synopsis",
                "before": scene.get("synopsis"),
                "after": parsed_scene.get("synopsis"),
                "reason": "场次提要不同或交换格式不承载",
            })
        if _norm_compare(scene.get("action")) != _norm_compare(parsed_scene.get("action")):
            differences.append({
                "path": f"{path}.action",
                "before": scene.get("action"),
                "after": parsed_scene.get("action"),
                "reason": "动作段落不同",
            })
        if _norm_compare(scene.get("transition")) != _norm_compare(parsed_scene.get("transition")):
            differences.append({
                "path": f"{path}.transition",
                "before": scene.get("transition"),
                "after": parsed_scene.get("transition"),
                "reason": "转场不同",
            })

        lines = scene.get("lines") or []
        parsed_lines = parsed_scene.get("lines") or []
        if len(lines) != len(parsed_lines):
            differences.append({
                "path": f"{path}.lines",
                "before": len(lines),
                "after": len(parsed_lines),
                "reason": "台词数量不同",
            })
        for line_index, (line, parsed_line) in enumerate(zip(lines, parsed_lines)):
            line_path = f"{path}.lines[{line_index}]"
            if _norm_compare(line.get("speaker_name")) != _norm_compare(parsed_line.get("speaker")):
                differences.append({
                    "path": f"{line_path}.speaker",
                    "before": line.get("speaker_name"),
                    "after": parsed_line.get("speaker"),
                    "reason": "说话人不同",
                })
            if _norm_compare(line.get("text")) != _norm_compare(parsed_line.get("text")):
                differences.append({
                    "path": f"{line_path}.text",
                    "before": line.get("text"),
                    "after": parsed_line.get("text"),
                    "reason": "台词文本不同",
                })
            if _norm_compare(line.get("parenthetical")) != _norm_compare(
                    parsed_line.get("parenthetical")):
                differences.append({
                    "path": f"{line_path}.parenthetical",
                    "before": line.get("parenthetical"),
                    "after": parsed_line.get("parenthetical"),
                    "reason": "括号说明不同",
                })

        if include_metadata and scene.get("conflict"):
            differences.append({
                "path": f"{path}.conflict",
                "before": scene.get("conflict"),
                "after": None,
                "reason": "交换格式不承载场次张力标注（保留在结构化层）",
            })

    if include_metadata:
        used_names = {
            _norm_text(parsed_line.get("speaker"))
            for parsed_scene in parsed_scenes
            for parsed_line in parsed_scene.get("lines") or []
        }
        for character in project_tree.get("characters") or []:
            name = _norm_text(character.get("name"))
            if name not in used_names:
                continue
            if character.get("appearance"):
                differences.append({
                    "path": f"characters[{name}].appearance",
                    "before": character.get("appearance"),
                    "after": None,
                    "reason": "交换格式不承载角色要素集（保留在结构化层）",
                })
            if character.get("stance"):
                differences.append({
                    "path": f"characters[{name}].stance",
                    "before": character.get("stance"),
                    "after": None,
                    "reason": "交换格式不承载哲思立场标注（保留在结构化层）",
                })

    return differences
