# V3 九类功能→真实源码→依赖→配置→测试

生产路径与公开路径使用相同相对目录；已通过来源审查的公开avatar-sync独立实现例外，不能用受限生产派生版本覆盖。路径移植、参考音脱敏与空白模板差异记录在来源清单。下列每一行是实际源码，而非页面占位。

| 类别 | 生产／公开源码路径 | 依赖与配置 | 测试依据／限制 |
|---|---|---|---|
| 1 中文前端 | runtime/ui/app/index.html、main.js、ui/、server.py、local_api.py | FastAPI/httpx、package.json；environment.example.ps1 | JS语法、手动面板回归、UI导入115条路由；本次不重跑GPU浏览器 |
| 2 LLM | runtime/ui/app/models_admin.py、runtime/llm/Switch-Girlfriend-LLM.ps1、start_llama_task.bat、runtime/core/providers/manifests/llm.json | llama.cpp／三GGUF，config/models.example.json | 作者三模型真实切换／推理／回滚历史PASS；新环境NOT_TESTED |
| 3 TTS | runtime/s2s_business/、runtime/core/providers/adapters/、vendor_fixes/、runtime/higgs/higgs_worker.py、runtime/windows/Switch-Girlfriend-Voice.ps1、third_party_modified/speech_to_speech_0.2.11/ | 0.2.11 overlay／Qwen／Higgs DLL／s2.cpp；tts.json | Python语法／关键导入／参考透传测试；三TTS现有环境历史PASS，新机HOLD |
| 4 音色库 | runtime/ui/app/voice_library_admin.py、ui/voice_library.js、higgs_worker.py | FFmpeg／CPU Whisper／Higgs；自己的ref.wav/ref.txt | 42项临时文件／fake worker隔离测试；实际克隆链历史PASS，相似度主观HOLD |
| 5 情绪 | runtime/core/providers/delivery.py、capabilities.py、tts_client.py、manifests/tts.json、runtime/ui/app/ui/girlfriend.js | 各模型原生控制 | 面板枚举／后端隔离回归；affection/amusement作者用户试听通过；determination音色漂移；Qwen独立情绪不宣传 |
| 6 数字人 | runtime/avatar/app/server/、avatars/wav2lip_avatar.py、runtime/ui/app/avatar-sync.js、audio-router.js | LiveTalking固定版本、合法Wav2Lip256/s3fd与自己的Avatar，avatar.json | 公开独立适配器5套离线PASS；旧环境有限实测文档保留，新机HOLD |
| 7 书库陪读 | runtime/reading/、runtime/reading_v2/、runtime/girlfriend_reading.py、runtime/ui/app/reading_admin.py、ui/reading*.js、reading_bridge.py | SQLite空schema、虚构样章、0.2.11 overlay | 28项隔离管理测试；零音频／取消／边界源码保留，稳定长文不重复压测 |
| 8 直播 | runtime/live/{service,router,normalizer,adapter,session,dedupe,barrage_bridge}.py、runtime/ui/app/live_interaction.js | BarrageGrab8888外部输入，config/live.example.json | 23项既有隔离逻辑／本地模拟E2E历史PASS；REAL_PLATFORM_FRESH_EVENT=HOLD；礼物未实现 |
| 9 服务管理 | runtime/ui7860_manager/{manager,Console,fixture_app,test_manager}、scripts/Manage-Project-Service.ps1、Native-Service-Console.ps1、Register-ProjectTasks.ps1、Start-AI-Girlfriend.cmd | Windows HMAC/ACL、独立visible console；本机新登记，不发布key | 管理器44项隔离生命周期及源码身份验证；本次不控制生产 |

九类均包含合法自研／已归因源码，完整运行仍需外部依赖。无法合法打包不代表不存在，也不把“说明了缺失”包装为开箱即用。Higgs worker必须把请求reference_audio_path/reference_text传给native clone调用，本版保留该修复。

## 2026-10-18 增量（聊天/陪读范围）

| 变更 | 文件 | 说明 |
|---|---|---|
| 音色上传自动识别 | runtime/ui/app/voice_library_admin.py、ui/voice_library.js | 新增 /api/voices/transcribe：上传参考音频先经本机 Whisper 提取参考文字，可编辑后保存；不保存/不激活音色 |
| 语音面板收纳 | runtime/ui/app/index.html、main.js、three_modes.css（新）、ui/girlfriend.js、ui/voice_library.js | 声音与音色面板移入聊天区折叠面板；设置页标签变为跳转入口；聊天变长自动收起 |
| 语音状态刷新 | runtime/ui/app/ui/girlfriend.js | 去重并发刷新、按签名重渲染、窗口聚焦/可见性/定时轻量回读 |
| 数字人媒体就绪等待 | runtime/ui/app/main.js | 开始通话前有界等待数字人媒体就绪（≤8s），避免首轮语音回落本机 |
| 会话容量握手 | runtime/ui/app/s2s-realtime-client.js | realtime 握手期间识别 session_limit_reached，立即以明确错误失败，不再悬挂 |
| 后台控制台选项 | runtime/ui7860_manager/{manager.py,Console.ps1}、scripts/Runtime-Common.ps1、runtime/windows/v3_console_mode.example.json | 可选单管理台模式：v3_console_mode.json 声明 background 时服务控制台隐藏、日志落文件；缺省/缺文件保持可见控制台行为不变 |
