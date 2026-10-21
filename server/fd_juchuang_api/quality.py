"""剧作质量门（change juchuang-continuity-layer task 4.1-4.4）。

六维 rubric：节奏（rhythm）、爽点/情绪回报（emotional_payoff）、对白（dialogue）、
格式（format）、连续性（continuity）、合规（compliance）。每维由规则项
（``RubricRule``）与问题项（``Problem``）组成；问题项必须带场次/台词定位与依据
文本，评分由「规则命中数 × 规则扣分」推导，界面可展开问题清单复核，不以单一
总分替代分维结果。

不变量（对应 spec「剧作质量门」与设计决策 6/7）：

1. 六维分列：``QualityAssessment.dimensions`` 始终包含六个键，``dimension_order``
   给出展示顺序；``overall_score`` 只是加权汇总，不替代分维结果。
2. 每维都有规则与定位字段：``RUBRIC`` 中每条规则都有 ``location_scope``
   （episode / scene / line）；每条 ``Problem.location`` 都含
   ``scene_id`` / ``line_idx`` / ``character`` 三项定位键。
3. 评分可复核：``DimensionScore`` 同时返回 ``rules_applied``、
   ``rule_results``（检查项数/命中数/状态）与 ``problems``，不存在只有数字
   没有支撑的分数。
4. 连续性维接入台账：经 ``ledger.check_generated`` 读取越知/时间线/伏笔冲突，
   ``Problem.ledger_refs`` 引用冲突事实的 ``fact_id`` 与 ``signature``
   （伏笔则为 ``foreshadow_id`` 与 ``hint_text``），``evidence`` 同步给出可读引用。
5. 空台账降级：连续性维 ``available=False``、``score=None``、``reason`` 明示，
   其余五维照常评分，创作流程不被阻断。
6. 合规红线阻断：命中 ``red_line=True`` 的合规规则时 ``blocked=True``，
   ``violation_details`` 逐条列出命中项；``/deliver`` 端点返回 409 阻断交付。
   项目级策略可禁用规则/红线、关闭阻断，策略随评估结果一并返回。
7. 确定性优先：全部检查为确定性启发式（正则/统计/台账比对），不调用模型，
   同一输入可重复复核。

API 前缀 ``/api/v1/quality``；鉴权沿用 ``screenplay`` 的 require_user，项目归属
校验经共库 ``projects`` 表（只读），越权与不存在统一 404。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import ledger

# ---- 鉴权接线（与 screenplay/ledger 同款兜底：personal 尚未交付时用 dev-token） ----

try:  # 正式路径：auth.py 的 require_user（含审计留痕）
    from .auth import require_user
except Exception:  # noqa: BLE001 —— personal 底座未交付时的降级兜底

    def require_user(request: Request) -> str:  # type: ignore[misc]
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token:
            for var in ("FD_JUCHUANG_AUTH_DEV_TOKENS", "FD__AUTH_DEV_TOKENS"):
                for pair in os.environ.get(var, "").split(","):
                    if "=" in pair and pair.split("=", 1)[0].strip() == token:
                        return pair.split("=", 1)[1].strip()
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


# ---- 常量 ----

DIMENSIONS: tuple[str, ...] = (
    "rhythm",
    "emotional_payoff",
    "dialogue",
    "format",
    "continuity",
    "compliance",
)

DIMENSION_LABELS: dict[str, str] = {
    "rhythm": "节奏",
    "emotional_payoff": "爽点/情绪回报",
    "dialogue": "对白",
    "format": "格式",
    "continuity": "连续性",
    "compliance": "合规",
}

DEFAULT_WEIGHTS: dict[str, float] = {
    "rhythm": 0.20,
    "emotional_payoff": 0.20,
    "dialogue": 0.20,
    "format": 0.15,
    "continuity": 0.15,
    "compliance": 0.10,
}

SEVERITY_PENALTIES: dict[str, float] = {
    "warning": 0.4,
    "error": 1.5,
    "critical": 3.0,
}

SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2}

EMPTY_LEDGER_REASON = (
    "连续性台账为空：该剧本尚无事实/知识/伏笔记录，连续性检查不可用（其余维度照常评分）"
)

# 评分下限与规则扣分上限：避免同类问题重复计数把分数打到失真
DIMENSION_FLOOR = 0.0
DEFAULT_RULE_CAP = 4.0


class QualityNotFound(LookupError):
    """项目/评估记录不存在（API 映射 404）。"""


class QualityConflict(ValueError):
    """策略/状态冲突（API 映射 409）。"""


# ---- 规则模型（4.1：六维 rubric） ----


@dataclass(frozen=True)
class RubricRule:
    """一条可复核规则。

    ``location_scope`` 是规则命中时的定位粒度，保证每一维的规则都声明了
    场次/台词定位能力（spec：每维均有规则与定位字段）。
    """

    rule_id: str
    dimension: str
    title: str
    description: str
    location_scope: Literal["episode", "scene", "line"]
    severity: Literal["warning", "error", "critical"] = "warning"
    penalty: float = SEVERITY_PENALTIES["warning"]
    cap: float = DEFAULT_RULE_CAP
    red_line: bool = False

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "dimension": self.dimension,
            "title": self.title,
            "description": self.description,
            "location_scope": self.location_scope,
            "severity": self.severity,
            "penalty": self.penalty,
            "cap": self.cap,
            "red_line": self.red_line,
        }


def _rule(
    rule_id: str,
    dimension: str,
    title: str,
    description: str,
    location_scope: Literal["episode", "scene", "line"],
    severity: Literal["warning", "error", "critical"] = "warning",
    penalty: float | None = None,
    cap: float = DEFAULT_RULE_CAP,
    red_line: bool = False,
) -> RubricRule:
    return RubricRule(
        rule_id=rule_id,
        dimension=dimension,
        title=title,
        description=description,
        location_scope=location_scope,
        severity=severity,
        penalty=SEVERITY_PENALTIES[severity] if penalty is None else penalty,
        cap=cap,
        red_line=red_line,
    )


# 六维 rubric。顺序即展示顺序；新增规则必须同时声明 location_scope。
RUBRIC: dict[str, list[RubricRule]] = {
    "rhythm": [
        _rule(
            "rhythm.scene_count", "rhythm", "场次数量",
            "一集的场次数量应在可编排范围内（3–60 场）；过少撑不起推进，过多容易失焦。",
            "episode", penalty=0.6, cap=1.2,
        ),
        _rule(
            "rhythm.scene_length_balance", "rhythm", "场次篇幅均衡",
            "场次台词量相对全集中位数不得过度失衡（超过 2.5 倍或低于 0.4 倍）。",
            "scene", penalty=0.4, cap=2.0,
        ),
        _rule(
            "rhythm.act_structure", "rhythm", "三幕结构",
            "场次达到 6 场时，铺垫/对抗/收束三幕都应有冲突或情绪节拍支撑。",
            "scene", penalty=0.6, cap=1.2,
        ),
        _rule(
            "rhythm.tension_progression", "rhythm", "张力推进",
            "连续 3 场及以上没有冲突标注、且冲突缺失场次超过一半时，张力曲线趋平。",
            "scene", penalty=0.5, cap=2.0,
        ),
        _rule(
            "rhythm.pacing_density", "rhythm", "节奏密度",
            "平均每场台词过少，或单场动作描述过长且没有台词，会造成节奏拖沓。",
            "scene", penalty=0.4, cap=2.0,
        ),
    ],
    "emotional_payoff": [
        _rule(
            "payoff.core_conflict", "emotional_payoff", "核心冲突",
            "全剧至少一场标注核心冲突；没有冲突就没有可兑现的情绪回报。",
            "scene", severity="error", penalty=1.5, cap=3.0,
        ),
        _rule(
            "payoff.payoff_timing", "emotional_payoff", "回报时点",
            "情绪峰值（高频强情绪台词+冲突）应落在后半程，过早释放会泄掉集末动力。",
            "scene", penalty=0.5, cap=1.5,
        ),
        _rule(
            "payoff.cliffhanger", "emotional_payoff", "集末钩子",
            "集末最后一句若是闭合陈述且转场为淡出/剧终，缺少续看动力。",
            "line", penalty=0.5, cap=1.0,
        ),
        _rule(
            "payoff.emotional_beats", "emotional_payoff", "情绪节拍",
            "一集应有足量强情绪节拍（感叹/疑问/省略/情绪词），低于台词量 15% 时情绪回报不足。",
            "episode", penalty=0.5, cap=1.5,
        ),
        _rule(
            "payoff.repetition", "emotional_payoff", "情绪重复",
            "同一句台词重复 3 次以上、或感叹句占比超过 60%，情绪表达单一化。",
            "line", penalty=0.3, cap=2.0,
        ),
    ],
    "dialogue": [
        _rule(
            "dialogue.line_length", "dialogue", "台词长度",
            "单句台词超过 120 字，口播与表演都会失去支点，应拆分或删减。",
            "line", penalty=0.3, cap=2.4,
        ),
        _rule(
            "dialogue.monologue_ratio", "dialogue", "对手戏均衡",
            "台词量达到 8 句时，单一角色占比超过 70% 会压掉对手戏。",
            "line", penalty=0.5, cap=1.0,
        ),
        _rule(
            "dialogue.voice_consistency", "dialogue", "声线一致性",
            "同一角色台词长度离散度过大（≥4 句且标准差 > 55），人物声线不稳定。",
            "line", penalty=0.4, cap=1.2,
        ),
        _rule(
            "dialogue.exposition", "dialogue", "说明式台词",
            "「因为…所以…」「正如你所知」「事实上」等说明性连接词把信息硬塞给观众。",
            "line", penalty=0.4, cap=2.0,
        ),
        _rule(
            "dialogue.on_the_nose", "dialogue", "直白情绪",
            "台词直接报出「我很生气/我恨你」等情绪标签，缺少潜台词。",
            "line", penalty=0.4, cap=2.0,
        ),
        _rule(
            "dialogue.parenthetical_tell", "dialogue", "括号说明代替表演",
            "括号说明大量使用「愤怒地/悲伤地」等情绪提示，属于 tell 而非 show。",
            "line", penalty=0.3, cap=1.5,
        ),
    ],
    "format": [
        _rule(
            "format.scene_heading", "format", "场次标题格式",
            "场次标题应为「第 N 场 …」或 Fountain 场景标题（INT./EXT. 地点 - 日/夜）。",
            "scene", penalty=0.4, cap=2.0,
        ),
        _rule(
            "format.scene_numbering", "format", "场次编号连续",
            "场次序号应从 1 起连续且不重复，便于 Fountain/FDX 往返与分镜引用。",
            "scene", penalty=0.3, cap=1.2,
        ),
        _rule(
            "format.transition", "format", "转场用法",
            "转场只使用标准值（CUT TO / DISSOLVE TO / 淡入 / 淡出 等）。",
            "scene", penalty=0.3, cap=1.2,
        ),
        _rule(
            "format.parenthetical_syntax", "format", "括号说明语法",
            "括号说明应非空、不超过 60 字、不自带括号（Fountain 导出会自动补括号）。",
            "line", penalty=0.3, cap=1.2,
        ),
        _rule(
            "format.speaker_present", "format", "说话人归属",
            "每句台词必须有角色表中的说话人，否则 Fountain/FDX 无法表达。",
            "line", severity="error", penalty=1.0, cap=2.0,
        ),
        _rule(
            "format.action_markup", "format", "动作段落格式",
            "动作段落不应混入 Markdown 标记（**、# 标题、- [ ] 任务列表）。",
            "scene", penalty=0.3, cap=1.2,
        ),
    ],
    "continuity": [
        _rule(
            "continuity.ledger_available", "continuity", "台账可用性",
            "台账为空时连续性检查不可用；本规则只报告可用性，不产生扣分问题。",
            "episode", penalty=0.0, cap=0.0,
        ),
        _rule(
            "continuity.character_identity", "continuity", "实体名一致性",
            "台词说话人必须在角色表中，避免生成/导入产生不存在的人物。",
            "line", penalty=0.5, cap=2.0,
        ),
        _rule(
            "continuity.knowledge_timeline", "continuity", "角色知识时点",
            "角色不得在获知时点之前使用该知识（越知 = 台账 block 级冲突）。",
            "line", severity="error", penalty=1.5, cap=3.0,
        ),
        _rule(
            "continuity.timeline_consistency", "continuity", "时间线一致性",
            "台词不得提及当前集次尚未成立（valid_from 在未来）的事实。",
            "line", penalty=0.5, cap=2.0,
        ),
        _rule(
            "continuity.foreshadow_status", "continuity", "伏笔状态",
            "已回收/已放弃的伏笔不得无重新埋设地再次出现；未到埋设集次的伏笔不得提前提及。",
            "line", penalty=0.5, cap=2.0,
        ),
        _rule(
            "continuity.foreshadow_overdue", "continuity", "伏笔逾期",
            "越过计划回收集次仍未回收的伏笔应提示，避免线索遗忘。",
            "episode", penalty=0.6, cap=1.8,
        ),
    ],
    "compliance": [
        _rule(
            "compliance.violence_instruction", "compliance", "暴力操作细节",
            "禁止出现可执行的暴力/武器/投毒操作细节。",
            "line", severity="critical", penalty=3.0, cap=6.0, red_line=True,
        ),
        _rule(
            "compliance.minor_harm", "compliance", "未成年人伤害",
            "涉及未成年人的性、暴力、拐卖、虐待内容一律红线。",
            "line", severity="critical", penalty=3.0, cap=6.0, red_line=True,
        ),
        _rule(
            "compliance.political_sensitivity", "compliance", "政治敏感内容",
            "颠覆政权、分裂国家、泄露国家秘密等政治敏感表述为红线。",
            "line", severity="critical", penalty=3.0, cap=6.0, red_line=True,
        ),
        _rule(
            "compliance.hate_speech", "compliance", "仇恨言论",
            "针对民族/群体的侮辱、清洗、灭绝言论为红线。",
            "line", severity="critical", penalty=3.0, cap=6.0, red_line=True,
        ),
        _rule(
            "compliance.illegal_drugs", "compliance", "毒品制作交易",
            "制毒、贩毒、毒品交易的具体方法或配方为红线。",
            "line", severity="critical", penalty=3.0, cap=6.0, red_line=True,
        ),
        _rule(
            "compliance.sexual_content", "compliance", "露骨性内容",
            "露骨性行为描写、性暴力为红线。",
            "line", severity="critical", penalty=3.0, cap=6.0, red_line=True,
        ),
        _rule(
            "compliance.self_harm", "compliance", "自伤自残方法",
            "自杀/自残的具体方法或教程为红线。",
            "line", severity="critical", penalty=3.0, cap=6.0, red_line=True,
        ),
        _rule(
            "compliance.privacy_leak", "compliance", "真实隐私信息",
            "身份证号、银行卡号、家庭住址等真实个人隐私信息为红线。",
            "line", severity="critical", penalty=3.0, cap=6.0, red_line=True,
        ),
    ],
}

RULE_INDEX: dict[str, RubricRule] = {
    rule.rule_id: rule for rules in RUBRIC.values() for rule in rules
}

REDLINE_RULES: dict[str, RubricRule] = {
    rule.rule_id: rule
    for rule in RUBRIC["compliance"]
    if rule.red_line
}


# 合规红线关键词/正则（确定性扫描；命中即 critical 并阻断交付）
COMPLIANCE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "compliance.violence_instruction": (
        re.compile(
            r"(?:制作|制造|组装|自制|DIY).{0,8}(?:炸弹|炸药|枪支|雷管|毒药|燃烧瓶)"
            r"|(?:下毒|投毒).{0,8}(?:步骤|方法|配方|教程)"
            r"|how\s+to\s+(?:make|build)\s+(?:a\s+)?(?:bomb|gun|explosive)",
            re.IGNORECASE,
        ),
    ),
    "compliance.minor_harm": (
        re.compile(
            r"(?:未成年|儿童|小学生|少女|少年|幼女|幼童).{0,12}"
            r"(?:性侵|强奸|猥亵|虐待|殴打|拐卖|自杀|自残)"
        ),
        re.compile(
            r"(?:性侵|强奸|猥亵|虐待|殴打|拐卖).{0,12}(?:未成年|儿童|少女|少年|幼女|幼童)"
        ),
    ),
    "compliance.political_sensitivity": (
        re.compile(
            r"(?:颠覆|推翻).{0,8}(?:国家政权|政府|制度)"
            r"|分裂国家|煽动民族仇恨|泄露国家秘密|现任国家领导人"
        ),
    ),
    "compliance.hate_speech": (
        re.compile(r"劣等民族|种族清洗|应该灭绝|支那|东亚病夫|灭绝人性"),
    ),
    "compliance.illegal_drugs": (
        re.compile(r"制毒|贩毒|毒品交易|冰毒配方|海洛因交易|种植罂粟|吸毒教程"),
    ),
    "compliance.sexual_content": (
        re.compile(
            r"(?:露骨|详细).{0,6}性(?:行为|爱|描写)"
            r"|强奸|轮奸|性奴|未成年.{0,4}性行为"
        ),
    ),
    "compliance.self_harm": (
        re.compile(r"自杀方法|割腕|上吊|跳楼自杀|安眠药自杀|自残教程|自伤教程"),
    ),
    "compliance.privacy_leak": (
        re.compile(r"身份证号|银行卡号|家庭住址|手机号码是|护照号码"),
    ),
}


# ---- 问题 / 评分模型 ----


class ProblemLocation(BaseModel):
    """问题定位：场次/台词/角色三要素始终存在（允许为 None，但键不缺席）。"""

    model_config = ConfigDict(extra="forbid")

    scene_id: int | None = None
    line_id: int | None = None
    line_idx: int | None = None
    character: str | None = None
    scope: Literal["episode", "scene", "line"] = "episode"
    offset: int | None = None
    excerpt: str | None = None


class LedgerFactRef(BaseModel):
    """引用台账中的冲突事实/伏笔（4.2：问题项引用台账冲突事实）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["fact", "foreshadow"] = "fact"
    fact_id: int | None = None
    foreshadow_id: int | None = None
    signature: str | None = None
    violation_type: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Problem(BaseModel):
    """一条可复核问题：维度、规则、严重度、定位、依据、台账引用。"""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    dimension: str
    severity: Literal["warning", "error", "critical"]
    message: str
    location: ProblemLocation
    evidence: str
    ledger_refs: list[LedgerFactRef] = Field(default_factory=list)
    penalty: float = 0.0


