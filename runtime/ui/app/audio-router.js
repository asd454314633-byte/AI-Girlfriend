/* ============================================================================
 * audio-router.js — 助手语音的唯一路由层（任务 AI_GIRLFRIEND_RUNTIME_LIFECYCLE_FINAL_REPAIR_01 §九/§十）
 *
 * 它解决的是一个问题：**"数字人挂了" 曾经等于 "整个系统静音"**。
 *
 * 旧形状（本文件要替换掉的）：
 *   avatar-sync.js 劫持 window.WebSocket，把 response.output_audio.delta 用
 *   stopImmediatePropagation 吞掉，只在 /humanaudio 投递成功时由 :8010 发声。
 *   于是 8010 不可用 = 音频被吞且没人播 = 用户完全听不到（§一 C 记录的同一条
 *   故障：视频失效与音频静音是同一个故障的两面）。
 *
 * 新形状：
 *   AudioSource 只有一个 = :8765 的 TTS 输出（由 s2s-realtime-client.js 的
 *   _onAudio 喂进来）。AudioSink 只有两个：`avatar`（:8010 口型+发声）与
 *   `local_browser`（AudioWorklet → ctx.destination）。每一轮只有一个"可听"
 *   的 sink，且 policy 是 fail-open：拿不准 avatar 会不会发声，就本地播。
 *   人可以消失，声音不能消失。
 *
 * 一条硬不变量（§十 "one audible sink per turn" + TEST 6 "无重复音频"）：
 *   一个 turn 的字节区间只能被一个 sink 播出一次。avatar→local 的降级只允许
 *   发生一次，且只移交 avatar **尚未确认接收**的那段尾巴（见 confirmedBytes /
 *   localBytes），所以不会出现同一段被播两遍。
 *
 * 依赖方向：本文件不 import 任何东西，也不碰 DOM、不 fetch。sink 由
 *   - s2s-realtime-client.js 注册 `local_browser`（它拥有 AudioWorklet），
 *   - avatar-sync.js 注册 `avatar`（它拥有 :8010 的会话与上传），
 * 各自注册自己那侧的事实。挂到 globalThis 上是为了让经典脚本（avatar-sync.js）
 * 与 ES module（main.js → client）看到同一个实例，和 s2sTurnControl 一个套路。
 * ========================================================================== */
