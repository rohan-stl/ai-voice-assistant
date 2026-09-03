// ===============================================================
// API URL — uses Shopify App Proxy (relative, no hardcoded URL)
// Override: set window.AI_ASSISTANT_API_BASE before this script
// ===============================================================

function getApiBaseUrl() {
  if (window.AI_ASSISTANT_API_BASE) {
    return window.AI_ASSISTANT_API_BASE.trim().replace(/\/$/, "");
  }
  // Shopify App Proxy route — Shopify forwards this to the backend
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

// ── Fetch live Shopify cart state ─────────────────────────────────
async function getNativeShopifyCart() {
  try {
    const res = await fetch("/cart.js");
    if (res.ok) return await res.json();
  } catch (e) {}
  return null;
}



// ===============================================================
// SHARED: RENDER API RESPONSE (used by both voice + text input)
// ===============================================================


// Monotonic sequence counters for request ordering & stale response suppression
let _reqSeqNum = 0;
let _latestHandledSeqNum = 0;

// Global handle for currently playing response audio
let _currentPlayingAudio = null;

function stopAllAudioPlayback() {
  // 1. Stop HTML5 Audio playback immediately
  if (_currentPlayingAudio) {
    try {
      _currentPlayingAudio.pause();
      _currentPlayingAudio.currentTime = 0;
      _currentPlayingAudio.src = "";
    } catch (e) {}
    _currentPlayingAudio = null;
  }

  document.querySelectorAll("audio").forEach(a => {
    try {
      a.pause();
      a.currentTime = 0;
      a.src = "";
    } catch (e) {}
  });

  // 2. Stop Web Speech Synthesis immediately
  if ('speechSynthesis' in window) {
    try {
      window.speechSynthesis.cancel();
    } catch (e) {}
  }
}



// ── Browser TTS via Web Speech Synthesis ─────────────────
let _activeTtsCallback = null;

function speakTextWithBrowserSpeech(text, status, onEndCallback = null) {
  if (onEndCallback) _activeTtsCallback = onEndCallback;
  try {
    if (!('speechSynthesis' in window)) {
      if (onEndCallback) setTimeout(onEndCallback, 2500);
      return;
    }
    stopAllAudioPlayback();

    const cleanText = text
      .replace(/\*\*(.*?)\*\*/g, "$1")
      .replace(/\*(.*?)\*/g, "$1")
      .replace(/[✅❌⚠️🎉🔍🛒🎙️👋📦💬✋🤖👤]/g, "")
      .replace(/https?:\/\/\S+/g, "")
      .replace(/[\#\_]/g, "")
      .replace(/\s+/g, " ")
      .trim();

    if (!cleanText) {
      if (onEndCallback) onEndCallback();
      return;
    }

    const doSpeak = () => {
      const utt = new SpeechSynthesisUtterance(cleanText);
      utt.lang = "en-US";
      utt.rate = 1.0;
      utt.pitch = 1.0;
      utt.volume = 1.0;

      const voices = window.speechSynthesis.getVoices();
      const preferred = voices.find(v =>
        v.lang.startsWith("en") &&
        (v.name.includes("Samantha") || v.name.includes("Daniel") ||
         v.name.includes("Karen") || v.name.includes("Moira") ||
         v.name.includes("Google US") || v.name.includes("Google UK") ||
         v.name.includes("Google") || v.name.includes("Natural"))
      ) || voices.find(v => v.lang === "en-US")
        || voices.find(v => v.lang.startsWith("en"));
      if (preferred) utt.voice = preferred;

      const finishTTS = () => {
        if (status) {
          status.textContent = isHandsFreeEnabled()
            ? 'Say "Hey Assistant" to start'
            : 'Tap the microphone to speak';
        }
        if (_activeTtsCallback) {
          const cb = _activeTtsCallback;
          _activeTtsCallback = null;
          try { cb(); } catch(e) {}
        }
      };

      if (status) {
        utt.onstart = () => { status.textContent = "Speaking response…"; };
      }
      utt.onend   = finishTTS;
      utt.onerror = finishTTS;

      window.speechSynthesis.speak(utt);
    };

    if (window.speechSynthesis.getVoices().length > 0) {
      doSpeak();
    } else {
      setTimeout(doSpeak, 250);
    }
  } catch (e) {
    console.warn("[AI Assistant] speakText error:", e);
    if (onEndCallback) setTimeout(onEndCallback, 2500);
  }
}

// Preload voices and unlock speechSynthesis on first user gesture
(function() {
  if (!('speechSynthesis' in window)) return;
  window.speechSynthesis.getVoices();
  window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();

  let _unlocked = false;
  const unlock = () => {
    if (_unlocked) return;
    _unlocked = true;
    const u = new SpeechSynthesisUtterance(" ");
    u.volume = 0; u.rate = 10;
    try { window.speechSynthesis.speak(u); } catch(e) {}
  };
  ["click","keydown","touchstart","pointerdown"].forEach(e =>
    document.addEventListener(e, unlock, { passive: true })
  );
})();


function isHandsFreeEnabled() {
  return !!(window.AiWakeWord && window.AiWakeWord.isEnabled && window.AiWakeWord.isEnabled());
}

function _q(sel) {
  if (typeof _wrapper !== "undefined" && _wrapper) return _wrapper.querySelector(sel);
  const wrapper = document.querySelector(".ai-fab-wrapper") || document.body;
  return wrapper ? wrapper.querySelector(sel) : null;
}

function _setCustomerTranscription(text) {
  const el = _q(".ai-customer-transcription");
  if (!el) return;
  if (text) {
    el.style.display = "block";
    el.textContent = text;
  } else {
    el.style.display = "none";
    el.textContent = "";
  }
}

function _clearCustomerTranscription() {
  _setCustomerTranscription(null);
}

function _hideSuggestionChips() {
  const chips = _q(".ai-suggestion-chips");
  if (chips) chips.style.display = "none";
}

function _showSuggestionChips() {
  const chips = _q(".ai-suggestion-chips");
  if (chips) chips.style.display = "flex";
}

function renderApiResponse(data, responseBox, status) {

  stopAllAudioPlayback();
  console.log("API response:", data);
  _clearCustomerTranscription();

  if (status) {
    status.textContent = isHandsFreeEnabled()
      ? 'Say "Hey Assistant" to start'
      : 'Tap the microphone to speak';
  }
  if (!responseBox) return;

  // Single dynamic response area — replace existing content cleanly
  responseBox.style.display = "flex";
  responseBox.style.flexDirection = "column";
  responseBox.innerHTML = "";

  const stage = data.stage || "browse";

  if (stage === "idle" || stage === "standby") {
    _showSuggestionChips();
  } else {
    _hideSuggestionChips();
  }

  // ── Latest Assistant Response Text (XSS-safe textContent) ───────
  const latestResponse = document.createElement("div");
  latestResponse.className = "ai-latest-response-text";
  latestResponse.textContent = data.answer || "No response received.";
  responseBox.appendChild(latestResponse);

  // ── Structured Confirmation Summary Card ────────────────────────
  if ((stage === "awaiting_confirm" || stage === "awaiting_cart_confirm") && (data.product_title || data.selected_product)) {
    const card = document.createElement("div");
    card.className = "ai-confirmation-card";

    const content = document.createElement("div");
    content.className = "ai-card-content";

    const prod = data.selected_product;
    if (prod && prod.images && prod.images.nodes && prod.images.nodes[0]) {
      const img = document.createElement("img");
      img.className = "ai-card-thumb";
      img.src = prod.images.nodes[0].url;
      img.alt = data.product_title || "Product";
      content.appendChild(img);
    }

    const details = document.createElement("div");
    details.className = "ai-card-details";

    const title = document.createElement("div");
    title.className = "ai-card-title";
    title.textContent = data.product_title || (prod ? prod.title : "Product");

    const opts = document.createElement("div");
    opts.className = "ai-card-options";
    const optsList = [];
    if (data.selected_options) {
      for (const [k, v] of Object.entries(data.selected_options)) {
        if (v) optsList.push(`${v}`);
      }
    }
    if (data.quantity) optsList.push(`Qty: ${data.quantity}`);
    opts.textContent = optsList.join(" · ");

    details.appendChild(title);
    details.appendChild(opts);
    content.appendChild(details);
    card.appendChild(content);

    const actions = document.createElement("div");
    actions.className = "ai-card-actions";

    const confirmBtn = document.createElement("button");
    confirmBtn.type = "button";
    confirmBtn.className = "ai-btn-confirm";
    confirmBtn.textContent = "Confirm Add to Cart";
    confirmBtn.addEventListener("click", () => {
      confirmBtn.disabled = true;
      confirmBtn.textContent = "Adding...";
      _processQuery("Yes");
    });

    const changeBtn = document.createElement("button");
    changeBtn.type = "button";
    changeBtn.className = "ai-btn-change";
    changeBtn.textContent = "Change";
    changeBtn.addEventListener("click", () => {
      _processQuery("Change options");
    });

    actions.appendChild(confirmBtn);
    actions.appendChild(changeBtn);
    card.appendChild(actions);

    responseBox.appendChild(card);
  }

  if (
    data.navigation &&
    data.navigation.requested === true &&
    typeof data.navigation.url === "string" &&
    /^\/collections\/[a-z0-9][a-z0-9-]*(\?.*)?$/i.test(data.navigation.url)
  ) {
    window.location.assign(data.navigation.url);
    return;
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
        });
      });
      candidateWrap.appendChild(btn);
    });
    responseBox.appendChild(candidateWrap);
  }

  if (data.auto_checkout && !data.added_variant_id) {
    window.location.href = "/checkout";
    return;
  }

  // =============================================================
  // CART SUCCESS — show card + sync to native Shopify theme cart
  // =============================================================
  if (data.added_variant_id) {
    (async () => {
      try {
        const rawId = String(data.added_variant_id);
        const numericId = rawId.includes("/")
          ? parseInt(rawId.split("/").pop(), 10)
          : parseInt(rawId, 10);
        if (!numericId || isNaN(numericId)) return;

        const addQty = typeof data.added_quantity === "number" ? data.added_quantity : 1;

        const formData = new FormData();
        formData.append("id", numericId);
        formData.append("quantity", addQty);
        formData.append("sections", "cart-drawer,cart-icon-bubble");

        let addResp = await fetch("/cart/add.js", {
          method: "POST",
          body: formData,
          headers: { "X-Requested-With": "XMLHttpRequest" },
        });

        if (!addResp.ok) {
          const formFallback = new FormData();
          formFallback.append("id", numericId);
          formFallback.append("quantity", addQty);
          addResp = await fetch("/cart/add.js", {
            method: "POST",
            body: formFallback,
            headers: { "X-Requested-With": "XMLHttpRequest" },
          });
        }

        if (!addResp.ok) {
          console.warn("[AI Assistant] Theme cart sync note:", addResp.status);
          return;
        }

        const addData = await addResp.json();

        if (addData.sections) {
          if (addData.sections["cart-drawer"]) {
            const drawer = document.querySelector("cart-drawer");
            if (drawer) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(addData.sections["cart-drawer"], "text/html");
              const newDrawer = parsedHtml.querySelector("cart-drawer");
              if (newDrawer) drawer.innerHTML = newDrawer.innerHTML;

              if (data.auto_checkout) {
                // Instantly navigate to checkout without showing cart drawer or waiting
                window.location.href = "/checkout";
                return;
              } else {
                drawer.classList.remove("is-empty");
                drawer.classList.add("active");
                if (typeof drawer.open === "function") drawer.open();
              }
            }
          }

          if (addData.sections["cart-icon-bubble"]) {
            const bubble = document.querySelector("#cart-icon-bubble");
            if (bubble) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(addData.sections["cart-icon-bubble"], "text/html");
              const newBubble = parsedHtml.querySelector("#cart-icon-bubble");
              bubble.innerHTML = newBubble ? newBubble.innerHTML : addData.sections["cart-icon-bubble"];
            }
          }
        }

        const cartResp = await fetch("/cart.js");
        if (cartResp.ok) {
          const nativeCart = await cartResp.json();
          const count = nativeCart.item_count || 0;
          document.querySelectorAll(
            ".cart-count-bubble, .cart__count, [data-cart-count], .header__icon--cart .badge, .cart-count"
          ).forEach(el => {
            el.textContent = count;
            if (count > 0) el.removeAttribute("hidden");
          });

          document.dispatchEvent(new CustomEvent("cart:refresh", { bubbles: true }));
          document.dispatchEvent(new CustomEvent("cart:updated", { bubbles: true }));
        }
      } catch (err) {}
    })();
  }

  // =============================================================
  // CART QUANTITY UPDATE — sync quantity update to native Shopify theme cart
  // =============================================================
  if (data.updated_variant_id && !data.added_variant_id && !data.removed_variant_id) {
    (async () => {
      try {
        const rawId = String(data.updated_variant_id);
        const numericId = rawId.includes("/")
          ? parseInt(rawId.split("/").pop(), 10)
          : parseInt(rawId, 10);

        if (!numericId || isNaN(numericId)) return;

        const targetQty = typeof data.updated_quantity === "number" ? data.updated_quantity : 1;

        const updateResp = await fetch("/cart/change.js", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
          body: JSON.stringify({
            id: String(numericId),
            quantity: targetQty,
            sections: "cart-drawer,cart-icon-bubble"
          }),
        });

        if (!updateResp.ok) return;

        const updateData = await updateResp.json();

        if (updateData.sections) {
          if (updateData.sections["cart-drawer"]) {
            const drawer = document.querySelector("cart-drawer");
            if (drawer) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(updateData.sections["cart-drawer"], "text/html");
              const newDrawer = parsedHtml.querySelector("cart-drawer");
              if (newDrawer) drawer.innerHTML = newDrawer.innerHTML;
              if (updateData.item_count === 0) drawer.classList.add("is-empty");
              else drawer.classList.remove("is-empty");
              drawer.classList.add("active");
              if (typeof drawer.open === "function") drawer.open();
            }
          }

          if (updateData.sections["cart-icon-bubble"]) {
            const bubble = document.querySelector("#cart-icon-bubble");
            if (bubble) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(updateData.sections["cart-icon-bubble"], "text/html");
              const newBubble = parsedHtml.querySelector("#cart-icon-bubble");
              bubble.innerHTML = newBubble ? newBubble.innerHTML : updateData.sections["cart-icon-bubble"];
            }
          }
        }

        const cartResp = await fetch("/cart.js");
        if (cartResp.ok) {
          const nativeCart = await cartResp.json();
          const count = nativeCart.item_count || 0;
          document.querySelectorAll(
            ".cart-count-bubble, .cart__count, [data-cart-count], .header__icon--cart .badge, .cart-count"
          ).forEach(el => {
            el.textContent = count;
            if (count > 0) el.removeAttribute("hidden");
          });

          document.dispatchEvent(new CustomEvent("cart:refresh", { bubbles: true }));
          document.dispatchEvent(new CustomEvent("cart:updated", { bubbles: true }));
        }
      } catch (err) {}
    })();
  }

  // =============================================================
  // CART REMOVAL — sync removal to native Shopify theme cart
  // =============================================================
  if (data.removed_variant_id) {
    (async () => {
      try {
        const rawId = String(data.removed_variant_id);
        const numericId = rawId.includes("/")
          ? parseInt(rawId.split("/").pop(), 10)
          : parseInt(rawId, 10);

        if (!numericId || isNaN(numericId)) return;

        const targetQty = typeof data.updated_quantity === "number" ? data.updated_quantity : 0;

        const removeResp = await fetch("/cart/change.js", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
          body: JSON.stringify({
            id: String(numericId),
            quantity: targetQty,
            sections: "cart-drawer,cart-icon-bubble"
          }),
        });

        if (!removeResp.ok) return;

        const removeData = await removeResp.json();

        if (removeData.sections) {
          if (removeData.sections["cart-drawer"]) {
            const drawer = document.querySelector("cart-drawer");
            if (drawer) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(removeData.sections["cart-drawer"], "text/html");
              const newDrawer = parsedHtml.querySelector("cart-drawer");
              if (newDrawer) drawer.innerHTML = newDrawer.innerHTML;
              if (removeData.item_count === 0) drawer.classList.add("is-empty");
              drawer.classList.add("active");
              if (typeof drawer.open === "function") drawer.open();
            }
          }

          if (removeData.sections["cart-icon-bubble"]) {
            const bubble = document.querySelector("#cart-icon-bubble");
            if (bubble) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(removeData.sections["cart-icon-bubble"], "text/html");
              const newBubble = parsedHtml.querySelector("#cart-icon-bubble");
              bubble.innerHTML = newBubble ? newBubble.innerHTML : removeData.sections["cart-icon-bubble"];
            }
          }
        }

        const cartResp = await fetch("/cart.js");
        if (cartResp.ok) {
          const nativeCart = await cartResp.json();
          const count = nativeCart.item_count || 0;
          document.querySelectorAll(
            ".cart-count-bubble, .cart__count, [data-cart-count], .header__icon--cart .badge, .cart-count"
          ).forEach(el => {
            el.textContent = count;
            if (count === 0) el.setAttribute("hidden", "true");
          });

          document.dispatchEvent(new CustomEvent("cart:refresh", { bubbles: true }));
          document.dispatchEvent(new CustomEvent("cart:updated", { bubbles: true }));
        }
      } catch (err) {}
    })();
  }

  // =============================================================
  // CART CLEAR — empty all items in native Shopify theme cart
  // =============================================================
  if (data.clear_cart) {
    (async () => {
      try {
        const clearResp = await fetch("/cart/clear.js", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
          body: JSON.stringify({ sections: "cart-drawer,cart-icon-bubble" }),
        });

        const clearData = clearResp.ok ? await clearResp.json() : {};

        if (clearData.sections) {
          if (clearData.sections["cart-drawer"]) {
            const drawer = document.querySelector("cart-drawer");
            if (drawer) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(clearData.sections["cart-drawer"], "text/html");
              const newDrawer = parsedHtml.querySelector("cart-drawer");
              if (newDrawer) drawer.innerHTML = newDrawer.innerHTML;
              drawer.classList.add("is-empty");
              drawer.classList.add("active");
              if (typeof drawer.open === "function") drawer.open();
            }
          }

          if (clearData.sections["cart-icon-bubble"]) {
            const bubble = document.querySelector("#cart-icon-bubble");
            if (bubble) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(clearData.sections["cart-icon-bubble"], "text/html");
              const newBubble = parsedHtml.querySelector("#cart-icon-bubble");
              bubble.innerHTML = newBubble ? newBubble.innerHTML : clearData.sections["cart-icon-bubble"];
            }
          }
        }

        document.querySelectorAll(
          ".cart-count-bubble, .cart__count, [data-cart-count], .header__icon--cart .badge, .cart-count"
        ).forEach(el => {
          el.textContent = "0";
          el.setAttribute("hidden", "true");
        });

        document.dispatchEvent(new CustomEvent("cart:refresh", { bubbles: true }));
        document.dispatchEvent(new CustomEvent("cart:updated", { bubbles: true }));
      } catch (err) {}
    })();
  }


  // =============================================================
  // DYNAMIC PAGE TRANSITION (SPA mode) — prevents full page reloads
  // =============================================================
  let isDifferentUrl = false;
  if (data.storefront_url) {
    try {
      const targetUrl = new URL(data.storefront_url, window.location.origin);
      const currentUrl = new URL(window.location.href);
      isDifferentUrl = (targetUrl.pathname + targetUrl.search) !== (currentUrl.pathname + currentUrl.search);
    } catch (e) {
      isDifferentUrl = true;
    }
  }

  // If navigating to a new page (e.g. Home -> Collection), swap content dynamically via SPA navigation
  if (data.storefront_url && isDifferentUrl && !data.cart && !data.auto_checkout) {
    try {
      history.pushState(null, "", data.storefront_url);
      fetch(data.storefront_url)
        .then(res => res.text())
        .then(html => {
          const parser = new DOMParser();
          const doc = parser.parseFromString(html, "text/html");
          const mainContent = doc.querySelector("main") || doc.querySelector("#MainContent") || doc.querySelector(".main-content") || doc.querySelector("#product-grid");
          const currentMain = document.querySelector("main") || document.querySelector("#MainContent") || document.querySelector(".main-content") || document.querySelector("#product-grid");
          if (mainContent && currentMain) {
            currentMain.innerHTML = mainContent.innerHTML;
          }
        }).catch(e => {});
    } catch (e) {}
  }




  if (data.audio) {
    try {
      const audioBytes = atob(data.audio);
      const arr = new Uint8Array(audioBytes.length);
      for (let i = 0; i < audioBytes.length; i++) arr[i] = audioBytes.charCodeAt(i);
      const blob = new Blob([arr], { type: "audio/mpeg" });
      const blobUrl = URL.createObjectURL(blob);
      const audio   = new Audio(blobUrl);
      audio.volume  = 1.0;
      _currentPlayingAudio = audio;

      audio.onplay  = () => { status.textContent = "Playing response…"; };
      audio.onended = () => {
        URL.revokeObjectURL(blobUrl);
        _currentPlayingAudio = null;
        status.textContent = "Tap the microphone to speak";
      };
      audio.onerror = () => {
        URL.revokeObjectURL(blobUrl);
        _currentPlayingAudio = null;
        speakTextWithBrowserSpeech(data.answer, status);
      };

      const playPromise = audio.play();
      if (playPromise !== undefined) {
        playPromise.then(() => {
          status.textContent = "Playing response…";
        }).catch(e => {
          console.warn("[AI Assistant] Autoplay policy prevented immediate playback, falling back to speech synthesis:", e.message);
          speakTextWithBrowserSpeech(data.answer, status);

          const unlockAndPlay = () => {
            stopAllAudioPlayback();
            audio.play().catch(err => speakTextWithBrowserSpeech(data.answer, status));

            ["pointerdown", "touchstart", "click", "keydown", "mousemove"].forEach(evtName => {
              document.removeEventListener(evtName, unlockAndPlay);
            });
          };

          ["pointerdown", "touchstart", "click", "keydown", "mousemove"].forEach(evtName => {
            document.addEventListener(evtName, unlockAndPlay, { once: true });
          });
        });
      }
    } catch (e) {
      speakTextWithBrowserSpeech(data.answer, status);
    }
  } else {
    speakTextWithBrowserSpeech(data.answer, status);
  }


} // end renderApiResponse



