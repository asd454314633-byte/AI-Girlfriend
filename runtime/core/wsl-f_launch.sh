#!/usr/bin/env bash
# F launch: process-local LD_LIBRARY_PATH -> dlopen precheck -> frozen s2s command
#
# !! PROVENANCE / READ THIS BEFORE EDITING (established by probe, 2026-09-24) !!
# This is the mirror of the WSL-era launch chain, NOT the one serving :8765 today:
#   repo copy --identical except for this comment block--> $S2S_HOME/f_launch.sh
#     (both were sha256 98da9eb61c0e064d... at 12:3x on 2026-09-24; the WSL copy has
#      not been touched since, so the only difference is the text you are reading)
#     <- reached only by scheduled task \GirlfriendS2S8765_Manual (State=Ready,
#        last run 2026-09-23 17:52) -> g11_restart_s2s.sh -> setsid nohup bash ...
#   What actually owns :8765 now is a Windows process launched by the PowerShell
#   lifecycle layer: runtime/core/venv/Scripts/speech-to-speech.exe, whose argv is
#   expanded from runtime/core/providers/manifests/{stt,tts}.json.  Nothing in this
#   repo copies it into WSL: the live file used to be patched in place with `sed -i`
#   (runtime/llm/patch_flaunch.sh), so the two trees drift silently in both directions.
# Consequence: the values below are a SECOND, DIVERGENT set of provider facts, and
# editing this file changes nothing on a running system.
#   --faster_whisper_stt_model_name large-v3        vs stt.json  {STT_MODEL_DIR} = models/native/whisper-large-v3
#   --qwen3_tts_model_name Qwen/Qwen3-TTS-...-Base   vs tts.json  {QWEN3_MODEL_DIR} = models/native/qwen3
#   case qwen3|higgs                                 vs the TTS manifest (a hard-coded whitelist:
#                                                     a newly registered provider exits 12 here)
#   --model_name Qwen3-14B-Instruct-Q4_K_M           vs the live 8090 server, which serves Gemma4 12B
# The first two are defensible only because HF_HUB_OFFLINE=1 resolves hub ids out of the
# Linux cache under $S2S_HOME -- a different asset store from the Windows one.
# Registering or moving a provider must NOT be done here.  Fix the manifests and the
# PowerShell entry points (runtime/windows/Switch-Girlfriend-Voice.ps1,
# scripts/Start-AI-Girlfriend.ps1); retire this chain by deleting the task, not by
# keeping it in sync.  See runtime/audit/system_v2/LIFECYCLE_MANIFEST_SWEEP.md.
set -u
S2S_HOME="${S2S_HOME:-$HOME/s2s}"
S2S_WIN_ROOT="${S2S_WIN_ROOT:-/mnt/d/AI_GF_OPEN/repo}"
cd "$S2S_HOME"
source "$S2S_HOME/.venv/bin/activate"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

SP="$S2S_HOME/.venv/lib/python3.12/site-packages"
# STEP4: build colon-joined LD_LIBRARY_PATH from all nvidia/*/lib dirs
NVIDIA_LIBS=$(find "$SP/nvidia" -maxdepth 3 -type d -name lib 2>/dev/null | sort | tr '\n' ':')
NVIDIA_COUNT=$(echo "$NVIDIA_LIBS" | tr ':' '\n' | grep -c .)
export LD_LIBRARY_PATH="${NVIDIA_LIBS}${LD_LIBRARY_PATH:-}"
echo "NVIDIA_LIB_DIR_COUNT=$NVIDIA_COUNT"
echo "LD_LIBRARY_PATH_SCOPE=PROCESS_ONLY"

# STEP4 dlopen precheck (must PASS before launch)
python - << 'PYEOF'
import ctypes
lib = ctypes.CDLL("libcublas.so.12")
print("DLOPEN_LIBCUBLAS=PASS")
PYEOF
if [ $? -ne 0 ]; then
  echo "DLOPEN_LIBCUBLAS=FAIL -> ABORT BEFORE LAUNCH"
  exit 11
fi

echo "=== launching frozen speech-to-speech command ==="
REF_TEXT=''
TTS_BACKEND=$(cat "$S2S_WIN_ROOT/runtime/llm/active_tts_backend.txt" 2>/dev/null || echo qwen3)
case "$TTS_BACKEND" in qwen3|higgs) ;; *) echo "INVALID_TTS_BACKEND=$TTS_BACKEND"; exit 12 ;; esac
echo "TTS_BACKEND=$TTS_BACKEND"
exec speech-to-speech \
  --mode realtime \
  --stt faster-whisper \
  --faster_whisper_stt_model_name large-v3 \
  --faster_whisper_stt_gen_language zh \
  --language zh \
  --llm_backend chat-completions \
  --model_name Qwen3-14B-Instruct-Q4_K_M \
  --responses_api_base_url http://localhost:8090/v1 \
  --responses_api_api_key sk-noauth \
  --responses_api_stream \
  --chat_size 24 \
  --compact_history \
  --num_pipelines 1 \
  --tts "$TTS_BACKEND" \
  --qwen3_tts_model_name Qwen/Qwen3-TTS-12Hz-1.7B-Base \
  --qwen3_tts_language zh \
  --qwen3_tts_ref_audio "$HOME/s2s/ref.wav" \
  --qwen3_tts_ref_text "$REF_TEXT" \
  --thresh 0.6 \
  --min_speech_ms 500 \
  --min_silence_ms 1200 \
  --speech_pad_ms 300 \
  >> "$S2S_WIN_ROOT/evidence/provenance_08/f_s2s_runtime.log" 2>&1
