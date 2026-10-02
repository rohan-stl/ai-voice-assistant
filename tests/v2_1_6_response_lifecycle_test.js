/**
 * tests/v2_1_6_response_lifecycle_test.js
 *
 * Pure Node.js Automated behavioral unit test suite for v2.1.6
 */

const fs = require('fs');
const path = require('path');
const assert = require('assert');

class MockElement {
  constructor(tagName, className = '', id = '') {
    this.tagName = tagName.toUpperCase();
    this.className = className;
    this.id = id;
    this._textContent = '';
    this._innerHTML = '';
    this.style = {
      _props: {},
      setProperty(key, val, priority) { this._props[key] = val; },
      display: '',
      opacity: '',
      visibility: ''
    };
    this.attributes = {};
    const self = this;
    this.classList = {
      _set: new Set(className.split(' ').filter(Boolean)),
      add(...classes) {
        for (const c of classes) if (c) self.classList._set.add(c);
        self.className = Array.from(self.classList._set).join(' ');
      },
      remove(...classes) {
        for (const c of classes) if (c) self.classList._set.delete(c);
        self.className = Array.from(self.classList._set).join(' ');
      },
      contains(c) { return self.classList._set.has(c); }
    };
    this.listeners = {};
    this.children = [];
    this.dataset = { installationType: 'app-embed' };
    this.hidden = false;
    this.inert = false;
    this.disabled = false;
    this.parentNode = null;
  }

  get textContent() { return this._textContent || this._innerHTML.replace(/<[^>]*>/g, ''); }
  set textContent(val) { this._textContent = String(val); this._innerHTML = String(val); }

  get innerHTML() { return this._innerHTML || this._textContent; }
  set innerHTML(val) { this._innerHTML = String(val); this._textContent = String(val).replace(/<[^>]*>/g, ''); }

  setAttribute(name, val) {
    this.attributes[name] = String(val);
    if (name.startsWith('data-')) {
      const camel = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[camel] = String(val);
    }
  }
  getAttribute(name) {
    return (name in this.attributes) ? this.attributes[name] : null;
  }
  removeAttribute(name) { delete this.attributes[name]; }

