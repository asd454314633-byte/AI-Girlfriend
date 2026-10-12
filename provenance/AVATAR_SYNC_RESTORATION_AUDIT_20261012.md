# avatar-sync.js 补齐记录（2026-10-12）

> **后续状态更新（2026-10-13）**：本文是 10 月 12 日补齐、授权筛选与离线测试时的历史记录，文中的 `NOT_TESTED` 是**当日状态**。此后独立适配层已在作者本机现有 8010 数字人服务完成 T1–T6 真实联调（据本机执行报告），并修复了一个可选状态元素空引用问题；请以 [后续真实验收记录](../docs/AVATAR_SYNC_REAL_ACCEPTANCE_20261013.md) 了解最新实测范围。原版分发限制不变。

本仓库唯一的运行时缺口曾经是前端同步适配层 `runtime/ui/app/avatar-sync.js`
（`index.html:649` 仍引用 `/avatar-sync.js`）。本文件记录该缺口如何被补齐，
以及补齐过程中对"能不能公开发"这件事做过的全部判定。

**结论：`runtime/ui/app/avatar-sync.js` 已随本仓库发布**，是依据本仓库有权使用的
材料重新实现的独立版本，通过了 5 组自研离线契约测试；**未做真实端到端验收**。

---

## 一、候选 A：原实现（永久排除）

| 项 | 值 |
| --- | --- |
| 本地位置 | 正式生产工作副本 `runtime/ui/app/avatar-sync.js`（本机保留，不随仓库发布），另有隔离副本 |
| SHA256 | `8c73f091f34379c104ee9a3165e0aba57d3cd9623786df59de5cc945b811401f`（51862 字节） |
| 来源 | 派生自 `liuw8233-cell/ai_girlfriend@699f7fe` 的 `assets/avatar-sync.js` |
| 上游锁定件 | SHA256 `f5b4840ae6353cec73395400a458420d3a6f15391676329062e164da7cd64ed8`（12193 字节） |
| 上游许可 | 该上游仓库**无 LICENSE 文件**，按默认规则保留所有权利 |
| 行级比对 | 357 行中 **201 行（56%）**在扩展版（1105 行）中逐字保留 |

判定：**不可再分发**，永久排除。不采用删除许可声明、改名、重格式化等任何绕过手段；
该文件始终留在隔离区，从未进入待公开提交。

## 二、候选 B：替代实现（已补齐并纳入）

补齐前 `50f1df3d46033c4cc27ebca43a54407c379653a04165c835bea90a1ef13478b1`
（31673 字节，78 个函数）；补齐后：

| 项 | 值 |
| --- | --- |
| 路径 | `runtime/ui/app/avatar-sync.js` |
| SHA256 | `05c4466a50ad0a4486c414624ad26accfe2eb66d044267f40af95687aff092f7` |
| 大小 | 44437 字节，98 个函数 |

开发依据均为本仓库有权使用的材料：本项目自研测试确立的行为契约、`audio-router.js`
的 sink 接口、已 vendored 且适用 Apache-2.0 的 LiveTalking 服务端路由、
`ui/reading.js` 的调用契约，以及公开的 WebRTC 标准。**作者不声明严格 clean-room，
本记录也不代其声明。**

### 2.1 表达层相似度（防止变相搬运）

对上游锁定件 `f5b4840a…64ed8` 做去注释后的两种口径比对，函数体脱离顺序独立比较：

| 口径 | 结果 | 公共块 | 配对函数体 |
| --- | --- | --- | --- |
| 精确 token | **2.84%** | 0 | 0 |
| 标识符归一化 | **4.18%** | 0 | 0 |

即：未检测到来自无授权上游的实质性逐字表达。**没有任何旧版本被复制、改名、
删许可或重格式化后伪装成新实现。**

### 2.2 补齐的四个阅读接口（非空壳）

补齐前缺失、现均已实现真实行为：

| 消费方调用点 | 接口 | 实现要点 |
| --- | --- | --- |
| `ui/reading.js:445` `avatar.expectReading(readingKey)` | `expectReading(key)` | 登记朗读段并返回可用于回溯的句柄；受 2 分钟绑定窗口与 64 条上限约束 |
| `ui/reading.js:547` `await window.AvatarSync.readingPlayback()` | `readingPlayback()` | 返回逐段真实边界；时间戳只写实测值，另附轮询粒度作为不确定度 |
| `ui/reading.js:455/458/467` `avatar.forgetReading(readingKey)` | `forgetReading(key)` | 精确弃用指定段；不误伤同会话其它朗读段 |
| `ui/reading.js:688` `AvatarSync?.interrupt?.()` | `interrupt(opts)` | router.cancel + 恰好一次 `/interrupt_talk`，并阻止迟到的 audio 复活该轮 |

### 2.3 调用点全量核对

