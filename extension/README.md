# Local Model IDE（Anvil VS Code 扩展）

把 Anvil 工单状态机装进 VS Code：本地模型改代码，三道确定性 gate 验证，人在 diff 里确认落盘。

## 功能

- **侧栏工作台**（活动栏 Local IDE 图标）：建 code/tidy 工单、SSE 实时事件流、gate 结果、待确认 diff 审阅
- **视觉工单**：勾选后模型可调用 visual_query 查看沙箱内图片（工单级授权）
- **右键建单**：资源管理器/编辑器中对 `.py` 文件右键 → 「Local IDE: 对此文件建工单」
- **三模块就绪**：一键检查代码模型（qwen3.5）、视觉模型（qwen2.5vl）、嵌入模型（bge-m3）
- **解释选中代码**：编辑器右键，选中的代码发给本地模型解释
- **确认落盘**：未点确认前真实文件字节级不变（autoApply 默认关）

## 使用

1. 先启动后端（Anvil 仓库根目录）：

   ```powershell
   .\start.ps1          # = python anvil.py --port 8099
   ```

2. VS Code 打开任意文件夹（或直接打开 Anvil 仓库），安装本扩展（F5 调试或装 vsix）
3. 令牌自动从 `<工作区>/.runtime/service-token` 读取；也可在设置里手填 `localIde.serviceToken`
4. 服务地址默认 `http://127.0.0.1:8099`（单进程版，网关在 `/model` 子路径）

## 安全设计

扩展本身**不做任何模型调用、不碰文件**——只发工单、看事件、开 diff。所有判断在服务端：
gate 是唯一真相源，模型自称完成不算数；写操作只落工单声明的目标文件。

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `localIde.serviceUrl` | `http://127.0.0.1:8099` | Anvil 单进程服务地址 |
| `localIde.gatewayUrl` | `http://127.0.0.1:8099/model` | 模型网关（OpenAI 兼容） |
| `localIde.defaultAdapter` | `qwen3.5` | 默认模型画像 |
| `localIde.autoApply` | `false` | 完成后是否自动落盘（默认必须人确认） |
| `localIde.serviceToken` | 空 | 令牌（空则自动从 .runtime/service-token 读） |
