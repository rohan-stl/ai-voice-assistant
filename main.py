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
import re
import tempfile
import base64
import hashlib
import time
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

import uuid
from shopify_auth import ShopContext, verify_app_proxy_signature, validate_shop_domain
from intent_router import (
    extract_quantity,
    _extract_quantity_delta,
    _handle_remove_from_cart,
    _build_precise_product_url,
    _handle_browse,
    _handle_awaiting_variant,
    _handle_awaiting_confirm,
    _parse_variant_title_to_options,
    _score_line_match,
    _classify_cart_quantity_intent,
    _handle_cart_quantity_intent,
    _collect_operation_options,
    _handle_awaiting_cart_confirm,
    route_message
)

from store_credentials_db import get_store_credentials
from shopify_service import (
    get_products, get_collections, get_collection_products, match_collection,
    extract_variant_nodes, normalize_shopify_variant, determine_product_availability
)
from llm_service import ask_llm, ask_llm_cart, _coerce_llm_filters
from voice_service import speech_to_text, text_to_speech
from product_filter import filter_products, normalize_filters, merge_filters, StructuredQuery
from storefront_url import build_storefront_url
from conversation_state import session_store, ConversationState
from cart_service import (
    save_pending_cart_selection, get_session_cart_context, remove_from_cart_by_variant,
    clear_session_cart_context, sync_native_cart,
    update_cart_line_quantity, fetch_live_cart_line,
)
import logger as log
from constants import WORD_TO_NUM

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
    message: Optional[str] = ""
    session_id: str = "default"
    sequence_num: Optional[int] = None
    shop_domain: Optional[str] = None
    admin_token: Optional[str] = None
    previous_filters: Optional[dict] = None
    cart_context: Optional[dict] = None
    intent: Optional[str] = None
    action_id: Optional[str] = None
    success: Optional[bool] = None
    result: Optional[dict] = None
    error: Optional[dict] = None
    product_id: Optional[str] = None
    option_name: Optional[str] = None
    option_value: Optional[str] = None
    selection_revision: Optional[int] = None


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────