// ===============================================================
// MIC / VOICE FLOW  — robust init for Shopify theme sections
// ===============================================================

function initAiAssistantButton(button) {

  if (button._aiInitialised) return;
  button._aiInitialised = true;

  let mediaRecorder = null;
  let audioChunks   = [];
  let stream        = null;
  let recognition   = null;
  let speechTranscript = "";

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  // Intercept touchstart & mousedown for instant 0ms audio interruption on physical press
  ["touchstart", "mousedown", "pointerdown"].forEach(evtName => {
    button.addEventListener(evtName, stopAllAudioPlayback, { passive: true });
  });

  button.addEventListener("click", async () => {
    // ── Instantly interrupt any playing response audio when mic is clicked ──
    stopAllAudioPlayback();

    // Support both old inline layout and new floating panel layout
    const container   = button.closest(".ai-shopping-assistant") || button.closest(".ai-fab-wrapper");
    const status      = container ? container.querySelector(".ai-assistant-status") : document.querySelector(".ai-assistant-status");
    const responseBox = container ? container.querySelector(".ai-assistant-response") : document.querySelector(".ai-assistant-response");

    // =============================================================
    // STOP RECORDING (second tap)
    // =============================================================
    if (mediaRecorder && mediaRecorder.state === "recording") {
      if (recognition) {
        try { recognition.stop(); } catch (e) {}
      }
      mediaRecorder.stop();
      if (status) status.textContent = "Processing…";
      // Switch to processing state on both mic button and FAB
      button.style.background = "";
      button.classList.remove("ai-fab--listening");
      button.classList.add("ai-fab--processing");
      const fabBtn2 = container ? container.querySelector(".ai-fab-btn") : null;
      if (fabBtn2 && fabBtn2 !== button) {
        fabBtn2.classList.remove("ai-fab--listening");
        fabBtn2.classList.add("ai-fab--processing");
      }
      return;
    }

    // =============================================================
    // START RECORDING (first tap)
    // =============================================================

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      status.textContent = "⚠️ Microphone not supported on this browser/connection.";
      console.error("[AI Assistant] navigator.mediaDevices unavailable — page must be served over HTTPS.");
      return;
    }

    try {
      status.textContent = "Requesting microphone…";

      stream = await navigator.mediaDevices.getUserMedia({ audio: true });

      mediaRecorder = new MediaRecorder(stream);
      audioChunks   = [];
      speechTranscript = "";

      // Initialize Web Speech Recognition for instant 0ms STT
      if (SpeechRecognition) {
        try {
          recognition = new SpeechRecognition();
          recognition.continuous = false; // Auto-stop when user finishes speaking
          recognition.interimResults = true;
          recognition.lang = "en-US";

          recognition.onresult = (event) => {
            let current = "";
            for (let i = event.resultIndex; i < event.results.length; i++) {
              current += event.results[i][0].transcript;
            }
            if (current.trim()) {
              speechTranscript = current.trim();
              status.textContent = `🎙️ "${speechTranscript}"`;
            }
          };
          recognition.start();
        } catch (e) {
          console.warn("[AI Assistant] Web Speech Recognition init fallback:", e);
        }
      }

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunks.push(e.data);
      };

      // ===========================================================
      // AFTER RECORDING STOPS → send to API
      // ===========================================================
      mediaRecorder.onstop = async () => {
        status.textContent = "Processing response…";
        // Ensure processing state is shown
        button.classList.remove("ai-fab--listening");
        button.classList.add("ai-fab--processing");
        const fabBtnStop = container ? container.querySelector(".ai-fab-btn") : null;
        if (fabBtnStop && fabBtnStop !== button) {
          fabBtnStop.classList.remove("ai-fab--listening");
          fabBtnStop.classList.add("ai-fab--processing");
        }

        const resetFabState = () => {
          button.classList.remove("ai-fab--listening", "ai-fab--processing");
          button.style.background = "";
          if (fabBtnStop && fabBtnStop !== button) {
            fabBtnStop.classList.remove("ai-fab--listening", "ai-fab--processing");
          }
        };

        // Always stop recognition and release microphone tracks immediately
        if (recognition) {
          try { recognition.stop(); } catch (e) {}
          recognition = null;
        }
        if (stream) {
          try { stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
          stream = null;
        }

        const apiBase = getApiBaseUrl();

        const cleanTranscript = speechTranscript
          .replace(/\[.*?\]/g, "")
          .replace(/\(.*?\)/g, "")
          .replace(/^[.\s,!\-?]+|[.\s,!\-?]+$/g, "")
          .trim();


        // Fetch live native Shopify cart state
        const nativeCart = await getNativeShopifyCart();

        // ── Fast path: use instant in-browser transcription if captured ──
        if (cleanTranscript && cleanTranscript.length > 1) {
          console.log("[AI Assistant] ⚡ Fast path using browser STT:", cleanTranscript);
          try {
            const response = await fetch(`${apiBase}/voice-text`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                message: cleanTranscript,
                session_id: getSessionId(),
                cart_context: nativeCart,
              }),
            });

            if (response.ok) {
              const data = await response.json();
              resetFabState();
              renderApiResponse(data, responseBox, status);
              return;
            }
          } catch (err) {
            console.warn("[AI Assistant] Fast path /voice-text failed, falling back to audio upload:", err);
          }
        }

        // ── Fallback path: upload audio to /voice ─────────────────────────
        const audioBlob = new Blob(audioChunks, { type: mediaRecorder.mimeType || "audio/webm" });
        console.log("[AI Assistant] Audio upload fallback:", audioBlob.type, audioBlob.size, "bytes");

        if (audioBlob.size < 200) {
          status.textContent = "Recording too short — please try again.";
          return;
        }

        const formData = new FormData();
        formData.append("file", audioBlob, "voice.webm");
        formData.append("session_id", getSessionId());

        try {
          const response = await fetch(`${apiBase}/voice`, {
            method: "POST",
            body: formData,
          });

          if (!response.ok) throw new Error(`Server returned ${response.status}`);

          const data = await response.json();
          resetFabState();
          renderApiResponse(data, responseBox, status);

        } catch (err) {
          console.error("[AI Assistant] Voice API error:", err);
          resetFabState();
          status.textContent = `⚠️ Error: ${err.message}`;
        }
      };

      mediaRecorder.start(250);
      status.textContent = "🎙️ Listening… tap again to stop.";
      // Apply listening state via CSS class (no inline style override)
      button.style.background = "";
      button.classList.add("ai-fab--listening");
      const fabBtnListen = container ? container.querySelector(".ai-fab-btn") : null;
      if (fabBtnListen && fabBtnListen !== button) {
        fabBtnListen.classList.add("ai-fab--listening");
      }

    } catch (err) {
      console.error("[AI Assistant] Microphone error:", err.name, err.message);

      // Surface a human-readable error in the widget
      const messages = {
        NotAllowedError:  "🚫 Microphone permission denied. Please allow mic access in your browser settings and try again.",
        NotFoundError:    "🎤 No microphone found. Please connect a microphone and try again.",
        NotReadableError: "🎤 Microphone is in use by another app. Close other apps and try again.",
        SecurityError:    "🔒 Microphone blocked — page must be on HTTPS.",
        AbortError:       "Microphone request was cancelled. Please try again.",
      };
      status.textContent = messages[err.name] || `Microphone error: ${err.message}`;

      // Reset visual state on error
      button.classList.remove("ai-fab--listening", "ai-fab--processing");
      button.style.background = "";
      const fabBtnErr = container ? container.querySelector(".ai-fab-btn") : null;
      if (fabBtnErr && fabBtnErr !== button) {
        fabBtnErr.classList.remove("ai-fab--listening", "ai-fab--processing");
      }

      if (stream) {
        stream.getTracks().forEach((t) => t.stop());
        stream = null;
      }
      mediaRecorder = null;
    }
  });
}

