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



// ===============================================================
// SHARED: RENDER API RESPONSE (used by both voice + text input)
// ===============================================================


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
function speakTextWithBrowserSpeech(text, status) {
  if (!('speechSynthesis' in window) || !text) return;
  try {
    window.speechSynthesis.cancel();

    const cleanText = text
      .replace(/\*\*(.*?)\*\*/g, "$1")
      .replace(/\*(.*?)\*/g, "$1")
      .replace(/[✅❌⚠️🎉🔍🛒🎙️👋📦💬✋🤖👤]/g, "")
      .replace(/https?:\/\/\S+/g, "")
      .replace(/[\#\_]/g, "")
      .replace(/\s+/g, " ")
      .trim();

    if (!cleanText) return;

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

      if (status) {
        utt.onstart = () => { status.textContent = "Speaking…"; };
        utt.onend   = () => { status.textContent = "Tap the microphone to speak"; };
        utt.onerror = () => { status.textContent = "Tap the microphone to speak"; };
      }
      window.speechSynthesis.speak(utt);
    };

    if (window.speechSynthesis.getVoices().length > 0) {
      doSpeak();
    } else {
      setTimeout(doSpeak, 250);
    }
  } catch (e) {
    console.warn("[AI Assistant] speakText error:", e);
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


function renderApiResponse(data, responseBox, status) {

  stopAllAudioPlayback();
  console.log("API response:", data);
  status.textContent = "Response received";
  if (!responseBox) return;

  responseBox.style.display = "block";
  responseBox.innerHTML = "";

  const stage = data.stage || "browse";

  // ── Answer text ────────────────────────────────────────────────
  const answerEl = document.createElement("div");
  answerEl.style.cssText = "margin-bottom:12px;font-weight:500;";
  answerEl.textContent = data.answer || "No response received.";
  responseBox.appendChild(answerEl);

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
  // INSTANT REDIRECT TO NEW FILTER PAGE & SPEAK ON TARGET PAGE
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

  // If navigating to a new page (e.g. Home -> Collection), preserve response to speak on the target page
  if (data.storefront_url && isDifferentUrl && !data.cart && !data._fromPendingRestore) {
    console.log("[AI Assistant] ⚡ Redirecting to new collection page and preserving speech:", data.storefront_url);
    try {
      sessionStorage.setItem("ai_pending_speak_response", JSON.stringify(data));
    } catch (e) {}
    window.location.href = data.storefront_url;
    return;
  }

  // Dynamic grid swap if grid is already present on current page
  if (data.storefront_url && !data.cart && !isDifferentUrl) {
    const currentGrid = document.querySelector('#product-grid, .product-grid, #main-collection-product-grid');
    if (currentGrid) {
      fetch(data.storefront_url)
        .then(res => res.text())
        .then(html => {
          const parser = new DOMParser();
          const doc = parser.parseFromString(html, 'text/html');
          const newGrid = doc.querySelector('#product-grid, .product-grid, #main-collection-product-grid');
          if (newGrid) currentGrid.innerHTML = newGrid.innerHTML;
        })
        .catch(e => {});
    }
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

    const container   = button.closest(".ai-shopping-assistant");
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
      button.style.background = "linear-gradient(155deg,#3d5afe,#2541d6)";
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

async function getNativeShopifyCart() {
  try {
    const res = await fetch("/cart.js");
    if (res.ok) return await res.json();
  } catch (e) {}
  return null;
}

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
          renderApiResponse(data, responseBox, status);

        } catch (err) {
          console.error("[AI Assistant] Voice API error:", err);
          status.textContent = `⚠️ Error: ${err.message}`;
        }
      };

      mediaRecorder.start(250);
      status.textContent = "🎙️ Listening… tap again to stop.";
      button.style.background = "linear-gradient(155deg,#e53935,#b71c1c)";

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
      const container   = document.querySelector(".ai-shopping-assistant");
      const status      = container ? container.querySelector(".ai-assistant-status") : document.querySelector(".ai-assistant-status");
      const responseBox = container ? container.querySelector(".ai-assistant-response") : document.querySelector(".ai-assistant-response");

      if (data && data.answer) {
        console.log("[AI Assistant] 🔊 Auto-speaking response on new collection page:", data);
        renderApiResponse(data, responseBox, status);
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