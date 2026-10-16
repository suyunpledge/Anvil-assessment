# Anvil 考核版

本地模型编码 Agent：qwen3.5:9b 驱动，分级记忆（HR）管上下文，视觉网关（SVSG）管看图。全程本地，断网可用。

- 框架底座：[suyunpledge/Anvil](https://github.com/suyunpledge/Anvil)（工单状态机 + 确定性 gate）
- 记忆服务：[suyunpledge/hierarchical-retrieval](https://github.com/suyunpledge/hierarchical-retrieval)（HR，三级检索）
- 视觉服务：[suyunpledge/svsg](https://github.com/suyunpledge/svsg)（SVSG，结构化视觉问答）

---

## 怎么用

### 环境要求

| 依赖 | 说明 |
|---|---|
| AutoClaw 内嵌 python | 自带 fastapi / uvicorn 等全部依赖 |
| Ollama + 3 个本地模型 | `qwen3.5:9b`（主模型）、`qwen2.5vl:7b`（视觉）、`bge-m3`（嵌入） |
| HR 服务 | `api_server.py`，监听 127.0.0.1:8000 |
| SVSG 服务 | `python -m svsg`，监听 127.0.0.1:3002 |

### 启动（按顺序）

```powershell
$PY = "C:\Program Files\AutoClaw\resources\python\python.exe"

# 0) 环境自检（离线跑，不调模型不花额度，预期 16/16 通过）
& $PY scripts/selftest.py --core

# 1) 起 HR 记忆服务
& $PY <HR目录>\api_server.py

# 2) 起 SVSG 视觉网关（本机有看门狗计划任务，通常已在跑，跳过即可）
python -m svsg

# 3) 起 Anvil 网关 + 工单服务
& $PY gateway/app.py      # 模型调用唯一出口；令牌写 .runtime/service-token
& $PY service/app.py

# 4) 端到端验收（真实 HTTP + 真实本地模型）
& $PY scripts/e2e_check.py
```

VS Code 里用：把 `extension/` 复制到 `~/.vscode/extensions/local-model-ide`，重启编辑器，左侧出现 Local IDE 图标。

### 跑一个工单

1. **提交工单**：指定目标文件 + 任务描述；需要看图的任务在 `visual_tasks` 里声明（不声明则模型看不到视觉工具）
2. **状态机自动执行**：读 → 改 → 语法门 → 类型门 → 测试门，全程无需人工盯守；写操作只允许落在目标文件上
3. **看结果**：gate 全过即工单完成；每轮注入的记忆内容、每次工具调用及入参自动落盘到 `.runtime/integration-log/`（即考核提交物②的接入记录）

---

## 怎么实现的

### 总体结构

```
用户 ──工单──▶ service/app.py ──▶ 状态机引擎
                                   │
                     ┌─────────────┼──────────────┐
                     ▼             ▼              ▼
                  recon 节点     edit 节点     三个 gate（纯程序，不调模型）
                  读/搜/看图     apply_patch   语法→类型→测试
                     │             │
                     ▼             ▼
              SVSG :3002       沙箱（写白名单）
              （visual_query）
                     │
                     ▼
              gateway/app.py ──▶ Ollama（qwen3.5:9b / qwen2.5vl:7b / bge-m3）
                     │
                     ▼
              HR :8000（对话记忆：检索注入 / 对话后入库）
```

### 三个本地模型的分工

| 模型 | 用途 | 接在哪里 |
|---|---|---|
| qwen3.5:9b | 主模型：理解工单、选工具、写补丁 | gateway → Ollama，走 mythos 画像（模型名覆盖） |
| qwen2.5vl:7b | 视觉：截图/报错图/设计稿问答 | SVSG 内部调用，Agent 侧只看到 visual_query 工具 |
| bge-m3 | 嵌入：记忆检索的向量层 | HR 内部使用 |

### 状态机（框架核心）

五节点固定链 `recon → edit → gate_syntax → gate_type → gate_test`，定义在 `core/statemachine/contract.py`：

- **gate 是唯一真相源**：模型说"完成了"不算数，语法/类型/测试三个程序化 gate 全过才算完成
- **按节点授权工具**：读节点只有读工具，改节点只有 apply_patch（防止整文件重写），越权调用直接终止工单
- **写白名单**：写操作只允许落在工单声明的目标文件上——模型改不了测试文件来把测试"改绿"
- **失败回退**：gate 失败按契约表退回 edit 节点修复，同类失败重复出现则退回 recon 重新读上下文

### 记忆接入（HR）

`context/builder.py` 在构建每轮 prompt 时调用 HR：

- **对话前**：用户消息先经 HR `/retrieve`（话题界域路由 → 关键句 → 全文段落），命中的记忆按 token 预算注入；`builder.py` 原有的 AST 切块 + 预算裁剪逻辑（管代码上下文）与记忆注入共用同一套预算，超预算从低优先级开始扔
- **对话后**：有价值的对话内容经 `/ingest` 入库，HR 自动做全文层 + 关键句层两级提炼
- **留痕**：每轮实际注入的 key_sentences / full_contexts、消耗 token 数，落盘 `.runtime/integration-log/`

### 视觉接入（SVSG）

SVSG 注册为状态机工具 `visual_query`（`core/statemachine/tools.py`）：

- **入参** `{image_path, question}`；**出参** SVSG 结构化答案（声明 + 证据锚点，不是一段自由文本）
- **任务路由**：工单声明 `visual_tasks` 字段控制可见性——声明了的工单，recon 节点工具集里才有 `visual_query`；没声明的工单模型想调也调不到。这就是"哪些任务给视觉、哪些不给"的显式开关
- **典型链路**：报错截图 → visual_query 返回"第 23 行 NameError" → edit 节点修复 → 测试 gate 验证

### 工具清单

| 工具 | 挂载节点 | 入参 | 出参 | 约束 |
|---|---|---|---|---|
| `read_file` | recon / edit | `{path}` | 文件内容（带版本号） | 路径必须在沙箱根内 |
| `list_dir` | recon | `{path}` | 目录清单 | 同上 |
| `search_code` | recon / edit | `{pattern}` | 命中行清单 | 同上 |
| `apply_patch` | edit | `{path, old, new, expected_version}` | 补丁结果 | 只落目标文件；版本号不符拒写 |
| `run_shell` | shell（预留） | `{cmd}` | 执行结果 | 命令闸白名单（v1 未开放） |
| `visual_query` | recon（考核版新增） | `{image_path, question}` | SVSG 结构化答案 | 仅 visual_tasks 声明的工单可见 |

### 安全默认

- 全部服务只监听 127.0.0.1，端点强制令牌（服务端强制，客户端开关无效）
- 测试子进程跑在干净环境里（丢代理/凭据，HOME 指向临时目录）
- 沙箱是缓解不是隔离：挡意外与顺手，不挡有意为之

---

## 提交物索引

| 考核提交物 | 位置 |
|---|---|
| ① 仓库 + README | 本文件 |
| ② 接入记录 | `.runtime/integration-log/`（自动落盘） |
| ③ 现场演示 | `docs/演示脚本.md` + 演示数据集 |
| ④ MCP 声明 | 未复用第三方 MCP，接入物均自研（见文首三链接） |
