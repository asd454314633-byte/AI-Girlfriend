# 当前公开源码的验证范围

本仓库通过隐私筛选、来源登记及 Git 历史整理后公开。已完成的隐私与哈希核验仅覆盖对应发布提交；后续变更需另行同步核验。

## 已完成（按 2026-10-12 发布记录）

- 数字人前端独立适配层 `runtime/ui/app/avatar-sync.js` 已纳入公开源码。
- 五组自研离线契约测试已通过：`test_avatar_sync_session.cjs`、`test_avatar_sync_session_reuse.cjs`、`test_avatar_sync_lifecycle.cjs`、`test_avatar_sync_ice.cjs`、`test_avatar_sync_reading.cjs`，均位于 `runtime/ui/app/`。
- 补齐了阅读接口 `expectReading` / `forgetReading` / `readingPlayback` / `interrupt`。
- 原含有未明确可再分发第三方代码的版本仍被排除。详见 `NOTICE.md` 和 `provenance/AVATAR_SYNC_RESTORATION_AUDIT_20261012.md`。

## 尚未完成

- 真实 GPU、数字人模型、LiveTalking 服务端连接及浏览器 WebRTC 音视频全链路：**NOT_TESTED**。
- 独立新环境的一键安装、真实语音/阅读全链路：**NOT_TESTED**。
- 真实麦克风打断：本轮按任务范围未执行。

**离线契约 PASS 不等于真实端到端 PASS。** 旧私有生产环境中的历史结果不能代替当前公开源码包的独立运行验收。
