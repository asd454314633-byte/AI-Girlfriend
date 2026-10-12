# NOTICE (attribution, derivative boundaries and license carve-outs)

This file records the per-file provenance classes verified on 2026-10-12 by:
(a) a git-blob diff of `runtime/avatar/app/**` against the pinned upstream
commit, and (b) line-level similarity checks of the UI files against their
upstream sources. The classification follows the prior private source audit. This tree is a privacy-filtered subset of that audited staging tree; original counts describe the prior audit, not this upload.

## 1. Self-developed (original) — top-level MIT license applies

- Service lifecycle & TTS switch control plane: `runtime/windows/*.ps1`
  (single-transaction switch authority, journal contract, idempotent avatar ensure).
- Mode system: `runtime/mode/*` (single-writer mode authority, per-mode prompts).
- Reading pipeline: `runtime/reading/**`, `runtime/reading_v2/**`
  (segmentation, per-segment playback receipts, checkpoint ledger,
  cross-chapter prefetch; frozen `ui/reading.js` included verbatim).
- Web UI application code: `runtime/ui/app/*.py`, `runtime/ui/app/*.js`,
  `runtime/ui/app/ui/**` — EXCEPT the files listed in §2/§3 below and the
  quarantined `avatar-sync.js` (§5).
- Business layer: `runtime/s2s_business/**`; provider adapters and
  vendor_fixes wrappers: `runtime/core/providers/**`; lifecycle scripts:
  `scripts/**`; gate tests: `runtime/tests/**`, `tests/**`.
- Character framework code: `runtime/character/*.py` (persona content excluded).
- Avatar server additions: `runtime/avatar/app/server/{idempotency,playback,
  session_lifecycle,terminate_gate}.py`, `runtime/avatar/app/tests/
  test_session_lifecycle_finalrepair01.py`, `runtime/avatar/app/utils/__init__.py`.
- speech-to-speech glue: `third_party_modified/speech_to_speech_0.2.11/api/
  openai_realtime/reading_bridge.py` is a NEW file (not present upstream),
  written for this project and installed into the pip package.

## 2. Third-party, verbatim (redistributed under their own licenses)

| What | Upstream | Version / commit | License |
|---|---|---|---|
| `runtime/avatar/app/**` (169 files incl. `tts/qwentts.py`, `avatars/wav2lip/models/*`, musetalk/whisper code, assets, READMEs, LICENSE) | https://github.com/lipku/LiveTalking | `c963ad409c556918b7d23999bf87c47a7c05c932` | Apache-2.0 (upstream LICENSE file kept in place) |
| `runtime/ui/app/vendor/openai-realtime-agents.umd.js` | https://github.com/openai/openai-realtime-agents (UMD bundle) | 0.14.3 (embedded version string), SHA256 `a8e2b9134e215d67dee000e874216d930f9daf76a9c1616bdc4844316edbba44` | MIT |

## 3. Third-party, modified derivatives (included; upstream license governs)