(function (globalThis) {
  'use strict';

  /** avatar 侧迟迟不给回执时，最多让本地兜底等这么久（毫秒）。 */
  const AVATAR_CONFIRM_GRACE_MS = 1500;
  /** 一轮音频停止增长、又没等到 response.done 时，多久算它 stalled。 */
  const AUDIO_STALL_MS = 4000;
  /** 一个 turn 结束后 retained buffer 的最长保留时间，防泄漏。 */
  const TURN_RETENTION_MS = 120000;
  /** 同时保留的 turn 数上限（含已结束但仍在 grace 里的）。 */
  const MAX_TURNS = 8;
  /** 一轮音频的字节上限（24kHz mono s16）。
   * LONG_AUDIO_TRUNCATION_FINAL_01：旧值 30s（24000*2*30）把所有超过 30 秒的
   * 合法长回复尾音静默丢弃——生产日志对账证实 8765 生成 58~74s、avatar 实收
   * 恰好 29.95~29.98s，正是"文字完整、语音后半截消失"的第一丢失层。
   *
   * FINAL_02 裁决（guard 绑定系统能力，不写死魔法数）：
   *   8090 = llama.cpp `-c 8192`，且 ChatCompletions 请求不带 max_tokens
   *   （base_openai_compatible_language_model 不设，extra_body 只有 disable-thinking）
   *   → 单次回复的输出 token 数至多被上下文窗口封顶（≈8100）。
   *   中文 ≈ 2 chars/token（Gemma 保守值）；TTS 语速实测 393 chars → 70s
   *   ≈ 5.6 chars/s，保守取 4.5 chars/s（慢速播报）。
   *   SAFETY_GUARD_DURATION = 3600s（保守推算：8100 × 2 / 4.5 ≈ 3600）。
   *
   *   ROLE = runaway safety guard。**3600s 不是精确的模型最大合法音频时长**：
   *   -c 8192 是上下文窗口，实际可用于输出的 token 数还取决于输入上下文占用；
   *   2 chars/token 与 4.5 chars/s 也都是估算。冻结它的理由只有两条——
   *   (1) 远高于当前正常回复与已实测验收的 101.85s 长回复，正常使用永不触发；
   *   (2) 能触发它意味着上游失控（例如 TTS worker 对单段死循环），那不是合法回复。
   *   ARCH_DEBT（非阻断，2026-09-26 登记）：若未来支持超长生成，应改为从实际
   *   output budget 动态派生；不要为把这个数字"算得更漂亮"而重开终验。
   *   命中时 fail-loud：trace + decisions + console.error + turn 标记
   *   truncatedAtGuard，最终状态置 AUDIO_TRUNCATED（不再能被记成完整成功）。 */
  const LLM_MAX_OUTPUT_TOKENS = 8100;   // -c 8192 扣除最小 prompt 占用
  const CHARS_PER_TOKEN = 2.0;          // Gemma 中文保守值
  const TTS_CHARS_PER_SECOND = 4.5;     // 实测 5.6，取保守下限
  const MAX_TURN_SECONDS = Math.ceil(LLM_MAX_OUTPUT_TOKENS * CHARS_PER_TOKEN / TTS_CHARS_PER_SECOND);
  const MAX_TURN_BYTES = 24000 * 2 * MAX_TURN_SECONDS;

  const SINK_LOCAL = 'local_browser';
  const SINK_AVATAR = 'avatar';

  /**
   * §十一 音频状态分级。秩（rank）是单调的：状态只许往前走，不许因为一条
   * 迟到的 delta 就把「正在播放」降级回「已生成」。
   */
  const STATE_RANK = {
    TTS_READY: 10,
    AUDIO_GENERATED: 20,
    AUDIO_PLAYING: 30,
    AUDIO_DONE: 40,
    AUDIO_SILENT: 40,
    AUDIO_CANCELLED: 40,
    // FINAL_02：guard 命中的轮次最终落到这个状态——比"正常完成"更显性，
    // 诊断面（lastTurns.state / turn_state 事件）能直接看出该轮被截断。
    AUDIO_TRUNCATED: 45,
  };

  function nowMs() {
    return (typeof performance !== 'undefined' && performance.now)
      ? performance.now() : Date.now();
  }

  /**
   * 一轮（一个 response_id）的路由状态。
   *
   * `sink` 是"当前可听 sink"；`confirmedBytes` 是 avatar 已经**服务端落库**
   * （/humanaudio 返回 code 0）的字节数；`localBytes` 是已经交给本地 sink 的
   * 字节数。两者相加 = 该轮已被处理的字节数，降级时只移交剩下的。
   */
  class Turn {
    constructor(responseId, meta) {
      this.responseId = responseId || '';
      this.sink = '';
      this.chunks = [];
      this.bytes = 0;
      this.confirmedBytes = 0;
      this.localBytes = 0;
      // §十一：音频状态与"画面状态(MEDIA_READY)"是两条独立的轴。这里只描述声音
      // 到底走到哪一步了：TTS_READY → AUDIO_GENERATED → AUDIO_PLAYING → AUDIO_DONE。
      this.state = '';
      this.begunAt = nowMs();
      this.lastDeltaAt = nowMs();
      this.endedAt = 0;
      this.cancelled = false;
      this.truncatedAtGuard = false;
      this.fellBack = '';
      this.fallbackTimer = null;
      this.stallTimer = null;
      this.audit = [];
      this.meta = meta || {};
    }

    /** 还没被任何 sink 承担的字节数。 */
    get outstanding() {
      return this.bytes - Math.max(this.confirmedBytes, this.localBytes);
    }

    /** 已经真的交给某个 sink 的字节数（本机写入或 :8010 回执）。 */
    get playedBytes() {
      return Math.max(this.confirmedBytes, this.localBytes);
    }

    trace(what, detail) {
      this.audit.push({ at: Math.round(nowMs()), what, detail: detail || '' });
      if (this.audit.length > 24) this.audit.shift();
    }
  }

  class AudioRouter {
    constructor() {
      /** @type {Map<string, {name: string, sink: object, priority: number}>} */
      this._sinks = new Map();
      /** @type {Map<string, Turn>} */
      this._turns = new Map();
      // Cancelled response ids.  A turn is deleted on cancel, so without this a
      // late delta with the same id would `begin()` a *fresh* turn and play it —
      // i.e. the audio of a round the user already interrupted coming back.
      this._cancelledIds = new Set();
      // 已经收口的轮次的最终裁决（audible / sink / 字节）。response-finished 发生在
      // turn 被删除之后，所以"这一轮到底有没有声音"必须留一份有界账本。
      /** @type {Map<string, {audible: boolean, sink: string, bytes: number, playedBytes: number, state: string, cancelled: boolean}>} */
      this._finished = new Map();
      this._activeId = '';
      this._observers = new Set();
      this.graceMs = AVATAR_CONFIRM_GRACE_MS;
      this.counters = {
        turns: 0,
        to_avatar: 0,
        to_local: 0,
        fail_open: 0,
        dropped_after_cancel: 0,
        dropped_no_sink: 0,
        double_audio_guard_hits: 0,
        silent_turns: 0,
        guard_truncated_turns: 0,
      };
      /** 最近 24 条决策，供页面/控制台读；也是 §十四 的浏览器侧账本。 */
      this.decisions = [];
      this.policy = {
        // 关掉它 = 永远本地播（口型层退化成纯画面），用于 A/B 与故障隔离。
        avatar_enabled: true,
      };
    }

    /* ------------------------------------------------------------ sinks */

    /**
     * @param {'avatar'|'local_browser'} name
     * @param {{
     *   write: (samples: Int16Array, meta: object) => void,
     *   available?: () => boolean,
     *   cancelTurn?: (meta: object) => void,
     *   endTurn?: (meta: object) => void,
     *   priority?: number,
     * }} sink
     */
    registerSink(name, sink, opts) {
      if (!name || !sink || typeof sink.write !== 'function') {
        throw new Error('registerSink needs a name and a sink with write()');
      }
      const priority = (opts && typeof opts.priority === 'number')
        ? opts.priority : (name === SINK_AVATAR ? 20 : 10);
      this._sinks.set(name, { name, sink, priority });
      this._log('sink_registered', { sink: name });
      return true;
    }

    unregisterSink(name) {
      const had = this._sinks.delete(name);
      if (had) this._log('sink_unregistered', { sink: name });
      return had;
    }

    hasSink(name) {
      return this._sinks.has(name);
    }

    /** 该 sink 现在能不能真的出声（avatar 侧就是 MEDIA_READY）。 */
    sinkAvailable(name) {
      const entry = this._sinks.get(name);
      if (!entry) return false;
      if (typeof entry.sink.available === 'function') {
        try {
          return Boolean(entry.sink.available());
        } catch (_) {
          return false;
        }
      }
      return true;
    }

    /* ----------------------------------------------------------- turns */

    /** 一轮开始（response.created）。此时就定 sink，避免边播边改主意。 */
    begin(responseId, meta) {
      const id = responseId || '';
      if (!id) return null;
      if (this._cancelledIds.has(id)) {
        // 已经取消过的轮次不得因为迟到 delta 复活。
        this.counters.dropped_after_cancel += 1;
        return null;
      }
      let turn = this._turns.get(id);
      if (turn) return turn;          // 重复的 created：幂等，不重置已播字节
      turn = new Turn(id, meta);
      turn.sink = this._pickSink();
      this._turns.set(id, turn);
      this._activeId = id;
      this.counters.turns += 1;
      if (turn.sink === SINK_AVATAR) this.counters.to_avatar += 1;
      else this.counters.to_local += 1;
      turn.trace('begin', `sink=${turn.sink}`);
      this._log('turn_begin', { responseId: id, sink: turn.sink });
      this._setState(turn, 'TTS_READY', `sink=${turn.sink}`);
      this._notify('begin', { responseId: id, sink: turn.sink, meta: meta || {} });
      this._prune();
      return turn;
    }

    /**
     * 一段 TTS PCM 到达。唯一入口，永远先记账再路由 —— "router must always
     * retain that turn's playable audio"（§九）。
     * @param {string} responseId
     * @param {Int16Array} samples
     * @param {object} [meta]
     */
    ingest(responseId, samples, meta) {
      if (!samples || !samples.length) return null;
      const id = responseId || this._activeId || '';
      if (!id) {
        // 没有归属就没有"每轮一个 sink"可言：宁可本地播（fail-open），
        // 也不能因为缺 id 而静音。
        this.counters.dropped_no_sink += 1;
        this._write(SINK_LOCAL, samples, { responseId: '', anonymous: true });
        return null;
      }
      const turn = this.begin(id, meta);
      if (!turn) return null;               // 被取消过的轮次：迟到 delta 到此为止
      if (turn.cancelled) {
        this.counters.dropped_after_cancel += 1;
        return turn;
      }
      if (turn.bytes + samples.length * 2 > MAX_TURN_BYTES) {
        turn.truncatedAtGuard = true;
        this.counters.guard_truncated_turns += 1;
        turn.trace('truncated', `${turn.bytes}B`);
        // FINAL_02：哨值命中是异常事件（命中 runaway 护栏意味着上游失控，
        // 例如 TTS worker 死循环），必须可观测，绝不允许静默丢尾。
        this._log('turn_truncated_at_guard', {
          responseId: id, keptBytes: turn.bytes, droppedBytes: samples.length * 2,
        });
        console.error('[audio-router] 单轮音频超过失控护栏 SAFETY_GUARD_DURATION（'
          + `${MAX_TURN_SECONDS}s，非模型合法最大时长），疑似上游失控：responseId=${id} kept=${turn.bytes}B`);
        return turn;
      }
      // 保留一份，之后任何降级都从这份里取，不依赖任何 sink 的缓存。
      turn.chunks.push(samples);
      turn.bytes += samples.length * 2;
      turn.lastDeltaAt = nowMs();
      this._setState(turn, 'AUDIO_GENERATED');

      // A mid-turn collapse of the avatar path.  `_failOpen` hands over the
      // WHOLE retained buffer, and the buffer already contains this very chunk,
      // so the chunk must not be written again below — that was a double play of
      // exactly `samples.length` bytes (test 2 caught it).
      let handedOver = false;
      if (turn.sink === SINK_AVATAR && !this.sinkAvailable(SINK_AVATAR)) {
        this._failOpen(turn, 'avatar_unavailable_mid_turn');
        handedOver = true;
      }
      if (!handedOver) {
        if (turn.sink === SINK_LOCAL) {
          this._writeLocal(turn, samples);
        } else {
          this._write(SINK_AVATAR, samples, Object.assign({ responseId: id }, meta || {}));
          // A turn that stops receiving deltas *and* never gets `response.done`
          // would otherwise sit on its audio until the pruner deletes it.  Arm
          // one stall timer per delta so "avatar stalled" becomes audible audio
          // within STALL_MS instead of becoming silence.
          this._armStallTimer(turn);
        }
      }
      this._prune();
      return turn;
    }

    /** 一轮结束（response.done / 手动收尾）。 */
    end(responseId, meta) {
      const turn = this._turns.get(responseId || '');
      if (!turn) return null;
      turn.endedAt = nowMs();
      turn.trace('end', `${turn.bytes}B sink=${turn.sink}`);
      this._notify('end', {
        responseId: turn.responseId, sink: turn.sink, bytes: turn.bytes, meta: meta || {},
      });
      if (turn.sink === SINK_AVATAR) {
        if (!turn.bytes) {
          this._finish(turn);
        } else {
          // avatar 侧的上传是异步的：给它一个有界的回执窗口，超时即本地兜底。
          this._scheduleFallback(turn, 'avatar_no_receipt');
        }
        return turn;
      }
      if (typeof this._sinks.get(SINK_LOCAL)?.sink.endTurn === 'function') {
        this._sinks.get(SINK_LOCAL).sink.endTurn(Object.assign({ responseId: turn.responseId }, meta || {}));
      }
      this._finish(turn);
      return turn;
    }

    /**
     * 一轮被取消（用户插话 / 手动停止）。
     * 语义：这一轮之后的字节不再交给任何 sink；已经交给 avatar 的尾巴由
     * avatar-sync 通过 :8010 的 cancel 处理，本地则靠 local sink 的 clear。
     */
    cancel(responseId, reason) {
      const turn = this._turns.get(responseId || '') || (this._activeId ? this._turns.get(this._activeId) : null);
      if (!turn) return null;
      turn.cancelled = true;
      turn.trace('cancel', reason || '');
      this._clearFallback(turn);
      this._clearStallTimer(turn);
      if (turn.responseId) {
        this._cancelledIds.add(turn.responseId);
        if (this._cancelledIds.size > 500) {
          this._cancelledIds.delete(this._cancelledIds.values().next().value);
        }
      }
      this._notify('cancel', { responseId: turn.responseId, sink: turn.sink, reason: reason || '' });
      // 本地 sink 必须被真的"停口"，只丢 buffer 会留下已经排进 AudioWorklet 的部分。
      this._write(SINK_LOCAL, null, { responseId: turn.responseId, clear: true });
      this._finish(turn);
      return turn;
    }

    /* ------------------------------------------------ avatar 侧回报接口 */

    /**
     * avatar sink 的**唯一**交接动作：一段音频已经在 :8010 落库。
     * @param {string} responseId
     * @param {number} bytes 该段字节数（累计语义由本函数负责）
     * @param {object} [receipt]
     */
    confirmAvatar(responseId, bytes, receipt) {
      const turn = this._turns.get(responseId || '');
      if (!turn || turn.sink !== SINK_AVATAR) return false;
      turn.confirmedBytes = Math.min(turn.bytes, turn.confirmedBytes + Math.max(0, bytes | 0));
      if (turn.confirmedBytes > 0) this._setState(turn, 'AUDIO_PLAYING', 'avatar');
      turn.trace('confirm', `${turn.confirmedBytes}/${turn.bytes}B`);
      if (turn.confirmedBytes >= turn.bytes) {
        this._clearFallback(turn);
        if (turn.endedAt) this._finish(turn);
      } else if (!turn.fallbackTimer && turn.endedAt) {
        this._scheduleFallback(turn, 'avatar_partial_receipt');
      }
      this._log('avatar_confirm', {
        responseId, confirmedBytes: turn.confirmedBytes, bytes: turn.bytes, receipt: receipt || null,
      });
      return true;
    }

    /** avatar sink 明确知道自己这轮发不出去了（8010 挂 / 上传失败 / 会话无效）。 */
    reportAvatarFailure(responseId, reason) {
      const turn = this._turns.get(responseId || '');
      if (!turn || turn.sink !== SINK_AVATAR) return false;
      this._failOpen(turn, reason || 'avatar_failed');
      return true;
    }

    /* --------------------------------------------------------- internals */

    /** 唯一策略点：本轮谁发声。 */
    _pickSink() {
      if (this.policy.avatar_enabled && this.sinkAvailable(SINK_AVATAR)) return SINK_AVATAR;
      return SINK_LOCAL;
    }

    /** 把 avatar 还没承担的字节全部交给本地 sink；每轮最多一次。 */
    _failOpen(turn, reason) {
      if (turn.sink === SINK_LOCAL) return;
      if (turn.fellBack) {
        this.counters.double_audio_guard_hits += 1;
        turn.trace('fallback_suppressed', `${turn.fellBack} vs ${reason}`);
        return;
      }
      turn.fellBack = reason;
      turn.sink = SINK_LOCAL;
      this.counters.fail_open += 1;
      this._clearFallback(turn);
      this._clearStallTimer(turn);
      const from = turn.confirmedBytes;
      turn.trace('fail_open', `${reason} from=${from}B to=${turn.bytes}B`);
      this._log('fail_open', {
        responseId: turn.responseId, reason, fromBytes: from, toBytes: turn.bytes,
      });
      this._notify('fail_open', { responseId: turn.responseId, reason, fromBytes: from });
      this._writeLocal(turn, this._sliceFrom(turn, from));
      if (!turn.endedAt) this._scheduleFallback(turn, 'avatar_unconfirmed_at_fail_open');
    }

    _scheduleFallback(turn, reason) {
      if (turn.fallbackTimer || turn.sink !== SINK_AVATAR) return;
      turn.fallbackTimer = setTimeout(() => {
        turn.fallbackTimer = null;
        if (turn.cancelled || turn.sink !== SINK_AVATAR) return;
        if (turn.confirmedBytes >= turn.bytes) return;   // 全确认了，不需要兜底
        this._failOpen(turn, reason);
      }, this.graceMs);
    }

    _clearFallback(turn) {
      if (turn.fallbackTimer) {
        clearTimeout(turn.fallbackTimer);
        turn.fallbackTimer = null;
      }
    }

    /**
     * One timer per audio delta, so a response that dies mid-stream (server
     * cancelled it, transport dropped, `response.done` never arrives) still
     * produces sound within AUDIO_STALL_MS instead of waiting for the pruner.
     */
    _armStallTimer(turn) {
      if (turn.stallTimer) return;         // one is enough; it re-arms on fire
      turn.stallTimer = setTimeout(() => {
        turn.stallTimer = null;
        if (turn.cancelled || turn.sink !== SINK_AVATAR) return;
        if (turn.confirmedBytes >= turn.bytes) return;
        if (nowMs() - turn.lastDeltaAt < AUDIO_STALL_MS) return;   // still streaming
        this._failOpen(turn, 'avatar_stalled');
      }, AUDIO_STALL_MS);
    }

    _clearStallTimer(turn) {
      if (turn.stallTimer) {
        clearTimeout(turn.stallTimer);
        turn.stallTimer = null;
      }
    }

    /** [fromByte, end) 的切片，用于只移交未确认的尾巴。 */
    _sliceFrom(turn, fromByte) {
      const out = [];
      let offset = 0;
      for (const chunk of turn.chunks) {
        const size = chunk.length * 2;
        if (offset + size > fromByte) {
          const skipBytes = Math.max(0, fromByte - offset);
          out.push(skipBytes ? chunk.subarray(skipBytes / 2) : chunk);
        }
        offset += size;
      }
      turn.localBytes = turn.bytes;
      if (!out.length) return new Int16Array(0);
      const total = out.reduce((n, a) => n + a.length, 0);
      const flat = new Int16Array(total);
      let at = 0;
      for (const a of out) { flat.set(a, at); at += a.length; }
      return flat;
    }

    _writeLocal(turn, samples) {
      if (!samples || !samples.length) return false;
      const ok = this._write(SINK_LOCAL, samples, { responseId: turn.responseId });
      if (ok && turn.sink === SINK_LOCAL) {
        turn.localBytes = turn.bytes;
        // 只有"字节真的写进了活的本地 sink"才算在发声，不看 TTS 有没有产出。
        this._setState(turn, 'AUDIO_PLAYING', 'local_browser');
      }
      return ok;
    }

    _write(name, samples, meta) {
      const entry = this._sinks.get(name);
      if (!entry) {
        // 目标 sink 不在：`local_browser` 缺席才是真故障（说明 client 没注册），
        // 这里只记账并丢，绝不因为路由本身出问题而抛错打断音频链路。
        if (name === SINK_LOCAL) this._log('local_sink_missing', { responseId: meta?.responseId || '' });
        return false;
      }
      try {
        entry.sink.write(samples, meta || {});
        return true;
      } catch (err) {
        this._log('sink_write_failed', { sink: name, error: String(err && err.message || err) });
        return false;
      }
    }

    _finish(turn) {
      this._clearFallback(turn);
      this._clearStallTimer(turn);
      turn.chunks = [];
      turn.bytes = Math.max(turn.bytes, turn.confirmedBytes, turn.localBytes);
      const audible = !turn.cancelled && turn.playedBytes > 0;
      // 收口时只剩"生成过但一个字节都没落到活 sink"这一种真静音，必须说出来。
      // FINAL_02：guard 命中的轮次不允许被记成"完整成功"——最终状态强制
      // AUDIO_TRUNCATED，并随收口账本（lastTurns）与计数器一起暴露。
      const finalState = turn.cancelled ? 'AUDIO_CANCELLED'
        : turn.truncatedAtGuard ? 'AUDIO_TRUNCATED'
        : (audible ? 'AUDIO_DONE' : 'AUDIO_SILENT');
      this._setState(turn, finalState,
        turn.cancelled ? 'cancelled'
          : turn.truncatedAtGuard ? 'guard'
          : `played=${turn.playedBytes}/${turn.bytes}B`);
      if (!audible && !turn.cancelled) {
        this.counters.silent_turns += 1;
        this._log('silent_turn', { responseId: turn.responseId, bytes: turn.bytes, sink: turn.sink });
      }
      if (turn.responseId) {
        this._finished.set(turn.responseId, {
          responseId: turn.responseId,
          audible,
          sink: turn.sink,
          state: turn.state,
          bytes: turn.bytes,
          playedBytes: turn.playedBytes,
          fellBack: turn.fellBack,
          cancelled: turn.cancelled,
          truncatedAtGuard: turn.truncatedAtGuard || false,
        });
        while (this._finished.size > 200) {
          this._finished.delete(this._finished.keys().next().value);
        }
      }
      this._turns.delete(turn.responseId);
      if (this._activeId === turn.responseId) this._activeId = '';
    }

    /**
     * §十一：音频状态分级。只在真的变化时通知，一次 response 最多 4 条，
     * 不会按 delta 打爆观察者。
     * @param {Turn} turn @param {string} state @param {string} [detail]
     */
    _setState(turn, state, detail) {
      if (!turn || turn.state === state) return false;
      // 单调：迟到的 delta 不许把「正在播放」说回「已生成」。
      if (STATE_RANK[turn.state] >= STATE_RANK[state]) return false;
      turn.state = state;
      turn.trace(`state:${state}`, detail || '');
      this._log('turn_state', {
        responseId: turn.responseId, state, sink: turn.sink,
        bytes: turn.bytes, playedBytes: turn.playedBytes,
      });
      this._notify('state', {
        responseId: turn.responseId, state, sink: turn.sink,
        bytes: turn.bytes, playedBytes: turn.playedBytes, detail: detail || '',
      });
      return true;
    }

    /**
     * 这一轮最终有没有出声。`response.done` 时轮次已经收口，所以读的是收口账本，
     * 而不是浏览器的 RMS 猜测 —— 数字人发声时本机本来就该是静的。
     * @returns {{audible: boolean, sink: string, state: string}|null}
     */
    turnAudibility(responseId) {
      const rec = responseId ? this._finished.get(responseId) : null;
      if (rec) return { audible: rec.audible, sink: rec.sink, state: rec.state };
      const live = responseId ? this._turns.get(responseId) : null;
      if (live) {
        return {
          audible: live.playedBytes > 0, sink: live.sink, state: live.state,
        };
      }
      return null;
    }

    /** 只回收"已经结束且已经不留尾巴"的轮次；进行中的轮次永远不回收。 */
    _prune() {
      const ids = Array.from(this._turns.keys());
      if (ids.length <= MAX_TURNS) return;
      for (const id of ids) {
        if (this._turns.size <= MAX_TURNS) break;
        const turn = this._turns.get(id);
        if (!turn || id === this._activeId) continue;
        const stale = turn.endedAt && (nowMs() - turn.endedAt) > TURN_RETENTION_MS;
        const abandoned = (nowMs() - turn.lastDeltaAt) > TURN_RETENTION_MS;
        if (stale || abandoned) {
          // 回收不得吞掉声音：还有未承担字节时先交给本地（正常情况下 stall
          // 定时器早就降级了，这里是最后一道）。
          if (turn.sink === SINK_AVATAR && turn.confirmedBytes < turn.bytes) {
            this._failOpen(turn, 'turn_discarded');
          }
          this._log('turn_pruned', { responseId: id, bytes: turn.bytes, sink: turn.sink });
          this._finish(turn);
        }
      }
    }

    _log(what, fields) {
      this.decisions.push(Object.assign({ at: Math.round(nowMs()), what }, fields || {}));
      if (this.decisions.length > 60) this.decisions.shift();
    }

    /** avatar-sync / 面板订阅轮次事件，替代旧的 WebSocket 劫持。
     *  事件：begin | end | cancel | fail_open | state
     *  `state` 的载荷带 §十一 的音频分级：TTS_READY → AUDIO_GENERATED →
     *  AUDIO_PLAYING → AUDIO_DONE / AUDIO_SILENT / AUDIO_CANCELLED。
     */
    on(event, fn) {
      if (typeof fn !== 'function') return () => {};
      const rec = { event, fn };
      this._observers.add(rec);
      return () => this._observers.delete(rec);
    }

    _notify(event, payload) {
      for (const rec of this._observers) {
        if (rec.event !== event) continue;
        try {
          rec.fn(payload);
        } catch (err) {
          this._log('observer_failed', { event, error: String(err && err.message || err) });
        }
      }
    }

    /** §十四：页面侧的可观测性。 */
    diagnostics() {
      const turns = [];
      for (const turn of this._turns.values()) {
        turns.push({
          responseId: turn.responseId,
          sink: turn.sink,
          state: turn.state,
          bytes: turn.bytes,
          confirmedBytes: turn.confirmedBytes,
          localBytes: turn.localBytes,
          ended: Boolean(turn.endedAt),
          cancelled: turn.cancelled,
          fellBack: turn.fellBack,
          ageMs: Math.round(nowMs() - turn.begunAt),
        });
      }
      return {
        counters: Object.assign({}, this.counters),
        policy: Object.assign({}, this.policy),
        sinks: Array.from(this._sinks.values()).map((e) => ({
          name: e.name, available: this.sinkAvailable(e.name), priority: e.priority,
        })),
        activeResponseId: this._activeId,
        openTurns: turns,
        lastTurns: Array.from(this._finished.values()).slice(-8),
        recent: this.decisions.slice(-24),
      };
    }

    /** 仅用于测试与换人：清空决策与轮次，不注销 sink。 */
    reset(reason) {
      for (const turn of Array.from(this._turns.values())) {
        // 与 _prune 同一条规矩：清空也不吞掉还没发声的字节。
        if (turn.sink === SINK_AVATAR && turn.confirmedBytes < turn.bytes) {
          this._failOpen(turn, `router_reset:${reason || 'manual'}`);
        }
        this._finish(turn);
      }
      this.decisions = [];
      this._log('reset', { reason: reason || 'manual' });
    }
  }

  const router = new AudioRouter();
  router.SINK_LOCAL = SINK_LOCAL;
  router.SINK_AVATAR = SINK_AVATAR;

  globalThis.s2sAudioRouter = router;
  globalThis.S2sAudioRouter = AudioRouter;

  if (typeof window !== 'undefined') {
    // 与 s2sTurnControl 一样：诊断入口，方便在控制台直接读"这一轮谁在发声"。
    window.__audioRouterDiagnostics = () => router.diagnostics();
  }
})(typeof globalThis !== 'undefined' ? globalThis : window);
