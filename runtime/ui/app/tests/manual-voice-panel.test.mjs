import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { mountGirlfriend, manualSelection } from '../ui/girlfriend.js';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.value = '';
    this.style = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(kind, fn) { this.listeners[kind] = fn; }
  setAttribute() {}
}

test('manual panel is backend-scoped, persistent, and resettable', async () => {
  const ids = ['gf-performance-fields', 'gf-performance-model', 'gf-performance-note',
    'gf-performance-reset', 'gf-voice-model', 'gf-voice-status'];
  const els = Object.fromEntries(ids.map(id => [id, new Element()]));
  globalThis.document = {
    getElementById: id => els[id] || null,
    createElement: tag => new Element(tag),
  };
  const stored = new Map();
  globalThis.localStorage = {
    getItem: key => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, value),
    removeItem: key => stored.delete(key),
  };
  let active = 'qwen3';
  // Current API provides declarative capabilities; the old empty response
  // fixture exercised the pre-registry UI and no longer describes production.
  const manifest = JSON.parse(fs.readFileSync(new URL('../../../core/providers/manifests/tts.json', import.meta.url), 'utf8'));
  const capabilities = manifest.providers.map(provider => ({
    id: provider.id, label_zh: provider.display_name_zh,
    controls: Object.entries(provider.capabilities).filter(([, spec]) => spec.display_label_zh && spec.supported)
      .sort((a, b) => (a[1].ui_order || 0) - (b[1].ui_order || 0))
      .map(([field, spec]) => ({ ...spec, field, values: (spec.allowed_values || []).filter(v => v.ui_visible !== false) })),
    runtime_controls: [],
  }));
  globalThis.fetch = async url => ({
    ok: true, status: 200,
    json: async () => url === '/api/voice-models'
      ? { active, ready: true, switch: {}, capabilities }
      : { models: [], active: '', health: { reachable: true }, switch: {} },
  });
  const sent = [];
  const mount = async () => {
    mountGirlfriend({ setManualVoiceMode: selection => sent.push(selection) });
    await new Promise(resolve => setImmediate(resolve));
  };
  const keys = () => els['gf-performance-fields'].children.map(field => field.children[1].dataset.voiceField);
  const control = key => els['gf-performance-fields'].children.find(field => field.children[1].dataset.voiceField === key).children[1];

  await mount();
  assert.deepEqual(keys(), ['emotion', 'custom_style']);
  stored.set('qwen3_voice_preferences', JSON.stringify({ emotion: 'gentle' }));
  assert.deepEqual(manualSelection().manual_voice_mode, {}, 'hidden legacy Qwen gentle must not be sent');
  control('custom_style').value = '简洁';
  control('custom_style').listeners.input();
  assert.equal(manualSelection().manual_voice_mode.custom_style, '简洁');
  assert.equal(stored.has('qwen3_voice_preferences'), true);

  active = 'higgs';
  await mount();
  assert.deepEqual(keys(), ['emotion', 'style', 'sound_effect', 'speed', 'pitch', 'expressiveness']);
  assert.deepEqual(manualSelection().manual_voice_mode, {});
  control('style').value = 'whispering';
  control('style').listeners.change();
  assert.equal(manualSelection().manual_voice_mode.style, 'whispering');

  active = 'fish';
  await mount();
  assert.deepEqual(keys(), ['emotion', 'custom_style']);
  assert.deepEqual(manualSelection().manual_voice_mode, {});
  control('custom_style').value = '轻声耳语';
  control('custom_style').listeners.input();
  assert.equal(manualSelection().manual_voice_mode.custom_style, '轻声耳语');

  active = 'qwen3';
  await mount();
  assert.equal(control('custom_style').value, '简洁');
  els['gf-performance-reset'].listeners.click();
  assert.deepEqual(manualSelection().manual_voice_mode, {});
  assert.equal(stored.has('qwen3_voice_preferences'), false);
  assert.equal(stored.has('higgs_voice_preferences'), true);
  assert.equal(stored.has('fish_voice_preferences'), true);
  assert.equal(sent.at(-1).tts_backend, 'qwen3');
});
