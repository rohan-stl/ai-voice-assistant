// API_BASE_URL is set automatically by start.sh when the Cloudflare Tunnel starts.
// Do not edit this URL manually — run ./start.sh and it will be patched for you.
const API_BASE_URL = "https://disclaimers-bubble-currency-auto.trycloudflare.com";

// ===============================================================
// SHARED: RENDER API RESPONSE (used by both voice + text input)
// ===============================================================

function renderApiResponse(data, responseBox, status) {
  console.log("API response:", data);

  status.textContent = "Response received";

  if (!responseBox) return;

  responseBox.style.display = "block";

  responseBox.innerHTML = "";

  const answerElement = document.createElement("div");

  answerElement.style.marginBottom = "12px";

  answerElement.style.fontWeight = "500";

  answerElement.textContent =
    data.answer || "No response received.";

  responseBox.appendChild(answerElement);

  // =============================================================
  // DISPLAY PRODUCTS
  // =============================================================

  const products = data.products || [];

  if (products.length > 0) {
    const productsContainer = document.createElement("div");

    productsContainer.className = "ai-assistant-products";

    products.forEach((product) => {
      const card = document.createElement("div");

      card.className = "ai-assistant-product-card";

      card.style.border = "1px solid #ddd";

      card.style.borderRadius = "10px";

      card.style.padding = "10px";

      card.style.marginTop = "10px";

      card.style.background = "#fff";

      // =========================================================
      // VARIANTS
      // =========================================================

      const variants = product.variants?.nodes || [];

      // =========================================================
      // FIND VARIANT IMAGE
      // =========================================================

      const variantWithImage = variants.find(
        (variant) => variant.image?.url
      );

      const imageUrl =
        variantWithImage?.image?.url ||
        product.featuredImage?.url;

      // =========================================================
      // PRODUCT IMAGE
      // =========================================================

      if (imageUrl) {
        const image = document.createElement("img");

        image.src = imageUrl;

        image.alt =
          variantWithImage?.image?.altText ||
          product.featuredImage?.altText ||
          product.title;

        image.style.width = "100%";

        image.style.maxWidth = "250px";

        image.style.borderRadius = "8px";

        image.style.display = "block";

        image.style.marginBottom = "8px";

        card.appendChild(image);
      }

      // =========================================================
      // PRODUCT TITLE
      // =========================================================

      const title = document.createElement("div");

      title.textContent = product.title;

      title.style.fontWeight = "600";

      title.style.fontSize = "16px";

      title.style.marginBottom = "5px";

      card.appendChild(title);

      // =========================================================
      // PRODUCT DESCRIPTION
      // =========================================================

      if (product.description) {
        const description = document.createElement("div");

        description.textContent = product.description;

        description.style.fontSize = "13px";

        description.style.marginBottom = "8px";

        description.style.color = "#555";

        card.appendChild(description);
      }

      // =========================================================
      // MATCHING VARIANTS
      // =========================================================

      if (variants.length > 0) {
        const variantsContainer = document.createElement("div");

        variantsContainer.style.fontSize = "13px";

        variantsContainer.style.marginBottom = "8px";

        const seenVariants = new Set();

        variants.forEach((variant) => {
          const variantKey = variant.title;

          if (seenVariants.has(variantKey)) {
            return;
          }

          seenVariants.add(variantKey);

          const variantElement = document.createElement("div");

          variantElement.textContent = `${variant.title} — $${variant.price}`;

          variantsContainer.appendChild(variantElement);
        });

        card.appendChild(variantsContainer);
      }

      // =========================================================
      // VIEW PRODUCT BUTTON
      // =========================================================

      const productUrl =
        product.onlineStoreUrl ||
        `https://bhavin-ai-shopping-assistant-demo.myshopify.com/products/${product.handle}`;

      const viewButton = document.createElement("a");

      viewButton.href = productUrl;

      viewButton.target = "_self";

      viewButton.textContent = "View Product";

      viewButton.style.display = "inline-block";

      viewButton.style.padding = "8px 14px";

      viewButton.style.borderRadius = "6px";

      viewButton.style.background = "#000";

      viewButton.style.color = "#fff";

      viewButton.style.textDecoration = "none";

      viewButton.style.marginTop = "5px";

      card.appendChild(viewButton);

      productsContainer.appendChild(card);
    });

    responseBox.appendChild(productsContainer);
  }

  // =============================================================
  // PLAY AI AUDIO RESPONSE
  // =============================================================

  if (data.audio) {
    try {
      console.log("Audio data received");

      const audioData = atob(data.audio);

      console.log("Decoded audio size:", audioData.length);

      const audioArray = new Uint8Array(audioData.length);

      for (let i = 0; i < audioData.length; i++) {
        audioArray[i] = audioData.charCodeAt(i);
      }

      const audioBlobResponse = new Blob([audioArray], {
        type: "audio/mpeg"
      });

      console.log("Audio blob type:", audioBlobResponse.type);

      console.log("Audio blob size:", audioBlobResponse.size);

      const audioUrl = URL.createObjectURL(audioBlobResponse);

      const audio = new Audio(audioUrl);

      // ===========================================================
      // AUDIO STARTED
      // ===========================================================

      audio.onplay = () => {
        console.log("🔊 Audio playback started");

        status.textContent = "Playing response...";
      };

      // ===========================================================
      // AUDIO FINISHED
      // ===========================================================

      audio.onended = () => {
        console.log("🔊 Audio playback ended");

        URL.revokeObjectURL(audioUrl);

        // -------------------------------------------------------
        // REDIRECT TO FILTERED SHOPIFY COLLECTION
        // -------------------------------------------------------

        if (data.storefront_url) {
          console.log("Redirecting to:", data.storefront_url);

          status.textContent = "Opening filtered products...";

          window.location.href = data.storefront_url;
        } else {
          status.textContent = "Tap the microphone to speak";
        }
      };

      // ===========================================================
      // AUDIO ERROR
      // ===========================================================

      audio.onerror = (error) => {
        console.error("🔴 Audio playback error:", error);

        URL.revokeObjectURL(audioUrl);

        // If audio fails, still redirect to the filtered collection.

        if (data.storefront_url) {
          console.log(
            "Redirecting after audio error:",
            data.storefront_url
          );

          window.location.href = data.storefront_url;
        } else {
          status.textContent = "Audio playback failed";
        }
      };

      // ===========================================================
      // PLAY AUDIO
      // ===========================================================

      audio.play();
    } catch (audioError) {
      console.error("Audio playback error:", audioError);

      // If audio processing fails, still redirect if URL exists.

      if (data.storefront_url) {
        console.log(
          "Redirecting after audio processing error:",
          data.storefront_url
        );

        window.location.href = data.storefront_url;
      } else {
        status.textContent = "Response received";
      }
    }
  } else {
    console.log("No audio received from API");

    // =============================================================
    // NO AUDIO BUT FILTER URL EXISTS
    // =============================================================

    if (data.storefront_url) {
      console.log("Redirecting:", data.storefront_url);

      window.location.href = data.storefront_url;
    } else {
      status.textContent = "Tap the microphone to speak";
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  // ===============================================================
  // MIC / VOICE FLOW
  // ===============================================================

  const buttons = document.querySelectorAll(".ai-assistant-button");

  buttons.forEach((button) => {
    let mediaRecorder = null;
    let audioChunks = [];
    let stream = null;

    button.addEventListener("click", async () => {
      const container = button.closest(".ai-shopping-assistant");
      const status = container.querySelector(".ai-assistant-status");
      const responseBox = container.querySelector(
        ".ai-assistant-response"
      );

      // =========================================================
      // START RECORDING
      // =========================================================

      if (!mediaRecorder || mediaRecorder.state === "inactive") {
        try {
          status.textContent = "Requesting microphone...";

          stream = await navigator.mediaDevices.getUserMedia({
            audio: true
          });

          mediaRecorder = new MediaRecorder(stream);

          audioChunks = [];

          mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
              audioChunks.push(event.data);
            }
          };

          // =====================================================
          // RECORDING STOPPED
          // =====================================================

          mediaRecorder.onstop = async () => {
            status.textContent = "Sending audio...";

            const audioBlob = new Blob(audioChunks, {
              type: mediaRecorder.mimeType
            });

            console.log(
              "Recorded audio:",
              audioBlob.type,
              audioBlob.size
            );

            const formData = new FormData();

            formData.append("file", audioBlob, "voice.webm");

            try {
              // =================================================
              // CALL VOICE API
              // =================================================

              const response = await fetch(
                `${API_BASE_URL}/voice`,
                {
                  method: "POST",
                  body: formData
                }
              );

              if (!response.ok) {
                throw new Error(`Server returned ${response.status}`);
              }

              console.log("Voice API status:", response.status);

              console.log(
                "Voice API content type:",
                response.headers.get("content-type")
              );

              const data = await response.json();

              renderApiResponse(data, responseBox, status);
            } catch (error) {
              console.error("Voice API error:", error);

              status.textContent = "Something went wrong";
            } finally {
              // ===============================================
              // STOP MICROPHONE
              // ===============================================

              if (stream) {
                stream.getTracks().forEach((track) => {
                  track.stop();
                });

                stream = null;
              }
            }
          };

          // =====================================================
          // START RECORDING
          // =====================================================

          mediaRecorder.start();

          status.textContent = "Listening... Click again to stop.";
        } catch (error) {
          console.error("Microphone error:", error);

          status.textContent = "Microphone access failed";
        }
      } else if (mediaRecorder.state === "recording") {
        // =======================================================
        // STOP RECORDING
        // =======================================================

        mediaRecorder.stop();

        status.textContent = "Processing...";
      }
    });
  });
});