USER_INPUT_STAGES = {
    "awaiting_product_clarification",
    "awaiting_collection",
    "awaiting_variant",
    "awaiting_variant_option",
    "awaiting_size",
    "awaiting_color",
    "awaiting_quantity",
    "awaiting_confirm",
    "awaiting_cart_confirm",
}






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
    auto_checkout: bool = False,
    removed_variant_id: Optional[str] = None,
    clear_cart: bool = False,
    updated_quantity: Optional[int] = None,
    added_variant_id: Optional[str] = None,
    added_quantity: Optional[int] = None,
    intent: Optional[str] = None,
    close_widget: bool = False,
    disable_hands_free: bool = False,
    auto_listen: Optional[bool] = None,
    sequence_num: Optional[int] = None,
    updated_variant_id: Optional[str] = None,
    shop_domain: Optional[str] = None,
) -> dict:
    resolved_stage = state.stage
    resolved_intent = intent or state.stage

    if needs_clarification and resolved_stage not in USER_INPUT_STAGES:
        resolved_stage = "awaiting_product_clarification"
        state.stage = "awaiting_product_clarification"

    if close_widget or resolved_intent in ("cancel", "end_conversation", "close_assistant"):
        resolved_intent = "cancel" if resolved_intent in ("cancel", "end_conversation", "close_assistant") else resolved_intent
        resolved_stage = "idle" if resolved_intent == "cancel" else resolved_stage
        requires_input = False
        conversation_complete = True
        auto_listen = False
        auto_close = True
    elif (state.stage in ("idle", "browse", "disabled") and not needs_clarification) or resolved_intent in ("disable_hands_free",):
        requires_input = False
        conversation_complete = True
        auto_listen = False
        auto_close = False
    else:
        requires_input = (
            (state.stage in USER_INPUT_STAGES or resolved_stage in USER_INPUT_STAGES or needs_clarification)
        ) and not added_variant_id and not auto_checkout
        conversation_complete = not requires_input
        if auto_listen is None:
            auto_listen = requires_input
        auto_close = conversation_complete and not requires_input

    # Missing options calculation & canonical stage resolution
    missing_options = []
    variant_options_list = []
    pending_option_name = None
    availability_status = "unknown"
    is_sold_out = False

    if state.selected_product:
        vars_nodes = extract_variant_nodes(state.selected_product.get("variants", {}))
        norm_vars = [normalize_shopify_variant(v) for v in vars_nodes]
        availability_status = determine_product_availability(norm_vars, True)
        if availability_status == "sold_out":
            is_sold_out = True

        opt_map = get_remaining_option_values(state.selected_product, state.selected_options, shop_domain=shop_domain or "")
        for opt_name, vals_items in opt_map.items():
            variant_options_list.append({
                "name": opt_name,
                "values": vals_items
            })
            if not pending_option_name and not any(item["selected"] for item in vals_items):
                pending_option_name = opt_name
            if not any(item["selected"] for item in vals_items):
                missing_options.append(opt_name)

        if is_sold_out:
            # Enforce schema consistency: never return both sold_out: true and non-empty variant_options
            variant_options_list = []

    if state.stage in ("awaiting_variant", "awaiting_variant_option", "awaiting_size", "awaiting_color"):
        resolved_stage = "awaiting_variant_option"

    expected_input = []
    if requires_input:
        if state.stage in ("awaiting_confirm", "awaiting_cart_confirm"):
            expected_input = ["confirmation"]
        elif state.stage == "awaiting_cart_quantity":
            expected_input = ["quantity"]
        elif state.stage == "awaiting_product_clarification":
            expected_input = ["product"]
        elif state.stage == "awaiting_collection":
            expected_input = ["collection"]
        elif state.stage in ("awaiting_variant", "awaiting_variant_option", "awaiting_size", "awaiting_color") or resolved_stage == "awaiting_variant_option":
            if missing_options:
                expected_input = [missing_options[0].lower()]
            else:
                expected_input = ["variant"]

    pending_action = state.get_pending_cart_action()
    cart_action = None
    requires_client_action = False
    if pending_action and pending_action.get("status") == "pending":
        # ── Stale-action guard: if the pending action is older than 45 s the
        #    frontend ACK was never received (e.g. page refresh before ACK).
        #    Auto-expire it so we do NOT re-send requires_client_action and
        #    accidentally add the item a second time.
        action_age = time.time() - (pending_action.get("created_at") or 0)
        if action_age > 45:
            state.update_pending_cart_action_status("expired")
            state.clear_pending_cart_action()
        else:
            cart_action = pending_action
            requires_client_action = True
            resolved_stage = "awaiting_client_cart_action"
            requires_input = False
            conversation_complete = False
            auto_listen = False
            auto_close = False

    # ── Enforce response-schema validation rules ────────────────────────────────
    if requires_input:
        conversation_complete = False
    if auto_listen:
        requires_input = True
        conversation_complete = False
        auto_close = False
    if auto_close:
        conversation_complete = True
        requires_input = False
        auto_listen = False

    conversation_active = not conversation_complete
    immediate_close = resolved_intent == "cancel" or close_widget or any(cw in (transcription or "").lower() for cw in ("close assistant", "close the assistant", "close it", "cancel"))

    selected_variant_id = added_variant_id or (state.selected_variant.get("id") if state.selected_variant else None)

    is_explicit_nav = any(kw in (transcription or "").lower() for kw in ("open", "take me to", "go to", "navigate to"))

    return {
        "transcription": transcription,
        "answer": answer,
        "message": answer,
        "stage": resolved_stage,
        "intent": resolved_intent,
        "availability_status": availability_status,
        "sold_out": is_sold_out,
        "requires_input": requires_input,
        "requires_client_action": requires_client_action,
        "cart_action": cart_action,
        "expected_input": expected_input,
        "conversation_active": conversation_active,
        "conversation_complete": conversation_complete,
        "auto_listen": auto_listen,
        "auto_close": auto_close,
        "immediate_close": immediate_close,
        "close_widget": close_widget or immediate_close,
        "disable_hands_free": disable_hands_free,
        "conversation_id": state.session_id,
        "product_id": state.selected_product.get("id") if state.selected_product else None,
        "product_title": state.selected_product.get("title") if state.selected_product else None,
        "selected_options": state.selected_options,
        "missing_options": missing_options,
        "variant_options": variant_options_list,
        "pending_option_name": pending_option_name,
        "selection_revision": state.selection_revision,
        "invalid_requested_options": state.invalid_requested_options or None,
        "selected_variant_id": selected_variant_id,
        "sequence_num": sequence_num,
        "filters": state.previous_filters,
        "products": products,
        "selected_product": state.selected_product,
        "selected_variant": state.selected_variant,
        "quantity": state.quantity,
        "needs_clarification": needs_clarification,
        "cart": cart,
        "storefront_url": storefront_url,
        "audio": audio_b64,
        "auto_checkout": auto_checkout,
        "removed_variant_id": removed_variant_id,
        "clear_cart": clear_cart,
        "updated_quantity": updated_quantity,
        "updated_variant_id": updated_variant_id,
        "added_variant_id": added_variant_id,
        "added_quantity": added_quantity,
        "collection": state.active_collection,
        "collection_candidates": state.collection_candidates if state.stage == "awaiting_collection" else [],
        "collection_has_next_page": state.collection_has_next_page,
        "total_matches": len(products),
        "navigation": {
            "requested": is_explicit_nav,
            "url": storefront_url,
        } if storefront_url else None,
    }





