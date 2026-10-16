# AI-Girlfriend V3 — local voice, avatar, reading and live interaction

[中文默认README](README.md) · [Installation](docs/INSTALL.md) · [Agent deployment guide (Chinese)](docs/AI_AGENT_FULL_DEPLOYMENT_ZH.md) · [Source map](docs/V3_CAPABILITY_SOURCE_MAP.md) · [Testing](docs/TESTING.md)

**Development-preview source release, not a ready-to-run model bundle. FRESH_INSTALL_E2E=HOLD.** This update synchronizes legally distributable V3 source, while excluding model weights, native engines, private voice/avatar assets, books, progress, credentials and runtime registrations.

The nine capability groups are the Chinese UI and matching APIs; DARK ROAST9B/Gemma4 12B/Qwen3 4B LLM management; Qwen3-TTS Base/Higgs v3 Q4/Fish S2 Pro adapters and transactional switching; voice library and per-request Higgs reference cloning; model-scoped delivery controls; LiveTalking/WebRTC and the independently implemented public avatar adapter; raw-text reading and library trash/restore; live-comment scheduling; and authenticated Windows service management.

Browser→7860 UI/API; 8090 LLM; 8765 speech session; 8010 avatar/WebRTC; 8781 live scheduler←8888 external collector. Higgs7077/Fish3030 are on-demand workers with one active TTS. Start with **Start-AI-Girlfriend.cmd** after completing the Chinese installation guide. The UI must use `Manage-Project-Service.ps1 -Service ui`, with an independent visible console, authenticated identity checks, busy refusal and graceful timeout protection. Direct uvicorn is not the formal deployment entry.

Create a dedicated Python3.12 venv, install control and CUDA-compatible speech dependencies, initialize empty settings, apply the speech-to-speech0.2.11 overlay, and configure legal local models, your own reference WAV/transcript and avatar. Separate Avatar dependencies and manual interactive tasks are required. `config/models.example.json`, `environment.example.ps1` and the synthetic-reference helper are editable examples. No private “Xiaoyan” reference is shipped; that legacy ID is only a compatibility identifier. See INSTALL for full commands, model origins, Windows/Python/CUDA requirements, recovery and troubleshooting.

Higgs affection and amusement were accepted by the author's human listener; determination is retained with documented voice drift. This does not guarantee the same result for another reference speaker. Fish gentle/whisper are retained; sarcastic is not claimed. Qwen Base is a cloning backend, not a verified independent emotion-instruction interface. No emotion Skill restoration is claimed.

Online voice upload/CPU transcription/edit/preview/activation/trash applies fully to Higgs. Qwen uses startup-level references; Fish uses externally prepared named voices. Reading includes import, chapters, strategies, checkpoints, cancellation, batch delete and conflict-safe restore. Live supports deduplicated comments and local simulated response-to-avatar playback, not unimplemented automatic gift replies.

Historical existing-environment validation and release-source tests are separate. `LOCAL_LIVE_E2E=PASS` refers to the former; `REAL_PLATFORM_FRESH_EVENT=HOLD`. New-machine model/avatar/playback E2E remains HOLD. An earlier short-chat missing-tail issue was NOT_REPRODUCED, not proven fixed. See TESTING for exact test layers.

Original code is MIT; vendored/modified Apache-2.0 components retain their notices. Model and native-engine terms are separate: Higgs and Fish models have research/non-commercial restrictions and additional commercial licensing conditions. Wav2Lip weights have separate restrictions. Unlicensed legacy avatar-sync source is excluded; the previously reviewed independent implementation is preserved.

The [existing demonstrations](docs/media/AI_Girlfriend_Real_Demo_110s.mp4) and four companion media remain **display-only**, under [MEDIA_USAGE_POLICY.md](docs/MEDIA_USAGE_POLICY.md). They are not training, cloning, reusable identity assets or proof of fresh-install acceptance. No new personal media is added.
