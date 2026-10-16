"""Offline regression for 任务 §7 E (跨段音色变化) and H (8010 数字人幂等).

Everything here is in-process.  No network, no GPU, no model weights, no
running :8010/:8765/:7860 service, no fixture audio — the two "providers" are the
fake adapters below and the "avatar renderer" is a counting receiver.

What each side contributes
--------------------------
* E: :mod:`continuity_contract` (staged body for ``providers/continuity.py``)
  freezes ``response_id / voice_identity / voice_controls / seed / reference_id /
  continuation_context`` once per response; a fake adapter then renders several
  segments under it.  The assertions are *invariants*, not waveforms: one identity,
  one emission, one seed, unique segment keys.
* H: :mod:`server.idempotency` (shipped at ``runtime/avatar/app/server/``) is the
  ``(response_id, segment_index)`` gate at the audio enqueue boundary, driven here
  through a fake receiver that records what actually got enqueued.  Its
  :meth:`~server.idempotency.SegmentGate.cancel_turn` is the single owner of
  "cancel the current response" on that service, including the barge-in case
  where the interrupted reply never enqueued a byte — covered by a named-cancel
  test *and* by the negative control next to it, which shows an unnamed cancel
  letting exactly that segment through.

Vocabulary guard: the contract's capability names are asserted to be a subset of
the registry's own ``TTS_DELIVERY_CAPABILITIES`` (:data:`providers.capabilities`),
and the per-provider expectations are read from the real
``manifests/tts.json`` — so this file fails if either the contract invents a
private vocabulary or a manifest row starts claiming continuity it never wired.

Run::

    runtime/core/venv/scripts/python.exe runtime/tests/test_continuity_and_idempotency.py
"""

from __future__ import annotations

