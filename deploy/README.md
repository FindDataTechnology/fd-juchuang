# deploy

照 fd-lex 部署配方（cheap fd-prod + TCR + ArgoCD）。

## 镜像

| 件 | 镜像 | 现网 tag（2026-11-13 实取） |
|---|---|---|
| web | `finddata-business/fd-juchuang-web` | `sha-7f984f3` |
| api | `finddata-business/fd-juchuang-api` | `sha-4c232db` |

tag 随出片产生。**注意**：本仓的 GitHub Actions 已停用自动出片（私有仓组织额度耗尽），
出片走快照仓通道；快照务必用 git URL 入口（本地路径入口有丢新增文件的旧缺陷，
见记忆 `build-snapshot-clean-after-archive-drops-new-files`）。

## 清单真源在哪

**不在本仓。** 剧创的工作负载由 `fd-infra-deploy` 的 Argo app `all-services-prod`
（`all-services/prod/fd-juchuang-web.yaml`、`fd-juchuang-api.yaml`）管理。
改副本数/镜像/环境一律改那两份，本仓不承载 k8s 清单。

2026-11-13 清掉了两处派生死角（自 fd-zhesi 派生时整目录带进来的）：

- `deploy/k8s/fd-zhesi-web.yaml` —— 一份**同名** `fd-zhesi-web` Deployment，镜像停在
  `sha-eb71d36`（线上已是 `sha-1de4801`）。谁 apply 它，改的是**哲思**线上那份，
  等于把兄弟产品回滚；它在本仓纯属误带。
- `deploy/argocd/application.yaml` —— 指向 `fd-zhesi-web.git` 的 Application，
  线上从未存在同名 app（剧创由 `all-services-prod` 驱动）。保留它等于给下一个人
  一个造出重复 app / 抢管对象的机会。

## 边缘

站点 `juchuang.finddatatech.cloud`，回源 NodePort 31903，证书在雷池侧——见
`SAFELINE-CERTS.md`。
