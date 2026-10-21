# fd--api

剧创门面服务端：/healthz、/api/version；结构化剧本、Fountain/FDX 交换、连续性台账、
底料管线（分镜/提示词）与剧作质量门。详见仓根 README。

## 测试与端到端验收

```bash
python3 -m pytest tests/ -q                     # 全量离线测试（CI 门禁）
python3 scripts/e2e_juchuang.py                 # 端到端全链（临时库，离线）
python3 scripts/e2e_juchuang.py --json-report /tmp/e2e.json
python3 scripts/e2e_juchuang.py --base http://127.0.0.1:8016 --token "$TOK"
```

`tests/` 覆盖三层：

| 文件 | 范围 |
| --- | --- |
| `test_screenplay.py` / `test_fountain.py` | 剧本五表、版本树、Fountain/FDX 往返与未识别元素报告 |
| `test_pipeline.py` | 分镜字段（staging/keyframe/model sheet/art bible）、提示词三形态、旧行空值读取 |
| `test_ledger.py` | 迁移回滚、双时态事实、伏笔生命周期、知识隔离、生成前上下文、生成后检查、空台账降级 |
| `test_quality.py` | 六维 rubric、问题定位、连续性维引用台账事实、合规红线阻断、策略与持久化 |
| `test_zhesi.py` / `test_skeleton.py` | 哲思契约消费与降级、门面探活 |
| `test_e2e_integration.py` | **5.1** 子进程跑 e2e 脚本并校验 JSON 报告；畸形导入、空项目、缺失角色、性能上界（20 场 × 30 台词） |
| `test_regression_compat.py` | **5.2** 旧剧本（无台账/无新字段）创建、分镜、导出、版本树不阻断；**5.3** 哲思不可达时台账与质量门独立可用（含静态 import 检查） |

`scripts/e2e_juchuang.py` 的 check id 即覆盖矩阵（文件头有完整清单），主要断言：

- **5.1 全链**：建项目/角色/场次/台词 → Fountain 导出→导入（场次/角色/台词/立场/冲突保持）
  → 台账（事实时点、角色知识隔离、伏笔逾期、生成前上下文）→ 分镜（扩展字段、
  生成前上下文注入、生成后越知拦截并定位台词）→ 逐镜编辑 → 三形态提示词 →
  六维质量门（问题定位、引用台账事实、红线阻断、交付 409）→ 项目/Fountain/FDX/
  镜头表/提示词集导出。
- **5.2 回归**：旧剧本无台账、旧版镜头行缺扩展字段、无 synopsis/action/conflict，
  创作流程与导出不阻断；质量门连续性维 `available=false`、`score=None` 并明示原因。
- **5.3 无哲思**：`/kernel/link` 降级 standalone 后，台账写入/查询与质量门评估仍
  200 且连续性维可用；台账/质量门/管线的 import 图不含 `zhesi_client`/`kernel`。
