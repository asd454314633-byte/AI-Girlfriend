# 参与贡献 / Contributing

感谢关注 **AI-Girlfriend**。仓库已经公开，欢迎围绕稳定性、运行文档、离线测试以及数字人和音频路由改进提交反馈。

## 适合贡献的内容

- 可复现的 WebRTC、数字人会话、TTS/音频路由问题及最小测试；
- 智能陪读分段、停止/取消、进度一致性方面的改进；
- 清晰的文档、跨环境安装指引和脱敏运行日志；
- 保持原有许可证和署名的第三方适配补丁。

## 提交 Issue 前

说明操作系统、Node/Python 版本、关联服务是否开启、受影响功能、复现步骤、期望与实际结果。涉及多模态问题可以描述时间戳、状态变化和日志特征，不必上传个人音视频。

**请先脱敏：** 不要提交密码、令牌、邮箱、聊天历史、私人书稿、人物/声音源素材、绝对本机用户名路径、数据库、未经授权的模型文件。

## Pull Request 流程

1. Fork 仓库，并从最新 `main` 建立独立分支；
2. 仅修改与问题直接有关的代码，解释原因与影响范围；
3. 修改数字人同步适配时，至少运行 [五组离线契约测试](docs/QUICKSTART.md#3-无-gpu-的最小验证)；
4. 如涉及真实端到端效果，明确实际测试环境与未测项目；离线 PASS 不得冒充真实运行 PASS；
5. 列明新引入第三方代码或素材的来源、版本和许可证；不要借助重命名或去注释规避授权；
6. 禁止向仓库上传私有生产 `avatar-sync.js`、私人配置、人物/语音素材和模型权重。

项目的 **MIT 许可只覆盖已标明的自研代码**；第三方代码仍须遵守各自许可。详见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。Wav2Lip 的部分资源和权重存在非商业限制，不能因为本仓库公开就自动改为 MIT。

## English summary

Issues and PRs with reproducible, privacy-safe evidence are welcome. Run relevant offline tests, preserve upstream attribution, and do not commit private models, user data or copyright-restricted materials. A local integration PASS does not establish cross-platform reliability.