import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
for _p in (
    str(RUNTIME / "core"),
    str(RUNTIME / "avatar" / "app"),
    str(RUNTIME / "audit" / "system_v2" / "tools"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from continuity_contract import (  # noqa: E402
    CONTINUITY_CAPABILITIES,
    CONTINUITY_FIELDS,
    ContinuationContext,
    pin_continuity,
    audit_run,
    response_seed,
)

from server.idempotency import (  # noqa: E402
    ACCEPT,
    DUPLICATE,
    ENFORCE,
    OBSERVE,
    OUT_OF_ORDER,
    REJECTS,
    STALE_RESPONSE,
    SegmentGate,
    UNTAGGED,
)

# ── fake providers ───────────────────────────────────────────────────


def _cap(*names):
    """A manifest-shaped capabilities block: named bits supported, rest not."""
    vocabulary = ("emotion", "custom_style", "speed", "pitch", "voice_clone",
                  "streaming", "continuation", "seed", "reference_cache")
    return {
        name: {"supported": name in names}
        for name in vocabulary
    }


#: Declares everything §E asks for -> must honour all six contract fields.
SPEC_RICH = {
    "id": "fake_full",
    "capabilities": _cap("voice_clone", "streaming", "continuation", "seed", "reference_cache"),
}

#: Declares nothing but streaming -> must run fine and say what it cannot do.
SPEC_BARE = {
    "id": "fake_bare",
    "capabilities": _cap("streaming"),
}

#: Declares seed + voice_clone but no continuation (the qwen3/higgs shape).
SPEC_SEED_ONLY = {
    "id": "fake_seeded",
    "capabilities": _cap("voice_clone", "seed", "reference_cache"),
}


class FakeAdapter:
    """A provider-agnostic stand-in that walks the same loop a real adapter walks.

    It is *deliberately* unable to pass a test it has not implemented: the
    continuation token is only threaded forward when the contract says the
    provider declared ``continuation``, and the synthesis record carries what the
    adapter was actually handed, so a silently-dropped seed is visible.
    """

    def __init__(self, spec, reference=None, selection=None, emission=""):
        self.spec = spec
        self.reference = reference
        self.selection = selection or {}
        self.emission = emission
        self.contract = pin_continuity(
            spec=spec,
            response_id="",
            voice_id=str(spec.get("id")),
            reference=reference,
            selection=self.selection,
            emission=emission,
        )
        self.calls: list[dict] = []

    def synthesize(self, response_id, segment_texts, start_index=0):
        """Re-pin for one response (the pipeline would do this once per response)."""
        contract = pin_continuity(
            spec=self.spec,
            response_id=response_id,
            voice_id=self.contract.voice_identity.voice_id,
            model_id=self.contract.voice_identity.model_id,
            reference=self.reference,
            selection=self.selection,
            emission=self.emission,
            continuation_context=self.contract.continuation_context,
        )
        envelopes = contract.segments(segment_texts, start_index=start_index)
        carried = contract.continuation_context
        for index, envelope in enumerate(envelopes):
            self.calls.append({
                "key": envelope.key(),
                "spoken": envelope.render(segment_texts[index]),
                "seed": envelope.contract.seed,
                "identity": envelope.contract.voice_identity.stable_key(),
                "emission": envelope.contract.emission,
                "carried_context": carried.token if carried else None,
            })
            # Only a provider that declared continuation may carry state forward.
            carried = envelope.context_for_next(f"kv-state@{envelope.segment_index}")
        return envelopes, contract


class FakeReceiver:
    """Stand-in for ``BaseAvatar.put_audio_file`` — records what reached the renderer."""

    def __init__(self):
        self.enqueued: list[tuple[str, int | None]] = []

    def __call__(self, response_id, segment_index):
        self.enqueued.append((str(response_id), segment_index))

    def frames_for(self, response_id):
        return [row for row in self.enqueued if row[0] == response_id]


# ── E: cross-segment voice continuity ───────────────────────────────


def segments_of_one_response_share_one_identity():
    """The §E core: N segments of one reply must be conditioned on one voice."""
    adapter = FakeAdapter(SPEC_RICH, reference="ref://xiaoyan.wav",
                          selection={"pitch": "low"}, emission="[whisper] ")
    texts = ["今天回来得挺早。", "我给你留了汤。", "先洗手。"]
    envelopes, contract = adapter.synthesize("resp_1", texts)

    report = audit_run(contract, envelopes)
    assert report["segments"] == 3, report
    assert report["identity_stable"], f"voice identity changed across segments: {report['identities']}"
    assert report["emission_stable"], f"delivery changed across segments: {report['emissions']}"
    assert report["seed_stable"] and report["seeds"] == {contract.seed}, report
    assert report["duplicate_keys"] == 0 and report["unique_keys"] == 3, report
    assert report["response_ids"] == {"resp_1"}, report
    assert report["degraded"] == (), f"a fully-capable provider reported degradation: {report}"
    assert all(row["spoken"].startswith("[whisper] ") for row in adapter.calls), adapter.calls


def seed_is_pinned_per_response_not_per_segment():
    """A per-seed-recomputed-per-segment provider is the §E bug, not a feature."""
    adapter = FakeAdapter(SPEC_SEED_ONLY, reference="ref://xiaoyan.wav")
    first, contract = adapter.synthesize("resp_A", ["一", "二", "三"])
    seeds_a = {row["seed"] for row in adapter.calls}
    assert len(seeds_a) == 1 and None not in seeds_a, f"seed varied inside a response: {seeds_a}"

    adapter.calls.clear()
    second, _ = adapter.synthesize("resp_B", ["一", "二"])
    seeds_b = {row["seed"] for row in adapter.calls}
    assert len(seeds_b) == 1, seeds_b
    assert seeds_b != seeds_a, "two responses drew the same seed: the key is not response-scoped"
    assert first[0].key() == ("resp_A", 0) and second[0].key() == ("resp_B", 0)


def seed_is_reproducible_from_the_contract_alone():
    """Retry of the same response must land on the same voice, by construction."""
    a = pin_continuity(spec=SPEC_SEED_ONLY, response_id="resp_C", voice_id="v", reference="ref://x.wav")
    b = pin_continuity(spec=SPEC_SEED_ONLY, response_id="resp_C", voice_id="v", reference="ref://x.wav")
    assert a.seed == b.seed == response_seed("resp_C", a.voice_identity)


def provider_without_continuation_runs_and_says_so():
    """§E "各 Adapter 自己决定能实现多少": no crash, no fake continuity."""
    adapter = FakeAdapter(SPEC_BARE, reference=None, selection={"emotion": "warm"}, emission="")
    envelopes, contract = adapter.synthesize("resp_bare", ["第一句。", "第二句。"])

    assert len(envelopes) == 2, envelopes
    assert contract.seed is None, "a provider that never declared `seed` was handed one anyway"
    assert "NOT_DECLARED:seed" in contract.unavailable, contract.unavailable
    assert "continuation_context" in contract.degraded(), contract.describe()
    assert "seed" in contract.degraded(), contract.describe()
    assert "reference_id" in contract.degraded(), contract.describe()
    assert [row["carried_context"] for row in adapter.calls] == [None, None], (
        "state was grafted onto a provider that did not declare continuation"
    )
    # Still usable: unique idempotency keys and the requested delivery text.
    assert [row.key() for row in envelopes] == [("resp_bare", 0), ("resp_bare", 1)]
    assert adapter.calls[1]["spoken"] == "第二句。"


def continuation_state_is_only_carried_when_declared():
    rich = FakeAdapter(SPEC_RICH, reference="ref://x.wav")
    rich.synthesize("resp_full", ["一", "二", "三"])
    assert [row["carried_context"] for row in rich.calls] == [None, "kv-state@0", "kv-state@1"], rich.calls


def reference_id_is_stable_across_segments_and_differs_per_reference():
    adapter = FakeAdapter(SPEC_RICH, reference=str(RUNTIME / "core" / "providers" / "manifests" / "tts.json"))
    envelopes, contract = adapter.synthesize("resp_ref", ["一", "二"])
    assert contract.reference_id and len({row.contract.reference_id for row in envelopes}) == 1
    other = pin_continuity(spec=SPEC_RICH, response_id="resp_ref", reference=str(RUNTIME / "core" / "providers" / "manifests" / "llm.json"))
    assert other.reference_id != contract.reference_id, "different reference material collapsed to one id"


def controls_snapshot_survives_a_mid_response_state_change():
    """The frozen emission is what stops a state-file rewrite landing mid-sentence."""
    contract = pin_continuity(spec=SPEC_RICH, response_id="resp_snap", reference="ref://x.wav",
                              selection={"pitch": "low"}, emission="[soft] ")
    envelopes = contract.segments(["一", "二"])
    # Simulate the operator flipping the picker between segment 1 and 2: the
    # adapter must not re-resolve, so both render() calls use the snapshot.
    assert {row.render("x") for row in envelopes} == {"[soft] x"}
    assert envelopes[1].contract.voice_controls == {"pitch": "low"}


def continuation_indices_keep_counting_across_batches():
    """A second batch for the same response must not restart at index 0 (would
    collide with the :8010 key for genuinely new audio)."""
    contract = pin_continuity(spec=SPEC_RICH, response_id="resp_tail", reference="ref://x.wav")
    first = contract.segments(["一", "二"])
    second = contract.segments(["三"], start_index=len(first))
    assert [row.segment_index for row in second] == [2]
    assert second[0].is_last() and not first[0].is_last()
    keys = [row.key() for row in (*first, *second)]
    assert len(set(keys)) == len(keys), keys


# ── H: 8010 enqueue idempotency ─────────────────────────────────────


def test_repeated_pair_enqueues_exactly_once():
    gate = SegmentGate(mode=ENFORCE)
    receiver = FakeReceiver()
    decisions = []
    for attempt in range(3):
        verdict, ran = gate.admit_and_apply("7", "resp_1", 0, lambda: receiver("resp_1", 0))
        decisions.append(verdict.decision)
    assert decisions == [ACCEPT, DUPLICATE, DUPLICATE], decisions
    assert receiver.enqueued == [("resp_1", 0)], f"duplicate reached the renderer: {receiver.enqueued}"


def test_distinct_segments_all_enqueue():
    gate = SegmentGate(mode=ENFORCE)
    receiver = FakeReceiver()
    for index in range(4):
        verdict, _ = gate.admit_and_apply("7", "resp_1", index, lambda i=index: receiver("resp_1", i))
        assert verdict.decision == ACCEPT, (index, verdict.to_dict())
    assert receiver.enqueued == [("resp_1", i) for i in range(4)]


def test_two_responses_do_not_interleave():
    """A0 B0 A1 B1: once B has rendered, A's late segments must not wedge into it."""
    gate = SegmentGate(mode=ENFORCE)
    receiver = FakeReceiver()
    outcomes = []
    for rid, index in (("resp_A", 0), ("resp_B", 0), ("resp_A", 1), ("resp_B", 1)):
        verdict, ran = gate.admit_and_apply("7", rid, index, lambda: receiver(rid, index))
        outcomes.append(verdict.decision)
    assert outcomes == [ACCEPT, ACCEPT, STALE_RESPONSE, ACCEPT], outcomes
    assert receiver.enqueued == [("resp_A", 0), ("resp_B", 0), ("resp_B", 1)], receiver.enqueued


def test_late_segment_of_previous_response_is_dropped():
    gate = SegmentGate(mode=ENFORCE)
    receiver = FakeReceiver()
    gate.admit_and_apply("7", "resp_old", 0, lambda: receiver("resp_old", 0))
    gate.admit_and_apply("7", "resp_new", 0, lambda: receiver("resp_new", 0))
    stale = gate.admit("7", "resp_old", 5)
    assert stale.decision == STALE_RESPONSE, stale.to_dict()
    verdict, ran = gate.admit_and_apply("7", "resp_old", 5, lambda: receiver("resp_old", 5))
    assert not ran and not receiver.frames_for("resp_old")[1:], receiver.enqueued


def test_interrupt_blocks_the_interrupted_response():
    """The 8765 side can still be streaming when the user barges in."""
    gate = SegmentGate(mode=ENFORCE)
    receiver = FakeReceiver()
    gate.admit_and_apply("7", "resp_A", 0, lambda: receiver("resp_A", 0))
    gate.mark_interrupted("7")
    verdict = gate.admit("7", "resp_A", 1)
    assert verdict.decision == STALE_RESPONSE, verdict.to_dict()
    assert gate.admit("7", "resp_B", 0).decision == ACCEPT


def test_a_named_cancel_blocks_a_response_that_never_enqueued():
    """§7 H 第 4 条的真实漏洞：被打断的那一通**一个字都还没入队**。

    :meth:`SegmentGate.mark_interrupted` can only move a watermark over response
    ids it has *seen*.  A reply interrupted before ``response.done`` has no
    sequence number yet, so its first (and only) upload would be handed a fresh
    seq *above* the watermark and accepted — i.e. 旧 response 插进新 response.
    Naming the id on the cancel is what closes it, and the negative control below
    shows the same call without an id still letting it through.
    """
    gate = SegmentGate(mode=ENFORCE)
    receiver = FakeReceiver()
    decision = gate.cancel_turn("7", response_id="resp_A", reason="user_interrupt")
    assert decision["cancelled_response_id"] == "resp_A", decision
    assert decision["will_drop_late_segments"] is True, decision
    verdict, ran = gate.admit_and_apply("7", "resp_A", 0, lambda: receiver("resp_A", 0))
    assert verdict.decision == STALE_RESPONSE and verdict.reason == "RESPONSE_CANCELLED", verdict.to_dict()
    assert not ran and receiver.enqueued == [], receiver.enqueued
    # The next turn is untouched — the blocklist is per response, not per session.
    assert gate.admit("7", "resp_B", 0).decision == ACCEPT
    assert gate.is_cancelled("7", "resp_A") and not gate.is_cancelled("7", "resp_B")
    assert gate.snapshot("7")["cancelled"] == ["resp_A"], gate.snapshot("7")


def test_negative_control_an_unnamed_cancel_cannot_block_an_unseen_response():
    """The check above would pass for the wrong reason if this one were true.

    A cancel that names nothing raises the watermark only over already-enqueued
    responses, so a reply interrupted *before* its first upload still gets in.
    This is the hole ``cancel_turn(response_id=...)`` exists to close: if anyone
    'simplifies' the routes back to ``mark_interrupted(session)``, the named test
    above starts failing and this one stops being the control.
    """
    gate = SegmentGate(mode=ENFORCE)
    gate.admit("7", "resp_prev", 0)
    watermark = gate.mark_interrupted("7")            # barged in, but nobody said which id
    verdict = gate.admit("7", "resp_A", 0)            # the interrupted reply's first segment
    assert verdict.decision == ACCEPT, (
        f"the unnamed cancel already blocks an unseen response ({verdict.to_dict()}) — "
        "the named-cancel test above is no longer proving anything"
    )
    assert verdict.segment_index == 0 and watermark >= 0
    assert not gate.is_cancelled("7", "resp_A")


def test_admit_turn_is_the_request_shaped_door_for_both_ends():
    """``/humanaudio`` (multipart) and ``/human`` (query) both hand over a mapping."""
    form = {"response_id": "resp_A", "segment_index": "0"}
    gate = SegmentGate(mode=ENFORCE)
    first, allowed = gate.admit_turn("7", form)
    second, allowed_again = gate.admit_turn("7", form)
    assert (first.decision, allowed) == (ACCEPT, True), first.to_dict()
    assert (second.decision, allowed_again) == (DUPLICATE, False), second.to_dict()
    # ``segment_count`` is advisory: it must not become a second key.
    assert gate.admit_turn("7", {**form, "segment_count": "1"})[0].decision == DUPLICATE
    # A legacy caller with neither field is still never blocked.
    verdict, ok = gate.admit_turn("7", {})
    assert (verdict.decision, ok) == (UNTAGGED, True), (verdict.to_dict(), ok)


def test_observe_control_admit_turn_never_blocks_even_a_literal_replay():
    """Same key, same gate logic, ``observe``: the flip is the only enforcer."""
    gate = SegmentGate(mode=OBSERVE)
    form = {"response_id": "resp_A", "segment_index": "0"}
    seen = [gate.admit_turn("7", form) for _ in range(3)]
    assert [v.decision for v, _ in seen] == [ACCEPT, DUPLICATE, DUPLICATE]
    assert all(allowed for _, allowed in seen), seen


def test_cancel_reason_vocabulary_and_blocklist_bounds():
    gate = SegmentGate(mode=ENFORCE, max_tracked=8)
    assert gate.cancel_turn("7", response_id="resp_x", reason="barge_in")["reason"] == "barge_in"
    assert gate.cancel_turn("7", response_id="resp_y", reason="tsundere")["reason"] == "unspecified"
    assert gate.cancel_turn("7", reason="")["cancelled_response_id"] == ""
    for i in range(40):
        gate.cancel_turn("7", response_id=f"resp_{i}")
    assert len(gate.snapshot("7")["cancelled"]) <= 8, gate.snapshot("7")
    assert gate.snapshot("7")["cancels"] == 43, gate.snapshot("7")
    # Teardown really drops the retired ids, or a long-lived session would block
    # a reused response id forever.
    gate.reset("7")
    assert gate.snapshot("7")["cancelled"] == []
    assert gate.admit("7", "resp_39", 0).decision == ACCEPT


def test_out_of_order_segment_below_high_water_is_flagged():
    gate = SegmentGate(mode=ENFORCE)
    assert gate.admit("7", "resp_A", 2).decision == ACCEPT
    assert gate.admit("7", "resp_A", 2).decision == DUPLICATE      # exact replay of a live key
    assert gate.admit("7", "resp_A", 1).decision == OUT_OF_ORDER   # never seen, but already passed
    assert gate.admit("7", "resp_A", 0).decision == OUT_OF_ORDER


def test_sessions_do_not_leak_into_each_other():
    gate = SegmentGate(mode=ENFORCE)
    assert gate.admit("7", "resp_A", 0).decision == ACCEPT
    assert gate.admit("8", "resp_A", 0).decision == ACCEPT, "key was judged across sessions"
    assert gate.admit("7", "resp_A", 0).decision == DUPLICATE


def test_untagged_audio_is_never_dropped():
    """A caller that adopted neither field must keep working verbatim."""
    gate = SegmentGate(mode=ENFORCE)
    receiver = FakeReceiver()
    for _ in range(2):
        verdict, ran = gate.admit_and_apply("7", "", None, lambda: receiver("", None))
        assert verdict.decision == UNTAGGED and ran, verdict.to_dict()
    assert len(receiver.enqueued) == 2


def test_observe_mode_never_changes_what_reaches_the_renderer():
    gate = SegmentGate(mode=OBSERVE)
    receiver = FakeReceiver()
    for _ in range(3):
        gate.admit_and_apply("7", "resp_A", 0, lambda: receiver("resp_A", 0))
    assert len(receiver.enqueued) == 3, "observe mode dropped audio (product behaviour change)"


def test_unindexed_response_is_accepted_once_and_retry_is_caught():
    """Documented semantics of response_id without segment_index: one upload per response.

    This is today's browser (:8010 ``humanaudio`` sees ``response_id`` only).  A
    retry is therefore caught, while a client that ever needs two uploads per
    response *must* send ``segment_index`` — otherwise this test is the guard that
    makes that requirement impossible to overlook.
    """
    gate = SegmentGate(mode=ENFORCE)
    assert gate.admit("7", "resp_A", None).decision == ACCEPT
    assert gate.admit("7", "resp_A", "0").decision == DUPLICATE
    assert gate.admit("7", "resp_A", "1").decision == ACCEPT
    assert gate.admit("7", "resp_A", "garbage").decision == DUPLICATE  # garbage == no key == slot 0


def test_gate_state_is_bounded():
    gate = SegmentGate(mode=ENFORCE, max_tracked=8)
    for i in range(40):
        assert gate.admit("7", f"resp_{i}", 0).decision == ACCEPT, i
    assert len(gate.snapshot("7")["responses"]) <= 8, gate.snapshot("7")


# ── E×H: one identity, two ends ────────────────────────────────────


def test_contract_keys_satisfy_the_gate_and_a_retry_does_not_double_enqueue():
    """The envelopes §E hands the adapter are the same pairs §H dedupes at 8010."""
    adapter = FakeAdapter(SPEC_RICH, reference="ref://x.wav", emission="")
    envelopes, contract = adapter.synthesize("resp_e2e", ["一", "二", "三"])
    gate = SegmentGate(mode=ENFORCE)
    receiver = FakeReceiver()
    for envelope in envelopes:
        rid, index = envelope.key()
        verdict, ran = gate.admit_and_apply("7", rid, index, lambda e=envelope: receiver(*e.key()))
        assert verdict.decision == ACCEPT and ran, verdict.to_dict()
    # The same response replayed end-to-end (client retry after a lost ACK).
    replayed = [gate.admit("7", *(row.key())).decision for row in envelopes]
    assert replayed == [DUPLICATE] * len(envelopes), replayed
    assert len(receiver.enqueued) == 3, receiver.enqueued


# ── vocabulary / registry cross-checks (no provider special-casing) ─


def test_contract_vocabulary_is_the_registries_own():
    from providers.capabilities import TTS_DELIVERY_CAPABILITIES

    unknown = [name for name in CONTINUITY_CAPABILITIES if name not in TTS_DELIVERY_CAPABILITIES]
    assert not unknown, f"contract invented capability bits: {unknown}"
    assert set(CONTINUITY_FIELDS) == {
        "response_id", "voice_identity", "voice_controls", "seed", "reference_id", "continuation_context"
    }, "contract fields drifted from the 任务 §7 E wording"


def test_manifest_capability_bits_drive_the_contract():
    """Read the real manifests: what each provider promised is what it gets."""
    from providers import registry

    rows = {row.get("id"): row for row in registry.providers("TTS")}
    expectations = {
        "qwen3": (False, False),   # declares neither seed nor continuation
        "higgs": (True, False),    # declares a seed, no continuation
        "test_fake": (False, True),  # declares continuation, no seed
    }
    for provider_id, (wants_seed, wants_continuation) in expectations.items():
        spec = rows.get(provider_id)
        assert spec is not None, f"{provider_id} vanished from manifests/tts.json"
        contract = pin_continuity(spec=spec, response_id="resp_x", reference="ref://x.wav")
        assert contract.supports("seed") is wants_seed, (provider_id, contract.capabilities)
        assert contract.supports("continuation") is wants_continuation, (provider_id, contract.capabilities)
        assert (contract.seed is not None) is wants_seed, contract.describe()
        if not wants_seed:
            assert "seed" in contract.degraded(), contract.describe()


def test_avatar_manifest_capability_claim_is_backed_by_the_boundary():
    """``avatar.json`` declared ``segment_dedupe``/``per_turn_attribution`` as the
    gap §7 H closes.  The gate has landed, so the tripwire now runs **both ways**:
    a ``supported: true`` is only allowed while the two files that have to
    cooperate for it do cooperate, and a quiet revert of the manifest to
    ``false`` is reported as the regression it would be.
    """
    from providers import registry

    spec = registry.get("AVATAR", "livetalking_webrtc")
    assert spec is not None, "livetalking_webrtc vanished from manifests/avatar.json"
    caps = spec.get("capabilities") or {}
    dedupe = (caps.get("segment_dedupe") or {}).get("supported")
    attribution = (caps.get("per_turn_attribution") or {}).get("supported")
    assert dedupe is True, (
        "the gate is still in server/idempotency.py but avatar.json calls "
        "segment_dedupe unsupported again — flip it back only by removing the gate"
    )
    assert str((caps.get("segment_dedupe") or {}).get("evidence", "")).strip(), (
        "segment_dedupe claimed supported with no evidence"
    )
    routes_src = (RUNTIME / "avatar" / "app" / "server" / "routes.py").read_text(
        encoding="utf-8"
    )
    assert "admit_turn" in routes_src, "routes.py no longer asks the gate on the enqueue doors"
    sync_src = (RUNTIME / "ui" / "app" / "avatar-sync.js").read_text(encoding="utf-8")
    assert "segment_index" in sync_src, (
        "the client stopped sending segment_index, so the key would collapse to "
        "whole-response only and a duplicated segment would be unrecognisable"
    )
    assert attribution is True, (
        "/humanaudio reads response_id (routes.py admit_turn + cancel_turn), so a "
        "false here is stale — per-turn attribution is exactly what the gate keys on"
    )


def test_no_vendor_name_appears_in_the_contract_layer():
    """§E: 不要围绕任何一个 TTS 做系统级设计 — enforced structurally.

    Docstrings and comments are stripped first (naming a provider while explaining
    a rule is documentation, not coupling); what is searched is the *executable*
    source, re-rendered from the AST, so a real ``if provider_id == "higgs"`` could
    not hide in a string-annotation or a decorator either.
    """
    import ast

    vendors = ["qwen", "higgs", "fish", "kokoro", "chattts", "pocket", "facebookmms", "minimax", "edgetts"]

    def executable_source(path: Path) -> str:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body.pop(0)
        return ast.unparse(tree)

    for path in (
        RUNTIME / "audit" / "system_v2" / "tools" / "continuity_contract.py",
        RUNTIME / "avatar" / "app" / "server" / "idempotency.py",
        RUNTIME / "avatar" / "app" / "server" / "routes.py",
    ):
        source = executable_source(path).lower()
        hits = [name for name in vendors if name in source]
        assert not hits, f"vendor special-casing appeared in {path.name}: {hits}"


TESTS = [
    segments_of_one_response_share_one_identity,
    seed_is_pinned_per_response_not_per_segment,
    seed_is_reproducible_from_the_contract_alone,
    provider_without_continuation_runs_and_says_so,
    continuation_state_is_only_carried_when_declared,
    reference_id_is_stable_across_segments_and_differs_per_reference,
    controls_snapshot_survives_a_mid_response_state_change,
    continuation_indices_keep_counting_across_batches,
    test_repeated_pair_enqueues_exactly_once,
    test_distinct_segments_all_enqueue,
    test_two_responses_do_not_interleave,
    test_late_segment_of_previous_response_is_dropped,
    test_interrupt_blocks_the_interrupted_response,
    test_a_named_cancel_blocks_a_response_that_never_enqueued,
    test_negative_control_an_unnamed_cancel_cannot_block_an_unseen_response,
    test_admit_turn_is_the_request_shaped_door_for_both_ends,
    test_observe_control_admit_turn_never_blocks_even_a_literal_replay,
    test_cancel_reason_vocabulary_and_blocklist_bounds,
    test_out_of_order_segment_below_high_water_is_flagged,
    test_sessions_do_not_leak_into_each_other,
    test_untagged_audio_is_never_dropped,
    test_observe_mode_never_changes_what_reaches_the_renderer,
    test_unindexed_response_is_accepted_once_and_retry_is_caught,
    test_gate_state_is_bounded,
    test_contract_keys_satisfy_the_gate_and_a_retry_does_not_double_enqueue,
    test_contract_vocabulary_is_the_registries_own,
    test_manifest_capability_bits_drive_the_contract,
    test_avatar_manifest_capability_claim_is_backed_by_the_boundary,
    test_no_vendor_name_appears_in_the_contract_layer,
]

if __name__ == "__main__":
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {test.__name__}")
    if failures:
        print(f"continuity + idempotency checks: FAIL ({failures} of {len(TESTS)})")
        raise SystemExit(1)
    print(f"continuity + idempotency checks: PASS ({len(TESTS)})")