| File | Upstream source (verified by line-level diff) | Retained verbatim | License |
|---|---|---|---|
| `runtime/avatar/app/app.py` | LiveTalking `c963ad409c` | modified | Apache-2.0 |
| `runtime/avatar/app/avatars/base_avatar.py` | LiveTalking `c963ad409c` | modified | Apache-2.0 |
| `runtime/avatar/app/avatars/audio_features/base_asr.py` | LiveTalking `c963ad409c` | modified | Apache-2.0 |
| `runtime/avatar/app/avatars/wav2lip_avatar.py` | LiveTalking `c963ad409c` + this project's D-mode upgrade | modified | Apache-2.0 |
| `runtime/avatar/app/server/routes.py` | LiveTalking `c963ad409c` + lipsync-mode API | modified | Apache-2.0 |
| `runtime/avatar/app/server/rtc_manager.py` | LiveTalking `c963ad409c` | modified | Apache-2.0 |
| `runtime/avatar/app/server/session_manager.py` | LiveTalking `c963ad409c` | modified | Apache-2.0 |
| `runtime/avatar/app/server/webrtc.py` | LiveTalking `c963ad409c` | modified | Apache-2.0 |
| `runtime/ui/app/main.js` (92% of upstream retained) | speech-to-speech `demo/main.js` @ `5ac2730e88a111229c5b56b94b515a1bd4d30c65` (https://github.com/huggingface/speech-to-speech; the smolagents/hf-realtime-voice Space is a copy of this demo) | modified | Apache-2.0 |
| `runtime/ui/app/index.html` (73%) | speech-to-speech `demo/index.html` @ `5ac2730e` | modified | Apache-2.0 |
| `runtime/ui/app/server.py` (96%) | speech-to-speech `demo/server.py` @ `5ac2730e` | modified | Apache-2.0 |
| `runtime/ui/app/style.css` (upstream + additions) | speech-to-speech `demo/style.css` @ `5ac2730e` | extended | Apache-2.0 |
| `runtime/ui/app/s2s-realtime-client.js` | speech-to-speech demo RealtimeSession adapter lineage | modified | Apache-2.0 |
| `third_party_modified/speech_to_speech_0.2.11/api/openai_realtime/websocket_router.py` | speech-to-speech 0.2.11 wheel (SHA256-verified against upstream, wheel `ac33e2e8…`): installed copy differs from `dist-info/RECORD` hash → modified | modified | Apache-2.0 |

Apache-2.0 attribution: the upstream LICENSE and copyright notices are
preserved; modifications are documented in this NOTICE. Apache-2.0 §4 requires
carrying the original license — do not strip `runtime/avatar/app/LICENSE`.

**Wav2Lip note (weights, not code).** No file from the original
Rudrabha/Wav2Lip repository is distributed here. The Wav2Lip *model weights*
(`wav2lip256.pth`, s3fd) carry their own research / non-commercial terms and
are record-only (`provenance/MODEL_AND_ASSET_SOURCES.csv`). LiveTalking's
Apache-2.0 code remains Apache-2.0; the weight restriction does not change
code licenses, but it binds anyone who downloads the weights.

## 4. Dependencies installed at setup time (not vendored)

speech-to-speech 0.2.11 (Apache-2.0, wheel SHA256 `ac33e2e8…b2a2`), 
faster-whisper, Silero VAD (via upstream VADHandler), llama.cpp — see
`docs/INSTALL.md` and `provenance/MODEL_AND_ASSET_SOURCES.csv`.

## 5. Quarantined for licensing (NOT in this tree)

- `runtime/ui/app/avatar-sync.js` — derives from
  `liuw8233-cell/ai_girlfriend@699f7fe` `assets/avatar-sync.js`
  (https://github.com/liuw8233-cell/ai_girlfriend). That repository has **no
  LICENSE file** (all rights reserved by default). Line-level diff shows 201
  of its 357 lines (56%) retained verbatim in our heavily extended version
  (1105 lines, including this project's 24 kHz fix, per-segment receipts and
  D-mode integration). Until the upstream grants a redistribution license or
  a separately reviewed replacement is authorized, this file stays out of the public
  tree. `index.html` still references `/avatar-sync.js`; without it the UI
  loads but the digital-human sync layer is absent.
  Re-verified 2026-10-12; see §7.

## 6. Private source archive scope

The unlicensed upstream-derived adapter described in §5 is excluded
**permanently**. The replacement adapter now shipped at
`runtime/ui/app/avatar-sync.js` is included; see §7. No claim of strict clean-room
isolation is made. Previous test assets, fixtures, user state and media were removed
before initializing this repository. Generic dependency vocabularies and the upstream
mel-filter numerical asset are software resources, not personal books or model weights.

## 7. Replacement adapter review (2026-10-12, superseded same day)

An independently developed replacement for `avatar-sync.js` was reviewed and is now
**admitted**, after completing the work that initially blocked it.

*Redistribution question.* Measured against the unlicensed upstream
`f5b4840ae6353cec…64ed8` (comments stripped; exact and identifier-normalized tokens;
function bodies compared independently of order): **2.84% exact, 4.18% normalized**,
no matched function bodies, no common blocks. It does not derive from the upstream file
and no prior version was copied, renamed, reformatted or stripped of notices in order
to get here. It was written against materials this tree is entitled to use: the
project's own behavioural contract, the sink interface of `audio-router.js`, the
Apache-2.0 LiveTalking server routes already vendored here, the call contract of
`ui/reading.js`, and the public WebRTC specification.

*Completeness.* The four previously missing reading interfaces are implemented with
real behaviour, not stubs: `expectReading(readingKey)`, `forgetReading(...)`,
`readingPlayback()` and `interrupt(...)`. All `AvatarSync` call sites were walked —
`ui/reading.js` (expect / playback / forget / optional-interrupt), the chat module,
`avatar_panel.js`, `avatar_layout.js` and `main.js` — and their argument order, async
contract, return shape and exception behaviour were matched. Playback boundaries are
derived from session-level `/is_speaking` flips and attributed to the registered
reading segment in order, so boundary times carry a documented polling-granularity
uncertainty rather than a fabricated exact value.

*Tests.* All five self-authored suites pass: `test_avatar_sync_session.cjs`,
`test_avatar_sync_session_reuse.cjs`, `test_avatar_sync_lifecycle.cjs`,
`test_avatar_sync_ice.cjs`, `test_avatar_sync_reading.cjs`. The failures originally
observed in the session suite were traced to fidelity gaps in the test doubles
(missing SDP structure, `console` and `AbortController` in the fake browser context);
the doubles were corrected and **no assertion or expected value was relaxed, deleted
or faked**.

*Verification boundary.* No real GPU, camera-driven avatar renderer, microphone or
live LiveTalking server was exercised, and the private production instance was neither
modified nor restarted. Reading playback on real hardware is therefore **NOT_TESTED**
and must not be presented as accepted production behaviour. Full evidence:
`provenance/AVATAR_SYNC_RESTORATION_AUDIT_20261012.md`.