// ---------------------------------------------------------------
// Check and auto-play pending speech response when landing on new page
// ---------------------------------------------------------------
function checkPendingSpeakResponse() {
  try {
    const raw = sessionStorage.getItem("ai_pending_speak_response");
    if (!raw) return;

    sessionStorage.removeItem("ai_pending_speak_response");
    const data = JSON.parse(raw);
    data._fromPendingRestore = true;

    setTimeout(() => {
      // Support both old inline widget and new floating panel
      const container   = document.querySelector(".ai-fab-wrapper") || document.querySelector(".ai-shopping-assistant");
      const status      = container ? container.querySelector(".ai-assistant-status") : document.querySelector(".ai-assistant-status");
      const responseBox = container ? container.querySelector(".ai-assistant-response") : document.querySelector(".ai-assistant-response");

      if (data && data.answer) {
        console.log("[AI Assistant] 🔊 Auto-speaking response on new collection page:", data);
        // Also open the panel so the user sees the response
        const panel  = container ? container.querySelector(".ai-chat-panel") : null;
        if (panel && !panel.classList.contains("ai-open")) panel.classList.add("ai-open");
        renderApiResponse(data, responseBox, status);

        // After TTS finishes speaking on the new page, conditionally reopen mic if question was asked
        _waitForTTSEnd(() => {
          if (!isHandsFreeEnabled()) return;

          const requiresInput = !!(data && data.requires_input);

          if (requiresInput) {
            setTimeout(() => {
              if (!isHandsFreeEnabled()) return;
              _openPanel();
              _state = "listening";
              _setFabState("listening");
              _setStatus("Listening for your answer…");
              _startQueryCapture(true /* isFollowUp */);
            }, 400);
          } else {
            _hasAskedAreYouStillThere = false;
            setTimeout(_toStandby, 800);
          }
        });
      }
    }, 400);
  } catch (e) {
    console.warn("[AI Assistant] Error restoring pending speech:", e);
  }
}

