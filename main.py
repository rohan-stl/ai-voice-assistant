"""
main.py — AI Shopping Assistant (conversational, multi-store)

Endpoints
─────────
GET  /                   health check
GET  /products           raw product list
POST /voice              audio file → full conversational response
POST /voice-text         text input → full conversational response
POST /session/reset      clear conversation state for a session

Cart uses the Shopify Admin REST API (Draft Orders) — no Storefront token needed.
All endpoints accept:
  session_id   (str) – frontend-generated, stored in localStorage
  shop_domain  (str) – Shopify store domain  (defaults to .env)
  admin_token  (str) – Admin API token       (optional, falls back to .env OAuth)
"""

import os
import json
import tempfile
import base64
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from shopify_service import get_products
from llm_service import ask_llm, ask_llm_cart
from voice_service import speech_to_text, text_to_speech
from product_filter import filter_products, normalize_filters, merge_filters
from storefront_url import build_storefront_url
from conversation_state import session_store, ConversationState
from cart_service import add_to_draft_order

load_dotenv()

_DEFAULT_DOMAIN      = os.getenv("SHOPIFY_STORE_DOMAIN", "")
_DEFAULT_ADMIN_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN", "")

app = FastAPI(title="AI Shopping Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────

class TextRequest(BaseModel):
    message: str
    session_id: str = "default"
    shop_domain: Optional[str] = None
    admin_token: Optional[str] = None
    previous_filters: Optional[dict] = None


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _resolve_domain(req_domain: Optional[str]) -> str:
    return req_domain or _DEFAULT_DOMAIN


def _resolve_admin_token(req_token: Optional[str]) -> Optional[str]:
    return req_token or _DEFAULT_ADMIN_TOKEN or None


def _build_response(
    state: ConversationState,
    answer: str,
    audio_b64: Optional[str],
    products: list,
    needs_clarification: bool = False,
    cart: Optional[dict] = None,
    storefront_url: Optional[str] = None,
    transcription: Optional[str] = None,
) -> dict:
    return {
        "transcription": transcription,
        "answer": answer,
        "stage": state.stage,
        "intent": state.stage,
        "filters": state.previous_filters,
        "products": products,
        "selected_product": state.selected_product,
        "selected_variant": state.selected_variant,
        "quantity": state.quantity,
        "needs_clarification": needs_clarification,
        "cart": cart,
        "storefront_url": storefront_url,
        "audio": audio_b64,
    }


def _generate_audio(text: str) -> tuple[str, str]:
    """Generate TTS audio. Returns (output_path, base64_string)."""
    output_path = tempfile.mktemp(suffix=".mp3")
    text_to_speech(text, output_path)
    with open(output_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    return output_path, audio_b64


def _find_variant(product: dict, variant_ref: Optional[str]) -> Optional[dict]:
    """
    Find the best matching variant in a product given a free-text variant_ref.
    Checks against variant title and each selectedOption value (case-insensitive).
    Returns None if no match.
    """
    if not variant_ref:
        variants = product.get("variants", {}).get("nodes", [])
        # If only one variant exists, return it automatically
        if len(variants) == 1:
            return variants[0]
        return None

    ref = variant_ref.strip().lower()
    # Strip leading action verbs the LLM may include in variant_ref
    for action in ("select ", "add ", "choose ", "get ", "want ", "show ", "i want "):
        if ref.startswith(action):
            ref = ref[len(action):]
    # Normalise spoken qualifiers: "size m" → "m", "colour blue" → "blue"
    for noise in ("size ", "colour ", "color ", "option "):
        ref = ref.replace(noise, "")
    ref = ref.strip()


    variants = product.get("variants", {}).get("nodes", [])

    # ── Strategy 1: full variant title substring ─────────────────────────
    # e.g. ref="blue / m" matches title="Blue / M"
    for v in variants:
        if ref in v.get("title", "").lower():
            return v

    # ── Strategy 2: single option value exact match ──────────────────────
    # e.g. ref="m" matches option Size=M; ref="blue" matches Color=Blue
    for v in variants:
        for opt in v.get("selectedOptions", []):
            if ref == opt.get("value", "").lower():
                return v

    # ── Strategy 3: ALL option values appear somewhere in ref ────────────
    # e.g. ref="blue m" → "blue" in ref AND "m" in ref → matches Blue/M
    # e.g. ref="medium blue" → "m" in ref (no) … try word boundary instead
    ref_words = set(ref.split())
    for v in variants:
        opts = v.get("selectedOptions", [])
        if not opts:
            continue
        if all(
            opt.get("value", "").lower() in ref            # "blue" in "blue m" ✓
            or opt.get("value", "").lower() in ref_words   # "m" in {"blue","m"} ✓
            for opt in opts
        ):
            return v

    # ── Strategy 4: fuzzy — any single option value is a substring of ref ─
    # e.g. ref="bluish m" contains "m" → first variant with Size=M
    for v in variants:
        for opt in v.get("selectedOptions", []):
            val = opt.get("value", "").lower()
            if val and val in ref_words:
                return v

    return None


def _available_variants_text(product: dict) -> str:
    variants = product.get("variants", {}).get("nodes", [])
    available = [
        v.get("title", "") for v in variants if v.get("availableForSale")
    ]
    return ", ".join(available) if available else "none"


def _find_product_by_ref(products: list, product_ref: Optional[str]) -> Optional[dict]:
    """Match product_ref (partial, case-insensitive) against product titles."""
    if not product_ref:
        return None
    ref = product_ref.strip().lower()
    for p in products:
        if ref in p.get("title", "").lower():
            return p
    return None


# ─────────────────────────────────────────────────────────
# Stage handlers
# ─────────────────────────────────────────────────────────

def _handle_browse(
    message: str,
    state: ConversationState,
    all_products: list,
    shop_domain: str,
) -> tuple[str, list, Optional[str], bool]:
    """
    Browse stage: run LLM to extract filters, filter products, return results.
    Returns (answer, shown_products, storefront_url, needs_clarification).
    """
    result = ask_llm(message, all_products)
    filters = result.get("filters", {})
    intent = result.get("intent", "general_question")
    answer = result.get("answer", "")

    filtered = all_products
    storefront_url = None

    if intent == "filter_products":
        if state.previous_filters:
            filters = merge_filters(filters, state.previous_filters)

        normalized = normalize_filters(filters, all_products)
        filtered = filter_products(all_products, filters)
        storefront_url = build_storefront_url(normalized)

        state.filtered_products = filtered
        state.previous_filters = filters

        if len(filtered) == 0:
            # No matches — stay in browse so user can refine
            state.stage = "browse"
            answer = (
                f"{answer} I couldn't find any products matching your request. "
                f"Try adjusting your filters."
            )
        elif len(filtered) == 1:
            # Exactly one product matched — auto-select it
            state.selected_product = filtered[0]
            state.stage = "awaiting_variant"

            # ── Fast-track: if the user already specified a variant (size/color)
            # in the same utterance, try to resolve it now and skip a turn.
            hint = " ".join(filter(None, [
                filters.get("color"), filters.get("size")
            ])).strip()
            if hint:
                fast_variant = _find_variant(filtered[0], hint)
                if fast_variant and fast_variant.get("availableForSale"):
                    state.selected_variant = fast_variant
                    state.quantity = 1
                    state.stage = "awaiting_confirm"
                    price = float(fast_variant.get("price", 0))
                    answer = (
                        f"Got it! Just to confirm: "
                        f"**{filtered[0]['title']}** — {fast_variant.get('title')} "
                        f"at ${price:.2f}. Shall I add this to your cart?"
                    )
                else:
                    available = _available_variants_text(filtered[0])
                    answer = (
                        f"{answer} I found **{filtered[0]['title']}** but couldn't "
                        f"find '{hint}'. Available options: {available}. "
                        f"Which would you like?"
                    )
            else:
                available = _available_variants_text(filtered[0])
                answer = (
                    f"{answer} I found one match: **{filtered[0]['title']}**. "
                    f"Available options: {available}. "
                    f"Which would you like?"
                )
        else:
            # Multiple matches shown — advance to awaiting_variant so the
            # next message (size/color/quantity/product pick) is handled by
            # ask_llm_cart, not treated as a brand new search.
            state.stage = "awaiting_variant"
            titles = ", ".join(p["title"] for p in filtered[:5])
            suffix = f" (showing {len(filtered)})" if len(filtered) > 5 else ""
            answer = (
                f"{answer} I found {len(filtered)} products{suffix}: {titles}. "
                f"Which one would you like, and what size or colour?"
            )
    else:
        state.filtered_products = all_products

    return answer, filtered, storefront_url, False


def _handle_awaiting_variant(
    message: str,
    state: ConversationState,
    all_products: list,
    shop_domain: str,
) -> tuple[str, list, Optional[str], bool, Optional[dict]]:
    """
    The user has seen product results and is now specifying a variant or saying add to cart.
    Returns (answer, shown_products, storefront_url, needs_clarification, cart).
    """
    result = ask_llm_cart(
        message,
        state.filtered_products or all_products,
        state.selected_product,
    )
    intent = result.get("intent", "general_question")
    product_ref = result.get("product_ref")
    variant_ref = result.get("variant_ref")
    quantity = result.get("quantity", 1)
    answer = result.get("answer", "")

    # ── User wants a completely new search ──────────────────────────────
    if intent == "filter_products":
        state.reset_selection()
        browse_answer, filtered, storefront_url, _ = _handle_browse(
            message, state, all_products, shop_domain
        )
        return browse_answer, filtered, storefront_url, False, None

    # ── User cancels ─────────────────────────────────────────────────────
    if intent == "confirm_no":
        state.reset_selection()
        return (
            "No problem! Let me know what you'd like to look for.",
            all_products,
            None,
            False,
            None,
        )

    # ── Resolve the product ──────────────────────────────────────────────
    product = state.selected_product
    if not product and product_ref:
        product = _find_product_by_ref(state.filtered_products, product_ref)

    if not product:
        # Multiple products visible — ask user to clarify
        if len(state.filtered_products) > 1:
            titles = ", ".join(
                p["title"] for p in state.filtered_products[:5]
            )
            return (
                f"Which product did you mean? I can see: {titles}.",
                state.filtered_products,
                None,
                True,
                None,
            )
        elif len(state.filtered_products) == 1:
            product = state.filtered_products[0]
        else:
            return (
                "I'm not sure which product you're referring to. "
                "Could you tell me the name?",
                state.filtered_products,
                None,
                True,
                None,
            )

    state.selected_product = product

    # ── Resolve the variant ──────────────────────────────────────────────
    variant = _find_variant(product, variant_ref)

    if not variant:
        available = _available_variants_text(product)
        return (
            f"I couldn't find size/option '{variant_ref}' for "
            f"**{product['title']}**. Available options: {available}.",
            state.filtered_products,
            None,
            True,
            None,
        )

    if not variant.get("availableForSale"):
        available = _available_variants_text(product)
        return (
            f"Sorry, **{variant.get('title')}** is currently out of stock for "
            f"**{product['title']}**. Available options: {available}.",
            state.filtered_products,
            None,
            False,
            None,
        )

    # ── All good — save selection and move to confirm ────────────────────
    state.selected_variant = variant
    state.quantity = quantity
    state.stage = "awaiting_confirm"

    price = float(variant.get("price", 0))
    total = price * quantity
    qty_str = f"{quantity}x " if quantity > 1 else ""

    confirm_answer = (
        f"Got it! Just to confirm: "
        f"{qty_str}**{product['title']}** — {variant.get('title')} "
        f"at ${price:.2f} each"
        + (f" (total ${total:.2f})" if quantity > 1 else "")
        + ". Shall I add this to your cart?"
    )

    return confirm_answer, state.filtered_products, None, False, None


def _handle_awaiting_confirm(
    message: str,
    state: ConversationState,
    all_products: list,
    shop_domain: str,
    admin_token: Optional[str] = None,
) -> tuple[str, list, Optional[str], bool, Optional[dict]]:
    """
    User should be saying yes/no to the pending add-to-cart.
    Cart is created/updated via Admin API Draft Orders — no Storefront token needed.
    Returns (answer, shown_products, storefront_url, needs_clarification, cart).
    """
    result = ask_llm_cart(
        message,
        state.filtered_products or all_products,
        state.selected_product,
    )
    intent = result.get("intent", "general_question")

    # ── User confirms ────────────────────────────────────────────────────
    if intent in ("confirm_yes", "add_to_cart"):
        product = state.selected_product
        variant = state.selected_variant

        if not product or not variant:
            state.stage = "browse"
            return (
                "Something went wrong with my memory of your selection. "
                "Could you start over?",
                all_products,
                None,
                False,
                None,
            )

        try:
            quantity = state.quantity

            # Admin API Draft Order — works with existing credentials
            cart = add_to_draft_order(
                session_id=state.cart_id or "session",  # re-use cart_id slot as draft key
                shop_domain=shop_domain,
                product=product,
                variant=variant,
                quantity=quantity,
                admin_token=admin_token,
            )
            # Store draft_id so subsequent adds accumulate into the same order
            state.cart_id = cart.get("cart_id", state.cart_id)

            price = float(variant.get("price", 0))
            qty_str = f"{quantity}x " if quantity > 1 else ""
            checkout_note = (
                f" Use the checkout link to complete your order."
                if cart.get("checkout_url") else ""
            )
            answer = (
                f"✅ Added {qty_str}**{product['title']}** "
                f"({variant.get('title')}) to your cart!{checkout_note}"
            )

            # Reset selection so user can continue shopping
            state.stage = "browse"
            state.selected_product = None
            state.selected_variant = None
            state.quantity = 1

            return answer, state.filtered_products, None, False, cart

        except Exception as exc:
            print(f"Cart error: {exc}")
            return (
                "Sorry, I had trouble adding that to your cart. Please try again.",
                state.filtered_products,
                None,
                False,
                None,
            )

    # ── User cancels ─────────────────────────────────────────────────────
    if intent == "confirm_no":
        state.stage = "awaiting_variant"
        return (
            "No problem! Would you like to choose a different size or colour?",
            state.filtered_products,
            None,
            False,
            None,
        )

    # ── User changes their mind / starts new search ───────────────────────
    if intent == "filter_products":
        state.reset_selection()
        browse_answer, filtered, storefront_url, _ = _handle_browse(
            message, state, all_products, shop_domain
        )
        return browse_answer, filtered, storefront_url, False, None

    # ── Fallback: re-ask ─────────────────────────────────────────────────
    product = state.selected_product
    variant = state.selected_variant
    answer = (
        f"Just to confirm — shall I add **{product['title']} "
        f"({variant.get('title')})** to your cart? Say yes or no."
    )
    return answer, state.filtered_products, None, False, None


# ─────────────────────────────────────────────────────────
# Core conversation dispatcher
# ─────────────────────────────────────────────────────────

def _process_message(
    message: str,
    session_id: str,
    shop_domain: str,
    admin_token: Optional[str] = None,
    transcription: Optional[str] = None,
) -> dict:
    state = session_store.get(session_id)

    # Fetch live product catalog
    all_products = get_products(
        shop_domain=shop_domain,
        admin_token=admin_token if admin_token else None,
    )

    cart = None
    storefront_url = None
    needs_clarification = False

    # ── Stage dispatch ────────────────────────────────────────────────────
    if state.stage in ("browse",):
        answer, shown_products, storefront_url, needs_clarification = _handle_browse(
            message, state, all_products, shop_domain
        )

    elif state.stage == "awaiting_variant":
        answer, shown_products, storefront_url, needs_clarification, cart = (
            _handle_awaiting_variant(message, state, all_products, shop_domain)
        )

    elif state.stage == "awaiting_confirm":
        answer, shown_products, storefront_url, needs_clarification, cart = (
            _handle_awaiting_confirm(
                message, state, all_products, shop_domain, admin_token
            )
        )

    else:
        # Unknown stage — reset
        state.reset_selection()
        answer, shown_products, storefront_url, needs_clarification = _handle_browse(
            message, state, all_products, shop_domain
        )

    session_store.save(session_id, state)

    # TTS
    audio_b64 = None
    audio_path = None
    try:
        audio_path, audio_b64 = _generate_audio(answer)
    except Exception as exc:
        print(f"TTS error (non-fatal): {exc}")
    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass

    return _build_response(
        state=state,
        answer=answer,
        audio_b64=audio_b64,
        products=shown_products,
        needs_clarification=needs_clarification,
        cart=cart,
        storefront_url=storefront_url,
        transcription=transcription or message,
    )


# ─────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "AI Shopping Assistant"}


@app.get("/products")
def products_endpoint(
    shop_domain: str = None,
    admin_token: str = None,
):
    domain = _resolve_domain(shop_domain)
    token = _resolve_admin_token(admin_token)
    return {"products": get_products(shop_domain=domain, admin_token=token)}


@app.post("/voice-text")
@app.post("/apps/ai-assistant/voice-text")
def voice_text(request: TextRequest):
    shop_domain  = _resolve_domain(request.shop_domain)
    admin_token  = _resolve_admin_token(request.admin_token)

    return _process_message(
        message=request.message,
        session_id=request.session_id,
        shop_domain=shop_domain,
        admin_token=admin_token,
    )


@app.post("/voice")
@app.post("/apps/ai-assistant/voice")
async def voice(

    file: UploadFile = File(...),
    session_id: str = Form("default"),
    shop_domain: str = Form(None),
    admin_token: str = Form(None),
):
    resolved_domain      = _resolve_domain(shop_domain)
    resolved_admin_token = _resolve_admin_token(admin_token)

    suffix = os.path.splitext(file.filename or "")[1] or ".webm"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        transcription = speech_to_text(tmp_path)
        print("Transcription:", transcription)

        if not transcription or not transcription.strip():
            return {"answer": "I didn't hear anything. Please try again.", "stage": "browse"}

        result = _process_message(
            message=transcription,
            session_id=session_id,
            shop_domain=resolved_domain,
            admin_token=resolved_admin_token,
            transcription=transcription,
        )
        return result

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.post("/session/reset")
def reset_session(session_id: str = "default"):
    session_store.delete(session_id)
    return {"status": "ok", "session_id": session_id}


# ─────────────────────────────────────────────────────────
# Legacy /ask endpoint (kept for compatibility)
# ─────────────────────────────────────────────────────────

from pydantic import BaseModel as BM

class AskRequest(BM):
    message: str
    previous_filters: Optional[dict] = None


@app.post("/ask")
def ask(request: AskRequest):
    all_products = get_products()
    result = ask_llm(request.message, all_products)
    filters = result.get("filters", {})
    filtered = all_products
    if result.get("intent") == "filter_products":
        filtered = filter_products(all_products, filters)
    return {
        "answer": result.get("answer", ""),
        "intent": result.get("intent", "general_question"),
        "filters": filters,
        "products": filtered,
    }