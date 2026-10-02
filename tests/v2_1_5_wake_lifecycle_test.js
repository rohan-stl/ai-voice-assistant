/**
 * v2.1.5 Behavioral Verification Suite (Pure Node.js)
 * Simulates real DOM events, SpeechRecognition, getUserMedia, AudioContext, timers, and visibility lifecycle.
 */
const fs = require('fs');
const path = require('path');

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
    this.classList = {
      _set: new Set(className.split(' ').filter(Boolean)),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); }
    };
    this.listeners = {};
    this.children = [];
    this.dataset = { installationType: 'app-embed' };
    this.hidden = false;
    this.inert = false;
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
  getAttribute(name) { return this.attributes[name] || null; }

  addEventListener(type, handler, options = {}) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push({ handler, options });
  }

  click() {
    const event = { preventDefault() {}, stopPropagation() {}, target: this };
    const handlers = (this.listeners['click'] || []).slice();
    for (const l of handlers) l.handler(event);
  }

  change() {
    const event = { preventDefault() {}, stopPropagation() {}, target: this };
    const handlers = (this.listeners['change'] || []).slice();
    for (const l of handlers) l.handler(event);
  }

  matchesSelector(sel) {
    if (sel.startsWith('.')) return this.classList.contains(sel.slice(1));
    if (sel.startsWith('[') && sel.endsWith(']')) {
      const inner = sel.slice(1, -1);
      if (inner.includes('=')) {
        const [attr, val] = inner.split('=');
        return this.getAttribute(attr) === val.replace(/["']/g, '');
      }
      return this.getAttribute(inner) !== null;
    }
    return false;
  }

  querySelector(sel) { return this._findFirst(sel); }
  querySelectorAll(sel) { const res = []; this._findAll(sel, res); return res; }

  _findFirst(sel) {
    if (this.matchesSelector(sel)) return this;
    for (const c of this.children) { const f = c._findFirst(sel); if (f) return f; }
    return null;
  }

  _findAll(sel, res) {
    if (this.matchesSelector(sel)) res.push(this);
    for (const c of this.children) c._findAll(sel, res);
  }

  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  contains(other) {
    if (other === this) return true;
    for (const c of this.children) if (c.contains(other)) return true;
    return false;
  }
}

class MockDocument extends MockElement {
  constructor() {
    super('DOCUMENT');
    this.readyState = 'complete';
    this.documentElement = { lang: 'en-US' };
    this.visibilityState = 'visible';
    this.body = new MockElement('BODY');
    this.appendChild(this.body);
  }

  createElement(tag) { return new MockElement(tag); }
  querySelectorAll(sel) { return this.body.querySelectorAll(sel); }
  querySelector(sel) { return this.body.querySelector(sel); }
}

async function runTests() {
  console.log("=================================================");
  console.log("  RUNNING ASSISTANT WAKE LIFECYCLE TEST SUITE v2.1.5");
  console.log("=================================================\n");

  let passedCount = 0;
  let failedCount = 0;
  const eventTimeline = [];

  function logEvent(name) {
    eventTimeline.push(name);
  }

  function assert(condition, message) {
    if (condition) {
      console.log(`  ✅ PASS: ${message}`);
      passedCount++;
    } else {
      console.error(`  ❌ FAIL: ${message}`);
      failedCount++;
    }
  }

  const document = new MockDocument();
  const root = new MockElement('DIV', 'ai-fab-wrapper');
  root.setAttribute('data-ai-assistant-root', 'true');
  root.setAttribute('data-installation-type', 'app-embed');

  const panel = new MockElement('DIV', 'ai-chat-panel');
  const header = new MockElement('DIV', 'ai-panel-header');
  const closeBtn = new MockElement('BUTTON', 'ai-panel-close');
  header.appendChild(closeBtn);
  panel.appendChild(header);

  const body = new MockElement('DIV', 'ai-panel-body');
  const status = new MockElement('DIV', 'ai-assistant-status');
  const transcription = new MockElement('DIV', 'ai-customer-transcription');
  const micOrb = new MockElement('BUTTON', 'ai-assistant-button ai-orb--idle');
  const responseBox = new MockElement('DIV', 'ai-assistant-response');
  const wwSection = new MockElement('DIV', 'ai-ww-section');
  const wwCheckbox = new MockElement('INPUT', 'ai-ww-enable-btn');
  const wwDot = new MockElement('SPAN', 'ai-ww-dot ai-ww-dot--disabled');
  wwSection.appendChild(wwCheckbox);
  wwSection.appendChild(wwDot);

  body.appendChild(status);
  body.appendChild(transcription);
  body.appendChild(micOrb);
  body.appendChild(responseBox);
  body.appendChild(wwSection);
  panel.appendChild(body);

  const fabWrap = new MockElement('DIV', 'ai-fab-btn-wrap');
  const fabBtn = new MockElement('BUTTON', 'ai-fab-btn');
  fabWrap.appendChild(fabBtn);

  root.appendChild(panel);
  root.appendChild(fabWrap);
  document.body.appendChild(root);

  // Mock getUserMedia
  let trackStopped = false;
  const mockTrack = {
    stop() { trackStopped = true; logEvent('track-stopped'); }
  };
  const mockStream = {
    getTracks() {
      return [mockTrack];
    }
  };

  // Mock Web Audio Context
  let audioContextState = 'suspended';
  let chimeNodesCreated = [];
  class MockGainNode {
    constructor() {
      this.gain = {
        setValueAtTime() {},
        exponentialRampToValueAtTime() {}
      };
    }
    connect() {}
    disconnect() { logEvent('audio-gain-disconnected'); }
  }
  class MockOscillatorNode {
    constructor() {
      this.frequency = { setValueAtTime() {} };
    }
    connect() {}
    start() { logEvent('audio-osc-started'); }
    stop() { logEvent('audio-osc-stopped'); }
    disconnect() { logEvent('audio-osc-disconnected'); }
  }
  class MockBiquadFilterNode {
    constructor() {
      this.type = 'lowpass';
      this.frequency = { setValueAtTime() {} };
    }
    connect() {}
    disconnect() { logEvent('audio-filter-disconnected'); }
  }

  class MockAudioContext {
    constructor() {
      this.state = audioContextState;
      this.destination = {};
      this.currentTime = 0;
    }
    resume() {
      audioContextState = 'running';
      this.state = 'running';
      logEvent('audio-context-resumed');
      return Promise.resolve();
    }
    createGain() { const g = new MockGainNode(); chimeNodesCreated.push(g); return g; }
    createOscillator() { const o = new MockOscillatorNode(); chimeNodesCreated.push(o); return o; }
    createBiquadFilter() { const f = new MockBiquadFilterNode(); chimeNodesCreated.push(f); return f; }
    createBuffer() { return {}; }
    createBufferSource() {
      return { connect() {}, start() {} };
    }
  }

  // Mock SpeechRecognition instances
  let activeMockRecognitions = [];
  class MockSpeechRecognition {
    constructor() {
      this.continuous = true;
      this.interimResults = true;
      this.lang = 'en-US';
      this.onstart = null;
      this.onresult = null;
      this.onerror = null;
      this.onend = null;
      this.started = false;
      this.aborted = false;
      activeMockRecognitions.push(this);
    }
    start() {
      this.started = true;
      logEvent('rec-start-called');
    }
    stop() {
      this.started = false;
      logEvent('rec-stop-called');
      if (this.onend) this.onend();
    }
    abort() {
      this.started = false;
      this.aborted = true;
      logEvent('rec-abort-called');
      if (this.onend) this.onend();
    }

    // Helper method to simulate browser events
    triggerStart() {
      if (this.onstart) this.onstart();
    }
    triggerWakeResult(text) {
      if (this.onresult) {
        this.onresult({
          resultIndex: 0,
          results: [[{ transcript: text }]]
        });
      }
    }
  }

  // Setup globals
  const windowListeners = {};
  global.window = {
    document,
    location: { search: '' },
    Shopify: { shop: 'test.myshopify.com' },
    localStorage: { getItem() { return null; }, setItem() {} },
    requestAnimationFrame(cb) { cb(); },
    speechSynthesis: null,
    AudioContext: MockAudioContext,
    webkitAudioContext: MockAudioContext,
    addEventListener(type, handler, options) {
      if (!windowListeners[type]) windowListeners[type] = [];
      windowListeners[type].push({ handler, options });
    },
    removeEventListener(type, handler) {
      if (windowListeners[type]) {
        windowListeners[type] = windowListeners[type].filter(l => l.handler !== handler);
      }
    },
    triggerEvent(type, eventObj = {}) {
      const handlers = (windowListeners[type] || []).slice();
      for (const l of handlers) l.handler(eventObj);
    }
  };
  global.document = document;
  Object.defineProperty(global, 'navigator', {
    value: {
      userAgent: 'node',
      mediaDevices: {
        getUserMedia() {
          return Promise.resolve(mockStream);
        }
      }
    },
    configurable: true,
    writable: true
  });
  global.window.navigator = global.navigator;
  global.localStorage = global.window.localStorage;
  global.requestAnimationFrame = global.window.requestAnimationFrame;
  global.SpeechRecognition = MockSpeechRecognition;
  global.window.SpeechRecognition = MockSpeechRecognition;

  // Load target JS code
  const jsPath = path.resolve(__dirname, '../frontend/ai-shopping-assistant/extensions/ai-shopping-assistant/assets/ai-assistant.js');
  const jsCode = fs.readFileSync(jsPath, 'utf8');
  eval(jsCode);

  const { AiAssistant, AiWakeWord } = global.window;

  // -------------------------------------------------------------
  // Test 1: User Gesture Enable & Microphones Access Confirmation
  // -------------------------------------------------------------
  console.log("Test Group 1: User Gesture & Microphones Access Confirmation");

  assert(!AiAssistant.getLifecycles().microphoneAccessConfirmed, "Initially access unconfirmed");
  assert(!AiAssistant.getLifecycles().wakeRecognitionActive, "Wake recognition inactive before onstart");

  wwCheckbox.checked = true;
  await AiWakeWord.enable(root);
  await new Promise(r => setTimeout(r, 100));

  assert(trackStopped, "getUserMedia track stopped immediately after permission check");
  assert(AiAssistant.getLifecycles().microphoneAccessConfirmed, "microphoneAccessConfirmed set to true");
  assert(audioContextState === 'running', "AudioContext resumed during user gesture");

  // Simulate SpeechRecognition onstart firing
  const wakeRecInstance = activeMockRecognitions[activeMockRecognitions.length - 1];
  assert(wakeRecInstance && wakeRecInstance.started, "Recognition start invoked");
  assert(!AiAssistant.getLifecycles().wakeRecognitionActive, "Green dot NOT displayed before onstart");

  wakeRecInstance.triggerStart();
  assert(AiAssistant.getLifecycles().wakeRecognitionActive, "Green dot displayed ONLY AFTER onstart fires");
  assert(AiAssistant.getLifecycles().assistantState === 'wake-ready', "Assistant state updated to wake-ready after onstart");

  // -------------------------------------------------------------
  // Test 2: Full Wake Phrase Activation Sequence & Event Order
  // -------------------------------------------------------------
  console.log("\nTest Group 2: Wake Phrase Activation Sequence & Event Order");
  eventTimeline.length = 0;

  // Trigger "Hey Assistant" transcript
  wakeRecInstance.triggerWakeResult("hey assistant");

  logEvent('panel-opened-check:' + AiAssistant.getLifecycles().panelOpen);
  assert(AiAssistant.getLifecycles().panelOpen, "Wake phrase opens panel");
  assert(!AiAssistant.getLifecycles().panelManuallyClosed, "Wake phrase resets panelManuallyClosed = false");

  // Give chime (550ms) and 175ms delay time to execute
  await new Promise(r => setTimeout(r, 900));

  const queryRecInstance = activeMockRecognitions[activeMockRecognitions.length - 1];
  assert(queryRecInstance && queryRecInstance !== wakeRecInstance, "Query recognition started after chime delay");
  assert(AiAssistant.getLifecycles().queryRecognitionActive, "Query recognition active");

  // Verify event sequence
  console.log("  Timeline:", eventTimeline.join(" -> "));
  const abortIdx = eventTimeline.indexOf('rec-abort-called');
  const oscStartIdx = eventTimeline.indexOf('audio-osc-started');
  const oscStopIdx = eventTimeline.indexOf('audio-osc-stopped');

  assert(abortIdx !== -1 && oscStartIdx !== -1 && oscStartIdx > abortIdx, "Wake listener aborted before chime start");
  assert(oscStopIdx > oscStartIdx, "Chime completed before query listener sequence finished");

  // End query recognition turn to return state to wake-ready
  queryRecInstance.stop();
  await new Promise(r => setTimeout(r, 100));

  // -------------------------------------------------------------
  // Test 3: Stale Wake Activation Cooldown
  // -------------------------------------------------------------
  console.log("\nTest Group 3: Wake Activation Cooldown");
  const beforeCooldownLifecycles = { ...AiAssistant.getLifecycles() };
  wakeRecInstance.triggerWakeResult("hey assistant");
  const afterCooldownLifecycles = AiAssistant.getLifecycles();

  assert(
    afterCooldownLifecycles.activeTurnId === beforeCooldownLifecycles.activeTurnId,
    "Duplicate wake phrase during 1500ms cooldown ignored"
  );

  // -------------------------------------------------------------
  // Test 4: Tab Visibility Handlers
  // -------------------------------------------------------------
  console.log("\nTest Group 4: Tab Visibility Lifecycle");
  document.visibilityState = 'hidden';
  const visHandlers = document.listeners['visibilitychange'] || [];
  for (const h of visHandlers) h.handler();

  assert(!AiAssistant.getLifecycles().wakeRecognitionActive, "Wake recognition stopped when tab hidden");

  document.visibilityState = 'visible';
  for (const h of visHandlers) h.handler();
  await new Promise(r => setTimeout(r, 50));

  const newWakeRec = activeMockRecognitions[activeMockRecognitions.length - 1];
  if (newWakeRec) newWakeRec.triggerStart();
  assert(AiAssistant.getLifecycles().wakeRecognitionActive, "Wake recognition resumed when tab becomes visible again");

  // -------------------------------------------------------------
  // Test 5: Bounded Retry Backoff Limit
  // -------------------------------------------------------------
  console.log("\nTest Group 5: Bounded Retry Backoff Limit");
  // Force hard permission error
  const errRec = activeMockRecognitions[activeMockRecognitions.length - 1];
  if (errRec.onerror) {
    errRec.onerror({ error: 'not-allowed' });
  }

  assert(AiAssistant.getLifecycles().assistantState === 'error', "Hard permission error -> transitions to ERROR state");

  // -------------------------------------------------------------
  // Test 6: Fallback Manual Mic Usability
  // -------------------------------------------------------------
  console.log("\nTest Group 6: Fallback Manual Mic Usability");
  micOrb.click();
  assert(AiAssistant.getLifecycles().panelOpen, "Manual mic click opens panel even when wake fails");

  closeBtn.click();
  assert(!AiAssistant.getLifecycles().panelOpen, "Close button closes panel cleanly");

  console.log("\n=================================================");
  console.log(`  TEST RESULTS: ${passedCount} PASSED, ${failedCount} FAILED`);
  console.log("=================================================\n");

  if (failedCount > 0) {
    process.exit(1);
  }
}

runTests().catch(err => {
  console.error("Test execution error:", err);
  process.exit(1);
});
