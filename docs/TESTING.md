# V3 发行验证与真实边界（2026-10-16）

本次测试针对独立发行目录，不改动或重启作者生产服务；实验管理器只控制自己的17860端口与新UUID登记。详细机器可读结果见`provenance/V3_RELEASE_VALIDATION.json`。

| 本次发行测试 | 结果 | 范围 |
|---|---|---|
| 书库管理隔离 | PASS 28/28 | 临时SQLite／书籍，删除、停止失败、同ID冲突、恢复保护 |
| 音色库隔离 | PASS 42/42 | 临时合成WAV，非法ID、缺素材激活、文件冲突、忙时保护、上传不阻塞异步健康 |
| 管理器隔离 | PASS 44/44 | HMAC、PID复用、身份错配、忙请求／WS拒停、排空、超时不强杀、自然退出；独立可见实验控制台 |
| 直播逻辑 | PASS 23/23 | fake事件、去重、观众隔离、限流／超时／队列／重连、本地临时WS；不是平台fresh-event |
| Higgs参考消费 | PASS 5/5 | fake native engine确认每请求参考音／文字、默认回退、缺素材拒绝、DLL实参；不加载模型 |
| TTS切换事务 | PASS 8/8 | fake执行器、临时journal、失败回滚、PID复用、并发授权、原子写保护；不探测生产 |
| 独立公开数字人适配器 | PASS 5套 | 会话、复用、生命周期、ICE、朗读回执契约；保留原公开实现与配套测试 |
| 情绪面板 | PASS 1套 | 真实manifest形状、模型作用域、隐藏Qwen旧值不发送、缓存／重置；未生成音频 |
| 语法／导入／映射 | PASS | Python/JSON/JS/PowerShell、相对JS依赖、UI关键导入115条路由、九类源码路径 |
| 空白源码重建 | 见机器可读结果 | 不包含GPU／权重／DLL；schema初始化零用户记录、配置幂等；不等于fresh-install E2E |

复现命令：

```powershell
python scripts/validate_public_source.py
python tests/test_reading_admin_isolated.py
python tests/test_voice_library_isolated.py
python tests/test_higgs_reference_public.py
python -m unittest discover -s tests/live -p 'test_*.py' -v
node --test --test-timeout=30000 runtime/ui/app/test_avatar_sync_*.cjs
node --test runtime/ui/app/tests/manual-voice-panel.test.mjs
```

Python测试需`config/requirements-control.txt`。项目模块导入需仓库根、runtime、runtime/core在PYTHONPATH；安装overlay会生成对应venv的路径hook。管理器完整测试为Windows专用，先核实17860未被其他服务使用，`python runtime/ui7860_manager/test_manager.py`会创建独立visible lab console及日志，不能拿生产记录测试。

作者特定安装布局的旧provider literal审计器及其包含私人路径的基线不随包发布；它要求作者私有venv内的原wheel文件，不适用于源码压缩包。未把该审计器在缺失venv情况下的结果算作发行回归PASS，也没有放宽其断言。公开源码完整性由九类映射、实际import、相对依赖及空白重建验证。

## 引用的作者现有环境历史结果

2026-10-15全面收口记录：生产业务补丁和正式管理器加载PASS；三LLM和三TTS实际切换／推理及失败回滚经过对应既有记录；音色克隆链PASS但主观相似度HOLD；短段朗读／暂停继续／跨段PASS；本地模拟直播至浏览器播放PASS。不是本版新机器独立安装结果。

`LOCAL_LIVE_E2E=PASS`；`REAL_PLATFORM_FRESH_EVENT=HOLD`。未声称已验收真实平台新弹幕或礼物自动回复。

2026-10-16用户情绪裁决：Higgs affection/amusement人耳认可、保留；determination保留但有声线漂移；sarcastic不启用；disgust不符合目标、不再优化。Qwen Base不宣传已验证独立原生情绪接口；历史情绪Skill原件未找到。私人试听WAV未公开，未重复生成或修改。

吞尾字：历史长音频30秒容量问题的有界扩容及AUDIO_TRUNCATED显式保护源码保留。普通聊天短句4.55秒故障后续复测NOT_REPRODUCED，未虚报新根因或根治。服务端音频完整不等于浏览器播完，需新环境同session/response最终播放验收。

## 仍然独立保留的状态

- FRESH_INSTALL_E2E=HOLD：没有从零下载模型／安装GPU组件／真人Avatar的独立新机器全链路。
- 新发行包真实模型、数字人和最终浏览器播放：NOT_TESTED（本轮限定源码发布，不扰动生产GPU）。
- 新声样的克隆主观相似度、情绪偏好：HOLD，由授权素材和用户试听决定。
- 历史独立适配器既有环境有限验收原文仍在`AVATAR_SYNC_REAL_ACCEPTANCE_20261013.md`，不能覆盖本版新环境测试范围。

发现旧测试用前注册表API空响应，更新fake数据为当前manifest，并明确验证隐藏Qwen情绪不会发送；没有为通过测试放宽业务断言。生产legacy adapter配套测试不用于独立公开adapter，后者保持原有5套契约。
