"""FakeLLMHandler: a deterministic, network-free language-model stage.

The CORE CONTRACT layer of 任务 §25 needs to prove that persona, memory, tool and
context-budget plumbing reaches *a* model without waiting for a 9 B GGUF to load,
and that the turn bookkeeping (chunk → token usage → EndOfResponse) holds for any
provider.  This handler records the exact message list it was handed and replies
from a canned script, so a test can assert "the Context Builder produced one
system block containing the canon line" rather than "the GPU said something".

Identity comes from the manifest (``providers/manifests/llm.json`` ->
``test_fake``); like every adapter it reads no other provider's name.
"""
from __future__ import annotations

import logging
from threading import Event
from typing import Any, Iterator

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.messages import (
    EndOfResponse,
    GenerateResponseRequest,
    LLMResponseChunk,
    TokenUsage,
)

from .. import registry
from .recorder import record

logger = logging.getLogger(__name__)

PROVIDER_ID = "test_fake"
DEFAULT_REPLY = (
    "好的，我记住了你说的这件事。",
    "那我们接着往下聊，你想先说哪一部分？",
)


def _chat_as_messages(chat: Any) -> list[dict[str, Any]]:
    """Best-effort view of what a model would receive, never a hard dependency.

    ``Chat.to_transformers_chat`` is the upstream shape; a stub chat object in a
    unit test only needs ``.items`` or to be a plain list.
    """
    if chat is None:
        return []
    if isinstance(chat, list):
        return [dict(row) for row in chat if isinstance(row, dict)]
    to_messages = getattr(chat, "to_transformers_chat", None)
    if callable(to_messages):
        try:
            return [dict(row) for row in to_messages() if isinstance(row, dict)]
        except Exception:  # pragma: no cover - defensive: a stub chat may be partial
            logger.debug("FakeLLM: to_transformers_chat failed", exc_info=True)
    raw = getattr(chat, "items", None)
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for row in raw:
            payload = getattr(row, "model_dump", lambda: row)()
            if isinstance(payload, dict):
                out.append(payload)
        return out
    return []


class FakeLLMHandler(BaseHandler):
    def setup(
        self,
        should_listen: Event,
        provider_id: str | None = None,
        provider_spec: dict[str, Any] | None = None,
        cancel_scope=None,
        speculative_turns=None,
        reply: tuple[str, ...] | list[str] | None = None,
        **_kwargs: Any,
    ) -> None:
        self.should_listen = should_listen
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.provider_id = str(provider_id or (provider_spec or {}).get("id") or PROVIDER_ID)
        self.spec = provider_spec or registry.get("LLM", self.provider_id) or {}
        self.reply = tuple(reply) if reply else DEFAULT_REPLY
        logger.info("FakeLLMHandler ready (provider_id=%s)", self.provider_id)

    def process(self, request: GenerateResponseRequest) -> Iterator[Any]:
        config = getattr(request, "runtime_config", None)
        chat = getattr(config, "chat", None)
        messages = _chat_as_messages(chat)
        system_text = "\n".join(
            str(row.get("content", ""))
            for row in messages
            if str(row.get("role", "")).lower() in {"system", "developer"}
        )
        user_text = "".join(
            str(row.get("content", ""))
            for row in messages
            if str(row.get("role", "")).lower() == "user"
        )
        record(
            "llm",
            self.provider_id,
            messages=messages,
            system_text=system_text,
            user_text=user_text.strip(),
            message_count=len(messages),
            instructions=str(getattr(getattr(request, "response", None), "instructions", "") or ""),
            turn_id=request.turn_id,
            turn_revision=request.turn_revision,
        )
        for chunk in self.reply:
            yield LLMResponseChunk(
                text=chunk,
                language_code=request.language_code,
                runtime_config=config,
                response=request.response,
                turn_id=request.turn_id,
                turn_revision=request.turn_revision,
                speech_stopped_at_s=request.speech_stopped_at_s,
            )
        yield TokenUsage(
            input_tokens=sum(len(str(row.get("content", ""))) for row in messages) // 4,
            output_tokens=sum(len(row) for row in self.reply) // 4,
            turn_id=request.turn_id,
            turn_revision=request.turn_revision,
        )
        yield EndOfResponse(
            turn_id=request.turn_id,
            turn_revision=request.turn_revision,
        )