class RuleResult(BaseModel):
    """规则执行结果：检查了多少项、命中多少问题、是否跳过。"""

    rule_id: str
    title: str
    status: Literal["ok", "violated", "skipped"]
    checked: int = 0
    problem_count: int = 0
    reason: str | None = None


class DimensionScore(BaseModel):
    """单维评分。``score=None`` 表示该维不可用（如空台账），不得当作满分。"""

    dimension: str
    label: str
    score: float | None
    available: bool = True
    reason: str | None = None
    weight: float
    rules_applied: list[str] = Field(default_factory=list)
    rules_disabled: list[str] = Field(default_factory=list)
    rule_results: list[RuleResult] = Field(default_factory=list)
    problems: list[Problem] = Field(default_factory=list)
    problem_count: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)


class ViolationDetail(BaseModel):
    """合规红线命中项（交付阻断时逐条列出）。"""

    rule_id: str
    severity: Literal["warning", "error", "critical"] = "critical"
    message: str
    location: ProblemLocation
    evidence: str
    red_line: bool = True


class QualityPolicy(BaseModel):
    """项目级质量门策略（可配置权重、禁用规则/红线、是否阻断交付）。"""

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    disabled_rules: list[str] = Field(default_factory=list)
    disabled_redlines: list[str] = Field(default_factory=list)
    block_on_redline: bool = True
    continuity_strict: bool = False


