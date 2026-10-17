# core 快照说明

本目录起源于本地原生框架快照。自 2026-10-17 原生集成版起，由 Anvil 本仓维护，
包含工单隔离、记忆/视觉接入、模型适配与安全修复。旧镜像同步只允许 `--check`；
上游更新须手工移植并运行全部回归，防止覆盖本地集成。

- vendor: local-model-framework
- synced_at: 2026-09-28 18:57:52
- upstream: local-model-framework（本地检出，路径已省略）
- upstream_rev: (非 git 仓库)
- entries: statemachine→core/statemachine, compatibility→core/compatibility, verification→core/verification

## 同步前的差异检查

若此处列出条目，说明上次同步后本地有改动（或上游有更新）。`仅本地` 项尤其要注意：那是快照被就地改过的痕迹。

- 无差异（快照与上游一致）
