# fd-juchuang

剧创（Juchuang）识律家族成员的单仓：web 三入口（`/` 官网 landing、`/workbench` 工作台、`/admin` 管理后台）+ FastAPI 服务端（个人层自有存储：剧本项目 / 角色卡 / 场次）。

成熟度 **incubation（孵化中）**：有站点、无商业面——不提供报价、客户组织或计量计费。成熟度与「今天能买什么」的唯一准星是 `fd-lex/docs/product-lineup.json`，本仓不自行声明。

## 产品定位

AI 时代的剧本工坊。功夫花在上游——结构、冲突、立场，画面只是最后一层皮肤：

- 剧本项目、角色、场次、台词全部结构化；
- 角色卡锁定外观要素，场次携带冲突元数据，保证跨镜头的角色一致性；
- 版本快照可回滚，整树可导出 JSON；
- 语料受公版边界约束（见 `fd-text-core` 的剧本垂域口径）。

## 仓结构

| 目录 | 内容 |
|---|---|
| `web/` | React 19 + Vite + TS + Tailwind v4 + i18next 双语（fd 舰队配方） |
| `server/` | FastAPI；包名 `fd_juchuang_api`，入口 `fd_juchuang_api.main:app` |
| `deploy/` | cheap fd-prod + TCR + ArgoCD（照 fd-lex 部署配方） |

## 本地开发

```sh
cd web && npm install && npm run dev
cd server && pip install -e ".[dev]" && pytest && uvicorn fd_juchuang_api.main:app --port 8010
```

## 姊妹仓

`fd-zhesi`（哲思）、`fd-shilun`（实论）、`fd-zhengxin`（政心）——同一家族、同一形制、各自独立售卖。

## 派生痕迹已清（2026-11-13）

本仓自 `fd-zhesi` 派生，早期带进了几处同名残件，已处理：

- `web/package.json` / `package-lock.json` 的包名 `fd-zhesi-web` → `fd-juchuang-web`
- `server/pyproject.toml` 的包名 `fd--api` → `fd-juchuang-api`
- 删除 `deploy/k8s/fd-zhesi-web.yaml`（同名的**哲思** Deployment，apply 会回滚兄弟产品）
  与 `deploy/argocd/application.yaml`（指向 zhesi 仓的僵尸 Application）

工作负载清单的真源在 `fd-infra-deploy` 的 `all-services-prod`，不在本仓——详见
`deploy/README.md`。