class QualityPolicyIn(BaseModel):
    """策略更新（未提供的字段沿用当前值）。"""

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] | None = None
    disabled_rules: list[str] | None = None
    disabled_redlines: list[str] | None = None
    block_on_redline: bool | None = None
    continuity_strict: bool | None = None


class QualityAssessment(BaseModel):
    """一集剧本的六维质量评估结果。"""

    id: int | None = None
    project_id: str
    episode_id: str
    episode: int
    dimensions: dict[str, DimensionScore]
    dimension_order: list[str] = Field(default_factory=lambda: list(DIMENSIONS))
    overall_score: float | None
    overall_formula: str = "weighted_average(available_dimensions)"
    blocked: bool = False
    redline_hit: bool = False
    block_reasons: list[str] = Field(default_factory=list)
    violation_details: list[ViolationDetail] = Field(default_factory=list)
    policy: QualityPolicy = Field(default_factory=QualityPolicy)
    stats: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class AssessmentSummary(BaseModel):
    id: int
    project_id: str
    episode_id: str
    episode: int
    overall_score: float | None
    blocked: bool
    redline_hit: bool
    created_at: str


class AssessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: int | None = Field(default=None, ge=1)
    episode_id: str | None = Field(default=None, max_length=100)
    scene_ids: list[int] | None = Field(default=None, max_length=200)
    persist: bool = True


class DeliveryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode: int | None = Field(default=None, ge=1)
    episode_id: str | None = Field(default=None, max_length=100)
    scene_ids: list[int] | None = Field(default=None, max_length=200)
    target: str = Field(default="screenplay", pattern="^(screenplay|fountain|fdx)$")


class DeliveryResult(BaseModel):
    delivered: bool
    blocked: bool
    project_id: str
    episode_id: str
    assessment_id: int | None = None
    overall_score: float | None = None
    dimensions: dict[str, float | None] = Field(default_factory=dict)
    violations: list[ViolationDetail] = Field(default_factory=list)
    block_reasons: list[str] = Field(default_factory=list)
    target: str = "screenplay"


# ---- 存储 / 迁移（独立迁移账本，不碰 personal 的 user_version） ----

MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS quality_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: str
    down: str


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="0001_assessments_policies",
        up="""
        CREATE TABLE IF NOT EXISTS quality_assessments (
            id INTEGER PRIMARY KEY,
            project_id TEXT NOT NULL,
            episode INTEGER NOT NULL,
            episode_id TEXT NOT NULL,
            overall_score REAL,
            blocked INTEGER NOT NULL DEFAULT 0,
            redline_hit INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (episode >= 1)
        );
        CREATE INDEX IF NOT EXISTS idx_quality_assessments_project
            ON quality_assessments(project_id, created_at);

        CREATE TABLE IF NOT EXISTS quality_policies (
            project_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
        down="""
        DROP INDEX IF EXISTS idx_quality_assessments_project;
        DROP TABLE IF EXISTS quality_policies;
        DROP TABLE IF EXISTS quality_assessments;
        """,
    ),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """与 screenplay/ledger 同库同约定（env FD_JUCHUANG_DB）。"""
    path = db_path or os.environ.get("FD_JUCHUANG_DB", "")
    if not path:
        path = str(Path.home() / ".fd-juchuang" / "juchuang.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    try:
        rows = conn.execute(
            "SELECT version, applied_at FROM quality_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(r["version"]): r["applied_at"] for r in rows}


def migrate(conn: sqlite3.Connection) -> int:
    """正向执行未应用的迁移，返回当前已应用版本数（幂等）。"""
    conn.executescript(MIGRATION_TABLE)
    applied = _applied_versions(conn)
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        conn.executescript(migration.up)
        conn.execute(
            "INSERT INTO quality_migrations (version, name, applied_at) VALUES (?,?,?)",
            (migration.version, migration.name, now()),
        )
        conn.commit()
    return len(_applied_versions(conn))


def rollback(conn: sqlite3.Connection, steps: int = 1) -> list[int]:
    """反向执行最近 ``steps`` 个迁移（测试与回滚预案用）。"""
    if steps < 1:
        return []
    applied = _applied_versions(conn)
    rolled: list[int] = []
    for version in sorted(applied, reverse=True)[:steps]:
        migration = next((m for m in MIGRATIONS if m.version == version), None)
        if migration is None:
            continue
        conn.executescript(migration.down)
        conn.execute("DELETE FROM quality_migrations WHERE version=?", (version,))
        conn.commit()
        rolled.append(version)
    return rolled


def migration_state(conn: sqlite3.Connection) -> list[dict]:
    applied = _applied_versions(conn)
    return [
        {
            "version": m.version,
            "name": m.name,
            "applied": m.version in applied,
            "applied_at": applied.get(m.version),
        }
        for m in MIGRATIONS
    ]


def _db() -> sqlite3.Connection:
    conn = connect()
    migrate(conn)
    return conn


def _pid(project_id: int | str) -> str:
    return str(project_id).strip()


def _loads(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---- 策略 ----


def default_policy() -> QualityPolicy:
    return QualityPolicy(weights=dict(DEFAULT_WEIGHTS))


def validate_policy(policy: QualityPolicy) -> QualityPolicy:
    """策略必须引用已知规则/维度，权重非负且覆盖六维。"""
    unknown_dims = set(policy.weights) - set(DIMENSIONS)
    if unknown_dims:
        raise ValueError(f"未知维度：{sorted(unknown_dims)}")
    missing = set(DIMENSIONS) - set(policy.weights)
    if missing:
        raise ValueError(f"权重缺少维度：{sorted(missing)}")
    if any(float(v) < 0 for v in policy.weights.values()):
        raise ValueError("权重不得为负")
    if sum(float(v) for v in policy.weights.values()) <= 0:
        raise ValueError("权重之和必须大于 0")
    unknown_rules = [r for r in policy.disabled_rules if r not in RULE_INDEX]
    if unknown_rules:
        raise ValueError(f"未知规则：{unknown_rules}")
    unknown_redlines = [r for r in policy.disabled_redlines if r not in REDLINE_RULES]
    if unknown_redlines:
        raise ValueError(f"未知合规红线：{unknown_redlines}")
    return policy


def _merge_policy_data(base: dict, patch: dict) -> dict:
    data = dict(base)
    patch = {k: v for k, v in patch.items() if v is not None}
    weights = patch.pop("weights", None)
    if weights is not None:
        data["weights"] = {**data.get("weights", {}), **weights}
    data.update(patch)
    return data


def load_policy(conn: sqlite3.Connection, project_id: int | str) -> QualityPolicy:
    migrate(conn)
    row = conn.execute(
        "SELECT payload FROM quality_policies WHERE project_id=?", (_pid(project_id),)
    ).fetchone()
    if row is None:
        return default_policy()
    raw = _loads(row["payload"])
    if not isinstance(raw, dict):
        return default_policy()
    try:
        return validate_policy(
            QualityPolicy.model_validate(
                _merge_policy_data(default_policy().model_dump(), raw)
            )
        )
    except ValueError:
        return default_policy()


def save_policy(conn: sqlite3.Connection, project_id: int | str,
                patch: QualityPolicyIn | dict) -> QualityPolicy:
    migrate(conn)
    base = load_policy(conn, project_id)
    payload = patch.model_dump(exclude_unset=True) if isinstance(patch, BaseModel) else dict(patch)
    merged = validate_policy(
        QualityPolicy.model_validate(
            _merge_policy_data(base.model_dump(), payload)
        )
    )
    pid = _pid(project_id)
    conn.execute(
        "INSERT INTO quality_policies (project_id, payload, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(project_id) DO UPDATE SET payload=excluded.payload,"
        " updated_at=excluded.updated_at",
        (pid, _dumps(merged.model_dump(mode="json")), now()),
    )
    conn.commit()
    return merged


# ---- 剧本数据读取 ----


def _conflict_text(value: Any) -> str:
    """场次冲突可能是纯文本或结构化对象（哲思注入），统一取可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("{") or raw.startswith("["):
            try:
                return _conflict_text(json.loads(raw))
            except (TypeError, ValueError):
                return raw
        return raw
    if isinstance(value, dict):
        for key in ("description", "text", "position"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
        a = value.get("a")
        b = value.get("b")
        if isinstance(a, str) or isinstance(b, str):
            return f"{a or ''} ↔ {b or ''}".strip(" ↔")
        return ""
    if isinstance(value, list):
        return " / ".join(str(v) for v in value if v)
    return str(value).strip()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """剧本/台账/质量三套表都保证存在（各自幂等；失败不阻断评估）。"""
    migrate(conn)
    try:
        from . import screenplay

        screenplay.migrate(conn)
    except Exception:  # noqa: BLE001 —— 剧本模块尚未就绪时退化
        pass
    try:
        ledger.migrate(conn)
    except Exception:  # noqa: BLE001 —— 台账模块尚未就绪时退化
        pass


def _load_script(conn: sqlite3.Connection, project_id: int | str,
                 scene_ids: list[int] | None = None) -> dict:
    """读取项目 → 场次（含台词与说话人），只读。"""
    _ensure_schema(conn)
    pid = _pid(project_id)
    if not pid.isdigit():
        raise QualityNotFound("项目不存在")
    project = conn.execute(
        "SELECT id, title, logline, user_id FROM projects WHERE id=?", (int(pid),)
    ).fetchone()
    if project is None:
        raise QualityNotFound(f"项目 {pid} 不存在")

    characters = [
        {"id": int(r["id"]), "name": str(r["name"])}
        for r in conn.execute(
            "SELECT id, name FROM characters WHERE project_id=? ORDER BY id",
            (int(pid),),
        ).fetchall()
    ]
    names = {c["name"] for c in characters}
    wanted = {int(s) for s in scene_ids} if scene_ids else None

    scenes: list[dict] = []
    for row in conn.execute(
        "SELECT id, idx, title, synopsis, action, transition, conflict"
        " FROM scenes WHERE project_id=? ORDER BY idx, id",
        (int(pid),),
    ).fetchall():
        sid = int(row["id"])
        if wanted is not None and sid not in wanted:
            continue
        lines = [
            {
                "id": int(line["id"]),
                "scene_id": sid,
                "idx": int(line["idx"]),
                "text": str(line["text"] or ""),
                "parenthetical": (
                    str(line["parenthetical"]) if line["parenthetical"] is not None else None
                ),
                "speaker_id": int(line["speaker_id"]),
                "speaker": (
                    str(line["speaker_name"]) if line["speaker_name"] is not None else ""
                ),
            }
            for line in conn.execute(
                "SELECT l.id, l.idx, l.text, l.parenthetical, l.speaker_id,"
                " c.name AS speaker_name"
                " FROM lines l LEFT JOIN characters c ON c.id = l.speaker_id"
                " WHERE l.scene_id=? ORDER BY l.idx, l.id",
                (sid,),
            ).fetchall()
        ]
        scenes.append({
            "id": sid,
            "idx": int(row["idx"]) if row["idx"] is not None else 1,
            "title": str(row["title"] or ""),
            "synopsis": str(row["synopsis"] or ""),
            "action": str(row["action"] or ""),
            "transition": str(row["transition"] or ""),
            "conflict": _conflict_text(_loads(row["conflict"])),
            "lines": lines,
        })

    flat_lines = [line for scene in scenes for line in scene["lines"]]
    return {
        "project": {
            "id": int(project["id"]),
            "title": project["title"],
            "logline": project["logline"],
        },
        "characters": characters,
        "character_names": names,
        "scenes": scenes,
        "lines": flat_lines,
        "scene_count": len(scenes),
        "line_count": len(flat_lines),
        "character_count": len(characters),
    }


# ---- 定位 / 问题辅助 ----


def _loc(scene_id: int | None = None, line_id: int | None = None,
         line_idx: int | None = None, character: str | None = None,
         scope: Literal["episode", "scene", "line"] | None = None,
         offset: int | None = None, excerpt: str | None = None) -> ProblemLocation:
    if scope is None:
        if line_id is not None or line_idx is not None:
            scope = "line"
        elif scene_id is not None:
            scope = "scene"
        else:
            scope = "episode"
    return ProblemLocation(
        scene_id=scene_id,
        line_id=line_id,
        line_idx=line_idx,
        character=character or None,
        scope=scope,
        offset=offset,
        excerpt=excerpt,
    )


def _excerpt(text: str, limit: int = 80) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _match_context(text: str, start: int, end: int, pad: int = 16) -> str:
    left = max(0, start - pad)
    right = min(len(text), end + pad)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{text[left:right].strip()}{suffix}"


@dataclass
class _RuleEntry:
    rule: RubricRule
    disabled: bool = False
    checked: int = 0
    problems: list[Problem] = field(default_factory=list)
    skip_reason: str | None = None

    def add(self, message: str, *, location: ProblemLocation | None = None,
            evidence: str = "", ledger_refs: list[LedgerFactRef] | None = None,
            severity: Literal["warning", "error", "critical"] | None = None,
            penalty: float | None = None) -> Problem:
        problem = Problem(
            rule_id=self.rule.rule_id,
            dimension=self.rule.dimension,
            severity=severity or self.rule.severity,
            message=message,
            location=location or _loc(scope=self.rule.location_scope),
            evidence=evidence,
            ledger_refs=list(ledger_refs or []),
            penalty=self.rule.penalty if penalty is None else penalty,
        )
        self.problems.append(problem)
        return problem


class _DimBuilder:
    """收集单维规则执行结果并计算分数。"""

    def __init__(self, dimension: str, policy: QualityPolicy):
        self.dimension = dimension
        self.available = True
        self.reason: str | None = None
        disabled_extra = set(policy.disabled_redlines) if dimension == "compliance" else set()
        self.entries: list[_RuleEntry] = [
            _RuleEntry(
                rule,
                disabled=(
                    rule.rule_id in policy.disabled_rules
                    or rule.rule_id in disabled_extra
                ),
            )
            for rule in RUBRIC[dimension]
        ]
        self._by_id = {entry.rule.rule_id: entry for entry in self.entries}

    def entry(self, rule_id: str) -> _RuleEntry:
        return self._by_id[rule_id]

    def mark_unavailable(self, reason: str) -> None:
        self.available = False
        self.reason = reason
        for entry in self.entries:
            if not entry.disabled:
                entry.skip_reason = reason

    def build(self, weight: float) -> DimensionScore:
        enabled = [e for e in self.entries if not e.disabled]
        if not enabled:
            self.available = False
            self.reason = self.reason or "策略禁用了本维全部规则"

        problems: list[Problem] = []
        results: list[RuleResult] = []
        total_penalty = 0.0
        for entry in self.entries:
            if entry.disabled:
                results.append(RuleResult(
                    rule_id=entry.rule.rule_id,
                    title=entry.rule.title,
                    status="skipped",
                    checked=0,
                    problem_count=0,
                    reason="策略禁用",
                ))
                continue
            if entry.problems:
                status: Literal["ok", "violated", "skipped"] = "violated"
                reason = None
            elif entry.skip_reason or entry.checked == 0:
                status = "skipped"
                reason = entry.skip_reason or "无可检查项"
            else:
                status = "ok"
                reason = None
            capped = min(entry.rule.cap, sum(p.penalty for p in entry.problems))
            total_penalty += capped
            results.append(RuleResult(
                rule_id=entry.rule.rule_id,
                title=entry.rule.title,
                status=status,
                checked=entry.checked,
                problem_count=len(entry.problems),
                reason=reason,
            ))
            problems.extend(entry.problems)

        problems.sort(key=lambda p: (
            SEVERITY_ORDER.get(p.severity, 9),
            p.location.scene_id if p.location.scene_id is not None else 0,
            p.location.line_idx if p.location.line_idx is not None else 0,
            p.rule_id,
        ))
        severity_counts = Counter(p.severity for p in problems)
        score = None
        if self.available:
            score = max(DIMENSION_FLOOR, round(10.0 - total_penalty, 2))
        return DimensionScore(
            dimension=self.dimension,
            label=DIMENSION_LABELS[self.dimension],
            score=score,
            available=self.available,
            reason=self.reason,
            weight=round(float(weight), 4),
            rules_applied=[e.rule.rule_id for e in enabled],
            rules_disabled=[e.rule.rule_id for e in self.entries if e.disabled],
            rule_results=results,
            problems=problems,
            problem_count=len(problems),
            severity_counts={
                "warning": int(severity_counts.get("warning", 0)),
                "error": int(severity_counts.get("error", 0)),
                "critical": int(severity_counts.get("critical", 0)),
            },
        )


# ---- 4.1 节奏 ----


def _scene_has_beat(scene: dict) -> bool:
    return any(_EMOTION_RE.search(line["text"] or "") for line in scene["lines"])


def _assess_rhythm(builder: _DimBuilder, script: dict) -> None:
    scenes = script["scenes"]
    count = len(scenes)

    entry = builder.entry("rhythm.scene_count")
    entry.checked = 1
    if count == 0:
        entry.add("剧本没有任何场次，无法评估节奏", location=_loc(scope="episode"),
                  evidence="场次数 0")
    elif count < 3:
        entry.add(f"仅 {count} 场，不足以形成完整的节奏推进", location=_loc(scope="episode"),
                  evidence=f"场次数 {count}（建议 ≥3）")
    elif count > 60:
        entry.add(f"{count} 场超出单集常规承载范围，节奏容易失焦",
                  location=_loc(scope="episode"), evidence=f"场次数 {count}（建议 ≤60）")

    entry = builder.entry("rhythm.scene_length_balance")
    lengths = [len(scene["lines"]) for scene in scenes]
    entry.checked = count
    if count >= 3 and lengths:
        median = statistics.median(lengths)
        for scene, length in zip(scenes, lengths):
            if length > max(median * 2.5, median + 8):
                entry.add(
                    f"第 {scene['idx']} 场台词量 {length} 句，显著高于全集中位 {median} 句",
                    location=_loc(scene_id=scene["id"], scope="scene"),
                    evidence=f"本场 {length} 句 / 全集中位 {median} 句",
                )
            elif median >= 3 and length < median * 0.4:
                entry.add(
                    f"第 {scene['idx']} 场仅 {length} 句，篇幅过短可能沦为过场",
                    location=_loc(scene_id=scene["id"], scope="scene"),
                    evidence=f"本场 {length} 句 / 全集中位 {median} 句",
                )

    entry = builder.entry("rhythm.act_structure")
    if count >= 6:
        entry.checked = 1
        bounds = [
            (0, max(1, math.ceil(count / 3))),
            (max(1, math.ceil(count / 3)), max(1, math.ceil(count * 2 / 3))),
            (max(1, math.ceil(count * 2 / 3)), count),
        ]
        for index, (start, end) in enumerate(bounds, start=1):
            segment = scenes[start:end]
            if not any(scene["conflict"] or _scene_has_beat(scene) for scene in segment):
                anchor = segment[0] if segment else scenes[min(start, count - 1)]
                entry.add(
                    f"第 {index} 幕（第 {start + 1}–{end} 场）没有冲突或情绪节拍",
                    location=_loc(scene_id=anchor["id"], scope="scene"),
                    evidence=f"该幕 {len(segment)} 场，冲突标注 0，情绪节拍 0",
                )
    else:
        entry.skip_reason = "场次少于 6，不适用三幕结构检查"

    entry = builder.entry("rhythm.tension_progression")
    entry.checked = count
    run_start: int | None = None
    flat_count = 0
    for index, scene in enumerate(scenes):
        if scene["conflict"]:
            if run_start is not None and index - run_start >= 3:
                anchor = scenes[run_start]
                entry.add(
                    f"第 {anchor['idx']}–{scene['idx'] - 1} 场连续 "
                    f"{index - run_start} 场没有冲突标注",
                    location=_loc(scene_id=anchor["id"], scope="scene"),
                    evidence=f"连续 {index - run_start} 场冲突为空",
                )
            run_start = None
        else:
            flat_count += 1
            if run_start is None:
                run_start = index
    if run_start is not None and count - run_start >= 3:
        anchor = scenes[run_start]
        entry.add(
            f"结尾连续 {count - run_start} 场没有冲突标注",
            location=_loc(scene_id=anchor["id"], scope="scene"),
            evidence=f"连续 {count - run_start} 场冲突为空",
        )
    if count >= 4 and flat_count / count > 0.5:
        first_flat = next((s for s in scenes if not s["conflict"]), scenes[0])
        entry.add(
            f"{flat_count}/{count} 场没有冲突标注，张力曲线趋平",
            location=_loc(scene_id=first_flat["id"], scope="scene"),
            evidence=f"冲突缺失场次 {flat_count}/{count}",
        )

    entry = builder.entry("rhythm.pacing_density")
    entry.checked = count + 1
    if count and script["line_count"] / count < 1.5:
        entry.add(
            f"平均每场仅 {script['line_count'] / count:.1f} 句台词，推进密度偏低",
            location=_loc(scope="episode"),
            evidence=f"台词 {script['line_count']} 句 / 场次 {count}",
        )
    for scene in scenes:
        if len(scene["action"]) > 800 and not scene["lines"]:
            entry.add(
                f"第 {scene['idx']} 场动作描述 {len(scene['action'])} 字且无台词",
                location=_loc(scene_id=scene["id"], scope="scene"),
                evidence=_excerpt(scene["action"]),
            )


# ---- 4.1 爽点 / 情绪回报 ----


_EMOTION_RE = re.compile(r"[！？…]|哈哈|呜呜|唉呀|天哪|竟然|居然|终于|没想到")
_DRAMATIC_RE = re.compile(r"突然|终于|原来|竟然|居然|必须|绝不能|唯一|最后一次")
_EXPOSITION_RE = re.compile(
    r"(?:因为|由于).{0,15}(?:所以|因此)|正如你所知|你知道的|事实上|也就是说"
    r"|换句话说|众所周知|让我解释|容我解释"
)
_ON_THE_NOSE_RE = re.compile(
    r"我很(?:生气|愤怒|难过|伤心|开心|高兴|害怕|痛苦|绝望)"
    r"|我(?:恨|爱)你|我非常(?:痛苦|绝望|生气|难过)"
)
_TELL_RE = re.compile(r"愤怒|悲伤|高兴|开心|难过|冷冷|激动|紧张|害怕|无奈|惊讶")


def _scene_intensity(scene: dict) -> int:
    score = 2 if scene["conflict"] else 0
    for line in scene["lines"]:
        text = line["text"] or ""
        score += len(_EMOTION_RE.findall(text))
        score += len(_DRAMATIC_RE.findall(text))
    return score


def _assess_payoff(builder: _DimBuilder, script: dict) -> None:
    scenes = script["scenes"]
    count = len(scenes)

    entry = builder.entry("payoff.core_conflict")
    entry.checked = count
    if count == 0:
        entry.add("没有场次，无法建立核心冲突", location=_loc(scope="episode"),
                  evidence="场次数 0")
    elif not any(scene["conflict"] for scene in scenes):
        anchor = scenes[0]
        entry.add(
            "全剧没有场次标注核心冲突，情绪回报缺少兑现对象",
            location=_loc(scene_id=anchor["id"], scope="scene"),
            evidence=f"冲突标注场次 0/{count}",
        )

    entry = builder.entry("payoff.payoff_timing")
    entry.checked = count
    if count >= 5:
        intensities = [_scene_intensity(scene) for scene in scenes]
        peak = max(range(count), key=lambda i: intensities[i])
        if intensities[peak] > 0 and peak <= count // 3:
            anchor = scenes[peak]
            entry.add(
                f"情绪峰值出现在第 {anchor['idx']}/{count} 场（前三分之一），后程动力不足",
                location=_loc(scene_id=anchor["id"], scope="scene"),
                evidence=f"峰值强度 {intensities[peak]}，位置 {peak + 1}/{count}",
            )

    entry = builder.entry("payoff.cliffhanger")
    if script["lines"]:
        entry.checked = 1
        last_scene = scenes[-1]
        last_line = last_scene["lines"][-1]
        text = (last_line["text"] or "").strip()
        transition = (last_scene["transition"] or "").strip().upper()
        closed_transition = transition in {"FADE OUT", "FADE OUT.", "THE END", "剧终", "淡出", "黑场"}
        if text and not re.search(r"[？?…]|——", text) and (
            text.endswith("。") or text.endswith("！") or closed_transition
        ):
            entry.add(
                "集末最后一句为闭合陈述且没有悬念，缺少续看钩子",
                location=_loc(
                    scene_id=last_scene["id"],
                    line_id=last_line["id"],
                    line_idx=last_line["idx"],
                    character=last_line["speaker"],
                ),
                evidence=_excerpt(text),
            )
    else:
        entry.skip_reason = "没有台词可检查集末钩子"

    entry = builder.entry("payoff.emotional_beats")
    entry.checked = script["line_count"]
    beats = sum(1 for line in script["lines"] if _EMOTION_RE.search(line["text"] or ""))
    expected = max(2, math.ceil(script["line_count"] * 0.15))
    if script["line_count"] and beats < expected:
        entry.add(
            f"强情绪节拍只有 {beats} 处，低于台词量 15%（期望 ≥{expected}）",
            location=_loc(scope="episode"),
            evidence=f"情绪节拍 {beats} / 台词 {script['line_count']}",
        )

    entry = builder.entry("payoff.repetition")
    entry.checked = script["line_count"]
    repeated = Counter(
        (line["text"] or "").strip() for line in script["lines"]
        if len((line["text"] or "").strip()) >= 2
    )
    for text, hits in repeated.items():
        if hits < 3:
            continue
        anchor = next(
            line for line in reversed(script["lines"])
            if (line["text"] or "").strip() == text
        )
        entry.add(
            f"同一句台词重复 {hits} 次，情绪表达单一",
            location=_loc(
                scene_id=anchor["scene_id"], line_id=anchor["id"],
                line_idx=anchor["idx"], character=anchor["speaker"],
            ),
            evidence=f"「{_excerpt(text)}」×{hits}",
        )
    if script["line_count"] >= 5:
        exclaims = sum(
            1 for line in script["lines"] if (line["text"] or "").strip().endswith("！")
        )
        ratio = exclaims / script["line_count"]
        if ratio > 0.6:
            anchor = next(
                line for line in script["lines"]
                if (line["text"] or "").strip().endswith("！")
            )
            entry.add(
                f"感叹句占比 {ratio:.0%}，情绪表达缺少层次",
                location=_loc(
                    scene_id=anchor["scene_id"], line_id=anchor["id"],
                    line_idx=anchor["idx"], character=anchor["speaker"],
                ),
                evidence=f"感叹句 {exclaims}/{script['line_count']}",
            )


# ---- 4.1 对白 ----


def _assess_dialogue(builder: _DimBuilder, script: dict) -> None:
    lines = script["lines"]
    names = " / ".join(sorted(script["character_names"])) or "（无角色）"

    entry = builder.entry("dialogue.line_length")
    entry.checked = len(lines)
    for line in lines:
        text = line["text"] or ""
        if len(text) > 120:
            entry.add(
                f"第 {line['idx']} 句台词 {len(text)} 字，超过单句上限 120 字",
                location=_loc(
                    scene_id=line["scene_id"], line_id=line["id"],
                    line_idx=line["idx"], character=line["speaker"],
                ),
                evidence=_excerpt(text),
            )

    entry = builder.entry("dialogue.monologue_ratio")
    entry.checked = len(lines)
    if len(lines) >= 8:
        by_speaker = Counter(line["speaker"] for line in lines if line["speaker"])
        if by_speaker:
            speaker, hits = by_speaker.most_common(1)[0]
            ratio = hits / len(lines)
            if ratio > 0.7:
                anchor = next(
                    line for line in reversed(lines) if line["speaker"] == speaker
                )
                entry.add(
                    f"{speaker} 占 {ratio:.0%} 台词量，对手戏被压制",
                    location=_loc(
                        scene_id=anchor["scene_id"], line_id=anchor["id"],
                        line_idx=anchor["idx"], character=speaker,
                    ),
                    evidence=f"{speaker} {hits} 句 / 全剧 {len(lines)} 句（角色：{names}）",
                )

    entry = builder.entry("dialogue.voice_consistency")
    entry.checked = len(lines)
    for speaker in sorted({line["speaker"] for line in lines if line["speaker"]}):
        speaker_lines = [line for line in lines if line["speaker"] == speaker]
        if len(speaker_lines) < 4:
            continue
        lengths = [len(line["text"] or "") for line in speaker_lines]
        spread = statistics.pstdev(lengths)
        if spread > 55:
            anchor = max(speaker_lines, key=lambda line: len(line["text"] or ""))
            entry.add(
                f"{speaker} 的台词长度离散度 {spread:.1f}，声线不稳定",
                location=_loc(
                    scene_id=anchor["scene_id"], line_id=anchor["id"],
                    line_idx=anchor["idx"], character=speaker,
                ),
                evidence=f"共 {len(speaker_lines)} 句，长度区间 {min(lengths)}–{max(lengths)} 字，标准差 {spread:.1f}",
            )

    entry = builder.entry("dialogue.exposition")
    entry.checked = len(lines)
    for line in lines:
        text = line["text"] or ""
        match = _EXPOSITION_RE.search(text)
        if match:
            entry.add(
                "说明式台词把信息直接塞给观众，缺少戏剧动作承载",
                location=_loc(
                    scene_id=line["scene_id"], line_id=line["id"],
                    line_idx=line["idx"], character=line["speaker"],
                    offset=match.start(), excerpt=_match_context(text, match.start(), match.end()),
                ),
                evidence=f"命中「{match.group(0)}」：{_match_context(text, match.start(), match.end())}",
            )

    entry = builder.entry("dialogue.on_the_nose")
    entry.checked = len(lines)
    for line in lines:
        text = line["text"] or ""
        match = _ON_THE_NOSE_RE.search(text)
        if match:
            entry.add(
                "台词直接报出情绪标签，潜台词不足",
                location=_loc(
                    scene_id=line["scene_id"], line_id=line["id"],
                    line_idx=line["idx"], character=line["speaker"],
                    offset=match.start(), excerpt=_match_context(text, match.start(), match.end()),
                ),
                evidence=f"命中「{match.group(0)}」：{_match_context(text, match.start(), match.end())}",
            )

    entry = builder.entry("dialogue.parenthetical_tell")
    parentheticals = [line for line in lines if (line["parenthetical"] or "").strip()]
    entry.checked = len(parentheticals)
    tell_hits = [line for line in parentheticals if _TELL_RE.search(line["parenthetical"] or "")]
    if len(tell_hits) >= 2 and len(tell_hits) / len(parentheticals) >= 0.3:
        for line in tell_hits:
            entry.add(
                f"括号说明「{line['parenthetical']}」直接标注情绪，应用动作/画面 show 出来",
                location=_loc(
                    scene_id=line["scene_id"], line_id=line["id"],
                    line_idx=line["idx"], character=line["speaker"],
                ),
                evidence=f"括号说明：{line['parenthetical']}",
            )


# ---- 4.1 格式 ----


_HEADING_RE = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百千]+\s*场|[内外]景|INT\.?/EXT|INT\.?|EXT\.?)",
    re.IGNORECASE,
)
_MARKUP_RE = re.compile(r"\*\*|^#{1,6}\s|^[-*]\s\[[ xX]\]", re.MULTILINE)
_ALLOWED_TRANSITIONS = {
    "FADE IN", "FADE IN:", "FADE OUT", "FADE OUT.", "FADE TO BLACK",
    "CUT TO", "CUT TO:", "SMASH CUT", "MATCH CUT", "DISSOLVE TO",
    "WIPE TO", "IRIS OUT", "THE END", "淡入", "淡出", "切至", "叠化",
    "划像", "黑场", "剧终",
}