// ---------------------------------------------------------------
// Attach to all existing buttons + watch for dynamically added ones
// ---------------------------------------------------------------

// ---------------------------------------------------------------
// Refresh native Shopify cart UI (handles browser Back button & page restores)
// ---------------------------------------------------------------
async function refreshNativeCart() {
  try {
    const res = await fetch("/cart.js");
    if (!res.ok) return;
    const cart = await res.json();
    const count = cart.item_count || 0;

    // Update all cart count badges in theme header
    document.querySelectorAll(
      ".cart-count-bubble, .cart__count, [data-cart-count], .header__icon--cart .badge, .cart-count"
    ).forEach(el => {
      el.textContent = count;
      if (count > 0) el.removeAttribute("hidden");
      else el.setAttribute("hidden", "true");
    });

    // Fetch fresh cart section HTML to update drawer and icon bubble
    try {
      const sectionRes = await fetch("/?sections=cart-drawer,cart-icon-bubble");
      if (sectionRes.ok) {
        const sections = await sectionRes.json();
        if (sections["cart-drawer"]) {
          const drawer = document.querySelector("cart-drawer");
          if (drawer) {
            const parser = new DOMParser();
            const parsedHtml = parser.parseFromString(sections["cart-drawer"], "text/html");
            const newDrawer = parsedHtml.querySelector("cart-drawer");
            if (newDrawer) drawer.innerHTML = newDrawer.innerHTML;
            if (count === 0) drawer.classList.add("is-empty");
            else drawer.classList.remove("is-empty");
          }
        }
        if (sections["cart-icon-bubble"]) {
          const bubble = document.querySelector("#cart-icon-bubble");
          if (bubble) {
            const parser = new DOMParser();
            const parsedHtml = parser.parseFromString(sections["cart-icon-bubble"], "text/html");
            const newBubble = parsedHtml.querySelector("#cart-icon-bubble");
            if (newBubble) bubble.innerHTML = newBubble.innerHTML;
          }
        }
      }
    } catch (e) {}

    document.dispatchEvent(new CustomEvent("cart:refresh", { bubbles: true }));
    document.dispatchEvent(new CustomEvent("cart:updated", { bubbles: true }));
  } catch (e) {}
}

