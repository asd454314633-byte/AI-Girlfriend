# 当前公开源码的验证范围

本仓库通过隐私筛选、来源登记和 Git 历史整理后公开。下述真实运行指标来源于作者提供的 **2026-10-13 本机无人值守验收执行报告**；本次文档同步直接核实了 GitHub 提交 `81d27e6` 的代码修复与哈希记录，但没有独立打开本机原始 WebM、口型拼图和诊断日志。因此应区分「执行报告记载的 PASS」与「独立复现」。

## 2026-10-12：开源准备与离线验收

- 独立数字人适配器 `runtime/ui/app/avatar-sync.js` 已公开；无再分发许可的原派生版本保持隔离。
- 五组离线契约测试通过：`test_avatar_sync_session.cjs`、`test_avatar_sync_session_reuse.cjs`、`test_avatar_sync_lifecycle.cjs`、`test_avatar_sync_ice.cjs`、`test_avatar_sync_reading.cjs`，均位于 `runtime/ui/app/`。
- 阅读接口 `expectReading`、`forgetReading`、`readingPlayback`、`interrupt` 已实现。其时尚未做真实服务联调，历史记录见 `provenance/AVATAR_SYNC_RESTORATION_AUDIT_20261012.md`。

## 2026-10-13：本机现有数字人环境真实联调

根据本机执行报告，使用现有 LiveTalking / 8010 服务对 **GitHub 独立适配器**测试 T1–T6，最终 `ALL_PASS = TRUE`，主要结果：

| 项目 | 本机执行报告结果 | 范围与说明 |
| --- | --- | --- |
| T1：WebRTC 连接 | PASS | `/offer`、ICE、`pc=connected`、真实 640×960 视频轨 |
| T2：真实音视频与口型 | PASS | 录制实际 WebRTC 音轨；25fps/927 帧；人脸中心区域音频-动作相关 `r=0.431`，滞后约 +200ms；说话/静音运动比 1.61×；仍建议人工复看片段评估自然度 |
| T3：长句表现 | PASS | 三轮首声延迟 1045/833/817ms，音频播放时长偏差约 0.41% |
| T4：三段陪读 | PASS | 朗读顺序、播放边界、forget、interrupt；停止声音约 841–855ms |
| T5：取消与安全性 | PASS | 恰好一次取消请求，无已取消音频复活 |
| T6：重连与会话清理 | PASS | 正确调用 `reconnect()`；无重复 `/offer`；全轮 230/230 测试会话释放 |
| 五组离线契约测试终跑 | PASS | 据执行报告，未删除断言或放宽预期 |

本次修复仅为 `onMediaTick` 对可选 `#gf-avatar-status` 元素的空值保护；源码及清单变更见 [提交 81d27e6](https://github.com/asd454314633-byte/AI-Girlfriend/commit/81d27e6488edc364b004438eae52686c11967af2)。

## 尚未覆盖 / 保留问题

- **完整新环境安装：NOT_TESTED。** 当前公开包不含模型权重、私人素材或本机配置；本轮复用了作者现有数字人运行环境。
- **真实麦克风打断：NOT_TESTED。** 本轮按要求没有执行麦克风输入测试。
- **与原私有版完整 A/B 性能等效：INCONCLUSIVE。** 只测试 GitHub 独立适配器，不以原版历史使用自动证明两个版本完全等效。
- **严重卡顿时的短暂双声道风险：OPEN。** 在无头浏览器主线程压力情境下，音频回执 1.5 秒宽限窗曾触发一次 fail-open 竞态；本轮没有修改该语义，也不能宣称风险消失。
- **数字人口型自然度：** 采样指标和目视拼图支持本机实例中的口型随语音变化，但音画审美/自然度仍可由维护者查看原片段复核。

详细结果见 [2026-10-13 真实验收记录](AVATAR_SYNC_REAL_ACCEPTANCE_20261013.md)。原始私有测试证据按执行报告保存在本机 `gh_adapter_acceptance/`（含 `ACCEPTANCE_REPORT.md`、`lipsync_montage.png` 和 `evidence/lipsync_clip.webm`），**不随公开仓库分发**。

**结论：本机指定环境中，GitHub 数字人适配器 T1–T6 真实联调通过；不等于一键安装、跨设备兼容或所有功能全面验收。**