def _assess_format(builder: _DimBuilder, script: dict) -> None:
    scenes = script["scenes"]

    entry = builder.entry("format.scene_heading")
    entry.checked = len(scenes)
    for scene in scenes:
        title = (scene["title"] or "").strip()
        if not title:
            entry.add(
                f"第 {scene['idx']} 场缺少场次标题",
                location=_loc(scene_id=scene["id"], scope="scene"),
                evidence="标题为空",
            )
        elif not _HEADING_RE.match(title):
            entry.add(
                f"场次标题「{_excerpt(title, 40)}」不符合「第 N 场」或 Fountain 场景标题格式",
                location=_loc(scene_id=scene["id"], scope="scene"),
                evidence=title,
            )

    entry = builder.entry("format.scene_numbering")
    entry.checked = len(scenes)
    seen: set[int] = set()
    for position, scene in enumerate(scenes, start=1):
        if scene["idx"] in seen:
            entry.add(
                f"场次序号 {scene['idx']} 重复",
                location=_loc(scene_id=scene["id"], scope="scene"),
                evidence=f"重复序号 {scene['idx']}",
            )
        elif scene["idx"] != position:
            entry.add(
                f"场次序号不连续：第 {position} 个场次序号为 {scene['idx']}",
                location=_loc(scene_id=scene["id"], scope="scene"),
                evidence=f"期望 {position}，实际 {scene['idx']}",
            )
        seen.add(scene["idx"])

    entry = builder.entry("format.transition")
    checked_transitions = 0
    for scene in scenes:
        transition = (scene["transition"] or "").strip()
        if not transition:
            continue
        checked_transitions += 1
        if transition.upper() not in _ALLOWED_TRANSITIONS and transition not in _ALLOWED_TRANSITIONS:
            entry.add(
                f"第 {scene['idx']} 场转场「{_excerpt(transition, 30)}」不是标准值",
                location=_loc(scene_id=scene["id"], scope="scene"),
                evidence=transition,
            )
    entry.checked = checked_transitions
    if checked_transitions == 0:
        entry.skip_reason = "没有转场可检查"

    entry = builder.entry("format.parenthetical_syntax")
    parens = [line for line in script["lines"] if line["parenthetical"] is not None]
    entry.checked = len(parens)
    for line in parens:
        raw = line["parenthetical"] or ""
        location = _loc(
            scene_id=line["scene_id"], line_id=line["id"],
            line_idx=line["idx"], character=line["speaker"],
        )
        if not raw.strip():
            entry.add("括号说明为空", location=location, evidence="parenthetical 为空串")
            continue
        if (raw.strip().startswith("(") and raw.strip().endswith(")")) or (
            raw.strip().startswith("（") and raw.strip().endswith("）")
        ):
            entry.add(
                "括号说明自带括号，Fountain 导出会重复包裹",
                location=location, evidence=raw,
            )
        if len(raw) > 60:
            entry.add(
                f"括号说明 {len(raw)} 字，超过 60 字应改写为动作",
                location=location, evidence=_excerpt(raw),
            )
        if "\n" in raw:
            entry.add("括号说明含换行，导出的 Fountain 元素会被拆块",
                      location=location, evidence=_excerpt(raw))
    if not parens:
        entry.skip_reason = "没有括号说明可检查"

    entry = builder.entry("format.speaker_present")
    entry.checked = len(script["lines"])
    for line in script["lines"]:
        speaker = (line["speaker"] or "").strip()
        location = _loc(
            scene_id=line["scene_id"], line_id=line["id"], line_idx=line["idx"],
            character=speaker or None,
        )
        if not speaker:
            entry.add("台词没有说话人，Fountain/FDX 无法表达",
                      location=location, evidence=_excerpt(line["text"]))
        elif script["character_names"] and speaker not in script["character_names"]:
            entry.add(
                f"说话人「{speaker}」不在角色表中",
                location=location, evidence=f"角色表：{sorted(script['character_names'])}",
            )

    entry = builder.entry("format.action_markup")
    entry.checked = sum(1 for scene in scenes if scene["action"])
    for scene in scenes:
        action = scene["action"] or ""
        match = _MARKUP_RE.search(action)
        if match:
            entry.add(
                f"第 {scene['idx']} 场动作段落含 Markdown 标记「{match.group(0)}」",
                location=_loc(scene_id=scene["id"], scope="scene",
                              offset=match.start(), excerpt=_match_context(action, match.start(), match.end())),
                evidence=_match_context(action, match.start(), match.end()),
            )


