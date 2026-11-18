# Qwen2.5-Coder-7B 适配与验收

默认模型已设为本机安装的 `qwen2.5-coder:7b`（Q4_K_M），画像为 `qwen-coder`。配置入口是 `anvil.toml` 的 `models.code` 和 `models.adapter`；HTTP 建单、RunnerOpts 与 `/presets` 使用同一个默认值。旧配置未填写 adapter 时，按已知模型名推导原画像，保留显式选择其他模型的能力。

## 调整

- Qwen Coder 的侦察、编辑和问答默认采用 8192 上下文、2048 输出预算、低温采样，并请求 Ollama 驻留 10 分钟。不发送 `think`，不同时发送 `format` 与 tools。32K 仍是画像上限，8K 是本机交互配置，不是模型能力上限。
- 工具调用优先接受原生 `tool_calls`，兼容正文 JSON。补丁 schema、提示和返回参数校验统一使用文本锚点；不再向该模型展示行号补丁。允许空 new_string 表示删除，仍要求非空 old_string。
- 中文文档修改使用引号内的唯一文字作锚点，避免把 Python 三引号误当成 JSON 字符串边界。补丁失败时框架刷新源码，下一轮直接修正补丁；工具读取结果补齐对应的 assistant 调用历史。
- 大文件侦察复用按任务选函数的上下文。请求发送前估算消息、工具 schema 和输出预算，超限明确返回；这不是精确 tokenizer。输出达到长度上限时拒绝执行残缺补丁。
- 原生传输错误统一映射到适配层失败类型，保留取消语义；健康检查按完整模型标签匹配。运行画像按适配器和模型隔离，避免继承其他模型的节点覆盖。
- `/integrations` 增加 `available_models`、`default_adapter`、`code_ready`。IDE 主进程据此标记缺失模型，继续兼容原来的接口字段和前端下拉框。

## 实测

运行 `scripts/qwen_acceptance.py`，使用独立端口、运行目录和测试项目，走真实 HTTP 建单、原生调度、Ollama、状态机、校验及确认写入。请求不传 adapter/model，以验证真实默认值。

| 场景 | 最终热态耗时，到待确认状态 | 校验与落盘 |
| --- | ---: | --- |
| 实现 average，空列表返回 0.0 | 2.610 秒 | 语法、类型、测试通过；确认后写入 |
| 修改 average 的中文文档 | 2.625 秒 | 三项校验通过；文档文字精确匹配 |

确认前原文件保持不变；测试文件和其他函数未改变；落盘后 3 项测试全部通过。最初冷态任务为 40.312 秒，其中首次模型请求 37.875 秒（包含加载、提示处理及生成，不能视为纯加载时间）。以上为两个小型 Python 任务的观察值，不代表大型项目、Java/C/C++ 或复杂 Agent 任务性能，也不是优化前后性能基准。

初次文档任务确实暴露了三引号补丁错误及重复读文件问题，记录保存在 `.runtime/qwen-acceptance/c03f38e03b/report.json`。修复后两次复验通过，最终记录在 `.runtime/qwen-acceptance/14f40d798a/report.json`，同目录保留服务日志与模型审计。

Ollama `/api/ps` 报告本次模型上下文为 8192，`size_vram` 为 4,994,472,344 字节。这是模型驻留统计，不等于整机显存占用，也未与 16K 做性能对照。

## 回归与生效

- 后端 `scripts/selftest.py --core --backend-only`：21/21 套件通过，包含 12 项新增适配回归；原有环境条件跳过共 3 项。
- IDE 主进程 TypeScript 检查通过；主进程运行时 11 项测试通过；真实后端接口契约测试 1 项通过。
- IDE 仅构建 `out/main`；renderer 的 9 个源文件和已有构建产物均保持原样。当前已打开的 IDE 下次重启加载新主进程，后端下次启动读取新默认配置。
- 验收子服务已退出，没有安装或下载其他模型。

本机未安装配置中的 `qwen2.5vl:7b` 和 `bge-m3:latest`。因此代码模型资源就绪，但全能力诊断 `ok` 仍为 false；本次真实验收关闭了 HR，未验证视觉或记忆入库，不更换嵌入维度或现有记忆数据。

## 前端接入

AnvilIDE 的 `src/renderer/src/anvilpanel.ts:166` 仍写死“代码 qwen3.5 / 视觉 qwen2.5vl / 记忆 bge-m3 就绪”，且不可用项目前只加文字标记。按用户要求留给 Claude 修改：状态文案应读取 integrations.models 与 code_ready，各能力分别判断；不可用选项可根据 bad 禁用。旧文案不代表后端实际仍使用 Qwen3.5。

参考官方协议：[Qwen 工具调用](https://qwen.readthedocs.io/en/v2.5/framework/function_call.html)、[Ollama 结构化输出](https://docs.ollama.com/capabilities/structured-outputs)。具体策略以上述本机实测为准。
