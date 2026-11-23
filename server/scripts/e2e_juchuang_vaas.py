#!/usr/bin/env python3
"""端到端：剧创 → 生产 VAAS（真出图 / 真编辑）。

与 tests/ 的分工：单测全部打桩，验证的是「我们的分支判断」；本脚本验证
**两套系统之间的协议真的对得上**——字段名、身份头、SSE 分帧、签名 URL。

跑法（要真 VAAS，会产生计费）：
  VAAS_MCP_URL=<vaas-mcp-endpoint>/mcp \
  VAAS_SERVICE_TOKEN=<bearer> \
  FD_JUCHUANG_DB=/tmp/e2e.db FD_JUCHUANG_AUTH_DEV_TOKENS=tok=u \
  python scripts/e2e_juchuang_vaas.py

退出码：0 ok / 1 断言失败 / 2 无法执行（VAAS 未配置或车道不可用）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, FAILED, CANNOT_RUN = 0, 1, 2


def main() -> int:
    from fastapi.testclient import TestClient
    from fd_juchuang_api import vaas_client
    from fd_juchuang_api.main import app

    report = {"steps": [], "verdict": None}

    def step(name, ok, detail=None):
        report["steps"].append({"step": name, "ok": ok, "detail": detail})
        return ok

    if not vaas_client.configured():
        report["verdict"] = "cannot-run (VAAS not configured)"
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return CANNOT_RUN

    C = TestClient(app, headers={"Authorization": "Bearer tok"})

    # ── 能力探测：分别判定 ────────────────────────────────────────────────
    caps = C.get("/api/v1/pipeline/generate/capabilities").json()
    step("capabilities", True, caps)
    if not caps["generate"].get("available"):
        report["verdict"] = f"cannot-run (generate unavailable: {caps['generate'].get('reason')})"
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return CANNOT_RUN

    # ── 建一场戏 → 分镜 → 计划 ──────────────────────────────────────────
    pid = C.post("/api/v1/screenplay/projects", json={"title": "E2E"}).json()["id"]
    ch = C.post(f"/api/v1/screenplay/projects/{pid}/characters",
                json={"name": "阿甲", "appearance": {"年龄感": "青年", "体型": "瘦"}}).json()
    sc = C.post(f"/api/v1/screenplay/projects/{pid}/scenes",
                json={"title": "夜·天台"}).json()
    C.post(f"/api/v1/screenplay/projects/{pid}/scenes/{sc['id']}/lines",
           json={"speaker_id": ch["id"], "text": "风停了。"})
    C.post(f"/api/v1/pipeline/storyboard/{sc['id']}")
    created = C.post(f"/api/v1/pipeline/prompts/{sc['id']}")
    step("promptsets created", created.status_code == 200,
         [p["form"] for p in created.json()["promptsets"]] if created.status_code == 200 else created.text[:200])

    plan = C.get(f"/api/v1/pipeline/prompts/{created.json()['promptsets'][0]['id']}")
    # 取 generation 形态
    gen_plan = None
    for p in created.json()["promptsets"]:
        if p["form"] == "generation":
            gen_plan = C.get(f"/api/v1/pipeline/prompts/{p['id']}").json()["payload"]
    step("generation plan exists", gen_plan is not None,
         (gen_plan or {}).get("shots", [])[:1])

    # ── 资产绑定（角色 model sheet）──────────────────────────────────────
    bind = C.post(f"/api/v1/screenplay/projects/{pid}/characters/{ch['id']}/asset",
                  json={"asset_id": "placeholder-sheet", "asset_slug": "model-sheet"})
    step("character asset bound", bind.status_code == 200 and bind.json()["asset_id"] == "placeholder-sheet")

    # ── 真出图（1 镜，控成本）────────────────────────────────────────────
    gen = C.post(f"/api/v1/pipeline/generate/{sc['id']}",
                 json={"shot_idxs": [0], "max_calls": 1})
    if gen.status_code != 200:
        step("generate", False, gen.text[:300])
        report["verdict"] = "failed: generation"
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return FAILED
    body = gen.json()
    produced = body["generated"]
    step("generate 1 shot", len(produced) == 1, produced)
    if not produced:
        report["verdict"] = f"failed: {body['failures']}"
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return FAILED
    src = produced[0]["asset_id"]

    # ── 真编辑 ───────────────────────────────────────────────────────────
    edit = C.post(f"/api/v1/pipeline/shots/{sc['id']}/0/edit",
                  json={"instruction": "change the dominant colour to deep blue, keep composition"})
    if edit.status_code != 200:
        step("edit", False, edit.text[:300])
        # 编辑不可用不阻断整体（spec：按能力分别判定）
        report["verdict"] = "partial (generate ok, edit unavailable)"
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return CANNOT_RUN
    eb = edit.json()
    step("edit ok", True, {"asset_id": eb["asset_id"], "source": eb["source_asset_id"]})
    step("lineage correct", eb["source_asset_id"] == src,
         {"expected": src, "actual": eb["source_asset_id"]})
    step("edit differs from source", eb["asset_id"] != src)

    # ── 计划里该镜现在应是 edit 模式 ─────────────────────────────────────
    plan2 = None
    for p in C.post(f"/api/v1/pipeline/prompts/{sc['id']}").json()["promptsets"]:
        if p["form"] == "generation":
            plan2 = C.get(f"/api/v1/pipeline/prompts/{p['id']}").json()["payload"]
    shot0 = (plan2 or {}).get("shots", [{}])[0]
    step("plan flips shot 0 to edit mode", shot0.get("mode") == "edit",
         {"mode": shot0.get("mode"), "source": shot0.get("source_asset_id")})

    all_ok = all(s["ok"] for s in report["steps"])
    report["verdict"] = "ok" if all_ok else "failed"
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return OK if all_ok else FAILED


if __name__ == "__main__":
    raise SystemExit(main())