# ---- 4.2 连续性（接入台账） ----


_CONTINUITY_RULE_BY_TYPE: dict[str, str] = {
    "knowledge_violation": "continuity.knowledge_timeline",
    "future_fact_mention": "continuity.timeline_consistency",
    "knowledge_unrecorded": "continuity.knowledge_timeline",
    "unknown_entity": "continuity.character_identity",
    "foreshadow_status_conflict": "continuity.foreshadow_status",
    "foreshadow_early_mention": "continuity.foreshadow_status",
    "foreshadow_overdue": "continuity.foreshadow_overdue",
}


def _assess_continuity(builder: _DimBuilder, script: dict,
                       conn: sqlite3.Connection, project_id: int | str,
                       episode: int, policy: QualityPolicy) -> None:
    availability = builder.entry("continuity.ledger_available")
    try:
        status = ledger.ledger_status(conn, project_id)
    except Exception as exc:  # noqa: BLE001 —— 台账模块异常按不可用降级
        status = {"available": False, "reason": f"连续性台账不可用：{exc}"}
    if not status.get("available"):
        reason = status.get("reason") or EMPTY_LEDGER_REASON
        builder.mark_unavailable(reason)
        return

    availability.checked = 1

    check_lines = [
        {
            "scene_id": line["scene_id"],
            "line_id": line["id"],
            "idx": line["idx"],
            "speaker": line["speaker"],
            "text": line["text"],
        }
        for line in script["lines"]
    ]
    try:
        check = ledger.check_generated(
            conn, project_id, episode, check_lines,
            known_characters=sorted(script["character_names"]),
            strict_knowledge=policy.continuity_strict,
        )
    except Exception as exc:  # noqa: BLE001
        builder.mark_unavailable(f"连续性检查失败：{exc}")
        return
    if not check.get("available"):
        builder.mark_unavailable(check.get("reason") or EMPTY_LEDGER_REASON)
        return

    # 先登记每条规则的检查规模，再映射冲突（评分可复核：检查了多少条台词）
    for rule_id in (
        "continuity.character_identity",
        "continuity.knowledge_timeline",
        "continuity.timeline_consistency",
        "continuity.foreshadow_status",
    ):
        builder.entry(rule_id).checked = len(check_lines)
    builder.entry("continuity.foreshadow_overdue").checked = 1

    for violation in check.get("violations") or []:
        vtype = str(violation.get("type") or "")
        rule_id = _CONTINUITY_RULE_BY_TYPE.get(vtype)
        if rule_id is None:
            continue
        entry = builder.entry(rule_id)
        fact_id = violation.get("fact_id")
        signature = violation.get("fact")
        foreshadow_id = violation.get("foreshadow_id")
        hint_text = violation.get("hint_text")

        refs: list[LedgerFactRef] = []
        evidence_parts: list[str] = []
        if fact_id is not None or signature:
            refs.append(LedgerFactRef(
                kind="fact",
                fact_id=int(fact_id) if fact_id is not None else None,
                signature=str(signature) if signature else None,
                violation_type=vtype,
                detail={
                    "learned_at_episode": violation.get("learned_at_episode"),
                    "episode": episode,
                    "matched": violation.get("matched"),
                },
            ))
            evidence_parts.append(
                f"台账事实 #{fact_id if fact_id is not None else '-'}：{signature or '（签名式知识）'}"
            )
            if violation.get("learned_at_episode") is not None:
                evidence_parts.append(
                    f"第{violation.get('learned_at_episode')}集才获知，当前第{episode}集"
                )
        if foreshadow_id is not None or hint_text:
            refs.append(LedgerFactRef(
                kind="foreshadow",
                foreshadow_id=int(foreshadow_id) if foreshadow_id is not None else None,
                signature=str(hint_text) if hint_text else None,
                violation_type=vtype,
                detail={"status": violation.get("status")},
            ))
            evidence_parts.append(
                f"台账伏笔 #{foreshadow_id if foreshadow_id is not None else '-'}「{hint_text or ''}」"
            )
        if not evidence_parts:
            evidence_parts.append(str(violation.get("message") or vtype))

        # 伏笔逾期本身没有台词坐标：若剧本中出现线索文本，锚定到该台词，
        # 否则退回 episode 级定位（定位字段仍完整存在）。
        location: ProblemLocation | None = None
        if vtype == "foreshadow_overdue" and hint_text:
            anchor = next(
                (line for line in script["lines"]
                 if hint_text in (line["text"] or "")),
                None,
            )
            if anchor is not None:
                location = _loc(
                    scene_id=anchor["scene_id"],
                    line_id=anchor["id"],
                    line_idx=anchor["idx"],
                    character=anchor["speaker"],
                    scope="line",
                    excerpt=hint_text,
                )

        severity: Literal["warning", "error", "critical"] = (
            "error" if vtype == "knowledge_violation" else "warning"
        )
        entry.add(
            str(violation.get("message") or f"台账冲突：{vtype}"),
            location=location or _loc(
                scene_id=violation.get("scene_id"),
                line_id=violation.get("line_id"),
                line_idx=violation.get("line_idx"),
                character=violation.get("character_name") or violation.get("character_id"),
                scope=("episode" if vtype == "foreshadow_overdue" else "line"),
                offset=violation.get("offset"),
                excerpt=violation.get("matched"),
            ),
            evidence="；".join(evidence_parts),
            ledger_refs=refs,
            severity=severity,
        )


