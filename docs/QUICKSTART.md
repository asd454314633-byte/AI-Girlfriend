# 快速开始 / Quick Start

> 当前是**开发预览版源码**，不是“一条命令安装 AI 女友”。本页帮助新访客先验证公开代码中的核心模块，而不会误启动大型模型或修改现有生产环境。

## 1. 先确认包含与不包含的内容

**已提供：** Web UI 与后端工程代码、人物/模式框架、语音适配接口、LiveTalking 相关源码及独立的 `avatar-sync.js`、五组离线测试、来源及许可记录。

**没有提供：** 模型权重、真实人物和声音素材、聊天记忆、用户书稿和个人数据库、现成的完整运行环境。需要按 [模型与资源清单](../provenance/MODEL_AND_ASSET_SOURCES.csv) 自行准备，并遵守各自许可证。

## 2. 克隆代码

要求安装 Git，并使用 PowerShell：

```powershell
git clone https://github.com/asd454314633-byte/AI-Girlfriend.git
cd AI-Girlfriend
```

如需更新已有本地副本，应先查看 `git status`，不要用强制重置覆盖个人修改。

## 3. 无 GPU 的最小验证

如果已经安装 Node.js，且 `node --version` 能输出版本号，可以在 PowerShell 执行：

```powershell
cd runtime/ui/app
node test_avatar_sync_session.cjs
node test_avatar_sync_session_reuse.cjs
node test_avatar_sync_lifecycle.cjs
node test_avatar_sync_ice.cjs
node test_avatar_sync_reading.cjs
```

五组为浏览器与数字人适配器的**离线契约测试**，不依赖 LiveTalking、GPU 或私人资产。若测试失败，记录 Node 版本、错误日志、系统版本并提交 Issue；不要自行删除断言或把失败项跳过当作通过。

## 4. 了解完整系统部署要求

请先阅读 [安装与运行说明](INSTALL.md)：

| 服务 | 示例端口 | 前置条件 |
| --- | --- | --- |
| Web UI + 管理 API | 7860 | Python 依赖与本地配置 |
| 本地大语言模型 | 8090 | 用户自行准备 LLM/运行器；非所有阅读操作必需 |
| 实时语音服务 | 8765 | STT/TTS 模型与软件依赖 |
| LiveTalking 数字人 | 8010 | GPU、相关模型和符合许可的素材 |

**不能把端口当作公共在线服务。** 本仓库没有已验证的全新机器一键安装流程。若只想探索架构，先看 [完整 README](../README.md) 的“整体架构与数据流”章节，无需运行任何服务。

## 5. 数字人适配器的真实测试边界

据 2026-10-13 本机执行报告，独立 `avatar-sync.js` 已在作者**既有** 8010 服务上完成 T1–T6 真实联调。该结果**不等于**其他用户克隆后可直接运行，也未覆盖真实麦克风打断和所有资源压力场景。

详细结论：[真实验收记录](AVATAR_SYNC_REAL_ACCEPTANCE_20261013.md) · [测试边界](TESTING.md)。

## 6. 遇到问题

- 阅读 [完整安装说明](INSTALL.md) 的服务入口和源代码路径。
- 在 [Issues](https://github.com/asd454314633-byte/AI-Girlfriend/issues) 提交复现步骤及脱敏日志。
- 请勿上传人物私人素材、聊天记录、模型密钥、家庭目录截图或受限制模型权重。
- 第三方代码与模型许可：请看 [NOTICE.md](../NOTICE.md) 和 [LICENSE](../LICENSE)。

**English:** This is a source-level development preview. Try the five offline Node.js adapter tests first. A fresh all-service deployment is not yet validated; obtain your own legally usable model weights and assets.
