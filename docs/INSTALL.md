# V3 Windows 安装：开发预览源码版

本包不含模型、原生DLL、人物和音色素材。**FRESH_INSTALL_E2E=HOLD**；静态重建不等于新机器已通过全链路。不要将指南命令直接用于已有生产实例。另见[中文Agent指引](AI_AGENT_FULL_DEPLOYMENT_ZH.md)、[功能映射](V3_CAPABILITY_SOURCE_MAP.md)。

## 环境与空白初始化

Windows10/11 x64、PowerShell7、Python3.12、Git、Node.js、FFmpeg、兼容的NVIDIA驱动／CUDA／torch。Avatar可单独使用上游支持的Python环境。作者现有GPU约24GB，LLM和一个TTS共享；不是显存最低值承诺。权重、上下文和Avatar共同影响占用，禁止同时驻留三个TTS。

```powershell
git clone https://github.com/asd454314633-byte/AI-Girlfriend.git
cd AI-Girlfriend
py -3.12 -m venv runtime/core/venv
& runtime/core/venv/Scripts/python.exe -m pip install -r config/requirements-control.txt
& runtime/core/venv/Scripts/python.exe scripts/initialize_public.py
```

初始化只写缺失配置、空目录和新的本机Higgs API key，绝不导入用户数据库。管理器首次启动另外生成owner-only HMAC登记，不提交密钥／登记／运行状态。书库及角色库按源码schema首次初始化。

## 合法依赖与模型

| 组件 | 位置／配置 | 来源与边界 |
|---|---|---|
| llama.cpp b11065或经验证的兼容版 | `tools/llama.cpp/llama-server.exe` | https://github.com/ggml-org/llama.cpp ，MIT，二进制不打包 |
| DARK ROAST9B、Gemma4 12B、Qwen3 4B GGUF | `runtime/llm/models.json`，由 `config/models.example.json` 初始化 | 按模板文件名／哈希下载；量化发布者与基础模型许可证分别核对 |
| Qwen3-TTS Base1.7B | `models/native/qwen3/` | https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base |
| Whisper large-v3 | `models/native/whisper-large-v3/` | https://huggingface.co/Systran/faster-whisper-large-v3 ，使用CTranslate2布局 |
| Higgs v3 Q4及Studio0.3.0 native engine | `tts_candidates/higgs_v3_q4/runtime/Higgs Audio v3 Studio 0.3.0 Portable/` | https://github.com/Saganaki22/Higgs-Audio-v3-Studio ，引擎／DLL未再分发，权利未独立确认；可设置HIGGS_ENGINE_DIR/HIGGS_MODEL_ROOT |
| Fish S2 Pro Q4及s2.cpp | `external/fish-s2-pro/`，按TTS manifest配置 | https://huggingface.co/fishaudio/s2-pro ，https://github.com/rodrigomatta/s2.cpp ，本地版本2c33261938da1a41d713768b1b391b4d368d7d2c；Fish Research License，商业另授权，不打包 |
| LiveTalking及Wav2Lip256/s3fd | 已归因源码在`runtime/avatar/app/`，权重／Avatar另取 | https://github.com/lipku/LiveTalking @ c963ad409c556918b7d23999bf87c47a7c05c932；Wav2Lip模型有研究／非商业限制，不能标为本仓库MIT |
| BarrageGrab | 外部服务`ws://127.0.0.1:8888/` | https://github.com/monsterxwx/BarrageGrab ，未打包，许可证及平台条款自行核对 |

完整来源见`provenance/MODEL_AND_ASSET_SOURCES.csv`。不因列出上游就保证所有依赖可再分发。Higgs主模型档位4B，界面约4.65B和Fish约4.95B是本地GGUF全部组件的张量计数；Q4是量化，不是4B。

先按官方torch安装指引选择适配驱动的torch/torchaudio CUDA build，再执行：

```powershell
& runtime/core/venv/Scripts/python.exe -m pip install -r config/requirements-speech.txt
& runtime/core/venv/Scripts/python.exe scripts/install_s2s_patches.py
& runtime/core/venv/Scripts/python.exe -m pip install -r runtime/ui/app/requirements.txt
py -3.12 -m venv runtime/avatar/venv
& runtime/avatar/venv/Scripts/python.exe -m pip install -r runtime/avatar/app/requirements.txt
```

speech-to-speech固定0.2.11，overlay安装器备份原文件、应用当前4份补丁并安装providers bootstrap hook。重装wheel后重新应用。`runtime/core/windows-production-requirements.txt`是作者环境快照，不是通用安装锁文件。

