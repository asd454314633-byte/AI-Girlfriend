# Install & Run Notes (staging — environment reconstruction guide)

> This staging tree contains **code only**. Models, weights and personal data are
> NOT included — see `provenance/MODEL_AND_ASSET_SOURCES.csv` and fetch them from
> their official sources. Paths below are relative to a deployment root.

## Services

| Port | Service | Entry |
|---|---|---|
| 7860 | Web UI (frontend + local API) | from `runtime/ui/app`, run `python -m uvicorn server:app --host 127.0.0.1 --port 7860` in a dedicated visible console |
| 8090 | Local LLM (llama.cpp server, Qwen3 14B GGUF) | llama.cpp binary + model (fetch separately) |
| 8765 | speech-to-speech 0.2.11 session service | `pip install speech-to-speech==0.2.11` in a venv; launch with `--tts qwen3` |
| 8010 | LiveTalking + Wav2Lip256 avatar / WebRTC | `runtime/avatar/app` (see `runtime/windows/Run-Girlfriend-Avatar8010.ps1`) |

## Control plane (recommended)

`scripts/Start-AI-Girlfriend.ps1` performs health-guarded startup:
- 8090 health guard, 8010 idempotent ensure, 8765 already-active fast-exit,
  only 7860 truly restarts.
- Success criterion is `logs/runtime/startup.log` `START_NATIVE_OK backend=<x>`,
  not the calling pipe.

State single-sources (do not hand-edit):
- `runtime/windows/active_tts_backend.txt` — committed TTS backend
- `runtime/windows/tts_switch_state.json` — switch transaction journal
  (Python-side sole writer)
- `runtime/windows/avatar_runtime_state.json` — 8010 lifecycle state
  (`generation` increments monotonically)

## TTS switch

`POST /api/voice-models/switch {"target": "qwen3|higgs|fish"}`; status via
`GET /api/voice-models`. Frontend consumes `switch.busy / switch.message / result`.

## Modes

`GET /api/mode` / `POST /api/mode` — authority is `runtime/mode/mode_authority.py`
→ `runtime/mode/mode_state.json` (atomic writes). localStorage is a hint only.
The third mode is a placeholder (`implemented=false`) and is rejected server-side.

## Reading mode

`7860 /api/reading/speak → 8765 /v1/speak → TTS queue` (raw text, no LLM).
Progress advances only on actual playback end; checkpoint DB is separate from
user memory storage.

## Verification gates

Run before any control-plane change (see `docs/TESTING.md`):
```
python runtime/tests/test_tts_switch_transaction.py
python runtime/tests/test_state_single_source.py
```

## Not covered here

- Model download/quantization (Qwen3 14B GGUF, Wav2Lip weights, s3fd, Wav2Lip256
  package) — fetch from official sources; Wav2Lip lineage is non-commercial.
- Windows-specific environment (python versions, torch/CUDA pins) — see
  `runtime/core/wsl-production-*.txt` and `windows-production-requirements.txt`
  as recorded baselines.

## speech-to-speech patches (third_party_modified/)

After `pip install speech-to-speech==0.2.11`, apply the project patches from
`third_party_modified/speech_to_speech_0.2.11/api/openai_realtime/`:

- `reading_bridge.py` — NEW file (self-developed); copy it into
  `<venv>/Lib/site-packages/speech_to_speech/api/openai_realtime/`.
- `websocket_router.py` — MODIFIED derivative of the upstream file
  (Apache-2.0); replace the installed copy with this one, or re-apply the
  equivalent import/patch. The upstream wheel hash for verification is in
  `provenance/COMPONENT_SOURCES.csv` (S2S_PACKAGE).

`providers/vendor_fixes/*` are installed by `providers.bootstrap` at import
time as wrappers; the vendor wheel stays pristine.

## avatar-sync.js gap (known limitation)

The frontend adapter is excluded because legacy redistribution permission remains unresolved. The experimental replacement and all acceptance artifacts are excluded too. Digital-human operation is incomplete in this source archive; no complete runtime acceptance is claimed.

User data, current selections and voice references are intentionally absent. The configuration contains generic relative examples; configure your own local installation before running any lifecycle script. Do not run these scripts against an existing production installation merely to inspect this archive.

## Capability levels (honest scope)

- Offline-verifiable without any model: code syntax/imports, config loading,
  unit-level logic tests (see `docs/TESTING.md`).
- Requires user-downloaded models: STT (whisper), TTS (qwen3/higgs), LLM
  (Qwen3 14B GGUF via llama.cpp), lip-sync (Wav2Lip256 weights, s3fd).
- Requires GPU + Windows: 8010 digital human (WebRTC), real-time pipeline.
