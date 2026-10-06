(function() {
  "use strict";

  // ===============================================================
  // SHOPIFY AI SHOPPING ASSISTANT — v2.1.13 DOUBLE-ADD + BADGE FIX
  // ===============================================================
  const VERSION = "2.1.12";

  function logWakeEvent(action, detail) {
    if (window.AI_DEBUG || (typeof window !== "undefined" && window.location?.search?.includes("ai_debug=1"))) {
      console.log(`[Wake] ${action}`, detail !== undefined ? detail : "");
    }
  }

  function delay(ms) {
    return new Promise(resolve => setTrackedTimeout(resolve, ms));
  }

  // API URL — uses Shopify App Proxy (relative, no hardcoded URL)
  function getApiBaseUrl() {
    if (window.AI_ASSISTANT_API_BASE) {
      return window.AI_ASSISTANT_API_BASE.trim().replace(/\/$/, "");
    }
    const root = typeof getCanonicalRoot === "function" ? getCanonicalRoot() : null;
    if (root && root.dataset?.apiBase) {
      return root.dataset.apiBase.trim().replace(/\/$/, "");
    }
    return "/apps/ai-assistant";
  }

  // ── Session ID (persisted in localStorage) ────────────────────────
  function getSessionId() {
    try {
      let id = localStorage.getItem("ai_session_id");
      if (!id) {
        id = "sess_" + Math.random().toString(36).slice(2, 10) + Date.now();
        localStorage.setItem("ai_session_id", id);
      }
      return id;
    } catch (e) {
      return "default";
    }
  }

  // ── Native Storefront Ajax Cart Adapter (v2.1.9 Token-Free) ──────
  const nativeCartAdapter = {
    getRoot() {
      return (window.Shopify && window.Shopify.routes && window.Shopify.routes.root) || "/";
    },
    async getCart() {
      const root = this.getRoot();
      const res = await fetch(`${root}cart.js`, { credentials: "same-origin" });
      if (!res.ok) throw new Error(`HTTP_${res.status}`);
      return await res.json();
    },
    async addItems(items) {
      const root = this.getRoot();
      const res = await fetch(`${root}cart/add.js`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ items })
      });
      if (!res.ok) throw new Error(`HTTP_${res.status}`);
      return await res.json();
    },
    async changeLine(lineKey, quantity) {
      const root = this.getRoot();
      const res = await fetch(`${root}cart/change.js`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ id: lineKey, quantity })
      });
      if (!res.ok) throw new Error(`HTTP_${res.status}`);
      return await res.json();
    },
    async updateLines(updates) {
      const root = this.getRoot();
      const res = await fetch(`${root}cart/update.js`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ updates })
      });
      if (!res.ok) throw new Error(`HTTP_${res.status}`);
      return await res.json();
    },
    async clearCart() {
      const root = this.getRoot();
      const res = await fetch(`${root}cart/clear.js`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin"
      });
      if (!res.ok) throw new Error(`HTTP_${res.status}`);
      return await res.json();
    }
  };

  async function getNativeShopifyCart() {
    try {
      return await nativeCartAdapter.getCart();
    } catch (e) {
      return null;
    }
  }

  async function notifyCartUpdate(freshCart, responseAnswer) {
    try {
      const cartObj = freshCart || (await getNativeShopifyCart());
      const count = cartObj?.item_count ?? cartObj?.total_quantity ?? 0;

      // 1a. Update count text inside .cart-count-bubble WITHOUT destroying child HTML.
      //     Dawn theme uses <span aria-hidden="true">N</span> inside the div; setting
      //     el.textContent on the *div* itself wipes out those spans so Dawn's own JS
      //     can no longer read them. We target the inner spans directly instead.
      document.querySelectorAll(".cart-count-bubble span[aria-hidden='true'], .cart-count-bubble span:first-child").forEach(el => {
        if (el.closest(".ai-fab-wrapper, .ai-chat-panel")) return;
        el.textContent = String(count);
      });
      document.querySelectorAll(".cart-count-bubble .visually-hidden").forEach(el => {
        el.textContent = `${count} item${count !== 1 ? "s" : ""}`;
      });

      // 1b. Toggle visibility of the bubble container itself.
      document.querySelectorAll(".cart-count-bubble").forEach(el => {
        if (el.closest(".ai-fab-wrapper, .ai-chat-panel")) return;
        if (count > 0) {
          el.removeAttribute("hidden");
          el.style.display = "";
        } else {
          el.setAttribute("hidden", "true");
        }
      });

      // 1c. Generic selectors for non-Dawn themes.
      document.querySelectorAll(
        ".cart__count, [data-cart-count], .header__icon--cart .badge, " +
        ".cart-count, .header__cart-count, cart-icon-bubble .icon-with-count__count"
      ).forEach(el => {
        el.textContent = String(count);
        if (count > 0) { el.removeAttribute("hidden"); el.style.display = ""; }
        else            { el.setAttribute("hidden", "true"); }
      });

      // 1d. Dawn Sections API — dynamically fetch sections that exist in the DOM
      try {
        const root = nativeCartAdapter.getRoot();
        const validSections = ["cart-icon-bubble", "cart-drawer", "cart-notification"];
        const sep = window.location.pathname.includes("?") ? "&" : "?";
        
        const fetchPromises = validSections.map(sec => 
          fetch(`${window.location.pathname}${sep}sections=${sec}&t=${Date.now()}`, { credentials: "same-origin", cache: "no-store" })
            .then(r => r.ok ? r.json() : null)
            .catch(() => null)
        );
        
        const results = await Promise.all(fetchPromises);
        let sectData = {};
        results.forEach(res => {
          if (res) Object.assign(sectData, res);
        });

        if (Object.keys(sectData).length > 0) {

            // Pass sectData to Dawn's web component if available
            const cartDrawer = document.querySelector("cart-drawer");
            let dawnHandled = false;
            if (cartDrawer && typeof cartDrawer.renderContents === "function") {
              try {
                cartDrawer.renderContents({ sections: sectData });
                dawnHandled = true;
              } catch (e) {
                console.warn("[AI Assistant] cartDrawer.renderContents error:", e);
              }
            }

            const parser = new DOMParser();

            // Replace cart-icon-bubble HTML
            if (sectData["cart-icon-bubble"] && !dawnHandled) {
              const newDoc = parser.parseFromString(sectData["cart-icon-bubble"], "text/html");
              const newEl = newDoc.body.querySelector("cart-icon-bubble") || newDoc.body.querySelector(".cart-count-bubble");
              const existEl = document.querySelector("cart-icon-bubble") || document.querySelector(".cart-count-bubble");
              if (newEl && existEl) {
                existEl.innerHTML = newEl.innerHTML;
              }
            }

            // Replace cart-drawer HTML in DOM directly so drawer UI updates instantly
            const drawerHtml = sectData["cart-drawer"] || sectData["cart-drawer-items"] || sectData["cart-notification"];
            if (drawerHtml && !dawnHandled) {
              const drawerDoc = parser.parseFromString(drawerHtml, "text/html");
              const newDrawerInner = drawerDoc.querySelector(".drawer__inner") || drawerDoc.querySelector("#CartDrawer") || drawerDoc.querySelector("cart-drawer");
              const existDrawerInner = document.querySelector("cart-drawer .drawer__inner") || document.querySelector("#CartDrawer") || document.querySelector("cart-drawer");
              if (newDrawerInner && existDrawerInner) {
                existDrawerInner.innerHTML = newDrawerInner.innerHTML;
              }
            }

            if (cartDrawer) {
              if (count === 0) {
                cartDrawer.classList.add("is-empty");
              } else {
                cartDrawer.classList.remove("is-empty");
              }
            }
          }
      } catch (_sectionErr) {
        console.warn("[AI Assistant] Section refresh failed:", _sectionErr);
      }

      // 2. Broadcast standard Shopify theme cart events.
      document.dispatchEvent(new CustomEvent("ai-assistant:cart-updated", { detail: cartObj }));
      document.dispatchEvent(new CustomEvent("cart:refresh", { bubbles: true, detail: cartObj }));
      document.dispatchEvent(new CustomEvent("theme:cart:reload", { bubbles: true, detail: cartObj }));
      document.dispatchEvent(new CustomEvent("cart:updated", { bubbles: true, detail: cartObj }));
      document.dispatchEvent(new CustomEvent("shopify:cart:change", { bubbles: true, detail: cartObj }));

      // 3. Trigger Dawn & modern OS 2.0 Web Component drawers.
      try {
        const cartItems = document.querySelector("cart-items");
        if (cartItems && typeof cartItems.onCartUpdate === "function") {
          cartItems.onCartUpdate();
        }
      } catch (e) {}

      // 4. If currently on the /cart page, reload so server-side Liquid re-renders the table.
      const pathname = window.location.pathname.replace(/\/$/, "");
      if (pathname === "/cart") {
        const textToSave = responseAnswer || "Updated your cart.";
        try {
          sessionStorage.setItem("ai_nav_restore", JSON.stringify({
            answer: textToSave,
            ts: Date.now(),
            speak: false
          }));
        } catch (e) {}
        setTrackedTimeout(() => {
          window.location.reload();
        }, 1200);
      }
    } catch (err) {
      console.error("[AI Assistant] notifyCartUpdate error:", err);
    }
  }


  function sanitizeCartContext(rawCart) {
    if (!rawCart || typeof rawCart !== "object") return null;
    return {
      item_count: rawCart.item_count || 0,
      items: Array.isArray(rawCart.items) ? rawCart.items.map(item => {
        const opts = {};
        if (Array.isArray(item.options_with_values)) {
          item.options_with_values.forEach(opt => { opts[opt.name] = opt.value; });
        } else if (item.options && typeof item.options === "object") {
          Object.assign(opts, item.options);
        }
        return {
          key: item.key || String(item.id || ""),
          variant_id: item.variant_id || item.id,
          product_title: item.product_title || item.title || "",
          variant_title: item.variant_title || "",
          quantity: item.quantity || 1,
          options: opts
        };
      }) : []
    };
  }

  async function fetchStorefrontProduct(handle) {
    if (!handle) return null;
    try {
      const root = nativeCartAdapter.getRoot();
      const res = await fetch(`${root}products/${handle}.js`, { credentials: "same-origin" });
      if (res.ok) return await res.json();
    } catch (e) {}
    return null;
  }

  function buildVariantOptionMap(product, variant) {
    const result = {};
    if (!product || !product.options || !variant || !variant.options) return result;
    product.options.forEach((option, index) => {
      const optName = typeof option === "string" ? option : (option.name || `Option${index + 1}`);
      result[optName] = variant.options[index];
    });
    return result;
  }

  function resolveVariantStrict(product, requestedOptions) {
    if (!product || !product.variants || !Array.isArray(product.variants)) {
      return { variant: null, error: "PRODUCT_NOT_PUBLISHED" };
    }

    const requestedKeys = Object.keys(requestedOptions || {});
    const matchingVariants = product.variants.filter(variant => {
      const optMap = buildVariantOptionMap(product, variant);
      for (const key of requestedKeys) {
        const reqVal = String(requestedOptions[key] || "").trim().toLowerCase();
        const matchedOptKey = Object.keys(optMap).find(k => k.trim().toLowerCase() === key.trim().toLowerCase());
        if (!matchedOptKey) return false;
        const actualVal = String(optMap[matchedOptKey] || "").trim().toLowerCase();
        if (reqVal !== actualVal) return false;
      }
      return true;
    });

    if (matchingVariants.length === 0) {
      if (product.variants.length >= 250) {
        return { variant: null, error: "VARIANT_RESOLUTION_INCOMPLETE" };
      }
      return { variant: null, error: "CART_VARIANT_UNAVAILABLE" };
    }

    if (matchingVariants.length > 1) {
      const requiredOptCount = Array.isArray(product.options) ? product.options.length : 0;
      if (requestedKeys.length < requiredOptCount || product.variants.length >= 250) {
        return { variant: null, error: "VARIANT_RESOLUTION_INCOMPLETE" };
      }
    }

    const targetVariant = matchingVariants[0];
    const isAvail = targetVariant.available === true || targetVariant.availableForSale === true;
    if (!isAvail) {
      return { variant: null, error: "CART_VARIANT_UNAVAILABLE" };
    }

    return { variant: targetVariant, error: null };
  }

  function findExactCartLine(cart, criteria) {
    if (!cart || !Array.isArray(cart.items)) return null;

    const targetLineKey = criteria.lineKey ? String(criteria.lineKey) : null;
    const targetVarId = criteria.variantId ? String(criteria.variantId) : null;

    for (const line of cart.items) {
      const lineKey = line.key ? String(line.key) : String(line.id || "");
      const lineVarId = String(line.variant_id || line.id || "");

      if (targetLineKey && lineKey === targetLineKey) {
        return line;
      }

      if (targetVarId && (lineVarId === targetVarId || lineVarId.split("/").pop() === targetVarId.split("/").pop())) {
        if (criteria.selectedOptions) {
          const lineOpts = {};
          if (Array.isArray(line.options_with_values)) {
            line.options_with_values.forEach(o => { lineOpts[o.name.toLowerCase()] = String(o.value).toLowerCase(); });
          }
          let allMatch = true;
          for (const [k, v] of Object.entries(criteria.selectedOptions)) {
            if (lineOpts[k.toLowerCase()] && lineOpts[k.toLowerCase()] !== String(v).toLowerCase()) {
              allMatch = false;
              break;
            }
          }
          if (allMatch) return line;
        } else {
          return line;
        }
      }
    }
    return null;
  }

  function getExactLineQuantity(cart, target) {
    if (!cart || !Array.isArray(cart.items)) return 0;
    let targetVarId = null;
    let targetLineKey = null;

    if (typeof target === "object" && target !== null) {
      targetVarId = target.variantId || target.id || target.variant_id;
      targetLineKey = target.lineKey || target.key;
    } else if (typeof target === "string" || typeof target === "number") {
      targetVarId = String(target);
    }

    const line = findExactCartLine(cart, { lineKey: targetLineKey, variantId: targetVarId });
    return line ? (line.quantity || 0) : 0;
  }

  // ── Persist completed cart action IDs across page refreshes (v2.1.12) ──
  // Without persistence, a refresh clears the in-memory Set and the backend
  // (which still has a "pending" action for <45 s) would re-send
  // requires_client_action, causing the item to be added a second time.
  function _loadCompletedCartActionIds() {
    try {
      const raw = sessionStorage.getItem("ai_completed_cart_actions");
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr)) return new Set(arr);
      }
    } catch (e) {}
    return new Set();
  }
  function _saveCompletedCartActionId(id) {
    try {
      const existing = _loadCompletedCartActionIds();
      existing.add(id);
      // Only keep last 50 IDs to avoid unbounded growth
      const arr = [...existing].slice(-50);
      sessionStorage.setItem("ai_completed_cart_actions", JSON.stringify(arr));
    } catch (e) {}
  }
  const completedCartActionIds = _loadCompletedCartActionIds();
  let cartActionInFlight = false;

  // Monotonic sequence counters for request ordering & stale response suppression
  let _reqSeqNum = 0;
  let _latestHandledSeqNum = 0;

  // Global handles for wake audio, AudioContext, and sequence state
  let audioContext = null;
  let audioUnlocked = false;

  let wakeSequenceRunning = false;
  let queryRecognitionActive = false;
  let queryRecognition = null;
  let isProcessing = false;
  let isSpeaking = false;
  let _currentPlayingAudio = null;

  // TTS State Handles (v2.1.8)
  let activeTtsAudio = null;
  let ttsAbortController = null;
  let settleActiveTts = null;

  // Auto-Listen Timer Handle (v2.1.10)
  let autoListenTimer = null;

  // Timers registry for clean destruction
  const activeTimers = new Set();
  function setTrackedTimeout(fn, ms) {
    const timer = setTimeout(() => {
      activeTimers.delete(timer);
      fn();
    }, ms);
    activeTimers.add(timer);
    return timer;
  }
  function clearAllTurnTimers() {
    for (const t of activeTimers) {
      clearTimeout(t);
    }
    activeTimers.clear();
  }

  // ===============================================================
  // LIFECYCLE & STATE OWNERSHIP MANAGEMENT (v2.1.5)
  // ===============================================================
  let microphoneAccessConfirmed = false;
  let handsFreeEnabled = false;
  try {
    handsFreeEnabled = localStorage.getItem("ai_hands_free_enabled") === "true";
  } catch (e) {}
  let handsFreeEnableInFlight = false;

  let wakeSessionId = 0;
  let wakeRecognitionActive = false;
  let wakeRecognition = null;
  let wakeRestartTimer = null;
  let wakeRestartAllowed = true;
  let stabilityTimer = null;

  let lastWakeActivationAt = 0;
  const WAKE_ACTIVATION_COOLDOWN_MS = 1500;
  const WAKE_RESTART_DELAYS = [250, 500, 1000, 2000, 3000];
  let wakeRestartAttempt = 0;

  let panelOpen = false;
  let panelManuallyClosed = false;
  let panelLifecycleId = 0;
  let activeTurnId = 0;
  let finalizedTurnId = null;

  let requestAbortController = null;
  let listenerController = null;
  let destroyed = false;

  let assistantState = "idle";
  let microphonePermission = "prompt";
  let microphoneHardError = null;

  function beginAssistantTurn() {
    activeTurnId += 1;
    finalizedTurnId = null;
    if (requestAbortController) {
      try { requestAbortController.abort("new-turn-started"); } catch (e) {}
    }
    requestAbortController = new AbortController();
    return activeTurnId;
  }

  function cancelActiveTurn(reason = "user-action") {
    activeTurnId += 1;
    finalizedTurnId = null;

    if (requestAbortController) {
      try { requestAbortController.abort(reason); } catch (e) {}
      requestAbortController = null;
    }

    stopQueryRecognition();
    stopWakeChime();
    cancelAssistantTts();
    clearAllTurnTimers();

    queryRecognitionActive = false;
    isProcessing = false;
    isSpeaking = false;
    wakeSequenceRunning = false;
  }

  function destroyWidget() {
    destroyed = true;
    logWakeEvent("destroyWidget invoked");

    if (listenerController) {
      try { listenerController.abort(); } catch (e) {}
      listenerController = null;
    }

    activeTurnId += 1;

    stopWakeRecognition({ reason: "widget-destroyed", allowRestart: false });
    stopQueryRecognition();
    stopWakeChime();
    cancelAssistantTts();
    clearAllTurnTimers();

    queryRecognitionActive = false;
    isProcessing = false;
    isSpeaking = false;
    wakeSequenceRunning = false;
  }

  function hideFloatingFab(rootNode = document) {
    // v2.1.10: Floating FAB must remain visible & interactive at all times
    showFloatingFab(rootNode);
  }

  function showFloatingFab(rootNode = document) {
    const wrapper = rootNode?.classList?.contains("ai-fab-wrapper") ? rootNode : (_q(".ai-fab-wrapper", rootNode) || _q(".ai-fab-wrapper"));
    if (!wrapper) return;
    const fabWrap = wrapper.querySelector(".ai-fab-btn-wrap") || wrapper;
    const fab = wrapper.querySelector(".ai-fab-btn");
    if (fabWrap) {
      fabWrap.classList.remove("ai-fab-btn-wrap--hidden");
      fabWrap.setAttribute("aria-hidden", "false");
    }
    if (fab) {
      fab.disabled = false;
      try { fab.removeAttribute("tabindex"); } catch (e) {}
      fab.setAttribute("aria-expanded", panelOpen ? "true" : "false");
    }
  }

  function openAssistantPanel(source = "user") {
    panelLifecycleId += 1;
    panelManuallyClosed = false;
    panelOpen = true;

    showFloatingFab();

    const root = getCanonicalRoot();
    if (root) {
      _openPanel(root);
    }
  }

  async function closeAssistantPanel(options = {}) {
    const { source = "user", cancelCurrentTurn = true } = options;
    panelLifecycleId += 1;
    panelManuallyClosed = (source !== "auto-complete" && source !== "auto-close" && source !== "system" && source !== "voice-close");
    panelOpen = false;

    showFloatingFab();

    const root = getCanonicalRoot();
    if (root) {
      _closePanel(root);
    }

    if (cancelCurrentTurn) {
      cancelActiveTurn("panel-closed");
    }

    if (handsFreeEnabled && microphoneAccessConfirmed && !microphoneHardError) {
      // v2.1.5-fix: Reset allowRestart flag that may have been locked by a prior
      // stopWakeRecognition({ allowRestart: false }) call during the active conversation.
      // Also give the browser's SpeechRecognition engine a brief settle window (350ms)
      // so its onend callback fires cleanly before we try to restart wake listening.
      wakeRestartAllowed = true;
      await delay(350);
      if (!destroyed && handsFreeEnabled && microphoneAccessConfirmed && !microphoneHardError) {
        const wakeStarted = await startWakeRecognitionSafely(root || document);
        if (!wakeStarted && shouldRestartWakeRecognition()) {
          scheduleWakeRecognitionRestart(wakeSessionId, root || document);
        }
      }
    } else {
      transitionAssistantState(AssistantState.IDLE, root || document);
    }
  }

  function getStorePreferenceKey() {
    const shopDomain = window.Shopify?.shop || "default";
    const normalizedShop = String(shopDomain || "").trim().toLowerCase();
    return `ai-wake-sound:${normalizedShop}`;
  }

  function isWakeSoundEnabled() {
    try {
      return localStorage.getItem(getStorePreferenceKey()) !== "false";
    } catch (e) {
      return true;
    }
  }

  function getSharedAudioContext() {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return null;
    if (!audioContext || audioContext.state === "closed") {
      audioContext = new AudioCtx();
    }
    return audioContext;
  }

  function unlockAssistantAudio() {
    try {
      const ctx = getSharedAudioContext();
      if (ctx) {
        if (ctx.state === "suspended") {
          ctx.resume().catch(() => {});
        }
        const buf = ctx.createBuffer(1, 1, 22050);
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        src.start(0);
      }

      if ('speechSynthesis' in window) {
        if (window.speechSynthesis.paused) {
          try { window.speechSynthesis.resume(); } catch(e) {}
        }
        try { window.speechSynthesis.getVoices(); } catch(e) {}
      }

      audioUnlocked = true;
      logWakeEvent("audio context state=" + (ctx ? ctx.state : "none"));
    } catch (error) {
      audioUnlocked = false;
    }
  }

  (function setupAutoAudioUnlock() {
    const unlock = () => {
      unlockAssistantAudio();
    };
    ["click", "touchstart", "pointerdown", "keydown"].forEach(evt => {
      document.addEventListener(evt, unlock, { capture: true, passive: true });
    });
  })();

  // ── Cancellable Web Audio Chime Synthesizer (v2.1.5 Smooth Master) ──
  let activeChimeNodes = [];

  function stopWakeChime() {
    for (const node of activeChimeNodes) {
      try { node.stop?.(); } catch (e) {}
      try { node.disconnect?.(); } catch (e) {}
    }
    activeChimeNodes = [];
  }

  const DEFAULT_WAKE_CHIME_GAIN = 0.09;
  const MAX_WAKE_CHIME_GAIN = 0.12;

  function playGeneratedWakeTone() {
    if (!isWakeSoundEnabled()) {
      return Promise.resolve({ played: false, reason: "sound-disabled" });
    }

    return new Promise((resolve) => {
      (async () => {
        try {
          stopWakeChime();
          const ctx = getSharedAudioContext();
          if (!ctx) { resolve({ played: false, reason: "no-audio-context" }); return; }
          if (ctx.state === "suspended") {
            try { await ctx.resume(); } catch(e) {}
          }

          const start = ctx.currentTime;
          const duration = 0.55;
          const end = start + duration;

          const configuredWakeGain = typeof window !== "undefined" ? window.AI_WAKE_CHIME_GAIN : null;
          const wakeGain = Math.min(
            Math.max(configuredWakeGain || DEFAULT_WAKE_CHIME_GAIN, 0.02),
            MAX_WAKE_CHIME_GAIN
          );

          // Dynamics Compressor to prevent clipping & maintain smooth loudness
          const compressor = ctx.createDynamicsCompressor();
          if (compressor.threshold) compressor.threshold.setValueAtTime(-18, start);
          if (compressor.knee) compressor.knee.setValueAtTime(20, start);
          if (compressor.ratio) compressor.ratio.setValueAtTime(4, start);
          if (compressor.attack) compressor.attack.setValueAtTime(0.003, start);
          if (compressor.release) compressor.release.setValueAtTime(0.15, start);
          compressor.connect(ctx.destination);
          activeChimeNodes.push(compressor);

          // Low-pass filter at 2800 Hz for warm smooth tone
          const filter = ctx.createBiquadFilter();
          filter.type = "lowpass";
          filter.frequency.setValueAtTime(2800, start);
          filter.connect(compressor);
          activeChimeNodes.push(filter);

          // Master gain envelope (clamped peak wakeGain ~0.09)
          const masterGain = ctx.createGain();
          masterGain.gain.setValueAtTime(0.0001, start);
          masterGain.gain.exponentialRampToValueAtTime(wakeGain, start + 0.04);
          masterGain.gain.exponentialRampToValueAtTime(0.0001, end);
          masterGain.connect(filter);
          activeChimeNodes.push(masterGain);

          // Note 1: C5 (523.25 Hz) from 0.00s -> 0.30s
          const osc1 = ctx.createOscillator();
          osc1.type = "sine";
          osc1.frequency.setValueAtTime(523.25, start);
          osc1.connect(masterGain);
          osc1.start(start);
          osc1.stop(start + 0.30);
          activeChimeNodes.push(osc1);

          // Note 2: E5 (659.25 Hz) from 0.14s -> 0.52s
          const osc2 = ctx.createOscillator();
          osc2.type = "sine";
          osc2.frequency.setValueAtTime(659.25, start + 0.14);
          osc2.connect(masterGain);
          osc2.start(start + 0.14);
          osc2.stop(end);
          activeChimeNodes.push(osc2);

          setTrackedTimeout(() => {
            stopWakeChime();
            resolve({ played: true, source: "web-audio-chime" });
          }, 550);
        } catch (err) {
          stopWakeChime();
          resolve({ played: false, reason: err.message });
        }
      })();
    });
  }

  async function playSmoothWakeChimeWithTimeout() {
    logWakeEvent("smooth chime started");
    let chimeTimer = null;
    try {
      await Promise.race([
        playGeneratedWakeTone(),
        new Promise((_, reject) => {
          chimeTimer = setTrackedTimeout(() => reject(new Error("WAKE_CHIME_TIMEOUT")), 900);
        })
      ]);
      logWakeEvent("smooth chime completed");
    } catch (error) {
      logWakeEvent("smooth chime warning", error?.message || error);
    } finally {
      if (chimeTimer) clearTimeout(chimeTimer);
      stopWakeChime();
    }
  }

  function stopAllAudioPlayback() {
    stopWakeChime();
    if (_currentPlayingAudio) {
      try {
        _currentPlayingAudio.pause();
        _currentPlayingAudio.currentTime = 0;
      } catch (e) {}
      _currentPlayingAudio = null;
    }
    if (window.speechSynthesis && window.speechSynthesis.speaking) {
      try { window.speechSynthesis.cancel(); } catch (e) {}
    }
  }

  function cancelAssistantTts(reason = "cancelled") {
    if (ttsAbortController) {
      try { ttsAbortController.abort(reason); } catch (e) {}
      ttsAbortController = null;
    }

    if (activeTtsAudio) {
      try { activeTtsAudio.pause(); activeTtsAudio.currentTime = 0; } catch (e) {}
      activeTtsAudio = null;
    }

    stopAllAudioPlayback();

    if (window.speechSynthesis && window.speechSynthesis.speaking) {
      try { window.speechSynthesis.cancel(); } catch (e) {}
    }

    if (typeof settleActiveTts === "function") {
      try {
        settleActiveTts({
          status: "cancelled",
          reason
        });
      } catch (e) {}
      settleActiveTts = null;
    }

    isSpeaking = false;
  }

  // ── Bounded TTS Engine ──────────────────────────────────────────
  async function speakAssistantResponse(data, turnId, status, rootNode = document) {
    if (turnId !== activeTurnId || panelManuallyClosed) return;

    const answerText = data.answer || "";
    const audioB64 = data.audio || null;

    if (!answerText && !audioB64) return;

    stopAllAudioPlayback();
    isSpeaking = true;
    transitionAssistantState(AssistantState.SPEAKING, rootNode);

    if (audioB64) {
      let ttsAudio = null;
      let ttsTimer = null;
      try {
        await Promise.race([
          new Promise((resolve, reject) => {
            if (turnId !== activeTurnId || panelManuallyClosed) { reject(new Error("STALE_TURN")); return; }
            ttsAudio = new Audio("data:audio/mp3;base64," + audioB64);
            activeTtsAudio = ttsAudio;
            _currentPlayingAudio = ttsAudio;
            ttsAudio.onplay = () => {
              if (status && turnId === activeTurnId && !panelManuallyClosed) status.textContent = "Assistant is speaking…";
            };
            ttsAudio.onended = () => resolve("ended");
            ttsAudio.onerror = (e) => reject(e || new Error("AUDIO_PLAYBACK_ERROR"));
            ttsAudio.play().catch(reject);
          }),
          new Promise((_, reject) => {
            ttsTimer = setTrackedTimeout(() => reject(new Error("TTS_TIMEOUT")), 15000);
          })
        ]);
      } catch (err) {
        console.warn("[TTS] ElevenLabs audio ended or failed; falling back to browser speech", err?.message);
        if (turnId === activeTurnId && !panelManuallyClosed) {
          await speakTextWithBrowserSpeech(answerText, status, turnId);
        }
      } finally {
        if (ttsTimer) clearTimeout(ttsTimer);
        if (ttsAudio) {
          try { ttsAudio.pause(); ttsAudio.currentTime = 0; } catch (e) {}
        }
        activeTtsAudio = null;
        _currentPlayingAudio = null;
        isSpeaking = false;
      }
    } else if (answerText) {
      await speakTextWithBrowserSpeech(answerText, status, turnId);
      isSpeaking = false;
    }
  }

  async function speakTextWithBrowserSpeech(text, status, turnId) {
    if (turnId !== activeTurnId || panelManuallyClosed) return;

    if (!('speechSynthesis' in window)) {
      await delay(1000);
      return;
    }

    const cleanText = text
      .replace(/\*\*(.*?)\*\*/g, "$1")
      .replace(/\*(.*?)\*/g, "$1")
      .replace(/[✅❌⚠️🎉🔍🛒🎙️👋📦💬✋🤖👤]/g, "")
      .replace(/https?:\/\/\S+/g, "")
      .replace(/[\#\_]/g, "")
      .replace(/\s+/g, " ")
      .trim();

    if (!cleanText) return;

    let failsafeTimer = null;
    let utterance = null;

    try {
      await Promise.race([
        new Promise((resolve) => {
          if (turnId !== activeTurnId || panelManuallyClosed) { resolve("stale"); return; }
          utterance = new SpeechSynthesisUtterance(cleanText);
          utterance.lang = "en-US";
          utterance.rate = 1.0;
          utterance.pitch = 1.0;

          const voices = window.speechSynthesis.getVoices();
          const preferredVoice = voices.find(v =>
            v.lang.startsWith("en") && (v.name.includes("Natural") || v.name.includes("Google") || v.name.includes("Samantha") || v.name.includes("Karen") || v.name.includes("Daniel"))
          ) || voices.find(v => v.lang.startsWith("en"));

          if (preferredVoice) utterance.voice = preferredVoice;

          utterance.onstart = () => {
            if (status && turnId === activeTurnId && !panelManuallyClosed) status.textContent = "Assistant is speaking…";
          };
          utterance.onend = () => resolve("ended");
          utterance.onerror = () => resolve("error");

          if (window.speechSynthesis.paused) window.speechSynthesis.resume();
          window.speechSynthesis.speak(utterance);
        }),
        new Promise((resolve) => {
          const wordsCount = cleanText.split(/\s+/).length;
          const durationMs = Math.min(45000, Math.max(15000, Math.ceil((wordsCount / 1.8) * 1000) + 5000));
          failsafeTimer = setTrackedTimeout(() => resolve("timeout"), durationMs);
        })
      ]);
    } catch (e) {
      console.warn("[TTS] Web Speech Synthesis error:", e);
    } finally {
      if (failsafeTimer) clearTimeout(failsafeTimer);
      if (turnId !== activeTurnId || panelManuallyClosed) {
        if (window.speechSynthesis && window.speechSynthesis.speaking) {
          try { window.speechSynthesis.cancel(); } catch (e) {}
        }
      }
    }
  }

  // ── Shared UI Helper Functions ────────────────────────────────────
  function _q(selector, rootNode = document) {
    return rootNode.querySelector(selector);
  }

  function _setStatus(text, rootNode = document) {
    const el = _q(".ai-assistant-status", rootNode);
    if (el) el.textContent = text;
  }

  function _setCustomerTranscription(text, rootNode = document) {
    const el = _q(".ai-customer-transcription", rootNode);
    if (!el) return;
    if (!text) {
      el.style.display = "none";
      el.textContent = "";
    } else {
      el.style.display = "block";
      el.textContent = text;
    }
  }

  function _clearCustomerTranscription(rootNode = document) {
    _setCustomerTranscription("", rootNode);
  }

  function _hideSuggestionChips(rootNode = document) {
    const el = _q(".ai-suggestion-chips", rootNode);
    if (el) el.style.display = "none";
  }

  const AssistantState = Object.freeze({
    IDLE: "idle",
    WAKE_READY: "wake-ready",
    WAKING: "waking",
    LISTENING: "listening",
    PROCESSING: "processing",
    SPEAKING: "speaking",
    SUCCESS: "success",
    ERROR: "error"
  });

  function transitionAssistantState(newState, rootNode = document) {
    if (newState === AssistantState.IDLE && handsFreeEnabled && microphoneAccessConfirmed && !microphoneHardError) {
      newState = AssistantState.WAKE_READY;
    }

    assistantState = newState;
    const wrapper = rootNode?.classList?.contains("ai-fab-wrapper") ? rootNode : (_q(".ai-fab-wrapper", rootNode) || _q(".ai-fab-wrapper") || document);
    const dots = wrapper?.querySelectorAll ? wrapper.querySelectorAll(".ai-ww-dot") : [];
    const orb = _q(".ai-assistant-button", wrapper);
    const status = _q(".ai-assistant-status", wrapper);

    let dotClass = "disabled";
    let orbClass = "idle";
    let statusMsg = "Tap the microphone to speak";

    switch (newState) {
      case AssistantState.IDLE:
        dotClass = "disabled";
        orbClass = "idle";
        statusMsg = "Tap the microphone to speak";
        break;
      case AssistantState.WAKE_READY:
        dotClass = "standby";
        orbClass = "wake-ready";
        statusMsg = "Say “Hey Assistant” or tap to speak";
        break;
      case AssistantState.WAKING:
        dotClass = "standby";
        orbClass = "listening";
        statusMsg = "Hey! How can I help?";
        break;
      case AssistantState.LISTENING:
        dotClass = "listening";
        orbClass = "listening";
        statusMsg = "Listening…";
        break;
      case AssistantState.PROCESSING:
        dotClass = "processing";
        orbClass = "processing";
        statusMsg = "Finding the best options…";
        break;
      case AssistantState.SPEAKING:
        dotClass = "speaking";
        orbClass = "speaking";
        statusMsg = "Assistant is speaking…";
        break;
      case AssistantState.SUCCESS:
        dotClass = "standby";
        orbClass = "wake-ready";
        statusMsg = "Done!";
        break;
      case AssistantState.ERROR:
        dotClass = "error";
        orbClass = "error";
        statusMsg = "⚠️ Microphone error or connection issue. Tap to retry.";
        break;
    }

    if (dots && dots.length) {
      dots.forEach(d => {
        d.className = `ai-ww-dot ai-ww-dot--${dotClass}`;
      });
    }
    if (orb) orb.className = `ai-assistant-button ai-orb--${orbClass}`;
    const fabBtn = wrapper?.querySelector ? wrapper.querySelector(".ai-fab-btn") : null;
    if (fabBtn) fabBtn.className = `ai-fab-btn ai-orb--${orbClass}`;
    if (status && statusMsg && !status.innerHTML.includes("<button")) status.textContent = statusMsg;
  }

  function handleMicError(err, rootNode = document) {
    const errName = err?.name || err?.error || "";
    let message = "Microphone unavailable. Please check permissions.";

    if (errName === "NotAllowedError" || errName === "PermissionDeniedError" || errName === "not-allowed" || errName === "service-not-allowed" || errName === "permission-denied") {
      microphoneAccessConfirmed = false;
      microphonePermission = "denied";
      microphoneHardError = "NotAllowedError";
      message = "Microphone blocked. Click 🔒 in address bar to allow mic access.";
    } else if (errName === "NotFoundError" || errName === "DevicesNotFoundError") {
      microphoneAccessConfirmed = false;
      microphoneHardError = "NotFoundError";
      message = "No microphone found on this device.";
    } else if (errName === "NotReadableError" || errName === "TrackStartError" || errName === "audio-capture") {
      microphoneHardError = "NotReadableError";
      message = "Microphone is busy in another application.";
    } else if (errName === "SecurityError") {
      microphoneAccessConfirmed = false;
      microphoneHardError = "SecurityError";
      message = "Microphone access requires HTTPS.";
    } else if (errName === "AbortError") {
      microphoneHardError = "AbortError";
      message = "Microphone request interrupted.";
    }

    transitionAssistantState(AssistantState.ERROR, rootNode);
    const status = _q(".ai-assistant-status", rootNode);
    if (status) {
      status.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="vertical-align:middle;margin-right:4px;"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg><span>${message} <button type="button" class="ai-retry-mic-btn" style="background:none;border:none;color:#3154f5;text-decoration:underline;cursor:pointer;font-weight:600;padding:0;margin-left:4px;">Retry</button></span>`;
      const retryBtn = status.querySelector(".ai-retry-mic-btn");
      if (retryBtn) {
        retryBtn.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          microphoneHardError = null;
          enableHandsFreeFromUserGesture(rootNode);
        });
      }
    }
  }

  function normalizeWakeText(value) {
    const input = String(value || "").toLowerCase();
    try {
      return input
        .replace(/[^\p{L}\p{N}\s]/gu, " ")
        .replace(/\s+/g, " ")
        .trim();
    } catch (e) {
      return input
        .replace(/[^a-z0-9\s]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
    }
  }

  function containsWakePhrase(transcript) {
    const value = normalizeWakeText(transcript);
    return [
      "hey assistant",
      "hi assistant",
      "hey assistance",
      "okay assistant",
      "ok assistant",
      "hey shop",
      "hey ai",
      "assistant",
      "assistance",
      "assistent"
    ].some((phrase) => value.includes(phrase));
  }

  // ── Authoritative State Finalizer ─────────────────────────────────
  async function finalizeAssistantTurn(options = {}, rootNode = document) {
    const {
      turnId,
      conversationComplete = false,
      requiresInput = false,
      autoListen = false
    } = options;

    if (typeof turnId === "number") {
      if (turnId !== activeTurnId) {
        return;
      }
      if (finalizedTurnId === turnId) {
        return;
      }
      finalizedTurnId = turnId;
    }

    if (panelManuallyClosed) {
      logWakeEvent("returned to wake-ready after turn (panel manually closed)");
      if (handsFreeEnabled && microphoneAccessConfirmed && !microphoneHardError) {
        wakeRestartAllowed = true;  // v2.1.5-fix: unlock restart after conversation end
        startWakeRecognitionSafely(rootNode);
      }
      return;
    }

    stopWakeChime();
    stopAllAudioPlayback();
    stopQueryRecognition();

    wakeSequenceRunning = false;
    queryRecognitionActive = false;
    isProcessing = false;
    isSpeaking = false;

    const wrapper = rootNode?.classList?.contains("ai-fab-wrapper") ? rootNode : _q(".ai-fab-wrapper") || document;
    const checkbox = wrapper?.querySelector?.(".ai-ww-enable-btn");
    if (checkbox) checkbox.checked = handsFreeEnabled;

    if (requiresInput && autoListen) {
      openAssistantPanel("auto-listen");
      transitionAssistantState(AssistantState.LISTENING, wrapper);
      startQueryRecognitionSafely({ turnId: beginAssistantTurn() }, wrapper);
      return;
    }

    if (conversationComplete) {
      if (handsFreeEnabled && microphoneAccessConfirmed && !microphoneHardError) {
        logWakeEvent("returned to wake-ready");
        startWakeRecognitionSafely(wrapper);
      } else {
        transitionAssistantState(AssistantState.IDLE, wrapper);
      }
      return;
    }

    if (handsFreeEnabled && microphoneAccessConfirmed && !microphoneHardError) {
      logWakeEvent("returned to wake-ready");
      startWakeRecognitionSafely(wrapper);
    } else {
      transitionAssistantState(AssistantState.IDLE, wrapper);
    }
  }

  function _openPanel(rootNode = document) {
    const wrapper = rootNode?.classList?.contains("ai-fab-wrapper") ? rootNode : (_q(".ai-fab-wrapper", rootNode) || _q(".ai-fab-wrapper"));
    if (!wrapper) return;
    const panel = wrapper.querySelector(".ai-chat-panel");
    const fabBtn = wrapper.querySelector(".ai-fab-btn");
    const status = wrapper.querySelector(".ai-assistant-status");
    if (status && status.innerHTML.includes("Microphone blocked")) {
      status.textContent = "Tap the microphone to speak";
    }

    if (panel) {
      panel.inert = false;
      try { panel.removeAttribute("inert"); } catch (e) {}
      panel.setAttribute("aria-hidden", "false");
      panel.classList.add("ai-open", "ai-chat-panel--open");
      panel.style.setProperty("display", "flex", "important");
      requestAnimationFrame(() => {
        panel.style.setProperty("opacity", "1", "important");
        panel.style.setProperty("visibility", "visible", "important");
        panel.style.setProperty("pointer-events", "auto", "important");
        panel.style.setProperty("transform", "translateY(0) scale(1)", "important");
      });
    }

    wrapper.classList.add("ai-panel-open");
    if (fabBtn) fabBtn.setAttribute("aria-expanded", "true");
    hideFloatingFab(wrapper);
  }

  function _closePanel(rootNode = document) {
    const wrapper = rootNode?.classList?.contains("ai-fab-wrapper") ? rootNode : (_q(".ai-fab-wrapper", rootNode) || _q(".ai-fab-wrapper"));
    if (!wrapper) return;
    const panel = wrapper.querySelector(".ai-chat-panel");
    const fabBtn = wrapper.querySelector(".ai-fab-btn");

    // 1. Show and enable the FAB first
    showFloatingFab(wrapper);

    // 2. Move focus out of the panel before setting aria-hidden
    const focusedElement = document.activeElement;
    if (focusedElement && panel && panel.contains(focusedElement)) {
      if (fabBtn && typeof fabBtn.focus === "function") {
        try {
          fabBtn.focus({ preventScroll: true });
        } catch (e) {}
      }
    }

    // 3. Set inert and aria-hidden
    if (panel) {
      panel.inert = true;
      panel.setAttribute("aria-hidden", "true");
      panel.classList.remove("ai-open", "ai-chat-panel--open");
      panel.style.setProperty("opacity", "0", "important");
      panel.style.setProperty("pointer-events", "none", "important");
      panel.style.setProperty("transform", "translateY(24px) scale(0.92)", "important");
      setTrackedTimeout(() => {
        if (!panel.classList.contains("ai-open")) {
          panel.style.setProperty("display", "none", "important");
          panel.style.setProperty("visibility", "hidden", "important");
        }
      }, 320);
    }

    wrapper.classList.remove("ai-panel-open");
    if (fabBtn) fabBtn.setAttribute("aria-expanded", "false");
  }

  async function renderApiResponse(data, responseBox, status, rootNode = document, turnId = activeTurnId) {
    if (!data || turnId !== activeTurnId || panelManuallyClosed) return;

    if (data.sequence_num && data.sequence_num < _latestHandledSeqNum) {
      console.warn(`[AI Assistant] Discarding stale response seq ${data.sequence_num}`);
      return;
    }

    if (data.sequence_num) {
      _latestHandledSeqNum = data.sequence_num;
    }

    // ── Client Cart Action Execution Handler (v2.1.9) ──────────────────
    if (data.requires_client_action && data.cart_action) {
      const action = data.cart_action;
      if (completedCartActionIds.has(action.action_id) || cartActionInFlight) {
        console.warn(`[AI Assistant] Action ${action.action_id} in flight or already completed.`);
        return;
      }

      cartActionInFlight = true;
      transitionAssistantState(AssistantState.PROCESSING, rootNode);
      if (status) status.textContent = "Updating your cart...";

      (async () => {
        let isSuccess = false;
        let resultData = null;
        let errorData = null;

        try {
          const actType = action.action_type || action.type;
          if (actType === "add") {
            let prod = null;
            if (action.product_handle) {
              prod = await fetchStorefrontProduct(action.product_handle);
            }
            let resolvedVar = null;
            if (prod) {
              const res = resolveVariantStrict(prod, action.selected_options);
              if (res.error) {
                errorData = { code: res.error, message: "Variant resolution failed" };
              } else {
                resolvedVar = res.variant;
              }
            } else if (action.variant_id) {
              const rawV = String(action.variant_id).split("/").pop();
              resolvedVar = { id: parseInt(rawV, 10), available: true };
            }

            if (!resolvedVar && !errorData) {
              errorData = { code: "VARIANT_RESOLUTION_INCOMPLETE", message: "Could not resolve variant" };
            }

            if (resolvedVar && !errorData) {
              const beforeCart = await nativeCartAdapter.getCart();
              const beforeQty = getExactLineQuantity(beforeCart, { variantId: resolvedVar.id });
              const expectedQty = action.quantity_mode === "increment" ? (beforeQty + action.quantity) : action.quantity;

              const addResult = await nativeCartAdapter.addItems([{
                id: resolvedVar.id,
                quantity: action.quantity
              }]);

              const afterCart = await nativeCartAdapter.getCart();
              const actualQty = getExactLineQuantity(afterCart, { variantId: resolvedVar.id });

              if (actualQty === expectedQty || (afterCart.item_count > beforeCart.item_count)) {
                isSuccess = true;
                resultData = { total_quantity: afterCart.item_count, items: afterCart.items };
              } else {
                // Controlled 1 retry
                await delay(300);
                const retryCart = await nativeCartAdapter.getCart();
                const retryQty = getExactLineQuantity(retryCart, { variantId: resolvedVar.id });
                if (retryQty === expectedQty) {
                  isSuccess = true;
                  resultData = { total_quantity: retryCart.item_count, items: retryCart.items };
                } else {
                  errorData = { code: "CART_QUANTITY_UNAVAILABLE", message: "Quantity verification failed" };
                }
              }
            }
          } else if (actType === "change" || actType === "remove") {
            const beforeCart = await nativeCartAdapter.getCart();
            const exactLine = findExactCartLine(beforeCart, {
              lineKey: action.line_key,
              variantId: action.variant_id,
              selectedOptions: action.selected_options
            });

            if (!exactLine) {
              errorData = { code: "CART_LINE_NOT_FOUND", message: "Line item not found in cart" };
            } else {
              const beforeQty = exactLine.quantity || 1;
              const expectedQty = actType === "remove" ? 0 : (action.quantity_mode === "increment" ? (beforeQty + action.quantity) : action.quantity);

              await nativeCartAdapter.changeLine(exactLine.key, expectedQty);

              const afterCart = await nativeCartAdapter.getCart();
              const actualQty = getExactLineQuantity(afterCart, { lineKey: exactLine.key, variantId: exactLine.variant_id });

              if (actualQty === expectedQty) {
                isSuccess = true;
                resultData = { total_quantity: afterCart.item_count, items: afterCart.items };
              } else {
                errorData = { code: "CART_QUANTITY_UNAVAILABLE", message: "Quantity update verification failed" };
              }
            }
          } else if (actType === "clear") {
            await nativeCartAdapter.clearCart();
            const afterCart = await nativeCartAdapter.getCart();
            if (afterCart.item_count === 0) {
              isSuccess = true;
              resultData = { total_quantity: 0, items: [] };
            } else {
              errorData = { code: "CART_CLEAR_FAILED", message: "Failed to clear cart" };
            }
          }
        } catch (err) {
          console.error("[AI Assistant] Client cart action execution error:", err);
          const msg = String(err.message || "");
          if (msg.includes("HTTP_429")) {
            errorData = { code: "HTTP_429", message: "Cart service busy" };
          } else {
            errorData = { code: "CART_OPERATION_FAILED", message: msg };
          }
        } finally {
          completedCartActionIds.add(action.action_id);
          _saveCompletedCartActionId(action.action_id);
          cartActionInFlight = false;
        }

        // Broadcast cart updated custom events & refresh theme UI
        try {
          const freshCart = await nativeCartAdapter.getCart();
          await notifyCartUpdate(freshCart, action.product_title ? `Updated ${action.product_title} in your cart.` : "");
        } catch (e) {}

        // Send acknowledgement system event to backend
        try {
          const ackResp = await fetch(`${getApiBaseUrl()}/voice-text`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              intent: "client_cart_action_result",
              session_id: getSessionId(),
              action_id: action.action_id,
              success: isSuccess,
              result: resultData,
              error: errorData,
              shop_domain: window.Shopify?.shop || ""
            })
          });

          if (ackResp.ok) {
            const finalData = await ackResp.json();
            if (finalData.success === false) {
              console.error("[AI Assistant] Acknowledgement backend error:", finalData.error);
            } else if (turnId === activeTurnId && !panelManuallyClosed) {
              try {
                const freshCart = await nativeCartAdapter.getCart();
                await notifyCartUpdate(freshCart, finalData.answer);
              } catch (e) {
                await notifyCartUpdate(null, finalData.answer);
              }
              // ── v2.1.12: Mark this as an ACK response so renderApiResponse
              // skips the legacy added/removed/updated_variant_id cart paths.
              // Those paths would cause a double-add since the cart action was
              // already executed above by nativeCartAdapter.
              finalData._from_ack = true;
              await renderApiResponse(finalData, responseBox, status, rootNode, turnId);
              
              if (finalData.auto_checkout || data.auto_checkout) {
                window.location.href = "/checkout";
              }
            }
          }
        } catch (ackErr) {
          console.error("[AI Assistant] Acknowledgement error:", ackErr);
        }
      })();

      return;
    }

    responseBox.innerHTML = "";

    if (data.answer) {
      const textEl = document.createElement("p");
      textEl.style.margin = "0";
      textEl.style.lineHeight = "1.5";
      textEl.innerHTML = data.answer.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");
      responseBox.appendChild(textEl);
    }

    if (Array.isArray(data.variant_options) && data.variant_options.length > 0) {
      const optWrap = document.createElement("div");
      optWrap.className = "ai-assistant-options-container";
      optWrap.style.marginTop = "10px";

      data.variant_options.forEach(optGroup => {
        const groupTitle = document.createElement("div");
        groupTitle.className = "ai-assistant-option-label";
        groupTitle.textContent = `${optGroup.name}:`;
        optWrap.appendChild(groupTitle);

        const btnGroup = document.createElement("div");
        btnGroup.className = "ai-assistant-option-buttons";

        (optGroup.values || []).forEach(valObj => {
          const valStr = typeof valObj === "object" ? valObj.value : String(valObj);
          const isAvail = typeof valObj === "object" ? valObj.available !== false : true;
          const isSel = typeof valObj === "object" ? valObj.selected === true : false;

          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "ai-assistant-option-btn";
          if (isSel) btn.classList.add("ai-assistant-option-btn--selected");
          btn.setAttribute("aria-pressed", isSel ? "true" : "false");

          if (!isAvail) {
            btn.disabled = true;
            btn.classList.add("ai-assistant-option-btn--disabled");
          }

          btn.textContent = valStr;

          btn.addEventListener("click", () => {
            if (!isAvail) return;
            _processQuery({
              intent: "select_variant_option",
              product_id: data.product_id || data.product?.id,
              option_name: optGroup.name,
              option_value: valStr,
              selection_revision: data.selection_revision,
              conversation_id: getSessionId()
            }, rootNode);
          });

          btnGroup.appendChild(btn);
        });

        optWrap.appendChild(btnGroup);
      });

      responseBox.appendChild(optWrap);
    }

    if (data.stage === "awaiting_collection" && Array.isArray(data.collection_candidates) && data.collection_candidates.length > 0) {
      const candidateWrap = document.createElement("div");
      candidateWrap.className = "ai-suggestion-chips";
      candidateWrap.style.marginTop = "8px";
      data.collection_candidates.forEach(cand => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ai-chip";
        btn.textContent = cand.title || cand.handle;
        btn.addEventListener("click", () => {
          _processQuery({
            intent: "select_collection_candidate",
            collection_id: cand.id,
            collection_handle: cand.handle,
            collection_title: cand.title,
            conversation_id: getSessionId()
          }, rootNode);
        });
        candidateWrap.appendChild(btn);
      });
      responseBox.appendChild(candidateWrap);
    }

    if (data.auto_checkout && !data.added_variant_id) {
      window.location.href = "/checkout";
      return;
    }

    if (data.clear_cart && !data.requires_client_action) {
      (async () => {
        try {
          await fetch("/cart/clear.js", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
          });
          const freshCart = await nativeCartAdapter.getCart();
          await notifyCartUpdate(freshCart, data.answer);
        } catch (e) {
          console.error("[AI Assistant] Native cart clear error:", e);
        }
      })();
    }

    // ── v2.1.12: Skip legacy variant cart paths when this is an ACK response.
    // The cart action was already executed by the client-cart-action block above.
    if (data.removed_variant_id && !data._from_ack && !data.requires_client_action) {
      (async () => {
        try {
          const rawId = String(data.removed_variant_id);
          const numericId = rawId.includes("/")
            ? parseInt(rawId.split("/").pop(), 10)
            : parseInt(rawId, 10);
          if (!numericId || isNaN(numericId)) return;

          await fetch("/cart/change.js", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: String(numericId), quantity: 0 }),
          });
          const freshCart = await nativeCartAdapter.getCart();
          await notifyCartUpdate(freshCart, data.answer);
        } catch (e) {
          console.error("[AI Assistant] Native cart remove error:", e);
        }
      })();
    }

    if (data.updated_variant_id && typeof data.updated_quantity === "number" && !data._from_ack && !data.requires_client_action) {
      (async () => {
        try {
          const rawId = String(data.updated_variant_id);
          const numericId = rawId.includes("/")
            ? parseInt(rawId.split("/").pop(), 10)
            : parseInt(rawId, 10);
          if (!numericId || isNaN(numericId)) return;

          await fetch("/cart/change.js", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: String(numericId), quantity: data.updated_quantity }),
          });
          const freshCart = await nativeCartAdapter.getCart();
          await notifyCartUpdate(freshCart, data.answer);
          
          if (data.auto_checkout) {
            window.location.href = "/checkout";
          }
        } catch (e) {
          console.error("[AI Assistant] Native cart quantity update error:", e);
        }
      })();
    }

    if (data.added_variant_id && !data._from_ack && !data.requires_client_action) {
      (async () => {
        try {
          const rawId = String(data.added_variant_id);
          const numericId = rawId.includes("/")
            ? parseInt(rawId.split("/").pop(), 10)
            : parseInt(rawId, 10);
          if (!numericId || isNaN(numericId)) return;

          const addQty = typeof data.added_quantity === "number" ? data.added_quantity : 1;

          await fetch("/cart/add.js", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items: [{ id: numericId, quantity: addQty }] }),
          });
          const freshCart = await nativeCartAdapter.getCart();
          await notifyCartUpdate(freshCart, data.answer);
          
          if (data.auto_checkout) {
            window.location.href = "/checkout";
          }
        } catch (e) {
          console.error("[AI Assistant] Native cart add error:", e);
        }
      })();
    }


    // ── Storefront Navigation — if storefront_url is provided, navigate/filter FIRST
    // so the customer sees the filtered page and hears the response ONCE on the results page.
    const navUrl = data.storefront_url || data.navigation?.url;
    if (navUrl && typeof navUrl === "string") {
      const currentPath = window.location.pathname + window.location.search;
      if (navUrl !== currentPath && !navUrl.endsWith(currentPath)) {
        try {
          sessionStorage.setItem("ai_nav_restore", JSON.stringify({
            answer: data.answer || data.message || "",
            requires_input: data.requires_input,
            auto_listen: data.auto_listen,
            stage: data.stage,
            ts: Date.now(),
            speak: true
          }));
          if (status) status.textContent = "Filtering products…";
          window.location.href = navUrl;
          return;
        } catch (e) {
          console.error("[AI Assistant] Storefront navigation error:", e);
        }
      }
    }

    // ── No navigation required — speak response ONCE on current page
    await speakResponseAndWait(data, turnId, status, rootNode);

    if (turnId !== activeTurnId || panelManuallyClosed) return;

    await handlePostResponseBehavior(data, turnId, rootNode);
  }

  // ── v2.1.6 Response Lifecycle Decision Controllers ─────────────────────
  function markTurnFinalized(turnId) {
    if (typeof turnId === "number") {
      finalizedTurnId = turnId;
    }
  }

  function invalidateCompletedTurn(turnId) {
    if (typeof turnId === "number" && turnId === activeTurnId) {
      activeTurnId += 1;
    }
  }

  function clearAutoListenTimer() {
    if (autoListenTimer) {
      clearTimeout(autoListenTimer);
      autoListenTimer = null;
    }
  }

  function scheduleAutoListen(turnId, callback, delayMs = 200) {
    clearAutoListenTimer();
    autoListenTimer = setTrackedTimeout(() => {
      autoListenTimer = null;
      if (typeof callback === "function") {
        callback();
      }
    }, delayMs);
  }

  async function speakResponseAndWait(data, turnId, status, rootNode = document) {
    const text = data?.message || data?.answer || "";
    if (!text && !data?.audio) return;

    transitionAssistantState(AssistantState.SPEAKING, rootNode);
    isSpeaking = true;

    stopWakeRecognition({
      reason: "assistant-speaking",
      allowRestart: false
    });

    stopQueryRecognition();

    return new Promise((resolve) => {
      const wordsCount = text.split(/\s+/).length;
      const ttsTimeoutMs = Math.min(45000, Math.max(20000, Math.ceil((wordsCount / 1.5) * 1000) + 6000));

      let ttsTimeoutTimer = setTrackedTimeout(() => {
        console.warn(`[AI Assistant] TTS speech timeout (${ttsTimeoutMs / 1000}s limit reached). Cancelling audio.`);
        cancelAssistantTts("tts-timeout");
        if (settleActiveTts) {
          settleActiveTts({ status: "timeout" });
          settleActiveTts = null;
        }
      }, ttsTimeoutMs);

      settleActiveTts = (res) => {
        if (ttsTimeoutTimer) {
          clearTimeout(ttsTimeoutTimer);
          activeTimers.delete(ttsTimeoutTimer);
          ttsTimeoutTimer = null;
        }
        // ── FIX (v2.1.11): Clear isSpeaking BEFORE resolving outer promise
        // so that handlePostResponseBehavior's wake-restart guard sees isSpeaking=false.
        if (turnId === activeTurnId) {
          isSpeaking = false;
        }
        if (ttsAbortController) {
          ttsAbortController = null;
        }
        resolve(res);
      };

      ttsAbortController = new AbortController();

      speakAssistantResponse(data, turnId, status, rootNode)
        .then(() => {
          if (settleActiveTts) {
            settleActiveTts({ status: "completed" });
            settleActiveTts = null;
          }
        })
        .catch((err) => {
          if (settleActiveTts) {
            settleActiveTts({ status: "error", error: err });
            settleActiveTts = null;
          }
        })
        .finally(() => {
          if (ttsTimeoutTimer) {
            clearTimeout(ttsTimeoutTimer);
            activeTimers.delete(ttsTimeoutTimer);
            ttsTimeoutTimer = null;
          }
          // Safety fallback: ensure isSpeaking is cleared even on unexpected paths
          if (turnId === activeTurnId) {
            isSpeaking = false;
          }
          if (ttsAbortController) {
            ttsAbortController = null;
          }
        });
    });
  }

  // ── Intelligent Question & Stage Detection ──────────────────────────────
  const USER_INPUT_STAGES = new Set([
    "awaiting_product_clarification",
    "awaiting_collection",
    "awaiting_variant",
    "awaiting_variant_option",
    "awaiting_size",
    "awaiting_color",
    "awaiting_quantity",
    "awaiting_confirm",
    "awaiting_cart_confirm",
    "awaiting_cart_quantity",
    "awaiting_client_cart_action",
    "awaiting_cart_checkout",
    "awaiting_shipping_address"
  ]);

  function responseRequiresInput(data) {
    if (!data) return false;
    if (data.close_widget === true || data.auto_close === true || data.immediate_close === true) return false;
    if (data.requires_input === true) return true;
    if (data.auto_listen === true) return true;
    if (data.stage && USER_INPUT_STAGES.has(data.stage)) return true;

    const text = (data.message || data.answer || "").trim();
    if (text) {
      if (/\?\s*$/.test(text)) return true;
      if (/\b(which|would you|what size|what color|shall I|do you want|please select|please choose|prefer)\b/i.test(text)) return true;
    }

    return false;
  }

  async function handlePostResponseBehavior(data, turnId, rootNode = document) {
    if (turnId !== activeTurnId) return;

    // Clear processing flag before evaluating wake restart
    isProcessing = false;

    if (handsFreeEnabled && !microphoneHardError) {
      microphoneAccessConfirmed = true;
    }

    const shouldCloseWidget = data.close_widget === true || data.auto_close === true || data.immediate_close === true;
    const requiresInput = !shouldCloseWidget && responseRequiresInput(data);
    const conversationComplete = (data.conversation_complete === true || shouldCloseWidget) && !requiresInput;
    const autoClose = shouldCloseWidget || (conversationComplete && data.auto_close === true);

    if (shouldCloseWidget || (conversationComplete && autoClose)) {
      await completeAndCloseConversation(turnId, data.intent, rootNode);
      return;
    }

    if (requiresInput) {
      await prepareForFollowUpListening(turnId, rootNode);
      return;
    }

    if (handsFreeEnabled && microphoneAccessConfirmed && !microphoneHardError) {
      // ── Buffer delay (350ms) ensures browser microphone hardware releases after TTS speech
      // before re-arming SpeechRecognition.
      await delay(350);
      if (turnId !== activeTurnId) return;

      // Re-enable wake restart (was locked by stopWakeRecognition during speech)
      wakeRestartAllowed = true;

      const wakeStarted = await startWakeRecognitionSafely(rootNode);
      if (!wakeStarted && shouldRestartWakeRecognition()) {
        logWakeEvent("wake restart after response: startWakeRecognitionSafely returned false, scheduling retry");
        scheduleWakeRecognitionRestart(wakeSessionId, rootNode);
      }
    } else {
      transitionAssistantState(AssistantState.IDLE, rootNode);
    }
  }

  async function prepareForFollowUpListening(turnId, rootNode = document) {
    if (turnId !== activeTurnId) return;
    if (panelManuallyClosed) return;

    openAssistantPanel("follow-up");

    stopWakeRecognition({
      reason: "follow-up",
      allowRestart: false
    });

    cancelAssistantTts("follow-up-transition");

    await delay(200);

    if (turnId !== activeTurnId) return;
    if (panelManuallyClosed) return;

    const started = await startQueryRecognitionSafely({
      turnId,
      source: "assistant-follow-up"
    }, rootNode);

    transitionAssistantState(
      started || !microphoneHardError ? AssistantState.LISTENING : AssistantState.ERROR,
      rootNode
    );
  }

  async function completeAndCloseConversation(turnId, intent, rootNode = document) {
    if (turnId !== activeTurnId) return;

    stopQueryRecognition();
    cancelAssistantTts("auto-complete");
    clearAutoListenTimer();

    markTurnFinalized(turnId);

    closeAssistantPanel({
      source: intent === "cancel" ? "voice-close" : "auto-complete",
      cancelCurrentTurn: false
    });

    invalidateCompletedTurn(turnId);
  }

  // ── Query Execution Handler ────────────────────────────────────────
  async function _processQuery(query, rootNode = document) {
    const turnId = beginAssistantTurn();
    stopAllAudioPlayback();

    const responseBox = _q(".ai-assistant-response", rootNode);
    const status = _q(".ai-assistant-status", rootNode);

    const isObj = typeof query === "object" && query !== null;
    const msgStr = isObj ? (query.text || query.collection_title || query.collection_handle || "Select collection") : String(query);

    _hideSuggestionChips(rootNode);
    _setCustomerTranscription(`You said: “${msgStr}”`, rootNode);
    isProcessing = true;
    transitionAssistantState(AssistantState.PROCESSING, rootNode);

    const currentSeq = ++_reqSeqNum;

    try {
      const apiBase = getApiBaseUrl();
      const nativeCart = await getNativeShopifyCart();
      const sanitizedCart = sanitizeCartContext(nativeCart);
      const requestBody = isObj ? {
        message: msgStr,
        session_id: getSessionId(),
        cart_context: sanitizedCart,
        sequence_num: currentSeq,
        intent: query.intent,
        product_id: query.product_id,
        option_name: query.option_name,
        option_value: query.option_value,
        selection_revision: query.selection_revision,
        collection_id: query.collection_id,
        collection_handle: query.collection_handle,
      } : {
        message: msgStr,
        session_id: getSessionId(),
        cart_context: sanitizedCart,
        sequence_num: currentSeq,
      };

      const resp = await fetch(`${apiBase}/voice-text`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
        signal: requestAbortController?.signal
      });

      let data = null;
      let isRespOk = resp.ok;
      try { data = await resp.json(); } catch(e) {}
      if (data && data.success === false) {
        isRespOk = false;
      }

      if (!isRespOk) {
        const errData = data || null;
        const errCode = errData?.error?.code;
        const errMessage = errData?.error?.message;
        const statusCode = data?.status_code || resp.status;

        if (statusCode === 503 || errCode === "STOREFRONT_CONFIGURATION_MISSING" || errCode === "SHOPIFY_AUTH_FAILED" || errCode === "SHOP_NOT_INSTALLED") {
          throw new Error(errMessage || "The shopping assistant is not completely configured for this store. Please contact the store administrator.");
        } else if (statusCode === 403 || errCode === "INVALID_PROXY_SIGNATURE") {
          throw new Error("Assistant request validation failed. Please refresh the page.");
        } else {
          throw new Error(errMessage || "I couldn't reach the shopping service. Please try again.");
        }
      }
      if (turnId === activeTurnId) {
        isProcessing = false;
      }

      if (turnId !== activeTurnId || panelManuallyClosed) return;
      if (currentSeq < _latestHandledSeqNum) return;

      await renderApiResponse(data, responseBox, status, rootNode, turnId);
    } catch (err) {
      if (err.name === "AbortError" || turnId !== activeTurnId || panelManuallyClosed) {
        return;
      }
      console.error("[AI Assistant] Voice query processing error:", err);
      transitionAssistantState(AssistantState.ERROR, rootNode);
      if (status) status.textContent = err.message || "I couldn't reach the shopping service. Please try again.";

      await finalizeAssistantTurn({
        turnId,
        conversationComplete: true,
        requiresInput: false,
        autoListen: false
      }, rootNode);
    } finally {
      if (turnId === activeTurnId) {
        isProcessing = false;
      }
    }
  }

  // ===============================================================
  // WAKE WORD CONTROLLER (v2.1.5 SAFEGUARDS)
  // ===============================================================
  function shouldRestartWakeRecognition() {
    return (
      handsFreeEnabled &&
      microphoneAccessConfirmed &&
      wakeRestartAllowed &&
      !wakeSequenceRunning &&
      !queryRecognitionActive &&
      !isSpeaking &&
      !isProcessing &&
      !destroyed &&
      document.visibilityState === "visible"
    );
  }

  function clearWakeRestartTimer() {
    if (wakeRestartTimer) {
      clearTimeout(wakeRestartTimer);
      wakeRestartTimer = null;
    }
  }

  function stopWakeRecognition({ reason = "manual", allowRestart = false } = {}) {
    wakeRestartAllowed = allowRestart;
    wakeSessionId += 1;
    wakeRecognitionActive = false;

    clearWakeRestartTimer();

    try { wakeRecognition?.abort(); } catch (e) {}
    wakeRecognition = null;

    logWakeEvent("stopped", { reason, allowRestart });
  }

  function scheduleWakeRecognitionRestart(sessionId, wrapper) {
    if (!shouldRestartWakeRecognition()) return;

    const delayIndex = Math.min(wakeRestartAttempt, WAKE_RESTART_DELAYS.length - 1);
    const delayMs = WAKE_RESTART_DELAYS[delayIndex];
    wakeRestartAttempt += 1;

    logWakeEvent("restart scheduled", { attempt: wakeRestartAttempt, delayMs });

    clearWakeRestartTimer();
    wakeRestartTimer = setTrackedTimeout(() => {
      if (shouldRestartWakeRecognition()) {
        startWakeRecognitionSafely(wrapper);
      }
    }, delayMs);
  }

  async function startWakeRecognitionSafely(wrapperNode) {
    const wrapper = wrapperNode || getCanonicalRoot() || document;
    if (!handsFreeEnabled || !microphoneAccessConfirmed || destroyed) return false;
    if (wakeRecognitionActive) return true;
    if (queryRecognitionActive || isSpeaking || isProcessing) return false;

    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      _setStatus("Hands-free wake listening isn’t available in this browser. Tap the microphone to speak.", wrapper);
      return false;
    }

    const sessionId = ++wakeSessionId;
    wakeRestartAllowed = true;
    logWakeEvent("recognition start requested", { session: sessionId });

    try {
      if (wakeRecognition) {
        try { wakeRecognition.abort(); } catch (e) {}
        wakeRecognition = null;
      }

      const recognition = new Recognition();
      wakeRecognition = recognition;
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = document.documentElement.lang || "en-US";

      return new Promise((resolve) => {
        let settled = false;

        const startTimeout = setTrackedTimeout(() => {
          if (settled) return;
          settled = true;
          wakeRecognitionActive = false;
          logWakeEvent("recognition start timeout", { session: sessionId });
          resolve(false);
        }, 2000);

        recognition.onstart = () => {
          if (sessionId !== wakeSessionId) return;

          clearTimeout(startTimeout);
          settled = true;
          wakeRecognitionActive = true;
          wakeRestartAttempt = 0;
          logWakeEvent("recognition started", { session: sessionId });

          transitionAssistantState(AssistantState.WAKE_READY, wrapper);

          // Reset retry attempt count only after remaining active continuously for 5 seconds
          clearTimeout(stabilityTimer);
          stabilityTimer = setTrackedTimeout(() => {
            if (sessionId === wakeSessionId && wakeRecognitionActive) {
              wakeRestartAttempt = 0;
              logWakeEvent("recognition stability confirmed (5s)");
            }
          }, 5000);

          resolve(true);
        };

        recognition.onresult = (event) => {
          if (sessionId !== wakeSessionId) return;
          if (!handsFreeEnabled || !wakeRecognitionActive) return;

          handleWakeRecognitionResult(event, sessionId, wrapper);
        };

        recognition.onerror = (event) => {
          if (sessionId !== wakeSessionId) return;
          logWakeEvent("recognition error", { session: sessionId, error: event?.error });

          handleWakeRecognitionError(event, sessionId, wrapper);

          if (!settled) {
            clearTimeout(startTimeout);
            settled = true;
            wakeRecognitionActive = false;
            resolve(false);
          }
        };

        recognition.onend = () => {
          if (sessionId !== wakeSessionId) return;

          wakeRecognitionActive = false;
          logWakeEvent("recognition ended", { session: sessionId });

          if (shouldRestartWakeRecognition()) {
            scheduleWakeRecognitionRestart(sessionId, wrapper);
          }
        };

        try {
          recognition.start();
        } catch (error) {
          clearTimeout(startTimeout);
          wakeRecognitionActive = false;
          logWakeEvent("start exception", error);
          if (!settled) {
            settled = true;
            resolve(false);
          }
        }
      });
    } catch (err) {
      wakeRecognitionActive = false;
      logWakeEvent("start outer exception", err);
      return false;
    }
  }

  function handleWakeRecognitionResult(event, sessionId, wrapper) {
    if (wakeSequenceRunning || Date.now() - lastWakeActivationAt < WAKE_ACTIVATION_COOLDOWN_MS) return;

    let transcript = "";
    for (let index = event.resultIndex; index < event.results.length; index++) {
      transcript += ` ${event.results[index][0]?.transcript || ""}`;
    }

    if (!containsWakePhrase(transcript)) return;

    wakeRestartAttempt = 0;
    logWakeEvent("phrase detected", { session: sessionId });
    beginWakeActivation(sessionId, wrapper);
  }

  function handleWakeRecognitionError(event, sessionId, wrapper) {
    const errName = event?.error || "";

    if (errName === "not-allowed" || errName === "service-not-allowed" || errName === "permission-denied") {
      microphoneAccessConfirmed = false;
      microphonePermission = "denied";
      microphoneHardError = "NotAllowedError";
      stopWakeRecognition({ reason: "permission-denied", allowRestart: false });
      transitionAssistantState(AssistantState.ERROR, wrapper);
      handleMicError({ name: "NotAllowedError" }, wrapper);
      return;
    }

    if (errName === "audio-capture" || errName === "no-speech" || errName === "aborted" || errName === "network") {
      logWakeEvent("interruption error", errName);
      if (shouldRestartWakeRecognition()) {
        scheduleWakeRecognitionRestart(sessionId, wrapper);
      }
      return;
    }
  }

  async function beginWakeActivation(detectedSessionId, wrapperNode) {
    if (wakeSequenceRunning) return;
    if (detectedSessionId !== wakeSessionId) return;
    if (!handsFreeEnabled) return;

    lastWakeActivationAt = Date.now();
    wakeSequenceRunning = true;

    logWakeEvent("background recognition stopped", { reason: "wake-detected" });
    stopWakeRecognition({ reason: "wake-detected", allowRestart: false });

    const root = wrapperNode || getCanonicalRoot() || document;

    try {
      openAssistantPanel("wake-word");
      transitionAssistantState(AssistantState.WAKING, root);

      await playSmoothWakeChimeWithTimeout();

      // 175ms delay to prevent chime bleeding into query transcription
      await delay(175);

      if (!handsFreeEnabled || panelManuallyClosed || destroyed) return;

      const turnId = beginAssistantTurn();
      const started = await startQueryRecognitionSafely({ turnId }, root);
      if (started) {
        logWakeEvent("query listener started");
        transitionAssistantState(AssistantState.LISTENING, root);
      } else {
        transitionAssistantState(AssistantState.ERROR, root);
      }
    } catch (error) {
      logWakeEvent("wake activation error", error);
      if (handsFreeEnabled && !panelManuallyClosed && !destroyed) {
        const turnId = beginAssistantTurn();
        await startQueryRecognitionSafely({ turnId }, root);
      }
    } finally {
      wakeSequenceRunning = false;
    }
  }

  function stopQueryRecognition() {
    queryRecognitionActive = false;
    if (queryRecognition) {
      try { queryRecognition.abort(); } catch (e) {}
      queryRecognition = null;
    }
  }

  async function startQueryRecognitionSafely(options = {}, wrapperNode) {
    const { turnId } = options;
    const root = wrapperNode || getCanonicalRoot() || document;

    const _SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!_SR) {
      _setStatus("Speech recognition not supported in this browser.", root);
      return false;
    }

    stopWakeRecognition({ reason: "query-started", allowRestart: false });
    stopQueryRecognition();

    stopAllAudioPlayback();
    queryRecognitionActive = true;
    transitionAssistantState(AssistantState.LISTENING, root);

    let finalText = "";
    let interimText = "";

    const maxTimer = setTrackedTimeout(() => {
      if (queryRecognition && turnId === activeTurnId) {
        try { queryRecognition.abort(); } catch(e) {}
      }
    }, 12000);

    let silenceTimer = null;

    try {
      const rec = new _SR();
      queryRecognition = rec;
      rec.continuous = true;
      rec.interimResults = true;
      rec.lang = document.documentElement.lang || "en-US";

      rec.onresult = (event) => {
        if (turnId !== activeTurnId || panelManuallyClosed) return;

        clearTimeout(silenceTimer);
        let newFinal = "";
        let currentInterim = "";
        for (let i = 0; i < event.results.length; i++) {
          if (event.results[i].isFinal) {
            newFinal += " " + event.results[i][0].transcript;
          } else {
            currentInterim += " " + event.results[i][0].transcript;
          }
        }
        finalText = newFinal.trim();
        interimText = currentInterim.trim();

        const currentDisplay = (finalText ? finalText + " " + interimText : interimText).replace(/\s+/g, " ").trim();
        _hideSuggestionChips(root);
        _setStatus("Listening…", root);
        if (currentDisplay) {
          _setCustomerTranscription(`“${currentDisplay}”`, root);
        } else {
          _clearCustomerTranscription(root);
        }

        silenceTimer = setTrackedTimeout(() => {
          if (queryRecognition === rec && turnId === activeTurnId) {
            try { rec.stop(); } catch(e) {}
          }
        }, 1400);
      };

      rec.onend = () => {
        clearTimeout(silenceTimer);
        clearTimeout(maxTimer);
        queryRecognitionActive = false;
        queryRecognition = null;

        if (turnId !== activeTurnId || panelManuallyClosed) return;

        const query = (finalText || interimText).replace(/\s+/g, " ").trim();
        if (query.length > 1) {
          isProcessing = true;
          transitionAssistantState(AssistantState.PROCESSING, root);
          _processQuery(query, root);
        } else {
          handleNoQuery(turnId, root);
        }
      };

      rec.onerror = (e) => {
        clearTimeout(silenceTimer);
        clearTimeout(maxTimer);
        queryRecognitionActive = false;
        queryRecognition = null;

        if (turnId !== activeTurnId || panelManuallyClosed) return;

        if (e.error === "aborted" || e.error === "no-speech") {
          handleNoQuery(turnId, root);
          return;
        }
        handleMicError(e, root);
      };

      rec.start();
      microphoneAccessConfirmed = true;
      microphonePermission = "granted";
      microphoneHardError = null;
      return true;
    } catch(e) {
      queryRecognitionActive = false;
      handleMicError(e, root);
      return false;
    }
  }

  function handleNoQuery(turnId, root) {
    if (turnId !== activeTurnId || panelManuallyClosed) return;
    finalizeAssistantTurn({
      turnId,
      conversationComplete: true,
      requiresInput: false,
      autoListen: false
    }, root);
  }

  async function enableHandsFreeFromUserGesture(wrapperNode) {
    if (handsFreeEnableInFlight) return;
    handsFreeEnableInFlight = true;

    const wrapper = wrapperNode || getCanonicalRoot() || document;
    unlockAssistantAudio();

    try {
      logWakeEvent("hands-free enable clicked");

      if (navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function") {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        microphoneAccessConfirmed = true;
        microphonePermission = "granted";
        microphoneHardError = null;

        stream.getTracks().forEach(track => track.stop());
        logWakeEvent("microphone access confirmed");
      } else {
        microphoneAccessConfirmed = true;
      }

      handsFreeEnabled = true;
      try { localStorage.setItem("ai_hands_free_enabled", "true"); } catch (e) {}
      wakeRestartAttempt = 0;

      const started = await startWakeRecognitionSafely(wrapper);
      if (started) {
        transitionAssistantState(AssistantState.WAKE_READY, wrapper);
      } else {
        transitionAssistantState(AssistantState.ERROR, wrapper);
        _setStatus("Hands-free wake listening isn’t available. Tap the microphone to speak.", wrapper);
      }
    } catch (error) {
      logWakeEvent("microphone permission error", error);
      microphoneAccessConfirmed = false;
      handsFreeEnabled = false;
      try { localStorage.setItem("ai_hands_free_enabled", "false"); } catch (e) {}

      const checkbox = wrapper?.querySelector(".ai-ww-enable-btn");
      if (checkbox) checkbox.checked = false;

      handleMicError(error, wrapper);
    } finally {
      handsFreeEnableInFlight = false;
    }
  }

  function disableHandsFree(wrapperNode) {
    handsFreeEnabled = false;
    try { localStorage.setItem("ai_hands_free_enabled", "false"); } catch (e) {}
    stopWakeRecognition({ reason: "user-disabled", allowRestart: false });

    const wrapper = wrapperNode || getCanonicalRoot() || document;
    const checkbox = wrapper?.querySelector?.(".ai-ww-enable-btn");
    if (checkbox) checkbox.checked = false;

    transitionAssistantState(AssistantState.IDLE, wrapper);
  }

  const AiWakeWord = {
    enable(wrapper) {
      return enableHandsFreeFromUserGesture(wrapper);
    },
    disable(wrapper) {
      disableHandsFree(wrapper);
    },
    isEnabled() {
      return handsFreeEnabled;
    },
    stopWakeRec() {
      stopWakeRecognition({ reason: "api-call", allowRestart: false });
    },
    stopQueryRec() {
      stopQueryRecognition();
    },
    toggleDirectQuery(wrapper) {
      const root = wrapper || getCanonicalRoot() || document;
      openAssistantPanel("mic-button");

      if (queryRecognitionActive || queryRecognition) {
        cancelActiveTurn("mic-toggle-off");
        finalizeAssistantTurn({
          turnId: activeTurnId,
          conversationComplete: true,
          requiresInput: false,
          autoListen: false
        }, root);
        return;
      }

      const turnId = beginAssistantTurn();
      startQueryRecognitionSafely({ turnId }, root);
    },
    triggerDirectQuery(wrapper) {
      const root = wrapper || getCanonicalRoot() || document;
      openAssistantPanel("direct-trigger");

      const turnId = beginAssistantTurn();
      startQueryRecognitionSafely({ turnId }, root);
    },
    resumeWakeListening(wrapper) {
      const root = wrapper || getCanonicalRoot() || document;
      if (handsFreeEnabled && microphoneAccessConfirmed && !microphoneHardError) {
        return startWakeRecognitionSafely(root);
      } else {
        stopWakeRecognition({ reason: "resume-cancelled", allowRestart: false });
        transitionAssistantState(AssistantState.IDLE, root);
        return Promise.resolve(false);
      }
    },
    init(wrapper) {
      const checkbox = wrapper.querySelector(".ai-ww-enable-btn");
      if (checkbox) {
        checkbox.checked = handsFreeEnabled;

        checkbox.addEventListener("change", () => {
          unlockAssistantAudio();
          if (checkbox.checked) {
            void enableHandsFreeFromUserGesture(wrapper);
          } else {
            disableHandsFree(wrapper);
          }
        });
      }

      wrapper.querySelectorAll(".ai-chip").forEach(chip => {
        chip.addEventListener("click", async () => {
          unlockAssistantAudio();
          const query = chip.getAttribute("data-query");
          if (query) {
            openAssistantPanel("chip");
            _processQuery(query, wrapper);
          }
        });
      });

      if (handsFreeEnabled && !microphoneHardError) {
        microphoneAccessConfirmed = true;
        if (!wakeRecognitionActive && !queryRecognitionActive && !isSpeaking && !isProcessing) {
          startWakeRecognitionSafely(wrapper).then(started => {
            if (started) {
              transitionAssistantState(AssistantState.WAKE_READY, wrapper);
            }
          });
        }
      }
    }
  };

  window.AiWakeWord = AiWakeWord;

  // ===============================================================
  // RECONCILE-BASED INITIALIZATION & LIFECYCLE CONTROLLER
  // ===============================================================
  function getCanonicalRoot() {
    const roots = [...document.querySelectorAll("[data-ai-assistant-root]")];
    if (!roots.length) return null;
    return (
      roots.find(root => root.dataset.installationType === "app-embed") ||
      roots[0] ||
      null
    );
  }

  async function handleMicrophoneAction(rootNode = document) {
    unlockAssistantAudio();

    if (!panelOpen) {
      openAssistantPanel("mic-action");
      const turnId = beginAssistantTurn();
      const started = await startQueryRecognitionSafely({
        turnId,
        source: "mic-click"
      }, rootNode);
      transitionAssistantState(started || !microphoneHardError ? AssistantState.LISTENING : AssistantState.ERROR, rootNode);
      return;
    }

    if (isSpeaking) {
      cancelAssistantTts("mic-interrupt");
      await delay(100);
      const turnId = beginAssistantTurn();
      const started = await startQueryRecognitionSafely({
        turnId,
        source: "mic-speaking-interrupt"
      }, rootNode);
      transitionAssistantState(started || !microphoneHardError ? AssistantState.LISTENING : AssistantState.ERROR, rootNode);
      return;
    }

    if (queryRecognitionActive) {
      stopQueryRecognition();
      transitionAssistantState(AssistantState.PROCESSING, rootNode);
      return;
    }

    stopWakeRecognition({ reason: "mic-click", allowRestart: false });
    const turnId = beginAssistantTurn();
    const started = await startQueryRecognitionSafely({
      turnId,
      source: "mic-manual-start"
    }, rootNode);
    transitionAssistantState(started || !microphoneHardError ? AssistantState.LISTENING : AssistantState.ERROR, rootNode);
  }

  function initializeAssistant(root) {
    destroyed = false;
    listenerController = new AbortController();
    const signal = listenerController.signal;

    // Event Delegation for all widget buttons to support multiple widget injections
    document.addEventListener("click", (e) => {
      const dynamicRoot = e.target.closest(".ai-fab-wrapper") || root;

      // FAB button click (toggles panel)
      const fabBtn = e.target.closest(".ai-fab-btn");
      if (fabBtn && dynamicRoot.contains(fabBtn)) {
        e.preventDefault();
        e.stopPropagation();
        if (typeof panelOpen !== 'undefined' && panelOpen) {
          closeAssistantPanel({ source: "fab-click" });
        } else {
          openAssistantPanel("fab-click");
        }
        return;
      }

      // Panel Close button click
      const closeBtn = e.target.closest(".ai-panel-close");
      if (closeBtn && dynamicRoot.contains(closeBtn)) {
        e.preventDefault();
        e.stopPropagation();
        closeAssistantPanel({ source: "close-button" });
        return;
      }

      // Standardized microphone action for panel Orb
      const micBtn = e.target.closest(".ai-assistant-button");
      if (micBtn && dynamicRoot.contains(micBtn)) {
        e.preventDefault();
        e.stopPropagation();
        handleMicrophoneAction(dynamicRoot);
        return;
      }

      // Outside click close listener
      if (panelOpen) {
        const clickedInside = dynamicRoot.contains(e.target);
        if (!clickedInside) {
          closeAssistantPanel({ source: "outside-click" });
        }
      }
    }, { signal });

    // Keyboard (Escape key) close listener
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && panelOpen) {
        closeAssistantPanel({ source: "escape" });
      }
    }, { signal });



    // Tab Visibility & Page Lifecycle handlers
    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        logWakeEvent("tab hidden -> pause wake recognition");
        stopWakeRecognition({ reason: "page-hidden", allowRestart: false });
      } else if (document.visibilityState === "visible") {
        logWakeEvent("tab visible -> resume wake recognition");
        if (handsFreeEnabled && microphoneAccessConfirmed && !queryRecognitionActive && !isSpeaking && !isProcessing) {
          void startWakeRecognitionSafely(root);
        }
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange, { signal });

    const handlePageHide = () => {
      logWakeEvent("pagehide event");
      stopWakeRecognition({ reason: "pagehide", allowRestart: false });
    };
    window.addEventListener("pagehide", handlePageHide, { signal });

    const handlePageShow = (e) => {
      logWakeEvent("pageshow event", { persisted: e.persisted });
      if (e.persisted) {
        reconcileAssistant();
      }
    };
    window.addEventListener("pageshow", handlePageShow, { signal });

    AiWakeWord.init(root);

    return function destroy() {
      destroyWidget();
    };
  }

  function reconcileAssistant() {
    const root = getCanonicalRoot();
    const current = window.__AI_SHOPPING_ASSISTANT_INSTANCE__;

    if (!root) {
      current?.destroy?.();
      delete window.__AI_SHOPPING_ASSISTANT_INSTANCE__;
      return;
    }

    if (current?.root === root && current?.version === VERSION) {
      return;
    }

    current?.destroy?.();

    document.querySelectorAll("[data-ai-assistant-root]").forEach(candidate => {
      const active = candidate === root;
      candidate.hidden = !active;
      candidate.inert = !active;
      candidate.setAttribute("aria-hidden", String(!active));
      if (!active) {
        candidate.style.setProperty("display", "none", "important");
      } else {
        candidate.style.setProperty("display", "flex", "important");
      }
    });

    if (root.parentNode !== document.body) {
      document.body.appendChild(root);
    }
    root.setAttribute("data-teleported", "");
    root.style.setProperty("display", "flex", "important");

    window.__AI_SHOPPING_ASSISTANT_INSTANCE__ = {
      version: VERSION,
      root,
      destroy: initializeAssistant(root)
    };

    // ── Navigation restore: if we navigated here from a product search, re-open
    // the panel and re-speak the assistant's response on the new page.
    try {
      const raw = sessionStorage.getItem("ai_nav_restore");
      if (raw) {
        const navState = JSON.parse(raw);
        // Only restore if navigation happened within the last 10 seconds
        if (navState && navState.answer && (Date.now() - navState.ts) < 10000) {
          sessionStorage.removeItem("ai_nav_restore");
          // Give the page a moment to finish rendering before opening panel
          setTimeout(async () => {
            openAssistantPanel("nav-restore");
            const restoreRoot = getCanonicalRoot();
            if (!restoreRoot) return;

            // Render the saved message into the response box
            const responseBox = restoreRoot.querySelector(".ai-assistant-response");
            if (responseBox) {
              responseBox.innerHTML = "";
              const textEl = document.createElement("p");
              textEl.style.margin = "0";
              textEl.style.lineHeight = "1.5";
              textEl.innerHTML = navState.answer.replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>");
              responseBox.appendChild(textEl);

              // Show the response box if it was hidden
              responseBox.style.display = "block";
            }

            // Speak the response ONCE on the destination page (only if navState.speak !== false)
            const newTurnId = ++activeTurnId;
            const fakeData = { answer: navState.answer, audio: null };
            const status = restoreRoot.querySelector(".ai-assistant-status");
            if (navState.speak !== false) {
              transitionAssistantState(AssistantState.SPEAKING, restoreRoot);
              await speakResponseAndWait(fakeData, newTurnId, status, restoreRoot);
            }
            if (newTurnId === activeTurnId) {
              const reqInput = navState.requires_input ?? responseRequiresInput({ answer: navState.answer, stage: navState.stage });
              await handlePostResponseBehavior(
                {
                  answer: navState.answer,
                  stage: navState.stage,
                  requires_input: reqInput,
                  auto_listen: navState.auto_listen ?? reqInput,
                  conversation_complete: !reqInput,
                  auto_close: false
                },
                newTurnId,
                restoreRoot
              );
            }
          }, 400);
        } else if (raw) {
          // Stale entry — clean up
          sessionStorage.removeItem("ai_nav_restore");
        }
      }
    } catch (e) {
      // sessionStorage not available or parse error — silently ignore
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", reconcileAssistant, { once: true });
  } else {
    reconcileAssistant();
  }

  document.addEventListener("shopify:section:load", () => queueMicrotask(reconcileAssistant));
  document.addEventListener("shopify:section:unload", () => {
    destroyWidget();
    queueMicrotask(reconcileAssistant);
  });

  window.AiAssistant = {
    version: VERSION,
    openPanel: (wrapper) => {
      openAssistantPanel("api");
    },
    closePanel: (wrapper) => {
      closeAssistantPanel({ source: "api" });
    },
    togglePanel: (wrapper) => {
      if (panelOpen) {
        closeAssistantPanel({ source: "api" });
      } else {
        openAssistantPanel("api");
      }
    },
    triggerVoice: (wrapper) => {
      const root = wrapper || getCanonicalRoot();
      if (root) {
        unlockAssistantAudio();
        openAssistantPanel("api-voice");
        if (window.AiWakeWord) {
          window.AiWakeWord.toggleDirectQuery(root);
        }
      }
    },
    getLifecycles: () => ({
      version: VERSION,
      panelLifecycleId,
      activeTurnId,
      wakeSessionId,
      panelOpen,
      panelManuallyClosed,
      assistantState,
      handsFreeEnabled,
      microphoneAccessConfirmed,
      microphonePermission,
      microphoneHardError,
      wakeRecognitionActive,
      queryRecognitionActive,
      wakeSequenceRunning
    }),
    destroyWidget
  };
})();