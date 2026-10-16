# AI Agent 全中文部署指引

仅在用户指定的新目录执行，先读NOTICE、MEDIA_USAGE_POLICY、INSTALL和九类功能映射。不从旧README推断已安装或已验收。

1. 记录仓库URL、提交、标签、Windows/Python/GPU/驱动版本。查询7860/8090/8765/8010/8781/8888与7077/3030监听身份。冲突未核实即HOLD，不能按PID或端口盲杀，也不能停止其他项目。
2. 创建runtime/core/venv，装control依赖，执行initialize_public.py。核对空书库／空数据库，新key不能输出或提交。安装脚本不能覆盖已有用户配置。
3. 逐个核对模型／DLL／素材上游许可证，按模板自行获取。DARK ROAST/Gemma4量化与基础模型分别核对；无再分发权就仅记录外部依赖。没有依赖不能编造替代品或声称开箱即用。
4. 编辑models.json路径与SHA256、manifest的Fish/Whisper/Qwen/Higgs资产。安装CUDA兼容torch，speech-to-speech==0.2.11，再执行install_s2s_patches.py；核对真实导入路径与bootstrap hook。
5. 准备自己授权或合成参考WAV和准确ref.txt，Avatar用自己的合法素材。docs/media只能查看，不用于训练、克隆或换脸。保留独立avatar-sync.js，不能从旧无许可证仓库覆盖。
6. 运行validate_public_source、书库／音色隔离测试及前端离线回归。测试只针对临时数据和fake网络，不连接已有生产服务。
7. 在新部署注册手动交互任务，再Start-AI-Girlfriend.cmd。核实8090→8765→8010→7860健康，以及每个服务独立可见控制台、端口标题、stdout/stderr。登记冲突拒绝覆盖，不盲目提升权限。
8. 检查7860管理器status中UUID、PID/创建时间、exe、runner SHA和实际加载模块。磁盘哈希不是加载证明；API200不是播放验收。
9. 逐一测试三LLM／三TTS切换、真实推理、状态同步、失败回滚，最后恢复默认。短句贯通聊天→TTS→数字人→浏览器实际播放，记录session/response id与尾音；不做大规模GPU压测。
10. 导入虚构样章，短段播放、暂停/继续、跨段、进度；隔离验证删除/恢复冲突。用合成或本人授权声样验证CPU ASR、文本修改、Higgs激活／生成／恢复。不能以不同文件哈希代替音色相似度验收。
11. 直播先模拟合法弹幕，验证解析、去重、不重复回复、LLM→TTS→数字人→浏览器回执。真实平台新弹幕单独验收；账号授权/验证码与公开开播必须明确许可。没有礼物自动回复能力。
12. 空闲时验证忙时拒停、排空、超时不强杀及正式启停。保存源码标签／配置／用户数据快照；失败回滚源码与对应配置，不删除数据、不重启Windows。
13. 报告PASS/PARTIAL/HOLD/NOT_TESTED，附加载版本、请求、播放和数据对账证据。新机器实测完成前FRESH_INSTALL_E2E=HOLD，主观情绪和音色偏好留给用户试听。

已收口情绪不再研发：Higgs affection/amusement获作者用户试听认可；determination保留但有音色漂移。此结果不能保证其他声样或新环境同样效果。
