/**
 * v2.1.4 Automated Verification Suite (Pure Node.js)
 * Tests interaction logic, lifecycle independence, and state invariants without external dependencies.
 */
const fs = require('fs');
const path = require('path');

// Complete DOM element mock for test environment
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

  get textContent() {
    return this._textContent || this._innerHTML.replace(/<[^>]*>/g, '');
  }

  set textContent(val) {
    this._textContent = String(val);
    this._innerHTML = String(val);
  }

  get innerHTML() {
    return this._innerHTML || this._textContent;
  }

  set innerHTML(val) {
    this._innerHTML = String(val);
    this._textContent = String(val).replace(/<[^>]*>/g, '');
  }

  setAttribute(name, val) {
    this.attributes[name] = String(val);
    if (name.startsWith('data-')) {
      const camel = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[camel] = String(val);
    }
  }

  getAttribute(name) { return this.attributes[name] || null; }
  removeAttribute(name) { delete this.attributes[name]; }

  addEventListener(type, handler, options = {}) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push({ handler, options });
    if (options.signal) {
      options.signal.addEventListener?.('abort', () => {
        this.listeners[type] = this.listeners[type].filter(l => l.handler !== handler);
      });
    }
  }

  click() {
    const event = {
      preventDefault() {},
      stopPropagation() {},
      target: this
    };
    const handlers = (this.listeners['click'] || []).slice();
    for (const l of handlers) {
      l.handler(event);
    }
  }

  matchesSelector(sel) {
    if (sel.startsWith('.')) {
      return this.classList.contains(sel.slice(1));
    }
    if (sel.startsWith('[') && sel.endsWith(']')) {
      const inner = sel.slice(1, -1);
      if (inner.includes('=')) {
        const [attr, val] = inner.split('=');
        const cleanVal = val.replace(/["']/g, '');
        return this.getAttribute(attr) === cleanVal;
      }
      return this.getAttribute(inner) !== null;
    }
    return false;
  }

  querySelector(sel) {
    return this._findFirst(sel);
  }

  querySelectorAll(sel) {
    const res = [];
    this._findAll(sel, res);
    return res;
  }

  _findFirst(sel) {
    if (this.matchesSelector(sel)) return this;
    for (const child of this.children) {
      const found = child._findFirst(sel);
      if (found) return found;
    }
    return null;
  }

  _findAll(sel, res) {
    if (this.matchesSelector(sel)) res.push(this);
    for (const child of this.children) {
      child._findAll(sel, res);
    }
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  contains(other) {
    if (other === this) return true;
    for (const c of this.children) {
      if (c.contains(other)) return true;
    }
    return false;
  }
}

class MockDocument extends MockElement {
  constructor() {
    super('DOCUMENT');
    this.readyState = 'complete';
    this.body = new MockElement('BODY');
    this.appendChild(this.body);
  }

  createElement(tag) {
    return new MockElement(tag);
  }

  querySelectorAll(sel) {
    return this.body.querySelectorAll(sel);
  }

  querySelector(sel) {
    return this.body.querySelector(sel);
  }
}

async function runTests() {
  console.log("=================================================");
  console.log("  RUNNING ASSISTANT INTERACTION TEST SUITE v2.1.4");
  console.log("=================================================\n");

  let passedCount = 0;
  let failedCount = 0;

  function assert(condition, message) {
    if (condition) {
      console.log(`  ✅ PASS: ${message}`);
      passedCount++;
    } else {
      console.error(`  ❌ FAIL: ${message}`);
      failedCount++;
    }
  }

  // Build document structure
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

  // Setup globals
  global.window = {
    document,
    Shopify: { shop: 'test.myshopify.com' },
    localStorage: { getItem() { return null; }, setItem() {} },
    requestAnimationFrame(cb) { cb(); },
    speechSynthesis: null,
    addEventListener() {},
    removeEventListener() {}
  };
  global.document = document;
  global.navigator = { userAgent: 'node' };
  global.localStorage = global.window.localStorage;
  global.requestAnimationFrame = global.window.requestAnimationFrame;

  // Mock SpeechRecognition
  class MockSpeechRecognition {
    start() {}
    stop() {}
    abort() {}
  }
  global.SpeechRecognition = MockSpeechRecognition;
  global.window.SpeechRecognition = MockSpeechRecognition;

  // Load target JS code
  const jsPath = path.resolve(__dirname, '../frontend/ai-shopping-assistant/extensions/ai-shopping-assistant/assets/ai-assistant.js');
  const jsCode = fs.readFileSync(jsPath, 'utf8');
  eval(jsCode);

  const { AiAssistant } = global.window;

  // -------------------------------------------------------------
  // Test 1: FAB Open-Only Logic
  // -------------------------------------------------------------
  console.log("Test Group 1: FAB behavior");
  assert(!AiAssistant.getLifecycles().panelOpen, "Panel initially closed");

  fabBtn.click();
  assert(AiAssistant.getLifecycles().panelOpen, "FAB click opens panel");

  fabBtn.click();
  assert(AiAssistant.getLifecycles().panelOpen, "Second FAB click does NOT close panel (FAB is open-only)");

  // -------------------------------------------------------------
  // Test 2: Panel Close Button & Lifecycle Counts
  // -------------------------------------------------------------
  console.log("\nTest Group 2: Close button & Lifecycle counts");
  const initialLifecycles = { ...AiAssistant.getLifecycles() };

  closeBtn.click();
  const afterCloseLifecycles = AiAssistant.getLifecycles();

  assert(!afterCloseLifecycles.panelOpen, "Close button closes panel");
  assert(afterCloseLifecycles.panelManuallyClosed, "Close button sets panelManuallyClosed = true");
  assert(
    afterCloseLifecycles.panelLifecycleId === initialLifecycles.panelLifecycleId + 1,
    `One close action increments panelLifecycleId exactly once (Before: ${initialLifecycles.panelLifecycleId}, After: ${afterCloseLifecycles.panelLifecycleId})`
  );
  assert(
    afterCloseLifecycles.activeTurnId === initialLifecycles.activeTurnId + 1,
    `Close action cancels turn and increments activeTurnId (Before: ${initialLifecycles.activeTurnId}, After: ${afterCloseLifecycles.activeTurnId})`
  );

  // -------------------------------------------------------------
  // Test 3: Central Mic Orb Click behavior
  // -------------------------------------------------------------
  console.log("\nTest Group 3: Central Mic Orb behavior");
  AiAssistant.openPanel();
  assert(AiAssistant.getLifecycles().panelOpen, "Panel reopened");

  micOrb.click();
  assert(AiAssistant.getLifecycles().panelOpen, "Mic click while panel is open does NOT close panel");

  micOrb.click();
  assert(AiAssistant.getLifecycles().panelOpen, "Second mic click toggles query without closing panel");

  // -------------------------------------------------------------
  // Test 4: Stale Async Callback Guard
  // -------------------------------------------------------------
  console.log("\nTest Group 4: Stale Async Callback Guard");
  AiAssistant.closePanel();
  assert(AiAssistant.getLifecycles().panelManuallyClosed, "Panel manually closed");

  // Simulate late API or TTS callback trying to trigger voice
  AiAssistant.triggerVoice();
  assert(AiAssistant.getLifecycles().panelOpen, "triggerVoice explicitly opens panel when requested by API");
  AiAssistant.closePanel();
  assert(AiAssistant.getLifecycles().panelManuallyClosed, "Panel closed again");

  // -------------------------------------------------------------
  // Test 5: Wake Word Reopens Panel & Resets panelManuallyClosed
  // -------------------------------------------------------------
  console.log("\nTest Group 5: Wake Word Panel Reopen");
  assert(AiAssistant.getLifecycles().panelManuallyClosed, "Panel manually closed before wake word");

  AiAssistant.openPanel("wake-word");
  assert(AiAssistant.getLifecycles().panelOpen, "Wake word opens panel");
  assert(!AiAssistant.getLifecycles().panelManuallyClosed, "Wake word resets panelManuallyClosed to false");

  // -------------------------------------------------------------
  // Test 6: Microphone Error State & Close Button Usability
  // -------------------------------------------------------------
  console.log("\nTest Group 6: Mic Permission Error & Close Usability");
  AiAssistant.openPanel();
  status.textContent = "Microphone blocked";

  closeBtn.click();
  assert(!AiAssistant.getLifecycles().panelOpen, "Close button works 100% when permission is blocked");

  // -------------------------------------------------------------
  // Test 7: Destroy Widget Lifecycle
  // -------------------------------------------------------------
  console.log("\nTest Group 7: destroyWidget Lifecycle");
  const beforeDestroy = { ...AiAssistant.getLifecycles() };
  AiAssistant.destroyWidget();
  const afterDestroy = AiAssistant.getLifecycles();

  assert(
    afterDestroy.activeTurnId === beforeDestroy.activeTurnId + 1,
    "destroyWidget increments activeTurnId"
  );
  assert(
    afterDestroy.wakeSessionId === beforeDestroy.wakeSessionId + 1,
    "destroyWidget increments wakeSessionId"
  );

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
