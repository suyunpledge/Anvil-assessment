# 内置组件来源与维护方式

本目录是固定源码快照，不是对子目录以外的本机路径做动态导入。仅复制源包、测试和项目元数据；不复制 `.env`、数据库、凭据、缓存、图片或模型权重。

| 组件 | 来源 | 基线 | 许可证证据 |
|---|---|---|---|
| HR 0.5.0 | https://github.com/suyunpledge/hierarchical-retrieval | dca400f23b118dae72c6f36d89a0166b15996540 | `hr/LICENSE`，原样保留 MIT 文本 |
| SVSG 5.2.1 | https://github.com/suyunpledge/svsg | 88b4dc2ab8dcf65545d51858c47e94fd4824a27f | 源项目 README 声明 MIT；本机源副本未提供独立 LICENSE 文件，本仓不杜撰版权人或许可证文本 |

SVSG 基线包括本机源工作区已存在的修改，文件清单记录在 `manifest.json` 的 `local_changes`；每个源包文件均附 SHA-256，可以定位“提交基线 + 未提交内容”的准确版本。源项目的这些修改未被覆盖或重置。

Anvil 的集成适配位于 `integration/`，包括统一模型传输、真实像素输入、区域复核、项目隔离、待入库队列与工单日志；上下文及状态机的接入调整留在本仓对应模块。上游源码的后续改动应手工移植，并重新执行 HR、SVSG 及 Anvil 回归。

检测运行时依赖 Ultralytics，模型权重是单独准备的外部资产；其源代码和模型许可证不能由本项目 README 中的 MIT 声明替代。对外再分发代码、权重或服务前，应按所使用版本的实际许可证处理。仓库不打包 YOLO 权重或用户图片。