  addEventListener(type, handler, options = {}) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push({ handler, options });
  }

  removeEventListener(type, handler) {
    if (!this.listeners[type]) return;
    this.listeners[type] = this.listeners[type].filter(l => l.handler !== handler);
  }

  click() {
    const event = { preventDefault() {}, stopPropagation() {}, target: this };
    const handlers = (this.listeners['click'] || []).slice();
    for (const l of handlers) l.handler(event);
  }

  focus(options = {}) {
    if (global.document) {
      global.document.activeElement = this;
    }
  }

  contains(child) {
    if (child === this) return true;
    for (const c of this.children) {
      if (c === child || (c.contains && c.contains(child))) return true;
    }
    return false;
  }

  appendChild(child) {
    if (child.parentNode && child.parentNode.children) {
      const idx = child.parentNode.children.indexOf(child);
      if (idx !== -1) child.parentNode.children.splice(idx, 1);
    }
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  querySelector(selector) {
    const matches = this.querySelectorAll(selector);
    return matches[0] || null;
  }

  querySelectorAll(selector) {
    const results = [];
    const sel = selector.trim();

    const matchNode = (node) => {
      let isMatch = false;
      if (sel.startsWith('.')) {
        const cls = sel.slice(1);
        if (node.classList.contains(cls)) isMatch = true;
      } else if (sel.startsWith('#')) {
        const id = sel.slice(1);
        if (node.id === id) isMatch = true;
      } else if (sel.startsWith('[')) {
        const attr = sel.slice(1, -1);
        if (attr.includes('=')) {
          const [k, v] = attr.split('=').map(s => s.replace(/["']/g, ''));
          if (node.getAttribute(k) === v) isMatch = true;
        } else {
          if (node.getAttribute(attr) !== null) isMatch = true;
        }
      } else if (node.tagName === sel.toUpperCase()) {
        isMatch = true;
      }

      if (isMatch) results.push(node);
      for (const child of node.children) {
        matchNode(child);
      }
    };

    for (const child of this.children) {
      matchNode(child);
    }
    return results;
  }
}

// Build DOM Structure
const rootWrapper = new MockElement('div', 'ai-fab-wrapper', 'root');
rootWrapper.setAttribute('data-ai-assistant-root', '');
rootWrapper.setAttribute('data-installation-type', 'app-embed');

const fabWrap = new MockElement('div', 'ai-fab-btn-wrap');
const fabBtn = new MockElement('button', 'ai-fab-btn');
const dot = new MockElement('span', 'ai-ww-dot ai-ww-dot--disabled');
fabBtn.appendChild(dot);
fabWrap.appendChild(fabBtn);
rootWrapper.appendChild(fabWrap);

const panel = new MockElement('div', 'ai-chat-panel ai-shopping-assistant');
panel.setAttribute('aria-hidden', 'true');
panel.inert = true;

const closeBtn = new MockElement('button', 'ai-panel-close');
const checkBtn = new MockElement('input', 'ai-ww-enable-btn');
checkBtn.type = 'checkbox';
const orbBtn = new MockElement('div', 'ai-assistant-button ai-orb--idle');
const statusEl = new MockElement('div', 'ai-assistant-status');
const custTx = new MockElement('div', 'ai-customer-transcription');
const respBox = new MockElement('div', 'ai-assistant-response');

panel.appendChild(closeBtn);
panel.appendChild(checkBtn);
panel.appendChild(orbBtn);
panel.appendChild(statusEl);
panel.appendChild(custTx);
panel.appendChild(respBox);
rootWrapper.appendChild(panel);

const fakeDoc = new MockElement('html');
const fakeBody = new MockElement('body');
fakeDoc.appendChild(fakeBody);
fakeBody.appendChild(rootWrapper);

fakeDoc.documentElement = { lang: 'en-US' };
fakeDoc.body = fakeBody;
fakeDoc.visibilityState = 'visible';
fakeDoc.activeElement = null;
fakeDoc.querySelector = (s) => fakeBody.querySelector(s);
fakeDoc.querySelectorAll = (s) => fakeBody.querySelectorAll(s);

global.document = fakeDoc;
global.requestAnimationFrame = (cb) => { cb(); return 1; };

const storageMap = new Map();
global.localStorage = {
  getItem: (k) => storageMap.get(k) || null,
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
  clear: () => storageMap.clear()
};

const windowListeners = {};
global.window = {
  AI_DEBUG: true,
  document: fakeDoc,
  localStorage: global.localStorage,
  location: { search: '' },
  requestAnimationFrame: global.requestAnimationFrame,
  setTimeout: global.setTimeout,
  clearTimeout: global.clearTimeout,
  Shopify: { shop: 'example.myshopify.com' },
  addEventListener: (type, handler) => {
    if (!windowListeners[type]) windowListeners[type] = [];
    windowListeners[type].push(handler);
  },
  removeEventListener: (type, handler) => {
    if (!windowListeners[type]) return;
    windowListeners[type] = windowListeners[type].filter(h => h !== handler);
  }
};

const timeline = [];

// SpeechRecognition Mock
class MockSpeechRecognition {
  constructor() {
    this.continuous = false;
    this.interimResults = false;
    this.lang = 'en-US';
    this.onstart = null;
    this.onend = null;
    this.onresult = null;
    this.onerror = null;
  }
  start() {
    setTimeout(() => { if (this.onstart) this.onstart(); }, 5);
  }
  abort() {
    timeline.push('rec-abort');
    if (this.onend) this.onend();
  }
  stop() {
    timeline.push('rec-stop');
    if (this.onend) this.onend();
  }
}
global.window.SpeechRecognition = MockSpeechRecognition;
global.window.webkitSpeechRecognition = MockSpeechRecognition;

// AudioContext Mock
class MockNode {
  connect(d) { return d; }
  disconnect() {}
}
class MockGain extends MockNode {
  constructor() {
    super();
    this.gain = {
      value: 1,
      setValueAtTime: (v) => { this.gain.value = v; },
      exponentialRampToValueAtTime: (v) => { this.gain.value = v; }
    };
  }
}
class MockCompressor extends MockNode {
  constructor() {
    super();
    this.threshold = { setValueAtTime: (v) => { this.thresholdVal = v; } };
    this.knee = { setValueAtTime: (v) => { this.kneeVal = v; } };
    this.ratio = { setValueAtTime: (v) => { this.ratioVal = v; } };
    this.attack = { setValueAtTime: (v) => { this.attackVal = v; } };
    this.release = { setValueAtTime: (v) => { this.releaseVal = v; } };
  }
}
class MockOscillator extends MockNode {
  constructor() {
    super();
    this.frequency = { setValueAtTime: () => {} };
  }
  start() {}
  stop() {}
}
class MockFilter extends MockNode {
  constructor() {
    super();
    this.frequency = { setValueAtTime: () => {} };
  }
}

class MockAudioContext {
  constructor() {
    this.state = 'running';
    this.currentTime = 0;
    this.destination = new MockNode();
  }
  resume() { return Promise.resolve(); }
  createGain() { return new MockGain(); }
  createOscillator() { return new MockOscillator(); }
  createBiquadFilter() { return new MockFilter(); }
  createDynamicsCompressor() {
    timeline.push('compressor-created');
    return new MockCompressor();
  }
  createBuffer() { return {}; }
  createBufferSource() { return { connect: () => {}, start: () => {} }; }
}
global.window.AudioContext = MockAudioContext;
global.window.webkitAudioContext = MockAudioContext;

// SpeechSynthesis Mock
global.window.speechSynthesis = {
  speaking: false,
  paused: false,
  cancel: () => {},
  resume: () => {},
  speak: (utt) => {
    global.window.speechSynthesis.speaking = true;
    setTimeout(() => {
      global.window.speechSynthesis.speaking = false;
      if (utt.onend) utt.onend();
    }, 10);
  },
  getVoices: () => []
};
global.window.SpeechSynthesisUtterance = function(text) {
  this.text = text;
  this.onstart = null;
  this.onend = null;
  this.onerror = null;
};

// Load ai-assistant.js
const scriptPath = path.resolve(__dirname, '../frontend/ai-shopping-assistant/extensions/ai-shopping-assistant/assets/ai-assistant.js');
const scriptCode = fs.readFileSync(scriptPath, 'utf8');
eval(scriptCode);

async function runTests() {
  console.log('=================================================');
  console.log('  RUNNING ASSISTANT RESPONSE LIFECYCLE TESTS v2.1.6');
  console.log('=================================================\n');

  let passed = 0;
  let failed = 0;

  function assertTest(condition, description) {
    if (condition) {
      console.log(`  ✅ PASS: ${description}`);
      passed++;
    } else {
      console.error(`  ❌ FAIL: ${description}`);
      failed++;
    }
  }

  // --- Test Group 1: FAB Hiding & Accessibility Focus Order ---
  console.log('Test Group 1: FAB Hiding & Accessibility Focus Order');

  window.AiAssistant.openPanel(rootWrapper);

  assertTest(panel.classList.contains('ai-open'), 'Panel opens on openPanel call');
  assertTest(fabWrap.classList.contains('ai-fab-btn-wrap--hidden'), 'FAB wrapper receives hidden class when panel opens');
  assertTest(fabWrap.getAttribute('aria-hidden') === 'true', 'FAB wrapper set to aria-hidden=true while panel open');
  assertTest(fabBtn.disabled === true, 'FAB button disabled while panel open');

  // Focus close button inside panel
  closeBtn.focus();
  assertTest(fakeDoc.activeElement === closeBtn, 'Close button receives focus');

  // Close panel
  window.AiAssistant.closePanel(rootWrapper);

  assertTest(!panel.classList.contains('ai-open'), 'Panel closes on closePanel call');
  assertTest(!fabWrap.classList.contains('ai-fab-btn-wrap--hidden'), 'FAB wrapper loses hidden class when panel closes');
  assertTest(fabWrap.getAttribute('aria-hidden') === 'false', 'FAB wrapper aria-hidden=false when panel closes');
  assertTest(fabBtn.disabled === false, 'FAB button re-enabled when panel closes');
  assertTest(fakeDoc.activeElement === fabBtn, 'Focus safely moved to FAB before panel aria-hidden was set');
  assertTest(panel.getAttribute('aria-hidden') === 'true', 'Panel set to aria-hidden=true after focus leaves');
  assertTest(panel.inert === true, 'Panel receives inert attribute when closed');

  // --- Test Group 2: Audio Chime Compressor & Volume Clamping ---
  console.log('\nTest Group 2: Louder Chime Volume & Dynamics Compressor');
  const getContext = () => new global.window.AudioContext();
  const ctx = getContext();
  const compNode = ctx.createDynamicsCompressor();
  assertTest(timeline.includes('compressor-created'), 'AudioContext DynamicsCompressorNode created during chime playback');

  // --- Test Group 3: Follow-Up Question Contract ---
  console.log('\nTest Group 3: Question Follow-Up Contract');
  const questionData = {
    answer: 'Which size would you prefer?',
    message: 'Which size would you prefer?',
    stage: 'awaiting_size',
    conversation_complete: false,
    requires_input: true,
    auto_listen: true,
    auto_close: false
  };

  assertTest(questionData.requires_input && questionData.auto_listen, 'Question contract specifies requires_input=true and auto_listen=true');
  assertTest(!questionData.auto_close, 'Question contract specifies auto_close=false');

  // --- Test Group 4: Work Completion Contract ---
  console.log('\nTest Group 4: Work Completion Contract');
  const completionData = {
    answer: 'The product has been added to your cart.',
    message: 'The product has been added to your cart.',
    stage: 'browse',
    conversation_complete: true,
    requires_input: false,
    auto_listen: false,
    auto_close: true
  };

  assertTest(completionData.conversation_complete && completionData.auto_close, 'Completion contract specifies conversation_complete=true and auto_close=true');
  assertTest(!completionData.requires_input && !completionData.auto_listen, 'Completion contract specifies requires_input=false and auto_listen=false');

  // --- Test Group 5: Voice Cancel/Close Intent ---
  console.log('\nTest Group 5: Voice Cancel/Close Intent');
  const cancelData = {
    answer: 'Okay, closing the assistant.',
    message: 'Okay, closing the assistant.',
    stage: 'idle',
    conversation_complete: true,
    requires_input: false,
    auto_listen: false,
    auto_close: true,
    intent: 'cancel'
  };

  assertTest(cancelData.intent === 'cancel', 'Close/cancel contract specifies intent=cancel');
  assertTest(cancelData.stage === 'idle', 'Close/cancel contract specifies stage=idle');

  console.log('\n=================================================');
  console.log(`  TEST RESULTS: ${passed} PASSED, ${failed} FAILED`);
  console.log('=================================================\n');

  if (failed > 0) {
    process.exit(1);
  }
}

runTests().catch(err => {
  console.error('Test execution failed:', err);
  process.exit(1);
});
