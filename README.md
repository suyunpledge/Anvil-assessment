# Anvil 考核版 · 本地模型 Agent 工程

> 一个在断网环境下也能稳定干活的编码 Agent：本地模型（qwen3.5:9b）驱动，框架替模型做判断，记忆靠分级检索（HR），看图靠视觉网关（SVSG）。

**项目定位**：考核方向 B「Agent 工程」的提交主体。它不是从零开始的新项目——底座是 Anvil 一期（离线优先的本地模型编码环境，已开源：[suyunpledge/Anvil](https://github.com/suyunpledge/Anvil)），考核版在此基础上接入两件自研服务，构成完整的「上下文管理 + 工具调用 + 视觉理解」Agent 系统。

---

## 一、它是什么

Anvil 考核版 = **工单状态机 + 分级记忆 + 视觉工具**，三层各管一段：

| 层 | 组件 | 职责 | 为什么要它 |
|---|---|---|---|
| 框架层 | 工单状态机（Anvil 核心） | 读→改→语法门→类型门→测试门五节点固定流程；写操作只许落在目标文件；gate 是唯一真相源 | 本地小模型不够聪明，与其相信模型的自觉，不如让框架替它做判断 |
| 记忆层 | HR 分级检索（自研，已开源） | 三级记忆：全文缓存→关键句库→话题界域；对话前检索注入、对话后入库 | 长对话的全文通记会稀释注意力；分级检索把「该装的装进上下文、不该装的裁掉」，是上下文窗口的乘法器 |
| 视觉层 | SVSG 视觉网关（自研，已开源） | 图片+问题 → 检测编译 → 声明验证 → 结构化答案；作为工具挂进状态机 | 编码场景里截图、报错图、设计稿都需要「看」；视觉走独立服务，Agent 只在需要的任务上调用它 |

三层各自的出处与许可：

- Anvil 框架层：本项目 `core/`、`service/`、`gateway/`（MIT）
- HR：[suyunpledge/hierarchical-retrieval](https://github.com/suyunpledge/hierarchical-retrieval)（MIT），以本地服务形式接入（`:8000`）
- SVSG：[suyunpledge/svsg](https://github.com/suyunpledge/svsg)（MIT），以本地服务形式接入（`:3002`）

**未复用任何第三方 MCP 服务**，两件接入物均为自研，故无 MCP 声明项。

## 二、30 秒上手

```powershell
# 解释器：本机用 AutoClaw 内嵌 python（自带 fastapi/uvicorn/依赖）
$PY = "C:\Program Files\AutoClaw\resources\python\python.exe"

# 0) 依赖检查（一次性，离线）
& $PY scripts/selftest.py --core          # 16/16 通过说明环境完好

# 1) 起 HR 记忆服务（分级检索，端口 8000）
& $PY <HR目录>\api_server.py

# 2) 起 SVSG 视觉网关（端口 3002）
python -m svsg                            # 或由看门狗计划任务自动拉起

# 3) 起 Anvil 网关 + 工单服务
& $PY gateway/app.py                      # 模型调用唯一出口：画像 + 审计 + 出网熔断
& $PY service/app.py                      # 状态机 HTTP + SSE；令牌写入 .runtime/service-token

# 4) 端到端验收（真实 HTTP + 真实本地模型）
& $PY scripts/e2e_check.py
```

VS Code 集成：把 `extension/` 复制到 `~/.vscode/extensions/local-model-ide`，重启编辑器，左侧出现 Local IDE 图标。

## 三、模型接入配置

| 项 | 值 | 说明 |
|---|---|---|
| 主模型 | `qwen3.5:9b`（Ollama 本地） | 工具调用走原生 tools 通道（冒烟实测通过）；走 mythos 画像，模型名覆盖 |
| 视觉模型 | `qwen2.5vl:7b`（Ollama 本地） | 由 SVSG 内部调用，Agent 侧无感 |
| 嵌入模型 | `bge-m3`（Ollama 本地） | HR 内部使用，三级检索的向量层 |
| 网关 | `gateway/app.py` | 所有模型调用的唯一出口，带画像、审计日志、出网熔断 |

全部本地化：**零在线 API、零额度消耗、断网可用**（与考核「推荐本地化部署，不推荐在线昂贵接口」对齐）。

## 四、提交物对照（考核四样）

| 考核要求 | 本仓库对应物 | 状态 |
|---|---|---|
| ① 仓库 + README（依赖、启动命令、演示数据、模型接入配置） | 本 README + `docs/` 既有交付文档 | ✅ 本文档 |
| ② 接入记录（模型名、每轮进入上下文的内容清单、工具清单与入参） | `.runtime/integration-log/`（接入日志，自动落盘） | 🔨 接线中 |
| ③ ≤5 分钟现场演示 | 演示脚本 + 演示数据集（见 `docs/演示脚本.md`，编写中） | 🔨 编写中 |
| ④ MCP 复用声明 | 未复用第三方 MCP；两件接入物均自研（见上文出处） | ✅ 无需声明 |

## 五、上下文管理：HR 怎么接

HR（分级检索）在 Anvil 里的位置是「对话记忆侧」：`context/builder.py` 原本管代码上下文（AST 切块 + token 预算裁剪），考核版在同一哲学下把对话记忆也接进来——

- **对话前**：用户消息先经 HR `/retrieve`（按话题界域路由 → 关键句 → 全文段落），命中的记忆按 token 预算注入 system 段
- **对话后**：有价值的对话内容经 `/ingest` 入库（全文层 + 关键句层自动提炼）
- **全链路留痕**：每轮注入了什么（key_sentences / full_contexts）、命中多少 token，自动落盘到接入日志——这就是提交物②的原始数据

设计起源见 `docs/记忆分层-起源.md`（2026-05 构想稿整理版）。

## 六、视觉理解：SVSG 怎么接

SVSG 作为工具注册进状态机（`core/statemachine/tools.py`），工具名 `visual_query`：

- **任务路由显式化**（考核原文「能够指定哪些任务给视觉，哪些不给」）：工单声明里新增 `visual_tasks` 字段——声明了的任务（如「读截图找报错行号」）工具集里含 `visual_query`，未声明的任务工具集里没有它，模型想调也调不到
- **输入输出契约**：入参 `{image_path, question}`；出参 SVSG 结构化答案（声明 + 证据锚点），落进工单审计日志
- **演示场景**：给一张报错截图 → Agent 调 visual_query → 拿到「第 23 行 NameError」→ 状态机走 edit 节点修复 → 测试 gate 验证

## 七、工具清单（状态机 v1 契约）

| 工具 | 挂载节点 | 入参 | 出参 | 沙箱约束 |
|---|---|---|---|---|
| `read_file` | recon / edit | `{path}` | 文件内容（带版本号） | 路径必须落在沙箱根内 |
| `list_dir` | recon | `{path}` | 目录清单 | 同上 |
| `search_code` | recon / edit | `{pattern}` | 命中行清单 | 同上 |
| `apply_patch` | edit | `{path, old, new, expected_version}` | 补丁结果 | 只许落在目标文件（write_scope=target_only） |
| `run_shell` | shell（预留） | `{cmd}` | 执行结果 | 命令闸白名单（v1 未开放） |
| `visual_query` | recon（考核版新增） | `{image_path, question}` | SVSG 结构化答案 | 仅 visual_tasks 声明的工单可见 |

（完整契约见 `core/statemachine/contract.py`；表格与代码同步维护。）

## 八、架构图

```
用户 ──工单──▶ service/app.py ──▶ 状态机引擎
                                   │
                     ┌─────────────┼─────────────┐
                     ▼             ▼             ▼
                 recon 节点     edit 节点      三个 gate（纯程序，不调模型）
                 读/搜/看图      apply_patch    语法→类型→测试
                     │             │
                     ▼             ▼
              SVSG :3002      沙箱（写白名单）
              （visual_query）
                     │
                     ▼
              gateway/app.py ──▶ Ollama（qwen3.5:9b / qwen2.5vl:7b / bge-m3）
                     │
                     ▼
              HR :8000（对话记忆：检索注入 / 对话后入库）
```

## 九、安全默认（不可配置绕过的部分）

| 默认行为 | 说明 |
|---|---|
| 只监听 127.0.0.1 | 网关、工单服务、HR、SVSG 全部本地回环 |
| 所有端点要令牌（除健康检查） | 服务端强制，客户端开关无效 |
| 写操作只落目标文件 | 模型无法改测试文件把测试「改绿」 |
| 测试子进程干净环境 | 丢代理/凭据，HOME 指向临时目录 |
| 沙箱是缓解不是隔离 | 挡意外不挡有意，见 `CODEX-HANDOFF.md` 4.3 |

---

## 许可证

MIT（Anvil 框架层；HR 与 SVSG 各自仓库同许可）。