// Automatically refresh cart UI when returning to page (Browser Back button / bfcache restore / tab focus)
window.addEventListener("pageshow", (event) => {
  if (event.persisted || (performance.getEntriesByType("navigation")[0] && performance.getEntriesByType("navigation")[0].type === "back_forward")) {
    refreshNativeCart();
  }
});
window.addEventListener("focus", refreshNativeCart);

// ================================================================
// WAKE-WORD HANDS-FREE SYSTEM
// State: disabled → standby → listening → processing → speaking → standby
// Triggered by "Hey Assistant" (case-insensitive, pronunciation-tolerant)
// ================================================================

;(function() {
"use strict";

const _SR = window.SpeechRecognition || window.webkitSpeechRecognition;

// Wake phrases — covers common mishearings / browser misrecognitions
const WAKE_PHRASES = [
  "hey assistant", "hey, assistant", "hey assistance", "hi assistant",
  "hay assistant", "hey a system", "hey assistance", "hey assistants",
  "hey assistant,", "hello assistant", "ok assistant", "okay assistant",
  "hey assistent", "hey persistent", "hey system", "hey assist", "assistant",
  "hey shopify", "hey shopping", "hey shopping assistant", "computer"
];

const CFG = {
  queryTimeoutMs:   7000,   // Max silence before "I didn't hear anything"
  maxQueryMs:       15000,  // Hard cap on a single query recording
  panelCloseDelayMs:3200,   // Pause after speaking before auto-closing
  restartDelayMs:   600,    // Debounce before restarting wake recognition
};

// ── Module state ────────────────────────────────────────────────
let _state    = "disabled"; // disabled|standby|listening|processing|speaking
let _wrapper  = null;
let _enabled  = false;
let _wakeRec  = null;       // Continuous SpeechRecognition for wake word
let _queryRec = null;       // One-shot SpeechRecognition for shopping query
let _queryTimer   = null;
let _ttsInterval  = null;

// ── Helpers ──────────────────────────────────────────────────────

function _matchWake(text) {
  const norm = text.toLowerCase()
    .replace(/[',\.\!\?]/g, " ")
    .replace(/\s+/g, " ").trim();
  if (WAKE_PHRASES.some(p => norm.includes(p))) return true;
  const words = norm.split(" ");
  const hasGreeting = words.some(w => ["hey", "hi", "hay", "hello", "ok", "okay"].includes(w));
  const hasAssistant = words.some(w => ["assistant", "assistent", "assistance", "assistants", "system", "shopify"].includes(w));
  return hasGreeting && hasAssistant;
}

function _extractInlineQuery(text) {
  const norm = text.toLowerCase().replace(/[',\.\!\?]/g, " ").replace(/\s+/g, " ");
  for (const p of WAKE_PHRASES) {
    const idx = norm.indexOf(p.replace(/,/g, "").replace(/\s+/g, " "));
    if (idx !== -1) {
      const after = text.slice(idx + p.length).replace(/^[\s,\.!\-]+/, "").trim();
      if (after.length > 1) return after;
    }
  }
  return "";
}

function _playBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.type = "sine";
    osc.frequency.setValueAtTime(680, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(1200, ctx.currentTime + 0.1);
    gain.gain.setValueAtTime(0.28, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.start(); osc.stop(ctx.currentTime + 0.3);
    setTimeout(() => { try { ctx.close(); } catch(e) {} }, 700);
  } catch(e) {}
}

// ── UI helpers ───────────────────────────────────────────────────

function _q(sel) { return _wrapper ? _wrapper.querySelector(sel) : null; }

function _setStatus(text) {
  const s = _q(".ai-assistant-status");
  if (s) s.textContent = text;
}

function _setFabState(stateName) {
  const fab  = _q(".ai-fab-btn");
  const dot  = _q(".ai-ww-dot");
  const wave = _q(".ai-panel-mic-area");
  if (fab) {
    fab.classList.remove(
      "ai-fab--listening","ai-fab--processing",
      "ai-fab--speaking","ai-fab--standby","ai-fab--disabled"
    );
    if (stateName) fab.classList.add("ai-fab--" + stateName);
  }
  if (dot) {
    dot.className = "ai-ww-dot ai-ww-dot--" + (stateName || "disabled");
  }
  // Toggle speaking wave class on mic area
  if (wave) {
    wave.classList.toggle("ai-wave--speaking", stateName === "speaking");
  }
}

function _openPanel() {
  const panel = _q(".ai-chat-panel");
  if (!panel) return;
  panel.classList.add("ai-open");
  panel.setAttribute("aria-hidden", "false");
  if (_wrapper) _wrapper.classList.add("ai-panel-open");
  const fab = _q(".ai-fab-btn");
  if (fab) {
    fab.setAttribute("aria-expanded", "true");
    fab.setAttribute("aria-label", "Close AI Shopping Assistant");
  }
}

function _closePanel() {
  const panel = _q(".ai-chat-panel");
  if (!panel) return;
  panel.classList.remove("ai-open");
  panel.setAttribute("aria-hidden", "true");
  if (_wrapper) _wrapper.classList.remove("ai-panel-open");
  const fab = _q(".ai-fab-btn");
  if (fab) {
    fab.setAttribute("aria-expanded", "false");
    fab.setAttribute("aria-label", "Open voice shopping assistant");
  }
}

function _updateEnableBtn(enabled) {
  const btn = _q(".ai-ww-enable-btn");
  if (!btn) return;
  btn.checked = !!enabled;
  btn.setAttribute("aria-checked", enabled ? "true" : "false");
}

// ── Wake-word recognition ────────────────────────────────────────

function _startWakeRec() {
  if (!_SR || !_enabled || _state !== "standby") return;
  if (_wakeRec) { try { _wakeRec.abort(); } catch(e) {} _wakeRec = null; }

  _wakeRec = new _SR();
  _wakeRec.continuous     = true;
  _wakeRec.interimResults = true;
  _wakeRec.lang           = "en-US";
  _wakeRec.maxAlternatives = 3;

  _wakeRec.onresult = (event) => {
    if (_state !== "standby") return;
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const res = event.results[i];
      for (let j = 0; j < res.length; j++) {
        const t = res[j].transcript || "";
        if (_matchWake(t)) { _onWakeDetected(t); return; }
      }
    }
  };

  _wakeRec.onerror = (e) => {
    console.warn("[AiWakeWord] Wake recognition error:", e.error);
    if (e.error === "not-allowed" || e.error === "service-not-allowed") {
      _setStatus("⚠️ Mic blocked. Click mic button to grant permission.");
      return;
    }
    if (!_enabled || _state !== "standby") return;
    if (e.error !== "aborted" && e.error !== "no-speech") {
      setTimeout(_startWakeRec, 1500);
    }
  };

  _wakeRec.onend = () => {
    if (_enabled && _state === "standby") {
      setTimeout(_startWakeRec, CFG.restartDelayMs);
    }
  };

  try {
    _wakeRec.start();
  } catch(e) {
    console.warn("[AiWakeWord] start() exception:", e);
    if (_enabled && _state === "standby") setTimeout(_startWakeRec, 2000);
  }
}

function _stopWakeRec() {
  if (_wakeRec) {
    try { _wakeRec.abort(); } catch(e) {}
    _wakeRec = null;
  }
}

// ── Wake detected ────────────────────────────────────────────────

function _onWakeDetected(transcript) {
  if (_state !== "standby") return;
  _stopWakeRec();
  _playBeep();
  _openPanel();

  const inlineQuery = _extractInlineQuery(transcript);
  if (inlineQuery.length > 1) {
    _state = "processing";
    _setFabState("processing");
    _setStatus(`Processing: "${inlineQuery}"`);
    _processQuery(inlineQuery);
  } else {
    _state = "listening";
    _setFabState("listening");
    _setStatus("I'm listening…");
    _startQueryCapture();
  }
}

// ── Module state ────────────────────────────────────────────────
let _hasAskedAreYouStillThere = false;

// ── Query capture ────────────────────────────────────────────────

function _startQueryCapture(isFollowUp = false) {
  if (_queryRec) { try { _queryRec.abort(); } catch(e) {} _queryRec = null; }
  clearTimeout(_queryTimer);

  let finalText   = "";
  let interimText = "";

  const handleTimeoutOrNoSpeech = () => {
    if (isFollowUp && !_hasAskedAreYouStillThere) {
      _hasAskedAreYouStillThere = true;
      _state = "speaking"; _setFabState("speaking");
      const msg = "Are you still there?";
      _setStatus(msg);
      const status = _q(".ai-assistant-status");
      speakTextWithBrowserSpeech(msg, status);
      _waitForTTSEnd(() => {
        if (!_enabled) return;
        setTimeout(() => {
          if (!_enabled) return;
          _openPanel();
          _state = "listening"; _setFabState("listening");
          _setStatus("Listening for your answer…");
          _startQueryCapture(true);
        }, 400);
      });
    } else {
      _hasAskedAreYouStillThere = false;
      _handleNoQuery();
    }
  };

  // 7-second silence timeout
  _queryTimer = setTimeout(() => {
    if (_queryRec) { try { _queryRec.abort(); } catch(e) {} _queryRec = null; }
    if (!finalText.trim()) {
      handleTimeoutOrNoSpeech();
    }
  }, CFG.queryTimeoutMs);

  // 15-second hard max
  const maxTimer = setTimeout(() => {
    if (_queryRec) { try { _queryRec.abort(); } catch(e) {} }
  }, CFG.maxQueryMs);

  let silenceTimer = null;

  _queryRec = new _SR();
  _queryRec.continuous     = true;
  _queryRec.interimResults = true;
  _queryRec.lang           = "en-US";
  _queryRec.maxAlternatives = 1;

  _queryRec.onresult = (event) => {
    clearTimeout(_queryTimer);
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
    _hideSuggestionChips();
    _setStatus("Listening…");
    if (currentDisplay) {
      _setCustomerTranscription(`“${currentDisplay}”`);
    } else {
      _clearCustomerTranscription();
    }
    _state = "listening"; _setFabState("listening");

    // Wait 1.4s of silence after last spoken phrase before finalizing
    silenceTimer = setTimeout(() => {
      if (_queryRec) {
        try { _queryRec.stop(); } catch(e) {}
      }
    }, 1400);
  };

  _queryRec.onend = () => {
    clearTimeout(_queryTimer);
    clearTimeout(silenceTimer);
    clearTimeout(maxTimer);
    _queryRec = null;
    const query = (finalText || interimText).replace(/\s+/g, " ").trim();
    if (query.length > 1) {
      _hasAskedAreYouStillThere = false;
      _state = "processing"; _setFabState("processing");
      _processQuery(query);
    } else {
      handleTimeoutOrNoSpeech();
    }
  };

  _queryRec.onerror = (e) => {
    clearTimeout(_queryTimer);
    clearTimeout(silenceTimer);
    clearTimeout(maxTimer);
    _queryRec = null;
    if (e.error === "no-speech" || e.error === "network") {
      handleTimeoutOrNoSpeech();
      return;
    }
    if (e.error === "aborted") return;
    _hasAskedAreYouStillThere = false;
    _handleNoQuery();
  };

  try {
    _queryRec.start();
  } catch(e) {
    _hasAskedAreYouStillThere = false;
    _handleNoQuery();
  }
}

function _stopQueryRec() {
  if (_queryRec) {
    try { _queryRec.abort(); } catch(e) {}
    _queryRec = null;
  }
  clearTimeout(_queryTimer);
  _queryTimer = null;
  _hasAskedAreYouStillThere = false;
}

// ── Process query ────────────────────────────────────────────────

async function _processQuery(query) {
  _stopQueryRec();
  const responseBox = _q(".ai-assistant-response");
  const status      = _q(".ai-assistant-status");

  const isObj = typeof query === "object" && query !== null;
  const msgStr = isObj ? (query.text || query.collection_title || query.collection_handle || "Select collection") : String(query);

  _hideSuggestionChips();
  _setCustomerTranscription(`You said: “${msgStr}”`);
  _setFabState("processing");
  _setStatus("Finding the best options…");

  const currentSeq = ++_reqSeqNum;

  try {
    const apiBase   = getApiBaseUrl();
    const nativeCart = await getNativeShopifyCart();
    const requestBody = isObj ? {
      message: msgStr,
      session_id: getSessionId(),
      cart_context: nativeCart,
      sequence_num: currentSeq,
      intent: query.intent,
      collection_id: query.collection_id,
      collection_handle: query.collection_handle,
    } : {
      message: msgStr,
      session_id: getSessionId(),
      cart_context: nativeCart,
      sequence_num: currentSeq,
    };

    const resp = await fetch(`${apiBase}/voice-text`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(requestBody),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    if (currentSeq < _latestHandledSeqNum) {
      console.warn(`[AiWakeWord] Discarding stale response seq ${currentSeq} (latest handled: ${_latestHandledSeqNum})`);
      return;
    }
    _latestHandledSeqNum = currentSeq;

    _state = "speaking"; _setFabState("speaking");
    _setStatus("Speaking response…");

    // Render response + cart sync (uses existing renderApiResponse)
    renderApiResponse(data, responseBox, status);

    // After TTS finishes, check structured intent flags returned by backend
    _waitForTTSEnd(() => {
      if (currentSeq !== _latestHandledSeqNum) {
        console.warn(`[AiWakeWord] Stale completion callback seq ${currentSeq} ignored.`);
        return;
      }

      if (data && data.disable_hands_free) {
        window.AiWakeWord.disable();
        return;
      }

      const isComplete = !!(data && (data.conversation_complete || data.stage === "complete" || data.auto_close));
      const requiresInput = !!(data && data.requires_input && data.auto_listen !== false);
      const isImmediateClose = !!(data && (data.immediate_close || data.intent === "close_assistant"));

      if (isImmediateClose) {
        _stopQueryRec();
        _hasAskedAreYouStillThere = false;
        closeCompletedConversation();
        return;
      }

      if (isComplete && !requiresInput) {
        _setFabState("speaking");
        setTimeout(() => {
          if (currentSeq !== _latestHandledSeqNum) return;
          closeCompletedConversation();
        }, 1500);
        return;
      }

      if (requiresInput) {
        // Wait 300-500ms after TTS finishes before reopening mic for user's answer
        setTimeout(() => {
          if (currentSeq !== _latestHandledSeqNum) return;
          _openPanel();
          _state = "listening";
          _setFabState("listening");
          _setStatus("Listening for your answer…");
          _startQueryCapture(true /* isFollowUp */);
        }, 400);
      } else {
        _hasAskedAreYouStillThere = false;
        if (isHandsFreeEnabled()) {
          setTimeout(_toStandby, 800);
        }
      }
    });

  } catch(err) {
    console.error("[AiWakeWord] Query error:", err);
    _hasAskedAreYouStillThere = false;
    const msg = "Sorry, I ran into a temporary issue. Please try again.";
    _setStatus("⚠️ " + msg);
    speakTextWithBrowserSpeech(msg, _q(".ai-assistant-status"), () => {
      if (_enabled) {
        setTimeout(() => {
          if (!_enabled) return;
          _openPanel();
          _state = "listening";
          _setFabState("listening");
          _setStatus("Listening for your answer…");
          _startQueryCapture(true /* isFollowUp */);
        }, 500);
      }
    });
  }
}

// Wait for response audio (HTML5 Audio or Web Speech TTS) to finish
function _waitForTTSEnd(callback) {
  let called = false;
  const done = () => {
    if (called) return;
    called = true;
    if (_ttsInterval) clearInterval(_ttsInterval);
    _ttsInterval = null;
    callback();
  };

  // Give audio/speech 300ms to start before we poll
  setTimeout(() => {
    const isSpeaking = () => {
      const isAudioPlaying = !!(_currentPlayingAudio && !_currentPlayingAudio.paused && !_currentPlayingAudio.ended);
      const isSpeechSpeaking = !!(window.speechSynthesis && window.speechSynthesis.speaking);
      return isAudioPlaying || isSpeechSpeaking;
    };

    if (!isSpeaking()) {
      done();
      return;
    }

    _ttsInterval = setInterval(() => {
      if (!isSpeaking()) done();
    }, 250);

    // Safety timeout of 30 seconds
    setTimeout(done, 30000);
  }, 300);
}

// ── State transitions ────────────────────────────────────────────

function closeCompletedConversation() {
  _stopQueryRec();
  _stopWakeRec();
  clearTimeout(_queryTimer);
  clearInterval(_ttsInterval);
  _ttsInterval = null;
  _hasAskedAreYouStillThere = false;

  const panel = _q(".ai-chat-panel");
  if (panel) {
    panel.classList.remove("ai-open");
    panel.setAttribute("aria-hidden", "true");
  }
  if (_wrapper) _wrapper.classList.remove("ai-panel-open");

  const fab = _q(".ai-fab-btn");
  if (fab) {
    fab.hidden = false;
    fab.removeAttribute("tabindex");
    fab.setAttribute("aria-expanded", "false");
    fab.setAttribute("aria-label", "Open AI Shopping Assistant");
    try { fab.focus(); } catch(e) {}
  }

  setTimeout(() => {
    const responseBox = _q(".ai-assistant-response");
    if (responseBox) {
      responseBox.style.display = "none";
      responseBox.innerHTML = "";
    }
    _clearCustomerTranscription();
    _showSuggestionChips();
  }, 350);

  if (isHandsFreeEnabled()) {
    _toStandby();
  } else {
    _state = "idle";
    _setFabState(null);
    _setStatus("Tap the microphone to start");
    _clearCustomerTranscription();
    _showSuggestionChips();
  }
}

function _toStandby() {
  _stopQueryRec();
  _state = "standby";
  _setFabState("standby");
  _setStatus('Say "Hey Assistant" to start');
  _clearCustomerTranscription();
  _showSuggestionChips();
  _startWakeRec();
}

function _handleNoQuery() {
  const msg = "I didn't hear anything. Say \"Hey Assistant\" to try again.";
  _state = "speaking"; _setFabState("speaking");
  _setStatus(msg);
  speakTextWithBrowserSpeech(msg, _q(".ai-assistant-status"));
  setTimeout(() => { if (_enabled) _toStandby(); }, 3500);
}

function _handleError(msg) {
  _state = "speaking"; _setFabState("speaking");
  _setStatus("⚠️ " + msg);
  speakTextWithBrowserSpeech(msg, _q(".ai-assistant-status"), () => {
    if (_enabled) setTimeout(_toStandby, 800);
  });
}

// ── Public API ───────────────────────────────────────────────────

window.AiWakeWord = {
  isSupported() { return !!_SR; },
  isEnabled() { return _enabled; },

  enable(wrapper) {
    if (!_SR) {
      const s = wrapper ? wrapper.querySelector(".ai-assistant-status") : null;
      if (s) s.textContent = "⚠️ Hands-free not supported in this browser. Use the mic button.";
      return false;
    }
    _wrapper = wrapper;
    _enabled = true;
    try {
      localStorage.setItem("ai_handsfree", "1");
      sessionStorage.setItem("ai_handsfree", "1");
    } catch(e) {}
    _updateEnableBtn(true);
    _toStandby();
    return true;
  },

  disable() {
    _enabled = false;
    _stopWakeRec();
    if (_queryRec) { try { _queryRec.abort(); } catch(e) {} _queryRec = null; }
    clearTimeout(_queryTimer);
    clearInterval(_ttsInterval);
    try {
      localStorage.removeItem("ai_handsfree");
      sessionStorage.removeItem("ai_handsfree");
    } catch(e) {}
    _state = "disabled";
    _setFabState(null);         // Remove all state classes → default royal blue
    _setStatus("Tap the microphone to start");
    _updateEnableBtn(false);
  },

  init(wrapper) {
    _wrapper = wrapper;

    const checkbox = wrapper.querySelector(".ai-ww-enable-btn");
    if (checkbox) {
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          window.AiWakeWord.enable(wrapper);
        } else {
          window.AiWakeWord.disable();
        }
      });
    }

    wrapper.querySelectorAll(".ai-chip").forEach(chip => {
      chip.addEventListener("click", () => {
        const query = chip.getAttribute("data-query");
        if (query) {
          _openPanel();
          _processQuery(query);
        }
      });
    });

    const dot = wrapper.querySelector(".ai-ww-dot");
    if (dot) dot.className = "ai-ww-dot ai-ww-dot--disabled";

    if (!_SR) {
      const section = wrapper.querySelector(".ai-ww-section");
      if (section) {
        const notice = section.querySelector(".ai-ww-privacy");
        if (notice) notice.textContent = "Hands-free listening is not supported in this browser. Use the microphone button instead.";
        const enableBtn = section.querySelector(".ai-ww-enable-btn");
        if (enableBtn) { enableBtn.disabled = true; }
      }
    } else {
      let isSaved = false;
      try {
        isSaved = localStorage.getItem("ai_handsfree") === "1" || sessionStorage.getItem("ai_handsfree") === "1";
      } catch(e) {}

      if (isSaved) {
        window.AiWakeWord.enable(wrapper);
      }
    }
  },
};

})(); // end AiWakeWord IIFE


function attachAllButtons() {
  document.querySelectorAll(".ai-assistant-button").forEach(initAiAssistantButton);
  checkPendingSpeakResponse();
  refreshNativeCart();
}

// Run immediately (handles cases where script loads after DOM)
attachAllButtons();

// Also run on DOMContentLoaded (handles deferred load before DOM ready)
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", attachAllButtons);
}

// MutationObserver handles Shopify's dynamic section rendering
const _aiObserver = new MutationObserver((mutations) => {
  for (const m of mutations) {
    for (const node of m.addedNodes) {
      if (node.nodeType !== 1) continue;
      if (node.classList?.contains("ai-assistant-button")) {
        initAiAssistantButton(node);
      }
      node.querySelectorAll?.(".ai-assistant-button").forEach(initAiAssistantButton);
    }
  }
});
_aiObserver.observe(document.body, { childList: true, subtree: true });