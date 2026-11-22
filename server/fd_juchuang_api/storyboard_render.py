"""分镜 → carousel 输入（change juchuang-video-generation task 1.1）。

为什么是 carousel 而不是新类型（design D2）：carousel 的语义就是「一图一段口播 +
Ken Burns + 交叉淡入」，正是分镜的形状。VAAS 侧 `video-types` 的 delta 也刚把
「不为单一数据源新增类型」写成硬约束——为剧创新建一个 `storyboard` type 等于
把同一件事实现两遍，且第二次实现必然先于第一次腐烂。

纯函数、不依赖网络、不依赖数据库：调用方把「每镜的图」传进来。
这样单测能直接验，不需要桩任何服务。

一条容易误解的语义：**段时长以音频为准，不以镜头时长为准**。carousel 的既有
语义是「旁白说完这一段，画面就切」；剧创侧的 `duration_sec` 用于生成预估，
不构成对成片时长的强制。返回值里显式带 `duration_source` 说明这一点。
"""

from __future__ import annotations

from typing import Any


def compile_carousel(storyboard: dict, images_by_shot: dict[int, str]) -> dict:
    """把一场戏的镜头表编译成 carousel 的 inputs。

    Args:
        storyboard: 镜头表，形状 `{"shots": [{"idx": 0, "visual": "...", ...}]}`。
            也接受 `{"id": ..., "shots": [...]}`（pipeline 的 storyboard 形状）。
        images_by_shot: `{shot_idx: 图路径或资产 id}`。缺图的镜头**不会被跳过**，
            而是整场报缺失（见返回的 `missing`）——静默跳过会让成片少一段而用户不知道。

    Returns:
        carousel 的 inputs（`script` 口播稿 + `images` 图清单），外加：
          - `segments`: 每段对应哪一镜（供前端对齐与排错）
          - `missing`: 缺图的镜头序号（非空时调用方 **不得**发起渲染）
          - `duration_source`: 恒为 "audio"，明示段时长口径
    """
    shots = sorted(storyboard.get("shots") or [], key=lambda s: s.get("idx", 0))

    missing: list[int] = []
    lines: list[str] = []
    images: list[str] = []
    segments: list[dict[str, Any]] = []

    for shot in shots:
        idx = shot.get("idx", 0)
        # 口播文本：台词优先；没台词回落到画面描述（默片镜头也要有话说，
        # 否则 carousel 那一段是静音——不是错误，但用户该知道来源）
        text = (shot.get("line") or shot.get("line_text") or "").strip()
        source = "line"
        if not text:
            text = (shot.get("visual") or shot.get("action") or "").strip()
            source = "visual"
        if not text:
            text = f"第 {idx} 镜"
            source = "placeholder"

        image = images_by_shot.get(idx)
        if not image:
            missing.append(idx)

        lines.append(text)
        if image:
            images.append(image)
        segments.append({
            "shot_idx": idx,
            "text": text,
            "text_source": source,
            "image": image,
            "duration_sec": shot.get("duration_sec"),
        })

    return {
        "script": "\n".join(lines),
        "images": images,
        "segments": segments,
        # 段时长以音频为准：carousel 按旁白切段，镜头时长只用于生成预估
        "duration_source": "audio",
        "missing": sorted(missing),
    }