def _generate_audio(text: str) -> tuple[str, str]:
    """Generate TTS audio. Returns (output_path, base64_string)."""
    output_path = tempfile.mktemp(suffix=".mp3")
    text_to_speech(text, output_path)
    with open(output_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    return output_path, audio_b64


from variant_resolver import (
    _SIZE_MAP, _SIZE_SYNONYMS, _SIZE_CANONICAL,
    _normalize_option_word,
    _extract_options_from_text,
    _get_product_option_values,
    _has_incomplete_quantity_phrase,
    _extract_all_unvalidated_options,
    _validate_and_apply_options,
    _resolve_variant_by_options,
    _find_variant,
    _is_variant_in_stock,
    is_shopify_default_title,
    get_remaining_option_values,
    _get_product_options_summary,
    _available_variants_text,
    _format_product_search_summary,
    _find_product_by_ref,
    _check_missing_options,
    invalidate_options_cache,
)



# ─────────────────────────────────────────────────────────
# Stage handlers
# ─────────────────────────────────────────────────────────














def _has_incomplete_quantity_phrase(text: str) -> bool:
    """
    True if user said a phrase like 'quantity should be', 'quantity to', 'make quantity'
    without specifying a numeric value.
    """
    clean = text.lower()
    if re.search(r"\b\d+\b", clean):
        return False
    for word in WORD_TO_NUM.keys():
        if re.search(r"\b" + re.escape(word) + r"\b", clean):
            return False

    incomplete_patterns = [
        r"\bquantity\s+(?:should\s+be|to\s+be|is|to|=|:)?\s*$",
        r"\bqty\s+(?:should\s+be|to\s+be|is|to|=|:)?\s*$",
        r"\bquantity\s+(?:should\s+be|to\s+be|is|to|=|:)\b",
        r"\bqty\s+(?:should\s+be|to\s+be|is|to|=|:)\b",
    ]
    return any(re.search(pat, clean) for pat in incomplete_patterns)












# ─────────────────────────────────────────────────────────
# Core conversation dispatcher
# ─────────────────────────────────────────────────────────

def _clean_transcription(text: str) -> str:
    if not text:
        return ""
    # Remove bracketed annotations like [music] [laughter] etc.
    cleaned = re.sub(r"\[.*?\]", "", text)
    cleaned = re.sub(r"\(.*?\)", "", cleaned)
    # Common speech-to-text (STT) mishearings for sizes & options
    stt_replacements = [
        (r"\bsize\s+them\b", "size M"),
        (r"\bsize\s+then\b", "size M"),
        (r"\bsize\s+end\b", "size M"),
        (r"\bsize\s+am\b", "size M"),
        (r"\bsize\s+el\b", "size L"),
        (r"\bsize\s+ex\b", "size X"),
        # STT: "card" / "rand" / "cart" mishearings
        (r"\bfrom\s+the\s+card\b", "from the cart"),
        (r"\bfrom\s+card\b", "from cart"),
        (r"\bin\s+the\s+card\b", "in the cart"),
        (r"\bin\s+my\s+card\b", "in my cart"),
        (r"\bmy\s+card\b", "my cart"),
        (r"\bin\s+my\s+rand\b", "in my cart"),
        (r"\bmy\s+rand\b", "my cart"),
        (r"\bin\s+the\s+rand\b", "in the cart"),
        (r"\bempty\s+the\s+card\b", "empty the cart"),
        (r"\bclear\s+the\s+card\b", "clear the cart"),
        (r"\bclear\s+card\b", "clear cart"),
        (r"\bempty\s+card\b", "empty cart"),
        (r"\bdrop\s+all\s+item\b", "drop all items"),
        # Cart inquiry phrasings
        (r"\bcart\s+details\b", "what is in my cart"),
        (r"\bcurrent\s+cart\b", "my cart"),
        (r"\bcart\s+contents\b", "what is in my cart"),
        (r"\bwhat\s+(is|are|do i have)\s+in\s+my\s+cart\b", "what is in my cart"),
    ]
    for pattern, replacement in stt_replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    # Strip leading/trailing punctuation and whitespace only
    cleaned = re.sub(r"^[\s.,:!?\-]+|[\s.,:!?\-]+$", "", cleaned).strip()
    return cleaned


def _process_message(
    message: str,
    session_id: str,
    shop_domain: str,
    shop_context: Optional[ShopContext] = None,
    admin_token: Optional[str] = None,
    transcription: Optional[str] = None,
    cart_context: Optional[dict] = None,
    sequence_num: Optional[int] = None,
) -> dict:
    state = session_store.get(session_id)

    # Sync backend cart with live Shopify native store cart (/cart.js) if provided
    if cart_context:
        sync_native_cart(session_id, cart_context)

    # ── Auto-acknowledge pending add-actions already in cart (prevents refresh double-add) ──
    # When a page refresh happens BEFORE the frontend ACK reaches us, the pending
    # action stays "pending".  The next request carries cart_context from the live
    # Shopify cart.  If the item is already there at the expected qty we auto-ACK
    # so the frontend doesn't execute the add a second time.
    _pa = state.get_pending_cart_action()
    if _pa and _pa.get("status") == "pending" and cart_context:
        _pa_age = time.time() - (_pa.get("created_at") or 0)
        _pa_op  = _pa.get("expected_operation") or _pa.get("type", "")
        if _pa_op == "add" and _pa_age > 5:          # >5 s: real ACK would have arrived by now
            _pa_var = str(_pa.get("variant_id") or "").split("/")[-1]
            _pa_qty = int(_pa.get("quantity") or 1)
            if _pa_var:
                _cart_qty = sum(
                    item.get("quantity", 0)
                    for item in (cart_context.get("items") or [])
                    if str(item.get("variant_id", "")).split("/")[-1] == _pa_var
                )
                if _cart_qty >= _pa_qty:
                    log.log_info(
                        f"[AutoACK] Pending add for variant {_pa_var} "
                        f"already in cart (cart_qty={_cart_qty} ≥ expected={_pa_qty}). "
                        f"Auto-acknowledging to prevent double-add."
                    )
                    state.update_pending_cart_action_status("succeeded")
                    state.clear_pending_cart_action()
                    state.stage = "browse"

    # Fetch live product catalog (cached 60s)
    prev_count = len(getattr(_process_message, "_last_product_count", {}).get(shop_domain, []))
    all_products = get_products(
        shop_domain=shop_domain,
        shop_context=shop_context,
        admin_token=admin_token if admin_token else None,
    )
    # Invalidate option vocabulary cache if the product list changed
    if len(all_products) != prev_count:
        invalidate_options_cache()
        if not hasattr(_process_message, "_last_product_count"):
            _process_message._last_product_count = {}
        _process_message._last_product_count[shop_domain] = all_products

    cleaned_msg = _clean_transcription(message)
    # Accept single characters (e.g. "M", "L", "S" for sizes; "Y"/"N" for yes/no)
    if not cleaned_msg:
        return _build_response(
            state=state,
            answer="I didn't hear anything. Tap the microphone to speak.",
            audio_b64=None,
            products=state.filtered_products or all_products,
            needs_clarification=False,
            cart=None,
            storefront_url=None,
            transcription=transcription or message,
            auto_checkout=False,
            removed_variant_id=None,
            clear_cart=False,
        )

    cart = None
    storefront_url = None
    needs_clarification = False
    auto_checkout = False
    removed_variant_id = None
    updated_quantity = None
    added_variant_id = None
    added_quantity = None

    # ── Idempotency: guard against voice-retry double mutations ───────────────
    # Use a 10-second time bucket so the same voice command sent twice within
    # 10 s is treated as a single request. The guard only suppresses MUTATIONS
    # (cart update/remove); reads and browse responses are always returned.
    _bucket = int(time.time() // 10)
    _req_id = hashlib.md5(
        f"{session_id}:{cleaned_msg}:{_bucket}".encode()
    ).hexdigest()[:12]
    _is_duplicate = _req_id in (state.processed_request_ids or [])
    # (We will add _req_id to processed_request_ids after a successful mutation)

    # ── Fast 0ms Deterministic Execution ──────────────────────────────
    fast_res = route_message(message, state, all_products, session_id, shop_domain, admin_token, shop_context)
    if fast_res:
        (
            answer,
            shown_products,
            storefront_url,
            needs_clarification,
            cart,
            auto_checkout,
            removed_variant_id,
            clear_cart,
            updated_quantity,
            added_variant_id,
            added_quantity,
        ) = fast_res[:11]

        intent = fast_res[11] if len(fast_res) > 11 else None
        close_widget = fast_res[12] if len(fast_res) > 12 else False
        disable_hands_free = fast_res[13] if len(fast_res) > 13 else False
        updated_variant_id = fast_res[14] if len(fast_res) > 14 else None


        log.log_info(f"⚡ Fast-path matched: stage={state.stage}")
        if storefront_url:
            log.log_storefront_url(storefront_url)
        if shown_products:
            log.log_products(len(shown_products), shown_products)
        response = _build_response(
            state=state,
            answer=answer,
            audio_b64=None,
            products=shown_products,
            needs_clarification=needs_clarification,
            cart=cart,
            storefront_url=storefront_url,
            transcription=transcription or message,
            auto_checkout=auto_checkout,
            removed_variant_id=removed_variant_id,
            clear_cart=clear_cart,
            updated_quantity=updated_quantity,
            added_variant_id=added_variant_id,
            added_quantity=added_quantity,
            intent=intent,
            close_widget=close_widget,
            disable_hands_free=disable_hands_free,
            sequence_num=sequence_num,
            updated_variant_id=updated_variant_id,
        )
        session_store.save(session_id, state)
        return response

    # ── Check for Remove / Add Intent ──────────────────────────────────
    remove_pattern = r"\b(remove|removed|removes|delete|deleted|deleting|take out|took out|drop|dropped|clear|empty|get rid of|discard|cancel)\b"
    add_pattern = r"\b(add|put|place|buy|play|pay|ad|had|plus|select|take|pick|get|bring|and)\b.*?\b(to|in|into)?\s*(my|the)?\s*(cart|basket|bag|checkout|checkout page)\b"
    add_keywords = (
        "add to cart", "and to cart", "add it to cart", "buy it", "buy now", "add this", "add to my cart",
        "put in cart", "add it to my cart", "add to cart.", "to the checkout page",
        "to checkout page", "to checkout", "go to checkout", "and go to checkout", "to cart", "into cart"
    )
    msg_lower = message.lower()
    is_remove_request = bool(re.search(remove_pattern, msg_lower))
    is_add_request = bool(re.search(add_pattern, msg_lower)) or any(k in msg_lower for k in add_keywords) or msg_lower.startswith("play ")


    if is_remove_request:
        cart_llm = ask_llm_cart(message, all_products, state.selected_product)
        prod_ref = cart_llm.get("product_ref")
        var_ref = cart_llm.get("variant_ref")

        answer, removed_variant_id, cart, updated_quantity = _handle_remove_from_cart(
            message=message,
            state=state,
            session_id=session_id,
            shop_domain=shop_domain,
            admin_token=admin_token,
            product_ref=prod_ref,
            variant_ref=var_ref,
            all_products=all_products,
        )
        shown_products = state.filtered_products or all_products

    elif is_add_request or state.stage in ("awaiting_variant", "awaiting_variant_option", "awaiting_size", "awaiting_color", "awaiting_product_clarification"):
        res = _handle_awaiting_variant(message, state, all_products, shop_domain, session_id, admin_token)
        answer, shown_products, storefront_url, needs_clarification, cart, auto_checkout = res[:6]
        if len(res) > 6:
            added_variant_id = res[6]
            added_quantity = res[7]

    # ── Stage dispatch ────────────────────────────────────────────────────
    elif state.stage == "awaiting_cart_confirm":
        # Cart quantity update confirmation — runs when the fast-path did not
        # catch a yes/no answer (e.g. the user replied with only a quantity change).
        if not _is_duplicate:
            res = _handle_awaiting_cart_confirm(
                message, state, all_products, session_id, shop_domain, admin_token
            )
            if res:
                answer, shown_products, storefront_url, needs_clarification, cart, auto_checkout = res[:6]
                if cart:
                    # Record successful mutation for idempotency
                    state.processed_request_ids = ([_req_id] + state.processed_request_ids)[:20]
            else:
                # Handler returned None (unrecognised input) — re-ask
                ops = state.pending_operations
                if ops:
                    vt = ops[0].get("variant_title", "")
                    vd = f" {vt}" if vt.lower() not in ("", "default title", "default") else ""
                    answer = (
                        f"Just to confirm — shall I update your{vd} **{ops[0]['product_title']}** "
                        f"quantity to {ops[0]['final_quantity']}?"
                    )
                else:
                    answer = "I'm not sure what you'd like to update. Could you say yes or no?"
                shown_products = state.filtered_products or all_products
        else:
            log.log_info(f"⚡ Idempotent duplicate blocked: {_req_id}")
            answer = "Done — your cart is already up to date."
            cart = get_session_cart_context(session_id)
            shown_products = state.filtered_products or all_products

    elif state.stage in ("browse",):
        answer, shown_products, storefront_url, needs_clarification = _handle_browse(
            message, state, all_products, shop_domain
        )

    elif state.stage == "awaiting_confirm":
        res = _handle_awaiting_confirm(message, state, all_products, shop_domain, session_id, admin_token)
        answer, shown_products, storefront_url, needs_clarification, cart, auto_checkout = res[:6]
        if len(res) > 6:
            added_variant_id = res[6]
            added_quantity = res[7]

    else:
        # Unknown stage — reset
        state.reset_selection()
        answer, shown_products, storefront_url, needs_clarification = _handle_browse(
            message, state, all_products, shop_domain
        )



    if storefront_url:
        log.log_storefront_url(storefront_url)
    if shown_products:
        log.log_products(len(shown_products), shown_products)




    # TTS — generate server audio for all responses
    audio_b64 = None
    audio_path = None
    try:
        audio_path, audio_b64 = _generate_audio(answer)
        log.log_tts(True)
    except Exception as exc:
        log.log_warn("TTS", str(exc))
    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass


    response = _build_response(
        state=state,
        answer=answer,
        audio_b64=audio_b64,
        products=shown_products,
        needs_clarification=needs_clarification,
        cart=cart,
        storefront_url=storefront_url,
        transcription=transcription or message,
        auto_checkout=auto_checkout,
        # Only send removed_variant_id for true full-removes (qty → 0).
        # For quantity reductions, send None to prevent the legacy frontend
        # /cart/change.js path from setting quantity=0 and wiping the line.
        removed_variant_id=removed_variant_id if (updated_quantity is None or updated_quantity == 0) else None,
        updated_quantity=updated_quantity,
        added_variant_id=added_variant_id,
        added_quantity=added_quantity,
        sequence_num=sequence_num,
    )
    session_store.save(session_id, state)
    return response




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


from shopify_auth import (
    ShopContext, resolve_shop_context, validate_shop_domain, verify_app_proxy_signature,
    InvalidShopDomainError, InvalidProxySignatureError, StorefrontConfigurationMissing,
)
from shopify_service import (
    get_products, get_collections, get_collection_products, match_collection,
    ShopifyAuthenticationError, ShopifyUpstreamError,
)

def handle_client_cart_action_result(
    session_id: str,
    action_id: Optional[str],
    success: Optional[bool],
    error: Optional[dict],
    result_data: Optional[dict],
    state: ConversationState,
    all_products: list,
) -> dict:
    pending_action = state.get_pending_cart_action()

    # Validate pending action and action_id match
    if not pending_action or pending_action.get("action_id") != action_id:
        return _build_response(
            state=state,
            answer="I couldn't verify the pending cart action.",
            audio_b64=None,
            products=state.filtered_products or all_products,
            intent="invalid_action_ack"
        )

    # Use backend authority for product & variant titles
    p_title = pending_action.get("product_title", "item")
    v_title = pending_action.get("variant_title", "")
    op_type = pending_action.get("expected_operation", "add")
    qty = pending_action.get("quantity", 1)

    if success is True:
        state.update_pending_cart_action_status("succeeded")
        state.clear_pending_cart_action()
        state.stage = "browse"
        state.reset_selection()

        qty_str = f"{qty}x " if qty > 1 else ""
        v_str = f" ({v_title})" if v_title else ""
        if op_type == "add":
            answer = f"✅ Added {qty_str}**{p_title}**{v_str} to your cart!"
        elif op_type == "change":
            answer = f"✅ Updated **{p_title}**{v_str} quantity to {qty}!"
        elif op_type == "remove":
            answer = f"✅ Removed **{p_title}**{v_str} from your cart!"
        elif op_type == "clear":
            answer = f"✅ Cleared all items from your cart!"
        else:
            answer = f"✅ Updated your cart!"

        state.stage = "browse"
        return _build_response(
            state=state,
            answer=answer,
            audio_b64=None,
            products=all_products,
            intent="cart_action_success",
            close_widget=True
        )
    else:
        state.update_pending_cart_action_status("failed")
        err_code = (error or {}).get("code", "UNKNOWN_ERROR")

        if err_code == "CART_VARIANT_UNAVAILABLE":
            answer = f"Sorry, that option is no longer available for **{p_title}**."
        elif err_code == "CART_QUANTITY_UNAVAILABLE":
            answer = f"Sorry, that quantity isn't available for **{p_title}**. Please choose a smaller amount."
        elif err_code == "PRODUCT_NOT_PUBLISHED":
            answer = f"This product is not available in the online store."
        elif err_code == "HTTP_429":
            answer = f"The cart is busy. Please wait a moment and try again."
        elif err_code == "VARIANT_RESOLUTION_INCOMPLETE":
            answer = f"Could not determine the exact variant for **{p_title}**. Please select options directly."
        else:
            answer = f"I couldn't update your cart. Please try again."

        state.clear_pending_cart_action()
        # Keep product selection active so customer can retry
        return _build_response(
            state=state,
            answer=answer,
            audio_b64=None,
            products=state.filtered_products or all_products,
            intent="cart_action_failed"
        )


@app.post("/voice-text")
@app.post("/apps/ai-assistant/voice-text")
def voice_text(request: TextRequest, raw_request: Request = None):
    query_params = dict(raw_request.query_params) if raw_request else {}
    try:
        shop_context = resolve_shop_context(request.shop_domain, query_params)
    except InvalidShopDomainError as err:
        return JSONResponse(status_code=200, content={"success": False, "status_code": 400, "error": {"code": "INVALID_SHOP_DOMAIN", "message": str(err), "retryable": False}})
    except InvalidProxySignatureError as err:
        return JSONResponse(status_code=200, content={"success": False, "status_code": 403, "error": {"code": "INVALID_PROXY_SIGNATURE", "message": str(err), "retryable": False}})
    except StorefrontConfigurationMissing as err:
        return JSONResponse(status_code=200, content={"success": False, "status_code": 503, "error": {"code": "STOREFRONT_CONFIGURATION_MISSING", "message": "The shopping assistant is not completely configured for this store.", "retryable": False}})

    try:
        shop_domain = shop_context.shop_domain
        log.log_request_start(request.session_id, shop_domain)

        # ── SYSTEM EVENT: Handle Client Cart Action Acknowledgement ────────────────
        if request.intent == "client_cart_action_result":
            state = session_store.get(request.session_id)
            try:
                all_prods = get_products(shop_domain=shop_domain, shop_context=shop_context, admin_token=shop_context.admin_token)
            except Exception:
                all_prods = []
            res = handle_client_cart_action_result(
                session_id=request.session_id,
                action_id=request.action_id,
                success=request.success,
                error=request.error,
                result_data=request.result,
                state=state,
                all_products=all_prods,
            )
            session_store.save(request.session_id, state)
            return res
        log.log_info(f"💬 Text Input (Browser STT): \"{request.message}\"")

        result = _process_message(
            message=request.message,
            session_id=request.session_id,
            shop_domain=shop_domain,
            shop_context=shop_context,
            admin_token=shop_context.admin_token,
            cart_context=request.cart_context,
            sequence_num=request.sequence_num,
        )

        log.log_stage(result.get("stage", "browse"))
        log.log_answer(result.get("answer", ""))

        return result
    except ShopifyAuthenticationError as exc:
        log.log_warn("SHOPIFY_AUTH", str(exc))
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "status_code": 503,
                "error": {
                    "code": "SHOPIFY_AUTH_FAILED",
                    "message": "The shopping assistant is not completely configured for this store.",
                    "retryable": False,
                }
            }
        )
    except ShopifyUpstreamError as exc:
        log.log_error("SHOPIFY_UPSTREAM", exc)
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "status_code": 502,
                "error": {
                    "code": "SHOPIFY_UPSTREAM_ERROR",
                    "message": "Unable to communicate with Shopify services. Please try again.",
                    "retryable": True,
                }
            }
        )
    except Exception as exc:
        log.log_error("voice_text endpoint error", exc)
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "status_code": 500,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected backend error occurred.",
                    "retryable": True,
                }
            }
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
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        log.log_request_start(session_id, resolved_domain)
        log.log_stt(len(content), "…")

        # speech_to_text() never raises — returns "" on any error/quota issue
        transcription = speech_to_text(tmp_path)

        if not transcription or not transcription.strip():
            log.log_warn("STT", "Empty transcription — quota likely exhausted, signalling client to use browser STT")
            # Tell the frontend to fall back to Web Speech API
            return JSONResponse(
                status_code=200,
                content={
                    "answer": "",
                    "stage": "browse",
                    "use_browser_stt": True,
                    "transcription": "",
                    "products": [],
                    "audio": None,
                }
            )

        log.log_stt(len(content), transcription)

        result = _process_message(
            message=transcription,
            session_id=session_id,
            shop_domain=resolved_domain,
            admin_token=resolved_admin_token,
            transcription=transcription,
        )

        log.log_stage(result.get("stage", "browse"))
        log.log_answer(result.get("answer", ""))

        return result

    except Exception as exc:
        log.log_error("Voice endpoint", exc)
        return JSONResponse(
            status_code=200,   # Return 200 so CORS headers are included
            content={
                "answer": "Sorry, I had trouble processing your voice. Please type your message instead.",
                "stage": "browse",
                "use_browser_stt": True,
                "transcription": "",
                "products": [],
                "audio": None,
            }
        )

    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass



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