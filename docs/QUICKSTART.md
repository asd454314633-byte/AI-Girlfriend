# V3 快速入口

这是开发预览源码，不是权重／私人素材一键包。按[INSTALL.md](INSTALL.md)创建环境、空白初始化、安装合法模型、自己的参考音与Avatar、登记手动任务。

日常运行`Start-AI-Girlfriend.cmd`，访问 http://127.0.0.1:7860 。7860唯一正式启停入口为`scripts/Manage-Project-Service.ps1 -Service ui`，不直接uvicorn。

服务：8090 LLM、8765语音、8010数字人、7860 UI、8781直播调度、8888外部采集；Higgs7077／Fish3030按需单后端驻留。

默认DARK ROAST9B＋qwen3＋你自己的参考音；本包不含“小烟”。设置中切换模型、声音和情绪；Higgs affection/amusement不会自动作用于Qwen Base。

`FRESH_INSTALL_E2E=HOLD`；源码检查、作者现有环境实测和新机器安装分别记账。详见[TESTING.md](TESTING.md)及[Agent部署指引](AI_AGENT_FULL_DEPLOYMENT_ZH.md)。
