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


function renderApiResponse(data, responseBox, status) {
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

  // =============================================================
  // CART SUCCESS — show card + sync to native Shopify theme cart
  // =============================================================
  if (data.cart && data.cart.lines && data.cart.lines.length > 0) {
    const lastLine = data.cart.lines[data.cart.lines.length - 1];

    // ── Show a success card with both Store Cart & Checkout links ─
    if (lastLine) {
      const successCard = document.createElement("div");
      successCard.style.cssText = "background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:12px;margin-bottom:12px;";
      const checkoutBtn = data.cart.checkout_url
        ? `<a href="${data.cart.checkout_url}" target="_blank" style="display:inline-block;margin-top:8px;margin-right:6px;padding:8px 14px;background:#000;color:#fff;border-radius:6px;text-decoration:none;font-size:12px;">Proceed to Checkout →</a>`
        : "";
      const mainCartBtn = `<a href="/cart" target="_self" style="display:inline-block;margin-top:8px;padding:8px 14px;background:#166534;color:#fff;border-radius:6px;text-decoration:none;font-size:12px;">View Store Cart 🛒</a>`;
      
      successCard.innerHTML = `
        <div style="font-weight:600;margin-bottom:4px;">🎉 Added to cart!</div>
        <div style="font-size:13px;color:#166534;">${lastLine.product_title || ""} — ${lastLine.variant_title || ""} × ${lastLine.quantity || 1}</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;">${mainCartBtn}${checkoutBtn}</div>
      `;
      responseBox.appendChild(successCard);
    }

    // ── Sync to native Shopify theme cart via /cart/add.js ─────
    (async () => {
      try {
        if (!lastLine || !lastLine.variant_id) return;
        const rawId = String(lastLine.variant_id);
        const numericId = rawId.includes("/")
          ? parseInt(rawId.split("/").pop(), 10)
          : parseInt(rawId, 10);
        if (!numericId || isNaN(numericId)) return;

        const addResp = await fetch("/cart/add.js", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
          body: JSON.stringify({
            items: [{ id: numericId, quantity: lastLine.quantity || 1 }],
            sections: "cart-drawer,cart-icon-bubble"
          }),
        });

        if (!addResp.ok) {
          console.warn("[AI Cart] /cart/add.js failed:", addResp.status);
          return;
        }

        const addData = await addResp.json();

        // ── Re-render Dawn Theme Cart Drawer HTML directly ──────────
        if (addData.sections) {
          if (addData.sections["cart-drawer"]) {
            const drawer = document.querySelector("cart-drawer");
            if (drawer) {
              const parser = new DOMParser();
              const parsedHtml = parser.parseFromString(addData.sections["cart-drawer"], "text/html");
              const newDrawer = parsedHtml.querySelector("cart-drawer");
              if (newDrawer) {
                drawer.innerHTML = newDrawer.innerHTML;
              }
              drawer.classList.remove("is-empty");
              drawer.classList.add("active");
              if (typeof drawer.open === "function") drawer.open();

              // ── Auto-click checkout after 3.5 seconds ───────────────
              setTimeout(() => {
                // Try clicking the native checkout button inside the drawer
                const checkoutBtn =
                  drawer.querySelector("a[href*='/checkouts'], a[href*='checkout']") ||
                  document.querySelector("cart-drawer a[href*='checkout']") ||
                  document.querySelector(".cart__checkout-button, #checkout, [name='checkout']");

                if (checkoutBtn) {
                  console.log("[AI Cart] ⏱️ Auto-clicking checkout button…");
                  checkoutBtn.click();
                } else {
                  // Fallback: navigate directly to /checkout
                  console.log("[AI Cart] ⏱️ Auto-redirecting to /checkout…");
                  window.location.href = "/checkout";
                }
              }, 3500);
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


        // ── Fallback count update & events for non-Dawn themes ──────
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
          console.log(`[AI Cart] ✅ Synced to native cart — ${count} item(s)`);

          document.dispatchEvent(new CustomEvent("cart:refresh", { bubbles: true }));
          document.dispatchEvent(new CustomEvent("cart:updated", { bubbles: true }));
        }
      } catch (err) {
        console.warn("[AI Cart] Native cart sync skipped:", err.message);
      }
    })();


  }

  // =============================================================
  // AUDIO — play TTS response, then redirect to filtered store page
  // =============================================================
  const shouldRedirect = !!data.storefront_url && !data.cart;

  if (data.audio) {
    try {
      const audioBytes = atob(data.audio);
      const arr = new Uint8Array(audioBytes.length);
      for (let i = 0; i < audioBytes.length; i++) arr[i] = audioBytes.charCodeAt(i);
      const blob = new Blob([arr], { type: "audio/mpeg" });
      const blobUrl = URL.createObjectURL(blob);
      const audio   = new Audio(blobUrl);

      audio.onplay  = () => { status.textContent = "Playing response..."; };
      audio.onended = () => {
        URL.revokeObjectURL(blobUrl);
        status.textContent = shouldRedirect ? "Opening filtered store page..." : "Tap the microphone to speak";
        if (shouldRedirect) window.location.href = data.storefront_url;
      };
      audio.onerror = () => {
        URL.revokeObjectURL(blobUrl);
        if (shouldRedirect) window.location.href = data.storefront_url;
        else status.textContent = "Tap the microphone to speak";
      };
      audio.play().catch(e => {
        console.warn("[AI] Audio autoplay blocked:", e.message);
        status.textContent = "Tap the microphone to speak";
        if (shouldRedirect) window.location.href = data.storefront_url;
      });
    } catch (e) {
      console.error("Audio error:", e);
      if (shouldRedirect) window.location.href = data.storefront_url;
      else status.textContent = "Response received";
    }
  } else {
    if (shouldRedirect) {
      window.location.href = data.storefront_url;
    } else {
      status.textContent = "Tap the microphone to speak";
    }
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

  button.addEventListener("click", async () => {
    const container  = button.closest(".ai-shopping-assistant");
    const status     = container.querySelector(".ai-assistant-status");
    const responseBox = container.querySelector(".ai-assistant-response");

    // =============================================================
    // STOP RECORDING (second tap)
    // =============================================================
    if (mediaRecorder && mediaRecorder.state === "recording") {
      mediaRecorder.stop();
      status.textContent = "Processing…";
      button.style.background = "linear-gradient(155deg,#3d5afe,#2541d6)";
      return;
    }

    // =============================================================
    // START RECORDING (first tap)
    // =============================================================

    // Guard: getUserMedia only works on HTTPS or localhost
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

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunks.push(e.data);
      };

      // ===========================================================
      // AFTER RECORDING STOPS → send to API
      // ===========================================================
      mediaRecorder.onstop = async () => {
        status.textContent = "Sending audio…";

        const audioBlob = new Blob(audioChunks, { type: mediaRecorder.mimeType || "audio/webm" });
        console.log("[AI Assistant] Recorded:", audioBlob.type, audioBlob.size, "bytes");

        if (audioBlob.size < 1000) {
          status.textContent = "Recording too short — please try again.";
          if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
          return;
        }

        const formData = new FormData();
        formData.append("file", audioBlob, "voice.webm");

        try {
          const apiBase = getApiBaseUrl();
          const response = await fetch(`${apiBase}/voice`, {
            method: "POST",
            body: formData,
          });


          if (!response.ok) throw new Error(`Server returned ${response.status}`);

          console.log("[AI Assistant] Voice API status:", response.status);

          const data = await response.json();
          renderApiResponse(data, responseBox, status);

        } catch (err) {
          console.error("[AI Assistant] Voice API error:", err);
          status.textContent = `⚠️ Error: ${err.message}`;
        } finally {
          if (stream) {
            stream.getTracks().forEach((t) => t.stop());
            stream = null;
          }
        }
      };

      mediaRecorder.start(250); // timeslice: fire ondataavailable every 250ms
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
// Attach to all existing buttons + watch for dynamically added ones
// ---------------------------------------------------------------

function attachAllButtons() {
  document.querySelectorAll(".ai-assistant-button").forEach(initAiAssistantButton);
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