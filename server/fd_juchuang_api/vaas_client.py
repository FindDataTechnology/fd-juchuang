"""VAAS 生成面客户端（change juchuang-image-iteration task 1.1）。

剧创 → VAAS 的唯一出口。设计约束（design D1）：**服务身份单租户**——
剧创持一枚服务令牌调 MCP 网关，VAAS 侧固定 `X-Forwarded-User` 为剧创的服务身份。
用户 JWT 不透传（VAAS 不认 Logto 的 issuer，透传只会 401）。

降级是第一等公民（spec「降级独立模式」）：
- 两项 env 缺失 → `unconfigured`，不是异常，是**可用性分类**；
- 上游不可达 → `unreachable`；
- 车道 down → `lane_down`；
- 请求被拒 → `rejected`。
每种都带可行动说明。**超时不自动重试**（视频/图片按次计费，重试=重复扣费）。

确实失败的路径才抛异常；「不可用」一律走返回值，让调用方能明示而不崩。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class Unavailable(Exception):
    """Capability is unavailable — carries a category so the UI can be specific."""

    def __init__(self, category: str, message: str):
        self.category = category  # unconfigured | unreachable | lane_down | rejected | timeout
        super().__init__(f"{category}: {message}")


# 能力分类常量（与 spec 的「按能力分别判定」对应）
GENERATE = "generate"
EDIT = "edit"


def _config() -> tuple[str, str] | None:
    """(url, token) or None when unconfigured. Never raises."""
    url = os.environ.get("VAAS_MCP_URL", "").strip().rstrip("/")
    token = os.environ.get("VAAS_SERVICE_TOKEN", "").strip()
    if not url or not token:
        return None
    return url, token


def configured() -> bool:
    return _config() is not None


def _post(url: str, token: str, payload: dict, session_id: str | None,
          timeout: float) -> tuple[dict | None, str | None]:
    """One JSON-RPC POST. Returns (parsed_result, session_id). Raises Unavailable."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        # 剧创的服务身份：VAAS hosted 只认网关证明过的 sub，且 fail closed。
        "X-Forwarded-User": os.environ.get("VAAS_SERVICE_IDENTITY", "juchuang-service"),
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sid = resp.headers.get("mcp-session-id") or session_id
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403):
            raise Unavailable("rejected", f"VAAS rejected the service identity (HTTP {e.code}): {detail}")
        raise Unavailable("unreachable", f"VAAS returned HTTP {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Unavailable("unreachable", f"cannot reach VAAS: {e}") from e

    # MCP streamable-http 回的是 SSE：取最后一条 data: 行
    data_lines = [ln[6:] for ln in body.splitlines() if ln.startswith("data: ")]
    if not data_lines:
        raise Unavailable("unreachable", f"VAAS returned no SSE data frame: {body[:200]}")
    try:
        doc = json.loads(data_lines[-1])
    except json.JSONDecodeError as e:
        raise Unavailable("unreachable", f"VAAS returned unparsable frame: {e}") from e
    return doc, sid


def _call_tool(tool: str, arguments: dict, timeout: float = 300.0) -> dict:
    """Invoke one VAAS MCP tool. Returns the tool's own dict payload.

    Raises Unavailable on every 'cannot do it' path, carrying the category that
    VAAS itself reported (lane_down vs unconfigured are different actions).
    """
    cfg = _config()
    if cfg is None:
        raise Unavailable("unconfigured",
                          "VAAS_MCP_URL / VAAS_SERVICE_TOKEN are not set — "
                          "generation is unavailable in this deployment")
    url, token = cfg

    init, sid = _post(url, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "fd-juchuang", "version": "1"}},
    }, None, timeout=30)

    doc, _ = _post(url, token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }, sid, timeout=timeout)

    result = (doc or {}).get("result") or {}
    blocks = result.get("content") or []
    text = blocks[0].get("text", "") if blocks else ""

    if result.get("isError"):
        # VAAS 的分类错误前缀保留下来，让上层能区分「车道坏」与「我们配错」
        low = text.lower()
        if "lane_down" in low:
            raise Unavailable("lane_down", text)
        if "unconfigured" in low or "credential_unavailable" in low:
            raise Unavailable("unconfigured", text)
        raise Unavailable("rejected", text)

    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {"raw": text}


def capabilities() -> dict:
    """Per-capability availability, so the UI can disable one action, not all.

    spec「降级独立模式」：文生图可用而编辑不可用是允许状态，二者 MUST 各自呈现。
    """
    out: dict[str, Any] = {GENERATE: {"available": False}, EDIT: {"available": False}}
    if not configured():
        for cap in out.values():
            cap.update(reason="unconfigured",
                       message="VAAS_MCP_URL / VAAS_SERVICE_TOKEN 未配置")
        return out
    try:
        listing = _call_tool("registry_list_generators", {"type": "image"}, timeout=30)
    except Unavailable as e:
        for cap in out.values():
            cap.update(reason=e.category, message=str(e))
        return out

    for entry in listing.get("items", []):
        lane = entry.get("lane") or {}
        health = lane.get("health")
        driver = entry.get("driver")
        target = EDIT if driver == "edit_image" else GENERATE
        if health == "active":
            out[target] = {"available": True, "generator": entry.get("name"),
                           "lane": lane}
        elif not out[target].get("available"):
            out[target] = {"available": False, "reason": "lane_down",
                           "message": f"{entry.get('name')} lane is {health} "
                                      f"({lane.get('reason_class')})",
                           "lane": lane}
    return out


