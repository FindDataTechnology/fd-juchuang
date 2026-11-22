"""分镜 → carousel 编译（change juchuang-video-generation tasks 1.2-1.4）。

纯函数测试：不桩任何服务。守三件容易做错的事：
1. 缺图不静默跳过（少一段而成片看不出）；
2. 无台词回落画面描述，且**标明**来源；
3. 同输入两次编译得同一结果（可重放）。
"""
from __future__ import annotations

from fd_juchuang_api.storyboard_render import compile_carousel


def _sb(*shots):
    return {"shots": [dict(s) for s in shots]}


def test_three_shots_map_in_order():
    out = compile_carousel(
        _sb({"idx": 0, "line": "风停了。", "duration_sec": 3},
            {"idx": 1, "line": "你听见了吗？", "duration_sec": 4},
            {"idx": 2, "line": "没有。", "duration_sec": 3}),
        {0: "a.png", 1: "b.png", 2: "c.png"},
    )
    assert out["images"] == ["a.png", "b.png", "c.png"]
    assert out["script"] == "风停了。\n你听见了吗？\n没有。"
    assert [s["shot_idx"] for s in out["segments"]] == [0, 1, 2]
    assert out["missing"] == []
    assert out["duration_source"] == "audio"


def test_shot_order_is_normalised():
    out = compile_carousel(
        _sb({"idx": 2, "line": "三"}, {"idx": 0, "line": "一"}, {"idx": 1, "line": "二"}),
        {0: "a", 1: "b", 2: "c"},
    )
    assert out["images"] == ["a", "b", "c"]
    assert out["script"] == "一\n二\n三"


def test_missing_image_is_reported_not_skipped():
    """缺图必须报出来 —— 静默跳过会让成片少一段而用户不知道。"""
    out = compile_carousel(
        _sb({"idx": 0, "line": "一"}, {"idx": 1, "line": "二"}),
        {0: "a.png"},
    )
    assert out["missing"] == [1]
    # 缺图的那镜**仍在 segments 里**（供 UI 指出是哪一镜）
    assert len(out["segments"]) == 2
    assert out["segments"][1]["image"] is None
    # 但 images 只含有的那几张（不能让 carousel 拿到一个空洞）
    assert out["images"] == ["a.png"]


def test_no_line_falls_back_to_visual_and_says_so():
    out = compile_carousel(
        _sb({"idx": 0, "visual": "雨后空荡的天台", "duration_sec": 3}),
        {0: "a.png"},
    )
    seg = out["segments"][0]
    assert seg["text"] == "雨后空荡的天台"
    assert seg["text_source"] == "visual"
    assert out["script"] == "雨后空荡的天台"


def test_both_empty_uses_placeholder_with_source():
    out = compile_carousel(_sb({"idx": 7}), {7: "a.png"})
    seg = out["segments"][0]
    assert seg["text_source"] == "placeholder"
    assert "7" in seg["text"]


def test_compile_is_deterministic():
    sb = _sb({"idx": 0, "line": "一"}, {"idx": 1, "line": "二"})
    a = compile_carousel(sb, {0: "a", 1: "b"})
    b = compile_carousel(sb, {0: "a", 1: "b"})
    assert a == b


def test_empty_storyboard_is_not_an_error_but_is_empty():
    out = compile_carousel({"shots": []}, {})
    assert out["images"] == [] and out["script"] == "" and out["missing"] == []
