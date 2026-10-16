"""One real 8765 text -> LLM -> active TTS audio turn for manual voice QA."""

from __future__ import annotations

import argparse
import base64
import json
import time
import wave
from pathlib import Path

from websockets.sync.client import connect


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", choices=("qwen3", "higgs", "fish"))
    parser.add_argument("--emotion", default="")
    parser.add_argument("--text", default="请只回复：你好。")
    parser.add_argument("--wav", default="", help="Save the exact Realtime output PCM as a WAV for tail-word QA")
    parser.add_argument("--sample-rate", type=int, default=16000)
    args = parser.parse_args()
    manual = {"emotion": args.emotion} if args.emotion else {}
    started = time.monotonic()
    audio_chunks = 0
    audio_b64_chars = 0
    audio_bytes = 0
    captured_audio = bytearray()
    transcript = []
    errors = []
    done_status = None
    with connect("ws://127.0.0.1:8765/v1/realtime", open_timeout=15, max_size=16 * 1024 * 1024) as ws:
        ws.send(json.dumps({
            "type": "session.update",
            "session": {"type": "realtime", "tts_backend": args.backend, "manual_voice_mode": manual},
        }, ensure_ascii=False))
        ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": args.text}
            ]},
        }, ensure_ascii=False))
        ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"]}}))
        deadline = time.monotonic() + 220
        while time.monotonic() < deadline:
            try:
                event = json.loads(ws.recv(timeout=min(20, deadline - time.monotonic())))
            except TimeoutError:
                continue
            kind = event.get("type")
            if kind in ("response.audio.delta", "response.output_audio.delta"):
                audio_chunks += 1
                audio_b64_chars += len(event.get("delta", ""))
                chunk = base64.b64decode(event.get("delta", ""))
                audio_bytes += len(chunk)
                if args.wav:
                    captured_audio.extend(chunk)
            elif kind in ("response.audio_transcript.delta", "response.output_audio_transcript.delta", "response.text.delta", "response.output_text.delta"):
                transcript.append(event.get("delta", ""))
            elif kind in ("response.audio_transcript.done", "response.output_audio_transcript.done", "response.output_text.done"):
                transcript.append(event.get("transcript") or event.get("text") or "")
            elif kind == "error":
                errors.append(event.get("error", {}).get("message", str(event))[:250])
            elif kind == "response.done":
                done_status = event.get("response", {}).get("status")
                break
    if args.wav:
        output = Path(args.wav)
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(args.sample_rate)
            wav.writeframes(captured_audio)
    print(json.dumps({
        "backend": args.backend, "manual_voice_mode": manual, "audio_chunks": audio_chunks,
        "audio_b64_chars": audio_b64_chars, "transcript": "".join(transcript)[:120],
        "transcript_tail": "".join(transcript)[-80:], "wav": args.wav,
        "audio_seconds": round(audio_bytes / (2 * args.sample_rate), 2),
        "transcript_chars": len("".join(transcript)),
        "response_status": done_status, "errors": errors,
        "elapsed_s": round(time.monotonic() - started, 2),
        "pass": bool(audio_chunks and done_status == "completed" and not errors),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