## 自己的参考音与Avatar

准备合法`models/native/qwen3/ref.wav`及准确`ref.txt`。可选运行`pwsh -File scripts/Make-Synthetic-Reference.ps1`生成OS合成测试参考，不是私人小烟。Higgs默认读取同份参考，可用HIGGS_REFERENCE_AUDIO/HIGGS_REFERENCE_TEXT覆盖。历史默认ID`xiaoyan`仅兼容标识，显示“默认参考音（自行提供）”。Fish需其合法工具链制作`external/fish-s2-pro/voices/default.s2voice`。

在线音色库完整支持面为Higgs逐请求上传／ASR／文本修正／试听／激活／回收站。Qwen参考是启动级，Fish命名音色需外部工具，不宣传三后端有相同在线克隆能力。

Avatar使用自己的合法图片／视频和模型，编辑AVATAR manifest的AVATAR_ID。**禁止从docs/media展示视频提取人脸、声音或帧作参考。** 跨设备WebRTC需可达信令、UDP、STUN/TURN与HTTPS，TURN凭据留本机。

## 正式启动

```powershell
. ./config/environment.example.ps1
pwsh -NoProfile -File scripts/Register-ProjectTasks.ps1
./Start-AI-Girlfriend.cmd
```

注册器只用于新部署：建立两个无触发器、交互用户、有限权限手动任务；同名存在即拒绝，不覆盖其他项目。入口依次检查8090→8765→8010→7860。用户访问 http://127.0.0.1:7860 ，各服务保持独立。长驻服务必须独立可见控制台、端口标题和实时输出，不能只看HTTP200。

7860正式管理命令：

```powershell
pwsh -File scripts/Manage-Project-Service.ps1 -Service ui -Action start
pwsh -File scripts/Manage-Project-Service.ps1 -Service ui -Action status
pwsh -File scripts/Manage-Project-Service.ps1 -Service ui -Action stop
pwsh -File scripts/Manage-Project-Service.ps1 -Service ui -Action restart
```

不以直接uvicorn为正式入口。UUID、PID+创建时间、exe／runner路径和SHA共同校验，HMAC认证，忙时拒停、安全排空、超时不强杀；未登记旧实例不会盲目接管。

## 日常功能

设置→语言模型：dark_roast/gemma4/qwen3_live；设置→声音与语气：qwen3/higgs/fish。经过资产检查、单驻留事务、健康检查及失败回滚。Higgs7077与Fish3030按需运行。默认dark_roast+qwen3+用户自己的参考音。Higgs情绪不会自动用于Qwen Base。

书库只提供`config/fictional_chapter.txt`虚构样章。导入→短段朗读→暂停/继续/停止→跨段→进度→删除/批删/回收站恢复；进度依据实际播放结束回执。音色页使用自己授权声音，CPU ASR、参考文字修正与Higgs激活；非法ID、缺素材、同ID恢复冲突和忙时操作必须拒绝。

直播：编辑`runtime/live/config.json`，默认enabled=false；连接自己合法的BarrageGrab8888，`pwsh -File scripts/Manage-Project-Service.ps1 -Service live -Action start`启动8781。8781负责解析／去重／调度，前端会话驱动LLM→TTS→数字人→浏览器回执。未授权不公开开播，不携带平台cookie；礼物自动回复未实现。历史`LOCAL_LIVE_E2E=PASS`，`REAL_PLATFORM_FRESH_EVENT=HOLD`。

## 验证、升级与故障

```powershell
python scripts/validate_public_source.py
python tests/test_reading_admin_isolated.py
python tests/test_voice_library_isolated.py
node --test --test-timeout=30000 runtime/ui/app/test_avatar_sync_*.cjs
curl.exe --noproxy '*' http://127.0.0.1:7860/api/models
curl.exe --noproxy '*' http://127.0.0.1:7860/api/voice-models
curl.exe --noproxy '*' http://127.0.0.1:8765/v1/pool
curl.exe --noproxy '*' http://127.0.0.1:8090/health
```

升级前保存git提交／标签、配置、用户数据离线备份和SHA256。业务空闲后经认证管理器退出，再换源码；失败回到旧标签和对应配置，对账数据，不能为测试删除用户数据。缺ref→配置合法WAV和准确文本；缺multipart→装control依赖；Higgs认证失败→核对本机key；Fish资产未声明→修manifest；GGUF_MISSING→下载验哈希；已有未登记进程→HOLD核对身份，不taskkill。Python/GPU依赖冲突要在独立venv解决，不修改其他项目。