# ---- 4.3 合规红线 ----


def _scan_texts(script: dict):
    """产出 (text, scene, line|None) 供合规扫描；场次文本与台词都覆盖。"""
    for scene in script["scenes"]:
        yield scene["title"], scene, None
        yield scene["synopsis"], scene, None
        yield scene["action"], scene, None
        for line in scene["lines"]:
            parenthetical = line["parenthetical"] or ""
            text = line["text"] or ""
            if parenthetical:
                text = f"{text}（{parenthetical}）"
            yield text, scene, line


def _assess_compliance(builder: _DimBuilder, script: dict) -> None:
    for rule in RUBRIC["compliance"]:
        entry = builder.entry(rule.rule_id)
        if entry.disabled:
            continue
        patterns = COMPLIANCE_PATTERNS.get(rule.rule_id, ())
        checked = 0
        seen: set[tuple] = set()
        for text, scene, line in _scan_texts(script):
            checked += 1
            if not text:
                continue
            for pattern in patterns:
                for match in pattern.finditer(text):
                    key = (
                        scene["id"],
                        line["id"] if line is not None else None,
                        match.group(0),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    context = _match_context(text, match.start(), match.end())
                    if line is not None:
                        location = _loc(
                            scene_id=scene["id"], line_id=line["id"],
                            line_idx=line["idx"], character=line["speaker"],
                            scope="line", offset=match.start(), excerpt=context,
                        )
                    else:
                        location = _loc(
                            scene_id=scene["id"], scope="scene",
                            offset=match.start(), excerpt=context,
                        )
                    entry.add(
                        f"命中合规红线「{rule.title}」：{match.group(0)}",
                        location=location,
                        evidence=f"命中「{match.group(0)}」：{context}",
                        severity="critical",
                    )
        entry.checked = checked


# ---- 评估入口 ----


def _normalize_policy(policy: QualityPolicy | dict | None) -> QualityPolicy:
    if policy is None:
        return default_policy()
    if isinstance(policy, QualityPolicy):
        return validate_policy(policy)
    base = default_policy().model_dump()
    try:
        return validate_policy(
            QualityPolicy.model_validate(_merge_policy_data(base, dict(policy)))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"质量门策略非法：{exc}") from exc


def assess_episode(conn: sqlite3.Connection, project_id: int | str,
                   episode: int | None = None,
                   scene_ids: list[int] | None = None,
                   episode_id: str | None = None,
                   policy: QualityPolicy | dict | None = None,
                   persist: bool = True) -> dict:
    """对一集（默认全剧本）执行六维质量评估。

    ``episode`` 同时是台账时点：连续性维按该集次检查越知/时间线/伏笔。
    返回 ``QualityAssessment`` 的 JSON 字典（含持久化 id）。
    """
    _ensure_schema(conn)
    script = _load_script(conn, project_id, scene_ids)
    if episode is None:
        episode = max((scene["idx"] for scene in script["scenes"]), default=1)
    episode = max(1, int(episode))
    label = (episode_id or f"ep-{episode}").strip() or f"ep-{episode}"
    resolved_policy = (
        _normalize_policy(policy) if policy is not None
        else load_policy(conn, project_id)
    )

    builders = {dimension: _DimBuilder(dimension, resolved_policy) for dimension in DIMENSIONS}
    _assess_rhythm(builders["rhythm"], script)
    _assess_payoff(builders["emotional_payoff"], script)
    _assess_dialogue(builders["dialogue"], script)
    _assess_format(builders["format"], script)
    _assess_continuity(
        builders["continuity"], script, conn, project_id, episode, resolved_policy
    )
    _assess_compliance(builders["compliance"], script)

    dimensions: dict[str, DimensionScore] = {
        dimension: builders[dimension].build(resolved_policy.weights[dimension])
        for dimension in DIMENSIONS
    }

    # 加权总分：只对可用维度归一化，并在公式字段中明示
    available = [
        dimension for dimension in DIMENSIONS
        if dimensions[dimension].available and dimensions[dimension].score is not None
    ]
    total_weight = sum(resolved_policy.weights[d] for d in available)
    overall = None
    if available and total_weight > 0:
        overall = round(
            sum(
                float(dimensions[d].score) * resolved_policy.weights[d]
                for d in available
            ) / total_weight,
            2,
        )

    violation_details: list[ViolationDetail] = []
    block_reasons: list[str] = []
    for problem in dimensions["compliance"].problems:
        rule = RULE_INDEX.get(problem.rule_id)
        if rule is None or not rule.red_line:
            continue
        violation_details.append(ViolationDetail(
            rule_id=problem.rule_id,
            severity="critical",
            message=problem.message,
            location=problem.location,
            evidence=problem.evidence,
            red_line=True,
        ))
        block_reasons.append(f"{problem.rule_id}: {problem.message}")
    redline_hit = bool(violation_details)
    blocked = redline_hit and resolved_policy.block_on_redline

    total_problems = sum(dimension.problem_count for dimension in dimensions.values())
    payload = QualityAssessment(
        project_id=_pid(project_id),
        episode_id=label,
        episode=episode,
        dimensions=dimensions,
        dimension_order=list(DIMENSIONS),
        overall_score=overall,
        overall_formula=(
            "weighted_average(available_dimensions)"
            if available
            else "unavailable(no_dimension)"
        ),
        blocked=blocked,
        redline_hit=redline_hit,
        block_reasons=block_reasons,
        violation_details=violation_details,
        policy=resolved_policy,
        stats={
            "scene_count": script["scene_count"],
            "line_count": script["line_count"],
            "character_count": script["character_count"],
            "problem_count": total_problems,
            "available_dimensions": available,
            "unavailable_dimensions": [
                d for d in DIMENSIONS if d not in available
            ],
            "weights_renormalized": len(available) < len(DIMENSIONS),
            "redline_hit_count": len(violation_details),
        },
        created_at=now(),
    ).model_dump(mode="json")

    if persist:
        _save_assessment(conn, payload)
    return payload


def _save_assessment(conn: sqlite3.Connection, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO quality_assessments"
        " (project_id, episode, episode_id, overall_score, blocked, redline_hit,"
        "  payload, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            payload["project_id"],
            int(payload["episode"]),
            payload["episode_id"],
            payload.get("overall_score"),
            1 if payload.get("blocked") else 0,
            1 if payload.get("redline_hit") else 0,
            _dumps(payload),
            payload.get("created_at") or now(),
        ),
    )
    assessment_id = int(cur.lastrowid)
    payload["id"] = assessment_id
    conn.execute(
        "UPDATE quality_assessments SET payload=? WHERE id=?",
        (_dumps(payload), assessment_id),
    )
    conn.commit()
    return assessment_id