def generate_image(prompt: str, slug: str | None = None,
                   size: str | None = None) -> dict:
    args: dict[str, Any] = {"prompt": prompt}
    if slug:
        args["slug"] = slug
    if size:
        args["size"] = size
    return _call_tool("generate_image", args)


def edit_image(source: str, instruction: str, slug: str | None = None,
               mask: str | None = None, references: list | None = None,
               n: int = 1) -> dict:
    args: dict[str, Any] = {"source": source, "instruction": instruction}
    if slug:
        args["slug"] = slug
    if mask:
        args["mask"] = mask
    if references:
        args["references"] = references
    if n and n > 1:
        args["n"] = n
    return _call_tool("edit_image", args)

# ─── 视频（change juchuang-video-generation）─────────────────────────────────

VIDEO = "video"


def video_capabilities() -> dict:
    """图生视频与成片渲染**分别**判定（spec：两条通路互不代表）。

    图生视频依赖首帧支持（VAAS `video.grok.first_frame.supported`）；
    成片渲染是本地算力，只要 VAAS 可达就可用。
    """
    out: dict[str, Any] = {
        "i2v": {"available": False},
        "render": {"available": False},
    }
    if not configured():
        for cap in out.values():
            cap.update(reason="unconfigured",
                       message="VAAS_MCP_URL / VAAS_SERVICE_TOKEN 未配置")
        return out
    try:
        listing = _call_tool("registry_list_generators", {"type": "video"}, timeout=30)
    except Unavailable as e:
        out["i2v"].update(reason=e.category, message=str(e))
        out["render"].update(reason=e.category, message=str(e))
        return out

    for entry in listing.get("items", []):
        lane = entry.get("lane") or {}
        if lane.get("health") == "active":
            out["i2v"] = {
                "available": True,
                "generator": entry.get("name"),
                "lane": lane,
                # 首帧是否被验证过。VAAS 的台账把它独立成字段，
                # 不写进 reason 文本——这里正因如此才读得到。
                "first_frame_supported": lane.get("first_frame_supported"),
            }
        elif not out["i2v"].get("available"):
            out["i2v"] = {"available": False, "reason": "lane_down",
                          "message": f"{entry.get('name')} lane is {lane.get('health')}",
                          "lane": lane}

    # 成片渲染走本仓自己的 render_video（本地算力）；只要 VAAS 可达即视为可用。
    if _render_available():
        out["render"] = {"available": True, "cost": "local"}
    else:
        out["render"] = {"available": False, "reason": "unconfigured",
                         "message": "渲染面不可达"}
    return out


def _render_available() -> bool:
    """render_video 是 VAAS 的本地算力工具：能列出工具就说明宿主在。"""
    try:
        _call_tool("templates_list", {}, timeout=20)
        return True
    except Unavailable:
        return False


def submit_video(prompt: str, first_frame: str | None = None,
                 duration: int | None = None, ratio: str | None = None,
                 resolution: str | None = None, slug: str | None = None) -> dict:
    """提交异步视频任务，秒级返回 job_id（不要在这里等出片）。"""
    args: dict[str, Any] = {"prompt": prompt}
    if first_frame:
        args["first_frame"] = first_frame
    if duration:
        args["duration"] = duration
    if ratio:
        args["ratio"] = ratio
    if resolution:
        args["resolution"] = resolution
    if slug:
        args["slug"] = slug
    return _call_tool("generate_video_submit", args, timeout=180)


def video_status(job_id: str) -> dict:
    return _call_tool("video_job_status", {"job_id": job_id}, timeout=60)


def render_carousel(script: str, inputs: dict, slug: str | None = None) -> dict:
    """成片渲染：把 carousel 的 inputs 交渲染面（零上游成本、确定性）。"""
    args: dict[str, Any] = {"type": "carousel", "script": script, "inputs": inputs}
    if slug:
        args["slug"] = slug
    return _call_tool("render_video", args, timeout=900)


def asset_file_path(asset_id: str) -> str | None:
    """账本 id → 渲染面可用的文件路径（downloads 相对路径）。

    VAAS 的 renders/render_video 只认文件路径；账本 id 是给它自己的生成/编辑
    接口用的。两者不能混（2026-11-22 实测：传 id 报 `carousel image not found`）。
    """
    try:
        detail = _call_tool("assets_get", {"id": asset_id}, timeout=30)
    except Unavailable:
        return None
    return detail.get("file_path") or (detail.get("asset") or {}).get("file_path")