除 `ui/reading.js` 外，走查了聊天模块、`avatar_panel.js`、`avatar_layout.js`、
`main.js`、`index.html` 的全部 `AvatarSync` 调用点，逐项确认参数顺序、异步约定、
返回结构与异常行为一致。已提供的其余接口（`config`、`registerAvatarSink`、
`connect`、`reconnect`、`disconnect`、`cancelAvatarTurn`、`mediaReady`、
`diagnostics`，以及 `sessionid`/`connectionState`/`suspended`/`peerConnection`
访问器）签名保持不变。

### 2.4 朗读账本的关键设计取舍

- **边界来源**：会话级 `/is_speaking` 的翻转 → 按登记顺序归属到具体朗读段。
  这是离线可验证的最小粒度，未经真机交叉验证。
- **不伪造精度**：`playoutDelayMs` 写实测值，`clockUncertaintyMs` 显式记录轮询粒度
  （默认 500ms，可用 `readingPollMs` 调整），不把 UDP 式观测包装成精确时钟。
- **只写真值**：聊天轮次触发的说话不会被误记成朗读段的起止；已 abandon / finished
  的段不再参与点亮。
- **终止语义**：停止或断线后旧段标记为 `abandoned` 而非删除，保留证据；重连不复活
  已取消任务；不引入无限重试。
- **有界增长**：`pruneReading()` 保留窗 180 秒、上限 64 条，先丢最老的终态记录。

## 三、测试

### 3.1 结果

| 测试 | 覆盖内容 | 结果 |
| --- | --- | --- |
| `test_avatar_sync_session.cjs` | 一次点击只允许一个 `/offer`、MEDIA_READY 之前不接管音频、回执与失败 paths | PASS |
| `test_avatar_sync_session_reuse.cjs` | 逐页单会话、single-flight、唯一重连定时器、每次拆除都释放 | PASS |
| `test_avatar_sync_lifecycle.cjs` | 服务端错误原样透传、重复 id 幂等、模式切换隔离、会话循环、崩溃重连、pagehide 释放 | PASS |
| `test_avatar_sync_ice.cjs` | mock 协商：成功 / 收集超时 / 无候选 / checking / 坏应答 / offer 超时 | PASS 6/6 |
| `test_avatar_sync_reading.cjs` | readingKey 绑定、逐段顺序边界、停止与断线后不再投递、拒收即把声音交回本机、异常安全 | PASS |

测试文件随本仓库发布在 `runtime/ui/app/`，`node test_avatar_sync_<name>.cjs`
即可运行，无需外部依赖或网络。

### 3.2 关于"有没有放宽断言"的声明

会话测试最初的失败（“一次点击只允许一个 /offer”，实际 `offers=0`）经逐层归因，
确认根因是**测试替身的保真度缺口**，而非被测实现缺陷：

1. 假浏览器上下文缺少 `console` → 真实错误链被吞，先补上后才看到后续报错；
2. `createOffer()` 返回占位字符串而非结构化 SDP → 缺 `m=` 行与 `a=candidate`，
   被被测实现的 ICE 健康检查正确拒绝；补 `HEALTHY_OFFER_SDP`；
3. 上下文中缺少 `AbortController` → 超时路径抛未定义错误。

三处修复全部作用于替身，使其更像真实浏览器。**未删除任何断言、未改变任何预期值、
未伪造成功状态。** 同一批替身修复同步应用于 reuse / lifecycle 两组的相同缺口。

## 四、验收边界（不得跨过这条线）

| 项 | 状态 |
| --- | --- |
| 离线契约测试 | 已通过 |
| 真实 GPU / 摄像头驱动的数字人渲染 | **NOT_TESTED** |
| 真实 LiveTalking 服务端的连接与音视频 | **NOT_TESTED** |
| 真实麦克风打断 | **未做（按任务要求明确禁止）** |
| 独立环境的一次性端到端验证 | 未执行：本机不具备隔离的 GPU + 数字人服务组合，且不得重启或改动正式生产实例 |

因此本适配层**不得被宣传为经过完整生产验收的稳定功能**。它是一次接口补齐 + 离线验证的成果，真实链路的成功与否需由使用者在自有环境中自行确认。

## 五、发布前置检查

- 来源：见 §1、§2.1，无无许可第三方源码被引入。
- 隐私：对 `avatar-sync.js` 与 5 个测试文件做过用户名、家目录绝对路径、邮箱、
  密钥、token、内网 IP 的命中扫描，**无命中**（`:8010` 为 LiveTalking 默认文档端口，
  非敏感信息）。
- 许可证：README、NOTICE 已同步说明"为什么不是原版"与"未做过的验收"。
- Git：普通提交、普通 push，未强推；未创建第二个仓库；历史完整。
- 正式生产实例：**未修改、未重启**。