def get_assessment(conn: sqlite3.Connection, project_id: int | str,
                   assessment_id: int) -> dict:
    migrate(conn)
    row = conn.execute(
        "SELECT payload FROM quality_assessments WHERE id=? AND project_id=?",
        (int(assessment_id), _pid(project_id)),
    ).fetchone()
    if row is None:
        raise QualityNotFound(f"评估 {assessment_id} 不存在")
    payload = _loads(row["payload"])
    if not isinstance(payload, dict):
        raise QualityNotFound(f"评估 {assessment_id} 数据损坏")
    return payload


def list_assessments(conn: sqlite3.Connection, project_id: int | str,
                     limit: int = 20) -> list[dict]:
    migrate(conn)
    rows = conn.execute(
        "SELECT id, project_id, episode, episode_id, overall_score, blocked,"
        " redline_hit, created_at FROM quality_assessments"
        " WHERE project_id=? ORDER BY id DESC LIMIT ?",
        (_pid(project_id), max(1, min(int(limit), 100))),
    ).fetchall()
    return [dict(row) for row in rows]


def latest_assessment(conn: sqlite3.Connection, project_id: int | str) -> dict:
    migrate(conn)
    row = conn.execute(
        "SELECT payload FROM quality_assessments WHERE project_id=?"
        " ORDER BY id DESC LIMIT 1",
        (_pid(project_id),),
    ).fetchone()
    if row is None:
        raise QualityNotFound("该项目还没有质量评估记录")
    payload = _loads(row["payload"])
    if not isinstance(payload, dict):
        raise QualityNotFound("评估数据损坏")
    return payload


def delivery_gate(conn: sqlite3.Connection, project_id: int | str,
                  episode: int | None = None,
                  scene_ids: list[int] | None = None,
                  episode_id: str | None = None,
                  target: str = "screenplay",
                  policy: QualityPolicy | dict | None = None) -> dict:
    """交付门：命中红线时 ``delivered=False`` 并列出命中项。"""
    assessment = assess_episode(
        conn, project_id, episode=episode, scene_ids=scene_ids,
        episode_id=episode_id, policy=policy, persist=True,
    )
    delivered = not assessment["blocked"]
    return {
        "delivered": delivered,
        "blocked": assessment["blocked"],
        "project_id": assessment["project_id"],
        "episode_id": assessment["episode_id"],
        "assessment_id": assessment.get("id"),
        "overall_score": assessment.get("overall_score"),
        "dimensions": {
            key: value.get("score")
            for key, value in assessment["dimensions"].items()
        },
        "violations": assessment["violation_details"],
        "block_reasons": assessment["block_reasons"],
        "target": target,
    }


def rubric_payload() -> dict:
    """完整 rubric（六维 + 规则项 + 定位粒度 + 默认权重）。"""
    return {
        "dimensions": [
            {
                "key": dimension,
                "label": DIMENSION_LABELS[dimension],
                "default_weight": DEFAULT_WEIGHTS[dimension],
                "rules": [rule.as_dict() for rule in RUBRIC[dimension]],
            }
            for dimension in DIMENSIONS
        ],
        "severity_penalties": dict(SEVERITY_PENALTIES),
        "default_weights": dict(DEFAULT_WEIGHTS),
        "redline_rules": sorted(REDLINE_RULES),
        "required_problem_location_fields": [
            "scene_id", "line_id", "line_idx", "character",
        ],
    }


# ---- API 路由 ----


router = APIRouter(prefix="/api/v1/quality", tags=["quality"])


def _need_project(conn: sqlite3.Connection, user_id: str, pid: int) -> str:
    """项目归属校验（共库 projects 表只读）；越权与不存在统一 404。"""
    try:
        from . import screenplay

        screenplay.migrate(conn)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"剧本模块未就绪：{exc}") from exc
    row = conn.execute(
        "SELECT id FROM projects WHERE id=? AND user_id=?", (pid, user_id)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return str(pid)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except QualityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QualityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/rubric")
def api_rubric() -> dict:
    """六维 rubric：规则、定位粒度、默认权重与红线清单。"""
    return rubric_payload()


@router.get("/projects/{pid}/policy", response_model=QualityPolicy)
def api_get_policy(pid: int, user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return load_policy(conn, pid).model_dump(mode="json")
    finally:
        conn.close()


@router.put("/projects/{pid}/policy", response_model=QualityPolicy)
def api_put_policy(pid: int, body: QualityPolicyIn,
                   user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        policy = _call(save_policy, conn, pid, body)
        return policy.model_dump(mode="json")
    finally:
        conn.close()


@router.post("/projects/{pid}/assess", response_model=QualityAssessment)
def api_assess(pid: int, body: AssessIn,
               user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(
            assess_episode, conn, pid,
            episode=body.episode, scene_ids=body.scene_ids,
            episode_id=body.episode_id, persist=body.persist,
        )
    finally:
        conn.close()


@router.get("/projects/{pid}/assessments", response_model=list[AssessmentSummary])
def api_list_assessments(pid: int, limit: int = 20,
                         user_id: str = Depends(require_user)) -> list[dict]:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return list_assessments(conn, pid, limit)
    finally:
        conn.close()


@router.get("/projects/{pid}/assessments/latest", response_model=QualityAssessment)
def api_latest_assessment(pid: int,
                          user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(latest_assessment, conn, pid)
    finally:
        conn.close()


@router.get("/projects/{pid}/assessments/{assessment_id}",
            response_model=QualityAssessment)
def api_get_assessment(pid: int, assessment_id: int,
                       user_id: str = Depends(require_user)) -> dict:
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        return _call(get_assessment, conn, pid, assessment_id)
    finally:
        conn.close()


@router.post("/projects/{pid}/deliver")
def api_deliver(pid: int, body: DeliveryIn,
                user_id: str = Depends(require_user)):
    """交付门：命中合规红线时返回 409 并逐条列出命中项；否则 200 放行。"""
    conn = _db()
    try:
        _need_project(conn, user_id, pid)
        result = _call(
            delivery_gate, conn, pid,
            episode=body.episode, scene_ids=body.scene_ids,
            episode_id=body.episode_id, target=body.target,
        )
        status_code = 200 if result["delivered"] else 409
        return JSONResponse(status_code=status_code, content=result)
    finally:
        conn.close()
