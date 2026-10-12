// Delivery guard only; Reading checkpoints and playback state remain authoritative.
export class ReadingInstructionGuard {
  constructor(current) { this.current = current; this.revision = 0; }
  invalidate() { this.revision += 1; }
  capture() { return { ...this.current(), revision: this.revision }; }
  unchanged(captured) {
    const now = this.capture();
    return captured.mode === now.mode && captured.revision === now.revision && captured.documentId === now.documentId
      && captured.documentRevision === now.documentRevision && captured.client === now.client
      && captured.liveSession === now.liveSession;
  }
  valid(captured) { return captured.mode === 'READING' && this.unchanged(captured); }
  check(captured) {
    if (!this.valid(captured)) throw new Error('STALE_INSTRUCTION_UPDATE');
  }
  async deliver(captured, build, apply) {
    const result = await build();
    this.check(captured);
    if (!result?.ok || result.mode !== 'READING' || !String(result.text || '').trim()) {
      throw new Error('PROMPT_BUILD_FAILED');
    }
    const applied = await apply(result.text);
    this.check(captured);
    if (!applied) throw new Error('INSTRUCTION_DELIVERY_FAILED');
  }
}
