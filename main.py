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

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from shopify_service import get_products, get_collections, get_collection_products, match_collection
from llm_service import ask_llm, ask_llm_cart
from voice_service import speech_to_text, text_to_speech
from product_filter import filter_products, normalize_filters, merge_filters
from storefront_url import build_storefront_url
from conversation_state import session_store, ConversationState
from cart_service import (
    add_to_draft_order, get_draft_order, remove_from_cart_by_variant,
    clear_draft_order, sync_native_cart,
    update_cart_line_quantity, fetch_live_cart_line,
)
import logger as log

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
    sequence_num: Optional[int] = None
    shop_domain: Optional[str] = None
    admin_token: Optional[str] = None
    previous_filters: Optional[dict] = None
    cart_context: Optional[dict] = None


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "single": 1,
}

def extract_quantity(text: str) -> int:
    text_lower = text.lower()
    # Strip size designations like "size 9", "size 10", "sz 9", "waist 32" so digits after "size" are not treated as quantity
    clean_text = re.sub(r"\b(size|sz|waist|length|number|no|#)\s*:?\s*#?\d+\b", "", text_lower)

    # Check for explicit action quantity (e.g. "add 2", "get 3", "buy 2")
    m_action = re.search(r"\b(?:add|get|buy|want|need|select)\s+(\d+)\b", clean_text)
    if m_action:
        val = int(m_action.group(1))
        if 1 <= val <= 99:
            return val

    # Match standalone digit only if not part of size designation
    m = re.search(r"\b(\d+)\b", clean_text)
    if m:
        val = int(m.group(1))
        if 1 <= val <= 99:
            return val

    for word, val in WORD_TO_NUM.items():
        if re.search(r"\b" + word + r"\b", clean_text):
            return val

    return 1


def _extract_quantity_delta(text: str) -> tuple[str, int]:
    """
    Return (mode, delta) for quantity-update language.

    mode values:
      "increment" – "add N more", "increase by N", "give me N more"
      "set"       – "set to N", "make it N", "change from X to N"
      "add_new"   – generic quantity (N) with no increment/set language

    delta: the integer quantity (1 if not found).
    """
    text_lower = text.lower()
    # Strip size tokens so "size 9" is not treated as quantity
    clean = re.sub(r"\b(size|sz|waist|length|number|no|#)\s*:?\s*#?\d+\b", "", text_lower)

    # ── "change from X to Y" → set to Y ─────────────────────────────────────
    m = re.search(r"\bchange\s+(?:it\s+)?from\s+\d+\s+to\s+(\d+)\b", clean)
    if m:
        return ("set", max(1, int(m.group(1))))

    # ── Set-language: "set/make/change quantity to N" ────────────────────────
    for pat in [
        r"\b(?:set|make|change)\s+(?:the\s+)?(?:quantity|qty)\s+(?:to\s+)?(\w+)\b",
        r"\b(?:set|make)\s+it\s+(?:to\s+)?(?:exactly\s+)?(\w+)\b",
        r"\bmake\s+that\s+(\w+)\b",
        r"\bquantity\s+to\s+(\w+)\b",      # "quantity to two" anywhere in sentence
        r"\bqty\s+to\s+(\w+)\b",
    ]:

        m = re.search(pat, clean)
        if m:
            val_str = m.group(1)
            val = WORD_TO_NUM.get(val_str) or (int(val_str) if val_str.isdigit() else None)
            if val and 1 <= val <= 99:
                return ("set", val)

    # ── Increment-language: "add N more", "N more", "increase by N" ──────────
    for pat in [
        r"\badd\s+(\w+)\s+more\b",
        r"\bgive\s+me\s+(\w+)\s+more\b",
        r"\bincrease\s+(?:it\s+|the\s+)?by\s+(\w+)\b",
        r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+more\b",
        r"\badd\s+(?:another|additional)\s+(\w+)\b",
    ]:
        m = re.search(pat, clean)
        if m:
            val_str = m.group(1)
            val = WORD_TO_NUM.get(val_str) or (int(val_str) if val_str.isdigit() else None)
            if val and 1 <= val <= 99:
                return ("increment", val)

    # ── Detect intent by keyword even if quantity is not next to the keyword ─
    has_more_lang = bool(re.search(
        r"\b(more|increase|additional|another|extra|again)\b", clean
    ))
    has_set_lang = bool(re.search(
        r"\b(set|make|change|final|total|exactly)\b", clean
    ))

    qty = extract_quantity(text)   # fallback numeric extraction
    if has_more_lang:
        return ("increment", qty)
    if has_set_lang:
        return ("set", qty)
    return ("add_new", qty)


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
) -> dict:
    resolved_intent = intent or state.stage

    if state.stage in ("idle", "browse", "disabled") or resolved_intent in ("end_conversation", "close_assistant", "disable_hands_free"):
        requires_input = False
    else:
        requires_input = (
            state.stage.startswith("awaiting_") or needs_clarification
        ) and not added_variant_id and not auto_checkout

    if auto_listen is None:
        auto_listen = requires_input

    # Missing options calculation
    missing_options = []
    if state.selected_product:
        variants = state.selected_product.get("variants", {}).get("nodes", [])
        all_option_names = []
        for v in variants:
            for opt in v.get("selectedOptions", []):
                opt_name = opt.get("name", "").strip()
                if opt_name and opt_name not in all_option_names:
                    all_option_names.append(opt_name)

        missing_options = [name for name in all_option_names if not state.selected_options.get(name)]

    resolved_stage = state.stage
    if state.stage == "awaiting_variant" and missing_options:
        resolved_stage = f"awaiting_{missing_options[0].lower()}"

    expected_input = []
    if requires_input:
        if state.stage in ("awaiting_confirm", "awaiting_cart_confirm"):
            expected_input = ["confirmation"]
        elif state.stage == "awaiting_cart_quantity":
            expected_input = ["quantity"]
        elif state.stage == "awaiting_variant" or resolved_stage.startswith("awaiting_"):
            if missing_options:
                expected_input = [missing_options[0].lower()]
            else:
                expected_input = ["variant"]

    conversation_active = requires_input or state.stage.startswith("awaiting_")
    conversation_complete = not conversation_active
    auto_close = conversation_complete and not requires_input
    immediate_close = resolved_intent == "close_assistant" or any(cw in (transcription or "").lower() for cw in ("close assistant", "close the assistant", "close it"))

    selected_variant_id = added_variant_id or (state.selected_variant.get("id") if state.selected_variant else None)

    is_explicit_nav = any(kw in (transcription or "").lower() for kw in ("open", "take me to", "go to", "navigate to"))

    return {
        "transcription": transcription,
        "answer": answer,
        "message": answer,
        "stage": resolved_stage,
        "intent": resolved_intent,
        "requires_input": requires_input,
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


def _handle_remove_from_cart(
    message: str,
    state: ConversationState,
    session_id: str,
    shop_domain: str,
    admin_token: Optional[str] = None,
    product_ref: Optional[str] = None,
    variant_ref: Optional[str] = None,
    all_products: Optional[list] = None,
) -> tuple[str, Optional[str], Optional[dict], Optional[int]]:
    """
    Check if the product/variant is in the session cart.
    If present: reduce quantity or remove line item, return (answer, removed_variant_id, updated_cart, new_qty).
    If absent: return clear message with actual cart contents.
    """
    current_cart = get_draft_order(session_id)
    lines = current_cart.get("lines", []) if current_cart else []
    target_line = None

    ref_prod = (product_ref or "").strip().lower()
    ref_var = (variant_ref or "").strip().lower()
    msg_lower = message.lower()

    # Extract quantity reduction if mentioned (e.g. "remove 1", "remove one quantity")
    remove_qty = None
    num_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "single": 1}
    for w, val in num_map.items():
        if re.search(r"\b" + w + r"\b", msg_lower):
            remove_qty = val
            break
    if not remove_qty:
        m = re.search(r"\b(\d+)\b", msg_lower)
        if m:
            remove_qty = int(m.group(1))

    # Noise words to ignore when matching product title in cart
    noise_words = {
        "one", "1", "two", "2", "three", "3", "four", "4", "five", "5",
        "quantity", "qty", "amount", "count", "number", "unit", "units",
        "items", "card", "board", "product", "item", "from", "the", "cart", "my", "a", "an"
    }
    raw_words = [w for w in re.split(r'\W+', ref_prod) if len(w) >= 2] if ref_prod else []
    clean_words = [w for w in raw_words if w not in noise_words]

    # 1. Search for matching line in session cart
    if lines:
        for line in lines:
            line_p_title = line.get("product_title", "").lower()
            line_v_title = line.get("variant_title", "").lower()

            match_prod = False
            if clean_words:
                if any(w in line_p_title for w in clean_words):
                    match_prod = True
            elif raw_words:
                if any(w in line_p_title for w in raw_words if w not in noise_words):
                    match_prod = True
            else:
                if any(w in line_p_title for w in re.split(r'\W+', msg_lower) if len(w) > 3 and w not in noise_words):
                    match_prod = True
                elif len(lines) == 1:
                    match_prod = True

            if match_prod:
                target_line = line
                break

        if not target_line and len(lines) == 1 and not clean_words:
            target_line = lines[0]

    # 2. If matched in session cart: perform actual removal or quantity update
    if target_line:
        var_id = target_line.get("variant_id", "")
        p_title = target_line.get("product_title", "Product")
        v_title = target_line.get("variant_title", "")
        curr_qty = target_line.get("quantity", 1)

        new_qty = None
        if remove_qty and curr_qty > remove_qty:
            new_qty = curr_qty - remove_qty

        updated_cart = remove_from_cart_by_variant(
            session_id=session_id,
            shop_domain=shop_domain,
            variant_id=var_id,
            admin_token=admin_token,
            new_quantity=new_qty,
        )

        log.log_info(f"Removed/updated {p_title} ({v_title}) from session cart")

        var_str = f" ({v_title})" if v_title and v_title.lower() not in ("default title", "default") else ""
        if new_qty:
            answer = f"Updated **{p_title}**{var_str} quantity to {new_qty} in your cart."
        else:
            answer = f"Removed **{p_title}**{var_str} from your cart."
        return answer, var_id, updated_cart, new_qty

    # Item was not found in the cart — tell the user clearly
    if not lines:
        answer = "Your cart is currently empty — there's nothing to remove."
    else:
        cart_contents = ", ".join(
            f"**{l.get('product_title', '?')}**"
            + (f" ({l.get('variant_title', '')})" if l.get("variant_title", "").lower() not in ("", "default title", "default") else "")
            for l in lines
        )
        product_mention = " ".join(clean_words) if clean_words else (ref_prod or "that product")
        answer = f"I couldn't find **{product_mention}** in your cart. Your cart currently has: {cart_contents}."
    return answer, None, current_cart, None



def _generate_audio(text: str) -> tuple[str, str]:
    """Generate TTS audio. Returns (output_path, base64_string)."""
    output_path = tempfile.mktemp(suffix=".mp3")
    text_to_speech(text, output_path)
    with open(output_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    return output_path, audio_b64


_SIZE_MAP = {

    "s": "small", "small": "s",
    "m": "medium", "medium": "m",
    "l": "large", "large": "l",
    "xl": "extra large", "extra large": "xl", "x-large": "xl",
    "xs": "extra small", "extra small": "xs", "x-small": "xs",
    "xxl": "2xl", "2xl": "xxl",
}


def _normalize_option_word(w: str) -> str:
    w = w.strip().lower()
    return _SIZE_MAP.get(w, w)


_SIZE_SYNONYMS = {
    "xxl": "XXL", "2xl": "XXL", "xx-large": "XXL",
    "xl": "XL", "x-large": "XL", "extra large": "XL",
    "l": "L", "large": "L",
    "m": "M", "medium": "M",
    "s": "S", "small": "S",
    "xs": "XS", "extra small": "XS", "x-small": "XS"
}


def _extract_options_from_text(product: dict, text: str) -> dict:
    """
    Extract key-value pairs for options (e.g. {"Color": "Navy", "Size": "XL"})
    from free-text user message based on canonical option values in product.
    """
    if not product or not text:
        return {}

    extracted = {}
    text_lower = text.lower().strip()

    variants = product.get("variants", {}).get("nodes", [])
    option_map = {}
    for opt in product.get("options", []):
        opt_name = opt.get("name", "").strip()
        opt_vals = opt.get("values", [])
        if opt_name and opt_vals:
            if opt_name not in option_map:
                option_map[opt_name] = set()
            for v in opt_vals:
                option_map[opt_name].add(str(v).strip())

    for v in variants:
        for opt in v.get("selectedOptions", []):
            opt_name = opt.get("name", "").strip()
            opt_val = opt.get("value", "").strip()
            if opt_name and opt_val:
                if opt_name not in option_map:
                    option_map[opt_name] = set()
                option_map[opt_name].add(opt_val)

    for opt_name, canonical_values in option_map.items():
        opt_name_lower = opt_name.lower()
        found_val = None

        if "size" in opt_name_lower:
            for syn, canonical_val in _SIZE_SYNONYMS.items():
                if any(val.lower() == canonical_val.lower() for val in canonical_values):
                    pattern = r"\b" + re.escape(syn) + r"\b"
                    if re.search(pattern, text_lower):
                        found_val = canonical_val
                        break

        if not found_val:
            for val in canonical_values:
                val_lower = val.lower()
                pattern = r"\b" + re.escape(val_lower) + r"\b"
                if re.search(pattern, text_lower):
                    found_val = val
                    break

        if found_val:
            canonical_match = next((v for v in canonical_values if v.lower() == found_val.lower()), found_val)
            extracted[opt_name] = canonical_match

    return extracted


_ALL_COLOR_MENTIONS = [
    "red", "black", "blue", "navy", "white", "green", "yellow", "pink",
    "grey", "gray", "brown", "purple", "orange", "beige", "khaki", "gold", "silver"
]

_ALL_SIZE_MENTIONS = [
    "xxl", "2xl", "extra large", "x-large", "xl", "large", "l",
    "medium", "m", "small", "s", "extra small", "x-small", "xs"
]


def _get_product_option_values(product: dict) -> dict[str, list[str]]:
    """
    Returns map of option_name -> list of canonical option values for a product.
    Example: {"Color": ["Navy", "White"], "Size": ["S", "M", "L", "XL"]}
    """
    res = {}
    if not product:
        return res
    for opt in product.get("options", []):
        name = opt.get("name", "").strip()
        vals = opt.get("values", [])
        if name and vals:
            res[name] = [str(v).strip() for v in vals]
    variants = product.get("variants", {}).get("nodes", [])
    for v in variants:
        for opt in v.get("selectedOptions", []):
            name = opt.get("name", "").strip()
            val = opt.get("value", "").strip()
            if name and val:
                if name not in res:
                    res[name] = []
                if val not in res[name]:
                    res[name].append(val)
    return res


def _has_incomplete_quantity_phrase(text: str) -> bool:
    """
    Returns True if user utterance contains a quantity phrase ("quantity should be", "quantity is", "and quantity")
    without providing a numeric digit or number word.
    """
    if not text:
        return False
    text_lower = text.lower()
    QTY_CUES = ("quantity should be", "quantity is", "and quantity", "quantity would be", "quantity of", "quantity should")
    has_cue = any(cue in text_lower for cue in QTY_CUES)
    if not has_cue:
        return False
    has_number = bool(re.search(r"\b\d+\b", text_lower)) or any(w in text_lower for w in WORD_TO_NUM.keys())
    return not has_number


_SIZE_CANONICAL = {
    "small": "S", "s": "S",
    "medium": "M", "m": "M",
    "large": "L", "l": "L",
    "extra large": "XL", "x-large": "XL", "xl": "XL",
    "extra small": "XS", "x-small": "XS", "xs": "XS",
    "2xl": "XXL", "xxl": "XXL",
}


def _extract_all_unvalidated_options(text: str) -> dict[str, str]:
    """
    Extract any color or size mentioned in user text (even unvalidated ones).
    Returns dict like {"Color": "Red", "Size": "L"}
    """
    text_lower = text.lower()
    found = {}

    for c in _ALL_COLOR_MENTIONS:
        pattern = r"\b" + re.escape(c) + r"\b"
        if re.search(pattern, text_lower):
            found["Color"] = c.capitalize()
            break

    for s in _ALL_SIZE_MENTIONS:
        pattern = r"\b" + re.escape(s) + r"\b"
        if re.search(pattern, text_lower):
            canonical_size = _SIZE_CANONICAL.get(s.lower(), s.upper())
            found["Size"] = canonical_size
            break

    return found


def _validate_and_apply_options(
    product: dict,
    text: str,
    state: ConversationState
) -> tuple[dict, dict, list]:
    """
    Extracts options from user message, validates them against product's real Shopify options.
    If an option is invalid (e.g. Red for Everyday Cotton T-Shirt):
      - Removes that option key from state.selected_options (sets to None/cleared)
      - Tracks it in state.invalid_requested_options (e.g. {"Color": "Red"})
      - Preserves valid options (e.g. Size: XL) and quantity
    Returns: (valid_opts, invalid_opts, missing_options)
    """
    if not product or not text:
        return {}, {}, []

    unvalidated = _extract_all_unvalidated_options(text)
    if not unvalidated:
        unvalidated = _extract_options_from_text(product, text)

    option_map = _get_product_option_values(product)

    valid_opts = {}
    invalid_opts = {}

    for key, req_val in unvalidated.items():
        matching_opt_name = next(
            (k for k in option_map.keys() if k.lower() == key.lower()),
            key
        )
        allowed_vals = option_map.get(matching_opt_name, [])
        canonical_match = next(
            (
                v for v in allowed_vals
                if v.lower() == req_val.lower()
                or _SIZE_MAP.get(v.lower(), v.lower()) == _SIZE_MAP.get(req_val.lower(), req_val.lower())
            ),
            None
        )

        if canonical_match:
            valid_opts[matching_opt_name] = canonical_match
            state.selected_options[matching_opt_name] = canonical_match
        else:
            invalid_opts[matching_opt_name] = req_val
            # Clear this option from state.selected_options so previous choice is NOT retained
            state.selected_options.pop(matching_opt_name, None)

    if invalid_opts:
        state.invalid_requested_options = dict(invalid_opts)

    all_option_names = list(option_map.keys())
    missing_options = [name for name in all_option_names if not state.selected_options.get(name)]

    return valid_opts, invalid_opts, missing_options


def _resolve_variant_by_options(product: dict, selected_options: dict) -> Optional[dict]:
    """
    Find the exact Shopify variant matching ALL selected options together.
    Returns None if no variant matches ALL non-empty selected options.
    """
    if not product or not selected_options:
        return None

    variants = product.get("variants", {}).get("nodes", [])
    if not variants:
        return None

    for v in variants:
        v_opts = {o.get("name", "").strip().lower(): o.get("value", "").strip().lower() for o in v.get("selectedOptions", [])}
        match = True
        for opt_name, opt_val in selected_options.items():
            if not opt_val:
                continue
            name_lower = opt_name.strip().lower()
            val_lower = opt_val.strip().lower()

            v_val = v_opts.get(name_lower)
            if not v_val:
                match = False
                break

            if _normalize_option_word(v_val) != _normalize_option_word(val_lower):
                match = False
                break

        if match:
            return v

    return None


def _find_variant(product: dict, variant_ref: Optional[str]) -> Optional[dict]:
    """
    Find the exact best matching variant in a product given a free-text variant_ref.
    Ensures size 'L' matches size L only and never matches 'XL' or other sizes.
    """
    if not variant_ref:
        variants = product.get("variants", {}).get("nodes", [])
        if len(variants) == 1:
            return variants[0]
        return None

    ref = variant_ref.strip().lower()
    action_noise = {
        "select", "add", "added", "adds", "choose", "get", "want", "show", "put", "place", "buy",
        "play", "pay", "size", "colour", "color", "option", "and", "go", "to", "checkout", "page",
        "cart", "my", "the", "a", "an", "this", "that", "in", "into", "for", "please", "item",
        "product", "quantity", "qty", "amount", "count", "number", "unit", "units", "items", "card",
        "shirt", "shirts", "tshirt", "tshirts", "snowboard", "snowboards"
    } | set(WORD_TO_NUM.keys())

    variants = product.get("variants", {}).get("nodes", [])
    if not variants:
        return None

    raw_words = [w.strip() for w in re.split(r'\W+', ref) if w.strip()]
    ref_words = [w for w in raw_words if w not in action_noise]
    if not ref_words:
        ref_words = raw_words
    if not ref_words:
        return None
    norm_ref_words = [_normalize_option_word(w) for w in ref_words]

    # ── Strategy 1: Multi-option All-Word Match (Highest Priority) ────────
    for v in variants:
        title = v.get("title", "").strip().lower()
        title_parts = [p.strip() for p in re.split(r'[\s/,-]+', title) if p.strip()]
        norm_title_parts = [_normalize_option_word(p) for p in title_parts]

        opts = v.get("selectedOptions", [])
        opt_vals = [o.get("value", "").strip().lower() for o in opts]
        norm_opt_vals = [_normalize_option_word(val) for val in opt_vals]

        all_vals = title_parts + norm_title_parts + opt_vals + norm_opt_vals
        if all(w in all_vals or norm_w in all_vals for w, norm_w in zip(ref_words, norm_ref_words)):
            return v

    # ── Strategy 2: Best-Match Scored Match ─────────────────────────────
    best_variant = None
    best_score = 0
    for v in variants:
        title = v.get("title", "").strip().lower()
        title_parts = [p.strip() for p in re.split(r'[\s/,-]+', title) if p.strip()]
        norm_title_parts = [_normalize_option_word(p) for p in title_parts]

        opts = v.get("selectedOptions", [])
        opt_vals = [o.get("value", "").strip().lower() for o in opts]
        norm_opt_vals = [_normalize_option_word(val) for val in opt_vals]

        all_vals = title_parts + norm_title_parts + opt_vals + norm_opt_vals
        score = sum(1 for w, norm_w in zip(ref_words, norm_ref_words) if w in all_vals or norm_w in all_vals)

        if score > best_score:
            best_score = score
            best_variant = v

    if best_variant and best_score > 0:
        return best_variant

    return None


def _is_variant_in_stock(variant: dict) -> bool:
    """
    Check if a variant is in stock and available for sale.
    Returns False if availableForSale is False or if inventoryQuantity is tracked and <= 0.
    """
    if not variant:
        return False
    if not variant.get("availableForSale", True):
        return False
    inv = variant.get("inventoryQuantity")
    if inv is not None and isinstance(inv, int) and inv <= 0:
        return False
    return True


def _get_product_options_summary(product: dict, selected_color: Optional[str] = None) -> str:
    variants = product.get("variants", {}).get("nodes", [])
    colors = set()
    sizes = set()
    for v in variants:
        if _is_variant_in_stock(v):
            v_opts = {o.get("name", "").lower(): o.get("value", "") for o in v.get("selectedOptions", [])}

            if selected_color:
                match_color = False
                for k, val in v_opts.items():
                    if ("color" in k or "colour" in k) and val.lower() == selected_color.lower():
                        match_color = True
                        break
                if not match_color:
                    continue

            for name, val in v_opts.items():
                if "color" in name or "colour" in name:
                    colors.add(val)
                elif "size" in name:
                    sizes.add(val)

    color_str = ", ".join(sorted(colors)) if colors else ""
    size_str = ", ".join(sorted(sizes)) if sizes else ""

    parts = []
    if color_str and not selected_color:
        parts.append(f"Colors: {color_str}")
    if size_str:
        parts.append(f"Sizes: {size_str}")

    return " | ".join(parts) if parts else "Available"


def _available_variants_text(product: dict) -> str:
    return _get_product_options_summary(product)


def _find_product_by_ref(products: list, product_ref: Optional[str]) -> Optional[dict]:
    """Match product_ref (substring or all-words match) against product titles."""
    if not product_ref:
        return None
    ref = product_ref.strip().lower()
    ref_words = [w for w in re.split(r'\W+', ref) if len(w) > 2]

    # Strategy 1: Substring match
    for p in products:
        title = p.get("title", "").lower()
        if ref in title:
            return p

    # Strategy 2: Word-token match (e.g. "liquid snowboard" matches "The Collection Snowboard: Liquid")
    if ref_words:
        for p in products:
            title = p.get("title", "").lower()
            if all(w in title for w in ref_words):
                return p
    return None


def _check_missing_options(product: dict, ref_text: str) -> tuple[bool, Optional[str], list[str]]:
    """
    Check if a user's selection covers all required option types (e.g. Color and Size).
    Only includes values from variants that are availableForSale AND match already-selected options.
    Returns (is_complete, missing_option_name, available_values_for_missing_option).
    """
    variants = product.get("variants", {}).get("nodes", [])
    if not variants or len(variants) <= 1:
        return True, None, []

    ref_lower = ref_text.lower()
    in_stock_variants = [v for v in variants if _is_variant_in_stock(v)]

    if not in_stock_variants:
        return True, None, []

    first_v_opts = in_stock_variants[0].get("selectedOptions", [])
    opt_names = [o.get("name", "").strip() for o in first_v_opts if o.get("name")]

    if not opt_names:
        return True, None, []

    selected_opt_values = {}
    missing_opt_name = None

    for name in opt_names:
        name_l = name.lower()
        satisfied_val = None

        possible_vals = set()
        for v in in_stock_variants:
            for o in v.get("selectedOptions", []):
                if o.get("name", "").strip().lower() == name_l:
                    possible_vals.add(o.get("value", "").strip())

        if "size" in name_l:
            ref_padded = f" {ref_lower} "
            for size_term in ("xxl", "2xl", "xl", "xs", "small", "medium", "large", " size s ", " size m ", " size l ", " s ", " m ", " l "):
                if size_term in ref_padded:
                    normalized_size = _SIZE_MAP.get(size_term.strip(), size_term.strip()).lower()
                    for val in possible_vals:
                        if val.lower() == normalized_size or _SIZE_MAP.get(val.lower(), val.lower()) == normalized_size:
                            satisfied_val = val
                            break
                    if satisfied_val:
                        break
            if not satisfied_val:
                words = re.split(r'\W+', ref_lower)
                for val in possible_vals:
                    val_l = val.lower()
                    norm_v = _SIZE_MAP.get(val_l, val_l)
                    if val_l in words or norm_v in words:
                        satisfied_val = val
                        break
        elif "color" in name_l or "colour" in name_l:
            for val in possible_vals:
                if val.lower() in ref_lower:
                    satisfied_val = val
                    break
        else:
            for val in possible_vals:
                if val.lower() in ref_lower:
                    satisfied_val = val
                    break

        if satisfied_val:
            selected_opt_values[name] = satisfied_val
        elif not missing_opt_name:
            missing_opt_name = name

    if not missing_opt_name:
        return True, None, []

    matching_in_stock = []
    for v in in_stock_variants:
        match_all = True
        v_opts = {o.get("name", "").strip(): o.get("value", "").strip() for o in v.get("selectedOptions", [])}
        for sel_name, sel_val in selected_opt_values.items():
            if v_opts.get(sel_name, "").lower() != sel_val.lower():
                match_all = False
                break
        if match_all:
            matching_in_stock.append(v)

    available_vals = set()
    for v in matching_in_stock:
        for o in v.get("selectedOptions", []):
            if o.get("name", "").strip().lower() == missing_opt_name.lower():
                available_vals.add(o.get("value", "").strip())

    return False, missing_opt_name, sorted(list(available_vals))



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
                state.selected_options_text = hint
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
                    answer = f"Found **{filtered[0]['title']}**. Options: {available}. Which would you like?"
            else:
                available = _available_variants_text(filtered[0])
                answer = f"Found **{filtered[0]['title']}**. Options: {available}. Which would you like?"
        else:
            state.stage = "awaiting_variant"
            answer = f"Found {len(filtered)} matching products. Which size or color would you like?"
    else:
        state.filtered_products = all_products

    return answer, filtered, storefront_url, False


def _handle_awaiting_variant(
    message: str,
    state: ConversationState,
    all_products: list,
    shop_domain: str,
    session_id: str,
    admin_token: Optional[str] = None,
) -> tuple[str, list, Optional[str], bool, Optional[dict], bool]:
    """
    The user has seen product results and is now specifying a variant or saying add to cart.
    Returns (answer, shown_products, storefront_url, needs_clarification, cart, auto_checkout).
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
    extracted_qty = extract_quantity(message)
    if extracted_qty > quantity:
        quantity = extracted_qty
    answer = result.get("answer", "")
    go_to_checkout = result.get("go_to_checkout", False) or intent == "checkout"

    # ── User wants a completely new search / list ───────────────────────
    msg_lower = message.lower()
    is_search_query = any(w in msg_lower for w in ("list", "catalog", "show", "find", "search", "browse", "products")) or intent == "filter_products"
    if is_search_query:
        state.reset_selection()
        browse_answer, filtered, storefront_url, _ = _handle_browse(
            message, state, all_products, shop_domain
        )
        return browse_answer, filtered, storefront_url, False, None, False

    # ── User cancels ─────────────────────────────────────────────────────
    if intent == "confirm_no":
        state.reset_selection()
        return (
            "No problem! Let me know what you'd like to look for.",
            all_products,
            None,
            False,
            None,
            False,
        )

    # ── Resolve the product ──────────────────────────────────────────────
    search_pool = state.filtered_products or all_products
    product = state.selected_product
    if not product and product_ref:
        product = _find_product_by_ref(search_pool, product_ref)

    if not product:
        action_stop_words = {
            "add", "added", "adds", "put", "place", "buy", "want", "get",
            "to", "in", "into", "my", "the", "a", "an", "this", "that",
            "please", "item", "product", "cart", "basket", "bag"
        }
        msg_words = [w for w in re.split(r'\W+', message.lower()) if len(w) > 2 and w not in action_stop_words]
        if msg_words:
            best_score = 0
            for prod in search_pool:
                title_lower = prod.get("title", "").lower()
                score = sum(1 for w in msg_words if w in title_lower)
                if score > best_score:
                    best_score = score
                    product = prod

    if not product:
        if len(search_pool) > 1:
            titles = ", ".join(p["title"] for p in search_pool[:5])
            return (
                f"Which product did you mean? I can see: {titles}.",
                search_pool,
                None,
                True,
                None,
                False,
            )
        elif len(search_pool) == 1:
            product = search_pool[0]
        else:
            return (
                "I'm not sure which product you're referring to. Could you tell me the name?",
                search_pool,
                None,
                True,
                None,
                False,
            )

    state.selected_product = product
    new_opts = _extract_options_from_text(product, message)
    if new_opts:
        state.selected_options.update(new_opts)

    variants = product.get("variants", {}).get("nodes", [])
    all_option_names = []
    for v in variants:
        for opt in v.get("selectedOptions", []):
            name = opt.get("name", "").strip()
            if name and name not in all_option_names:
                all_option_names.append(name)

    missing_options = [name for name in all_option_names if not state.selected_options.get(name)]

    if missing_options:
        state.stage = "awaiting_variant"
        next_missing = missing_options[0]
        opts_fmt = ", ".join(f"{v}" for k, v in state.selected_options.items()) if state.selected_options else "Option"
        available_vals = set()
        for v in variants:
            if _is_variant_in_stock(v):
                for o in v.get("selectedOptions", []):
                    if o.get("name", "").strip().lower() == next_missing.lower():
                        available_vals.add(o.get("value", "").strip())
        vals_str = ", ".join(sorted(available_vals))
        answer = f"{opts_fmt} selected. Which {next_missing.lower()} would you like for **{product['title']}**? Available: {vals_str}."
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # ── Resolve the exact variant by option combination ─────────────────
    variant = _resolve_variant_by_options(product, state.selected_options) or _find_variant(product, message)

    if not variant or not _is_variant_in_stock(variant):
        state.stage = "awaiting_variant"
        opts_fmt = ", ".join(f"{v}" for k, v in state.selected_options.items())
        available_summary = _get_product_options_summary(product)
        answer = f"Sorry, **{product['title']}** in {opts_fmt} is currently out of stock. Available options: {available_summary}."
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    state.selected_variant = variant
    state.quantity = quantity

    has_explicit_add = any(w in msg_lower for w in ("add", "buy", "cart", "checkout", "check out", "basket", "bag", "put in", "place in"))
    if has_explicit_add:
        try:
            cart = add_to_draft_order(
                session_id=session_id,
                shop_domain=shop_domain,
                product=product,
                variant=variant,
                quantity=quantity,
                admin_token=admin_token,
            )
            state.cart_id = cart.get("cart_id", state.cart_id)
            qty_str = f"{quantity}x " if quantity > 1 else ""
            answer = f"✅ Added {qty_str}**{product['title']}** ({variant.get('title')}) to your cart!"
            state.stage = "browse"
            state.reset_selection()
            return answer, state.filtered_products or all_products, None, False, cart, False, None, False, None, variant.get("id"), quantity
        except Exception as exc:
            log.log_error("Direct add to cart", exc)

    state.stage = "awaiting_confirm"
    opts_speech = ", ".join(f"{k.lower()} {v}" for k, v in state.selected_options.items())
    answer = f"You selected the **{product['title']}** in {opts_speech}. Would you like me to add it to your cart?"
    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    state.selected_variant = variant
    state.quantity = quantity

    # ── Directly add resolved variant to cart ─────────────────────────────
    try:
        cart = add_to_draft_order(
            session_id=session_id,
            shop_domain=shop_domain,
            product=product,
            variant=variant,
            quantity=quantity,
            admin_token=admin_token,
        )
        state.cart_id = cart.get("cart_id", state.cart_id)

        qty_str = f"{quantity}x " if quantity > 1 else ""
        answer = (
            f"✅ Added {qty_str}**{product['title']}** "
            f"({variant.get('title')}) to your cart!"
            + (" Taking you to checkout now." if go_to_checkout else "")
        )

        log.log_cart_success(quantity, cart.get("checkout_url", ""))

        # Reset for next shopping
        state.stage = "browse"
        state.selected_product = None
        state.selected_variant = None
        state.quantity = 1
        state.selected_options_text = ""

        return answer, state.filtered_products, None, False, cart, go_to_checkout, variant.get("id"), quantity

    except Exception as exc:
        log.log_error("Cart add (direct)", exc)
        return (
            "Sorry, I had trouble adding that to your cart. Please try again.",
            state.filtered_products, None, False, None, False,
        )

    return confirm_answer, state.filtered_products, None, False, None, False


def _handle_awaiting_confirm(
    message: str,
    state: ConversationState,
    all_products: list,
    shop_domain: str,
    session_id: str,
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
    msg_lower = message.lower()
    is_search_intent = any(w in msg_lower for w in ("list", "catalog", "show", "find", "search", "browse", "products")) or intent == "filter_products"
    has_confirm_word = any(w in msg_lower for w in ("yes", "yeah", "yep", "sure", "ok", "okay", "add", "buy", "confirm", "checkout", "do it"))
    has_option_word = any(w in msg_lower for w in ("size", "color", "colour", "small", "medium", "large", "xl", "xs", "xxl", "2xl", " s ", " m ", " l ", "black", "blue", "red", "white")) or bool(re.search(r"\bsize\s*[a-z0-9]+\b", msg_lower))

    if has_option_word and not has_confirm_word:
        return _handle_awaiting_variant(message, state, all_products, shop_domain, session_id, admin_token)

    if is_search_intent and not has_confirm_word:
        state.reset_selection()
        browse_answer, filtered, storefront_url, _ = _handle_browse(
            message, state, all_products, shop_domain
        )
        return browse_answer, filtered, storefront_url, False, None, False

    # ── User confirms OR says checkout ───────────────────────────────────
    go_to_checkout = result.get("go_to_checkout", False) or intent in ("checkout",)

    if intent in ("confirm_yes", "add_to_cart", "checkout"):
        product = state.selected_product
        variant = state.selected_variant

        if not product or not variant:
            state.stage = "browse"
            return (
                "Something went wrong with my memory of your selection. "
                "Could you start over?",
                all_products, None, False, None, False,
            )

        if not _is_variant_in_stock(variant):
            available = _available_variants_text(product)
            state.stage = "awaiting_variant"
            return (
                f"Sorry, **{variant.get('title')}** is currently out of stock for "
                f"**{product['title']}**. Available options: {available}.",
                state.filtered_products or all_products,
                None,
                False,
                None,
                False,
            )

        try:
            quantity = state.quantity
            extracted_qty = extract_quantity(message)
            if extracted_qty > 1:
                quantity = extracted_qty
            elif result.get("quantity", 1) > 1:
                quantity = result.get("quantity")

            cart = add_to_draft_order(
                session_id=session_id,
                shop_domain=shop_domain,
                product=product,
                variant=variant,
                quantity=quantity,
                admin_token=admin_token,
            )
            state.cart_id = cart.get("cart_id", state.cart_id)

            qty_str = f"{quantity}x " if quantity > 1 else ""
            answer = (
                f"✅ Added {qty_str}**{product['title']}** "
                f"({variant.get('title')}) to your cart!"
                + (" Taking you to checkout now." if go_to_checkout else "")
            )

            log.log_cart_success(quantity, cart.get("checkout_url", ""))

            state.stage = "browse"
            state.selected_product = None
            state.selected_variant = None
            state.quantity = 1

            return answer, state.filtered_products, None, False, cart, go_to_checkout, variant.get("id"), quantity

        except Exception as exc:
            log.log_error("Cart add failed", exc)
            return (
                "Sorry, I had trouble adding that to your cart. Please try again.",
                state.filtered_products, None, False, None, False,
            )

    # ── User cancels ─────────────────────────────────────────────────────
    if intent == "confirm_no":
        state.stage = "awaiting_variant"
        return (
            "No problem! Would you like to choose a different size or colour?",
            state.filtered_products, None, False, None, False,
        )

    # ── User changes their mind / starts new search ───────────────────────
    if intent == "filter_products":
        state.reset_selection()
        browse_answer, filtered, storefront_url, _ = _handle_browse(
            message, state, all_products, shop_domain
        )
        return browse_answer, filtered, storefront_url, False, None, False

    # ── Fallback: re-ask ─────────────────────────────────────────────────
    product = state.selected_product
    variant = state.selected_variant
    answer = (
        f"Shall I add **{product['title']} ({variant.get('title')})** to your cart?"
    )
    return answer, state.filtered_products, None, False, None, False


# ─────────────────────────────────────────────────────────
# Cart Quantity Intent — pattern constants
# ─────────────────────────────────────────────────────────

# Messages that indicate adding more (increment)
_QTY_INCREMENT_PATTERNS = [
    r"\badd\s+(?:\w+\s+)?more\b",          # "add two more", "add more"
    r"\badd\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b", # "add two T-shirts"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+more\b",
    r"\bincrease\s+(?:it\s+|the\s+)?by\b",  # "increase by", "increase it by"
    r"\bgive\s+me\s+(?:\w+\s+)?more\b",     # "give me two more"
    r"\badd\s+(?:another|additional)\b",    # "add another", "add additional"
    r"\bmore\s+of\s+(?:the\s+)?same\b",     # "more of the same"
    r"\bmore\s+of\s+(?:that|those|them)\b", # "more of those"
]

# Messages that indicate setting to an absolute quantity
_QTY_SET_PATTERNS = [
    r"\bset\s+(?:the\s+)?(?:quantity|qty)\b",
    r"\bmake\s+(?:the\s+)?(?:quantity|qty)\b",
    r"\bchange\s+(?:the\s+)?(?:quantity|qty)\b",
    r"\bchange\s+(?:it\s+)?from\s+\d+\s+to\s+\d+\b",
    r"\bfinal\s+quantity\b",
]

# Language that means "the exact same item already in my cart"
_SAME_CART_PATTERNS = [
    r"\bsame\b",                                 # "same jeans", "the same"
    r"\blike\s+the\s+one\s+in\s+(?:my\s+)?cart\b",
    r"\balready\s+in\s+(?:my\s+)?cart\b",
    r"\bmore\s+of\s+(?:the\s+)?same\b",
    r"\bthat\s+(?:same\s+)?(?:jeans|shirt|t-?shirt|tshirt|product|item|one)\b",
    r"\bthe\s+one\s+(?:i\s+have|in\s+(?:my\s+)?cart)\b",
    r"\bjust\s+like\b",
    r"\blike\s+in\s+(?:my\s+)?cart\b",
    r"\bin\s+my\s+cart\b",                       # "like the jeans in my cart"
]


def _parse_variant_title_to_options(variant_title: str) -> dict:
    """
    Best-effort parse "XL / Blue" → {"Size": "XL", "Color": "Blue"}.
    Used to pre-fill selected_options from an existing cart line.
    """
    if not variant_title or variant_title.lower() in ("default title", "default", ""):
        return {}
    parts = [p.strip() for p in re.split(r"\s*/\s*|\s+/\s+", variant_title)]
    SIZE_CANONICAL = {"XS", "S", "M", "L", "XL", "XXL", "2XL",
                      "Small", "Medium", "Large", "Extra Large", "Extra Small"}
    opts: dict = {}
    for part in parts:
        if part.upper() in {v.upper() for v in SIZE_CANONICAL}:
            opts.setdefault("Size", part)
        else:
            opts.setdefault("Color", part)
    return opts


def _score_line_match(text: str, line: dict, noise: set) -> int:
    """Score how well a user's text matches a cart line product title."""
    p_title = line.get("product_title", "").lower()
    words = [w for w in re.split(r"\W+", text.lower()) if len(w) > 2 and w not in noise]
    return sum(1 for w in words if w in p_title)


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


def _classify_cart_quantity_intent(
    message: str,
    state: ConversationState,
    all_products: list,
    session_id: str,
) -> Optional[dict]:
    """
    Determine if the message is a cart quantity update command.

    Priority Order:
      1. Explicit options stated in current request
      2. Explicit corrections
      3. Previously saved selections
      4. Existing cart options ONLY when user says 'same' / 'already in cart'
      5. Product defaults only when no selection required
    """
    msg_lower = message.lower()

    # Must have a non-empty cart
    current_cart = get_draft_order(session_id)
    cart_lines = current_cart.get("lines", []) if current_cart else []
    if not cart_lines:
        return None

    is_increment = any(re.search(p, msg_lower) for p in _QTY_INCREMENT_PATTERNS)
    is_set = any(re.search(p, msg_lower) for p in _QTY_SET_PATTERNS)
    has_qty_phrase = _has_incomplete_quantity_phrase(message)

    if not is_increment and not is_set and not has_qty_phrase:
        return None   # not a quantity command

    mode, delta = _extract_quantity_delta(message)
    if mode == "add_new":
        mode = "increment" if is_increment else "set"

    quantity_missing = has_qty_phrase and delta == 1 and not re.search(r"\b(one|1)\b", msg_lower)

    is_same_ref = any(re.search(p, msg_lower) for p in _SAME_CART_PATTERNS)

    noise = {
        "add", "more", "increase", "set", "make", "the", "my", "same", "quantity", "qty",
        "to", "by", "of", "a", "an", "in", "cart", "order", "please", "i", "want", "need",
        "give", "me", "like", "one", "two", "three", "four", "five", "six", "seven",
        "eight", "nine", "ten", "just", "also", "and", "or", "from", "change", "update",
        "another", "additional", "extra", "again", "into", "for", "is", "should", "be",
    }

    # ── Single-product line matching & option priority ───────────────────────
    best_line = None
    best_score = 0
    for line in cart_lines:
        score = _score_line_match(msg_lower, line, noise)
        if score > best_score:
            best_score = score
            best_line = line

    if not best_line and len(cart_lines) == 1:
        best_line = cart_lines[0]
        best_score = 1

    if not best_line or best_score == 0:
        return None

    # Check if catalog product matches
    matched_product = _find_product_by_ref(all_products, best_line.get("product_title", ""))
    explicit_options = {}
    if matched_product:
        explicit_options = _extract_options_from_text(matched_product, message)

    # RULE 1 & 4: Explicit options in current request take priority over cart options
    # If user gave explicit options (e.g. Color: White) AND did NOT say "same", check if cart line matches
    matched_cart_line = None
    if explicit_options and not is_same_ref:
        for line in cart_lines:
            line_opts = _parse_variant_title_to_options(line.get("variant_title", ""))
            # Check if all explicit options match line_opts
            if all(line_opts.get(k, "").lower() == v.lower() for k, v in explicit_options.items()):
                matched_cart_line = line
                break

    op_version = 1
    if explicit_options and not is_same_ref and not matched_cart_line:
        # User specified a DIFFERENT variant than what's in cart (e.g. White XL when Navy XL is in cart)
        # Do NOT reuse Navy line! Target new variant creation.
        resolved_var = _resolve_variant_by_options(matched_product, explicit_options) if matched_product else None
        var_id = resolved_var.get("id", "") if resolved_var else ""
        var_title = resolved_var.get("title", "") if resolved_var else "/".join(explicit_options.values())

        curr_qty = 0
        final_qty = delta if (mode == "set" or quantity_missing) else delta

        intent = "cart_add_new_variant"

        # Check if matched_product missing any options
        missing = []
        if matched_product:
            variants = matched_product.get("variants", {}).get("nodes", [])
            all_opt_names = []
            for v in variants:
                for oe in v.get("selectedOptions", []):
                    n = oe.get("name", "").strip()
                    if n and n not in all_opt_names:
                        all_opt_names.append(n)
            missing = [n for n in all_opt_names if not explicit_options.get(n)]

        status = "missing_quantity" if quantity_missing else ("ready" if not missing else "collecting_options")

        return {
            "intent": intent,
            "operations": [{
                "operation_id": "op-1",
                "operation_version": op_version,
                "product_title": matched_product.get("title", best_line.get("product_title", "")) if matched_product else best_line.get("product_title", ""),
                "source_line_id": None,   # DO NOT touch existing cart line!
                "variant_id": var_id,
                "variant_title": var_title,
                "selected_options": explicit_options,
                "missing_options": missing,
                "quantity_mode": mode,
                "quantity_delta": delta if not quantity_missing else 1,
                "quantity_missing": quantity_missing,
                "current_quantity": curr_qty,
                "final_quantity": final_qty,
                "reuse_cart_variant": False,
                "status": status,
            }],
            "reuse_cart_variant": False,
            "mode": mode,
            "delta": delta,
        }

    # Cart line matched or is_same_ref=True
    target_line = matched_cart_line or best_line
    curr_qty = target_line.get("quantity", 1)
    final_qty = (curr_qty + delta) if mode == "increment" else delta
    intent = "cart_set_line_quantity" if mode == "set" else "cart_increment_existing_line"

    cart_line_opts = _parse_variant_title_to_options(target_line.get("variant_title", ""))
    merged_opts = {**cart_line_opts, **explicit_options}

    return {
        "intent": intent,
        "operations": [{
            "operation_id": "op-1",
            "operation_version": op_version,
            "product_title": target_line.get("product_title", ""),
            "source_line_id": target_line.get("line_id", ""),
            "variant_id": target_line.get("variant_id", ""),
            "variant_title": target_line.get("variant_title", ""),
            "selected_options": merged_opts,
            "missing_options": [],
            "quantity_mode": mode,
            "quantity_delta": delta if not quantity_missing else 1,
            "quantity_missing": quantity_missing,
            "current_quantity": curr_qty,
            "final_quantity": final_qty,
            "reuse_cart_variant": is_same_ref or not explicit_options,
            "status": "missing_quantity" if quantity_missing else "ready",
        }],
        "reuse_cart_variant": is_same_ref or not explicit_options,
        "mode": mode,
        "delta": delta,
    }


def _handle_cart_quantity_intent(
    intent_result: dict,
    state: ConversationState,
    all_products: list,
    session_id: str,
    shop_domain: str,
    admin_token: Optional[str] = None,
) -> tuple:
    """
    Handle a classified cart quantity intent.
    For 'same as cart' (reuse_cart_variant=True) or known cart-line ops:
      show a confirmation message → stage = awaiting_cart_confirm.
    For generic product references (reuse_cart_variant=False, missing options):
      ask for missing options → stage = awaiting_variant.
    """
    operations = intent_result.get("operations", [])
    if not operations:
        return None

    intent = intent_result.get("intent", "cart_increment_existing_line")
    state.pending_cart_intent = intent
    state.pending_operations = [dict(o) for o in operations]

    EMPTY = ("", "default title", "default")

    def _variant_display(o: dict) -> str:
        vt = o.get("variant_title", "")
        return f" {vt}" if vt.lower() not in EMPTY else ""

    # ── Single op or first op needing options ────────────────────────────────
    op = next(
        (o for o in state.pending_operations if o.get("operation_id") == (state.active_operation_id or "op-1")),
        state.pending_operations[0]
    )

    # Incomplete quantity phrase specified without numbers
    if op.get("quantity_missing"):
        state.stage = "awaiting_cart_quantity"
        state.active_operation_id = op["operation_id"]
        product = _find_product_by_ref(all_products, op.get("product_title")) or state.selected_product
        if product:
            state.selected_product = product
        opts_str = ", ".join(f"{v}" for k, v in op.get("selected_options", {}).items())
        p_title = product.get("title", op.get("product_title", "")) if product else op.get("product_title", "")
        if opts_str:
            answer = f"You selected the **{p_title}** in {opts_str}. What quantity would you like?"
        else:
            answer = f"For **{p_title}**, what quantity would you like?"
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    if op.get("reuse_cart_variant") or op.get("source_line_id"):
        # Known exact cart line — go to confirmation
        state.active_operation_id = None
        state.stage = "awaiting_cart_confirm"
        vd = _variant_display(op)
        if op["quantity_mode"] == "increment":
            answer = (
                f"Your{vd} **{op['product_title']}** currently has quantity {op['current_quantity']}. "
                f"I'll increase it by {op['quantity_delta']}, making the total {op['final_quantity']}. "
                f"Shall I continue?"
            )
        else:
            answer = (
                f"I'll set your{vd} **{op['product_title']}** quantity to {op['final_quantity']}. "
                f"Shall I continue?"
            )
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # New variant request (or generic reference requiring options)
    product = _find_product_by_ref(all_products, op.get("product_title"))
    if not product:
        return None   # couldn't match product — fall through to normal flow

    state.selected_product = product
    state.selected_options = dict(op.get("selected_options", {}))
    state.stage = "awaiting_variant"
    state.active_operation_id = op["operation_id"]

    variants = product.get("variants", {}).get("nodes", [])
    all_option_names = []
    for v in variants:
        for oe in v.get("selectedOptions", []):
            n = oe.get("name", "").strip()
            if n and n not in all_option_names:
                all_option_names.append(n)

    missing = [n for n in all_option_names if not state.selected_options.get(n)]
    op["missing_options"] = missing

    if not missing:
        # All options already known (explicitly given in request)
        variant = _resolve_variant_by_options(product, state.selected_options)
        if variant:
            op["variant_id"] = variant.get("id", "")
            op["variant_title"] = variant.get("title", "")
            op["status"] = "ready"
        state.stage = "awaiting_cart_confirm"
        state.active_operation_id = None

        opts_str = ", ".join(f"{v}" for k, v in state.selected_options.items())
        p_title = product.get("title", "")

        if op.get("current_quantity", 0) == 0:
            answer = (
                f"You selected the {p_title} in {opts_str}. "
                f"I'll add one to your cart. Shall I continue?"
            )
        else:
            qty_word = "add" if op["quantity_mode"] == "increment" else "set"
            answer = (
                f"Got it — I'll {qty_word} {op['quantity_delta']} to your **{p_title}** "
                f"({opts_str}). Shall I continue?"
            )
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # Ask for first missing option
    next_missing = missing[0]
    available_vals: set = set()
    all_colors: set = set()
    all_sizes: set = set()
    for v in variants:
        if _is_variant_in_stock(v):
            for oe in v.get("selectedOptions", []):
                n = oe.get("name", "").strip()
                val = oe.get("value", "").strip()
                if "color" in n.lower() or "colour" in n.lower():
                    all_colors.add(val)
                elif "size" in n.lower():
                    all_sizes.add(val)
                if n.lower() == next_missing.lower():
                    available_vals.add(val)

    options_lines = []
    if all_colors:
        options_lines.append(f"Colors: {' or '.join(sorted(all_colors))}.")
    if all_sizes:
        options_lines.append(f"Sizes: {', '.join(sorted(all_sizes))}.")

    qty_desc = (
        f"{op['quantity_delta']} more" if op["quantity_mode"] == "increment"
        else f"quantity to {op['quantity_delta']}"
    )
    answer = (
        f"Which option would you like to {qty_desc} for **{product['title']}**?\n"
        + "\n".join(options_lines)
    )
    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None


def _collect_operation_options(
    message: str,
    state: ConversationState,
    all_products: list,
    session_id: str,
    shop_domain: str,
    admin_token: Optional[str] = None,
) -> Optional[tuple]:
    """
    Handle a follow-up message when state.pending_operations is active and an
    operation is still collecting missing options.
    """
    if not state.pending_operations or not state.active_operation_id:
        return None

    active_op: Optional[dict] = None
    active_idx: Optional[int] = None
    for idx, op in enumerate(state.pending_operations):
        if op.get("operation_id") == state.active_operation_id:
            active_op = op
            active_idx = idx
            break

    if active_op is None or active_idx is None:
        return None

    msg_lower = message.lower()
    product = state.selected_product

    # Extract new option values for the active operation
    if product:
        new_opts = _extract_options_from_text(product, msg_lower)
        if new_opts:
            active_op["selected_options"] = {**active_op.get("selected_options", {}), **new_opts}
            active_op["operation_version"] = active_op.get("operation_version", 1) + 1

    # Re-extract quantity if the user provided/changed it in this message
    q_mode, q_delta = _extract_quantity_delta(message)
    if q_mode in ("increment", "set") and q_delta >= 1:
        active_op["quantity_mode"] = q_mode
        active_op["quantity_delta"] = q_delta
        active_op["quantity_missing"] = False
        active_op["operation_version"] = active_op.get("operation_version", 1) + 1
    elif q_mode == "add_new" and q_delta > 1:
        active_op["quantity_delta"] = q_delta
        active_op["quantity_missing"] = False
        active_op["operation_version"] = active_op.get("operation_version", 1) + 1

    # Recalculate final_quantity
    if active_op["quantity_mode"] == "increment":
        active_op["final_quantity"] = active_op.get("current_quantity", 0) + active_op["quantity_delta"]
    else:
        active_op["final_quantity"] = active_op["quantity_delta"]

    state.pending_operations[active_idx] = active_op

    # Recompute missing options
    variants = product.get("variants", {}).get("nodes", []) if product else []
    all_option_names: list = []
    for v in variants:
        for oe in v.get("selectedOptions", []):
            n = oe.get("name", "").strip()
            if n and n not in all_option_names:
                all_option_names.append(n)
    still_missing = [n for n in all_option_names if not active_op.get("selected_options", {}).get(n)]
    active_op["missing_options"] = still_missing
    state.pending_operations[active_idx] = active_op

    EMPTY = ("", "default title", "default")

    if still_missing:
        # Still need more options for this operation
        next_missing = still_missing[0]
        available_vals: set = set()
        for v in variants:
            if _is_variant_in_stock(v):
                for oe in v.get("selectedOptions", []):
                    if oe.get("name", "").strip().lower() == next_missing.lower():
                        available_vals.add(oe.get("value", "").strip())
        opts_so_far = ", ".join(v for v in active_op.get("selected_options", {}).values())
        prefix = f"{opts_so_far} selected. " if opts_so_far else ""
        answer = (
            f"{prefix}Which {next_missing.lower()} would you like for "
            f"**{active_op['product_title']}**? "
            f"Available: {', '.join(sorted(available_vals))}."
        )
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # Active op is complete — resolve its variant
    if product:
        variant = _resolve_variant_by_options(product, active_op.get("selected_options", {}))
        if variant:
            active_op["variant_id"] = variant.get("id", "")
            active_op["variant_title"] = variant.get("title", "")
            active_op["status"] = "ready"
            state.pending_operations[active_idx] = active_op

    # All operations complete — show combined confirmation
    state.stage = "awaiting_cart_confirm"
    state.active_operation_id = None

    ops = state.pending_operations
    if len(ops) == 1:
        op = ops[0]
        opts_str = ", ".join(f"{v}" for k, v in op.get("selected_options", {}).items())
        vt = op.get("variant_title", "")
        vd = vt if vt.lower() not in EMPTY else opts_str
        p_title = op.get("product_title", "")
        if op.get("current_quantity", 0) == 0:
            answer = f"You selected the **{p_title}** in {vd}. I'll add {op['quantity_delta']} to your cart. Shall I continue?"
        elif op["quantity_mode"] == "increment":
            answer = (
                f"Got it — I'll add {op['quantity_delta']} more **{p_title}** "
                f"({vd}), bringing the total to {op['final_quantity']}. Shall I continue?"
            )
        else:
            answer = (
                f"Got it — I'll set **{p_title}** ({vd}) "
                f"quantity to {op['final_quantity']}. Shall I continue?"
            )
    else:
        lines_desc = []
        for op in ops:
            opts_str = ", ".join(f"{k} {v}" for k, v in op.get("selected_options", {}).items())
            vt = op.get("variant_title", "")
            vd = vt if vt.lower() not in EMPTY else opts_str
            if op["quantity_mode"] == "increment":
                lines_desc.append(
                    f"• Add {op['quantity_delta']} more **{op['product_title']}** ({vd}), "
                    f"from {op['current_quantity']} to {op['final_quantity']}"
                )
            else:
                lines_desc.append(
                    f"• Set **{op['product_title']}** ({vd}) quantity to {op['final_quantity']}"
                )
        answer = "I'll make these cart changes:\n" + "\n".join(lines_desc) + "\nShall I continue?"

    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None


def _handle_awaiting_cart_confirm(
    message: str,
    state: ConversationState,
    all_products: list,
    session_id: str,
    shop_domain: str,
    admin_token: Optional[str] = None,
) -> Optional[tuple]:
    """
    The user is confirming (or cancelling or correcting) a pending cart quantity update.
    """
    if not state.pending_operations:
        state.stage = "browse"
        return None

    msg_lower = message.lower()
    has_yes = bool(re.search(
        r"\b(yes|yeah|yep|yup|sure|ok|okay|go|do\s+it|continue|confirm|proceed|please|absolutely|correct|right|definitely)\b",
        msg_lower
    ))
    has_no = bool(re.search(
        r"\b(no|nope|nah|cancel|stop|never\s+mind|nevermind|don.?t|forget\s+it|leave\s+it)\b",
        msg_lower
    ))

    has_correction = any(rt in msg_lower for rt in (
        "instead", "make it", "change", "give me", "give", "switch", "prefer", "rather", "actually",
        "red", "black", "blue", "navy", "white", "green", "yellow", "pink", "grey", "gray", "brown", "purple", "orange",
        "small", "medium", "large", "extra large", "xl", "l", "m", "s", "xs", "xxl",
        "size", "color", "colour", "jeans", "shirt", "t-shirt", "tshirt", "shoes", "pants", "dress",
        "two", "three", "four", "five", "quantity", "qty"
    ))

    if has_no and not has_yes and not has_correction:
        state.pending_cart_intent = None
        state.pending_operations = []
        state.active_operation_id = None
        state.stage = "browse"
        return (
            "Okay, cancelled.",
            state.filtered_products or all_products, None, False, None, False, None, False, None, None, None,
        )

    # Re-extract quantity from the confirmation message (user may override: "Yes, make it 4")
    q_mode, q_delta = _extract_quantity_delta(message)
    qty_override = q_mode in ("increment", "set") and q_delta > 0
    if qty_override:
        for op in state.pending_operations:
            op["quantity_mode"] = q_mode
            op["quantity_delta"] = q_delta
            op["operation_version"] = op.get("operation_version", 1) + 1
            if q_mode == "increment":
                op["final_quantity"] = op.get("current_quantity", 0) + q_delta
            else:
                op["final_quantity"] = q_delta

    if not has_yes:
        # Neither yes nor no — check for OPTION CORRECTION during confirmation (e.g. "No, I want Red color")
        op = state.pending_operations[0] if state.pending_operations else None
        if op:
            product = _find_product_by_ref(all_products, op.get("product_title", "")) or state.selected_product
            if product:
                valid_opts, invalid_opts, missing_opts = _validate_and_apply_options(product, message, state)

                if invalid_opts:
                    inv_key, inv_val = list(invalid_opts.items())[0]
                    opt_map = _get_product_option_values(product)
                    allowed = opt_map.get(inv_key, [])
                    allowed_str = " and ".join(allowed) if len(allowed) <= 2 else ", ".join(allowed[:-1]) + f", and {allowed[-1]}"
                    p_title = product.get("title", "this item")
                    answer = f"{inv_val} is not available for the **{p_title}**. Available {inv_key.lower()}s are {allowed_str}. Which {inv_key.lower()} would you like?"
                    state.stage = f"awaiting_{inv_key.lower()}"
                    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

                corr_opts = valid_opts or _extract_options_from_text(product, message)
                if corr_opts:
                    # Explicit Option Correction!
                    merged_opts = {**op.get("selected_options", {}), **corr_opts}
                    op["selected_options"] = merged_opts
                    op["source_line_id"] = None
                    op["variant_id"] = None
                    op["operation_version"] = op.get("operation_version", 1) + 1

                    new_var = _resolve_variant_by_options(product, merged_opts)
                    if new_var:
                        op["variant_id"] = new_var.get("id", "")
                        op["variant_title"] = new_var.get("title", "")
                        op["status"] = "ready"

                    # Check if new_var exists in live cart
                    current_cart = get_draft_order(session_id)
                    cart_lines = current_cart.get("lines", []) if current_cart else []
                    matched_line = None
                    for line in cart_lines:
                        l_opts = _parse_variant_title_to_options(line.get("variant_title", ""))
                        if all(l_opts.get(k, "").lower() == v.lower() for k, v in merged_opts.items()):
                            matched_line = line
                            break

                    if matched_line:
                        op["source_line_id"] = matched_line.get("line_id")
                        op["current_quantity"] = matched_line.get("quantity", 1)
                        if op["quantity_mode"] == "increment":
                            op["final_quantity"] = op["current_quantity"] + op["quantity_delta"]
                        else:
                            op["final_quantity"] = op["quantity_delta"]
                    else:
                        op["source_line_id"] = None
                        op["current_quantity"] = 0
                        op["final_quantity"] = op["quantity_delta"]

                    state.pending_operations[0] = op
                    state.stage = "awaiting_cart_confirm"

                    opts_str = ", ".join(f"{v}" for k, v in merged_opts.items())
                    p_title = product.get("title", op.get("product_title", ""))
                    vt = op.get("variant_title") or opts_str

                    if op["current_quantity"] == 0:
                        answer = (
                            f"You selected the {p_title} in {opts_str}. "
                            f"I'll add {op['quantity_delta']} to your cart. Shall I continue?"
                        )
                    else:
                        answer = (
                            f"Your {vt} **{p_title}** currently has quantity {op['current_quantity']}. "
                            f"I'll increase it by {op['quantity_delta']}, making the total {op['final_quantity']}. "
                            f"Shall I continue?"
                        )
                    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

        if qty_override:
            # Re-show confirmation with updated quantities
            ops = state.pending_operations
            EMPTY = ("", "default title", "default")
            if len(ops) == 1:
                op = ops[0]
                vt = op.get("variant_title", "")
                vd = f" {vt}" if vt.lower() not in EMPTY else ""
                if op["quantity_mode"] == "increment":
                    answer = (
                        f"Updated: I'll increase your{vd} **{op['product_title']}** by "
                        f"{op['quantity_delta']}, making the total {op['final_quantity']}. "
                        f"Shall I continue?"
                    )
                else:
                    answer = (
                        f"Updated: I'll set your{vd} **{op['product_title']}** "
                        f"quantity to {op['final_quantity']}. Shall I continue?"
                    )
            else:
                lines_desc = []
                for op in ops:
                    vt = op.get("variant_title", "")
                    vd = f" {vt}" if vt.lower() not in EMPTY else ""
                    lines_desc.append(
                        f"• {op['product_title']}{vd}: quantity → {op['final_quantity']}"
                    )
                answer = "Updated plan:\n" + "\n".join(lines_desc) + "\nShall I continue?"
            return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None
        return None  # unrecognised — let normal flow handle it

    # ── Execute all pending operations ────────────────────────────────────────
    results: list[str] = []
    errors: list[str] = []
    final_cart: Optional[dict] = None
    EMPTY = ("", "default title", "default")
    last_updated_variant_id: Optional[str] = None
    last_updated_quantity: Optional[int] = None
    last_removed_variant_id: Optional[str] = None
    last_added_variant_id: Optional[str] = None
    last_added_quantity: Optional[int] = None

    for op in state.pending_operations:
        line_id = op.get("source_line_id", "")
        variant_id = op.get("variant_id", "")

        # If source_line_id is None, this is adding a NEW variant to cart (e.g. White XL when Navy XL is in cart)
        if not line_id and variant_id:
            try:
                final_cart = add_to_draft_order(
                    session_id=session_id,
                    shop_domain=shop_domain,
                    variant_id=variant_id,
                    quantity=op["quantity_delta"],
                    admin_token=admin_token,
                )
                vt = op.get("variant_title", "")
                results.append(f"Added {op['quantity_delta']} **{op['product_title']}** ({vt}) to your cart.")
                last_added_variant_id = variant_id
                last_added_quantity = op["quantity_delta"]
                continue
            except Exception as exc:
                log.log_error(f"Cart add variant for {op['product_title']}", exc)
                errors.append(f"Failed to add **{op['product_title']}**: Shopify returned an error.")
                continue

        # Existing cart line update
        live_line = fetch_live_cart_line(
            session_id, line_id=line_id, variant_id=variant_id
        )
        if not live_line:
            # Fall back to adding variant as new if live line was removed
            if variant_id:
                try:
                    final_cart = add_to_draft_order(
                        session_id=session_id, shop_domain=shop_domain,
                        variant_id=variant_id, quantity=op["quantity_delta"],
                        admin_token=admin_token,
                    )
                    vt = op.get("variant_title", "")
                    results.append(f"Added {op['quantity_delta']} **{op['product_title']}** ({vt}) to your cart.")
                    last_added_variant_id = variant_id
                    last_added_quantity = op["quantity_delta"]
                    continue
                except Exception:
                    pass
            errors.append(f"**{op['product_title']}** is no longer in your cart.")
            continue

        live_qty = live_line.get("quantity", op.get("current_quantity", 1))
        new_qty = (
            live_qty + op["quantity_delta"]
            if op["quantity_mode"] == "increment"
            else op["quantity_delta"]
        )
        new_qty = max(0, new_qty)

        # Idempotency: live cart is already at target — skip silently
        if live_qty == new_qty:
            vt = op.get("variant_title", "")
            vd = f" {vt}" if vt.lower() not in EMPTY else ""
            results.append(
                f"**{op['product_title']}**{vd} is already at quantity {new_qty}."
            )
            last_updated_variant_id = variant_id
            last_updated_quantity = new_qty
            continue

        try:
            final_cart = update_cart_line_quantity(
                session_id=session_id,
                shop_domain=shop_domain,
                line_id=line_id,
                variant_id=variant_id,
                new_quantity=new_qty,
                admin_token=admin_token,
            )
            vt = op.get("variant_title", "")
            vd = f" {vt}" if vt.lower() not in EMPTY else ""
            if new_qty == 0:
                results.append(f"Removed{vd} **{op['product_title']}** from your cart.")
                last_removed_variant_id = variant_id
            else:
                results.append(f"**{op['product_title']}**{vd} is now quantity {new_qty}.")
                last_updated_variant_id = variant_id
                last_updated_quantity = new_qty
        except Exception as exc:
            log.log_error(f"Cart line update for {op['product_title']}", exc)
            errors.append(f"Failed to update **{op['product_title']}**: Shopify returned an error.")

    # Clean up state
    state.pending_cart_intent = None
    state.pending_operations = []
    state.active_operation_id = None
    state.stage = "browse"

    if errors and not results:
        answer = "Sorry, I couldn't update your cart: " + " ".join(errors)
    elif results:
        answer = "Done. " + " ".join(results)
        if errors:
            answer += " " + " ".join(errors)
    else:
        answer = "No changes were made to your cart."

    # Use last known cart if no mutation succeeded
    if not final_cart:
        final_cart = get_draft_order(session_id)

    return (
        answer,
        state.filtered_products or all_products, None, False, final_cart,
        False, last_removed_variant_id, False, last_updated_quantity, last_added_variant_id, last_added_quantity,
        None, False, False, last_updated_variant_id
    )


def _fast_parse_message(
    message: str,
    state: ConversationState,
    all_products: list,
    session_id: str,
    shop_domain: str,
    admin_token: Optional[str] = None,
) -> Optional[tuple]:
    """
    Ultra-fast deterministic parser for common voice commands (add, remove, search, clear).
    Bypasses LLM network calls when intent & entity can be extracted directly.
    """
    msg_lower = message.lower().strip()
    msg_stripped = msg_lower.strip(".,!? ")

    # ── Greetings & Help Fast-Path ─────────────────────────────────────────
    if msg_stripped in {"hi", "hello", "hey", "help", "who are you", "what can you do", "start"}:
        answer = "Hello! I'm your AI Shopping Assistant. I can help you find products, check options, add items to your cart, or proceed to checkout. What are you looking for today?"
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # ── INTENT PRIORITY 1: Close & Disable Commands ─────────────────────────────
    CLOSE_PHRASES = {
        "close", "close it", "close assistant", "close panel", "close widget",
        "minimize", "hide", "hide assistant", "exit assistant", "close the assistant",
        "close it please", "exit", "close chat"
    }
    if msg_stripped in CLOSE_PHRASES or bool(re.search(r"\b(close|minimize|hide)\b.*?\b(assistant|widget|panel|chat)\b", msg_lower)):
        state.reset_selection()
        state.stage = "idle"
        state.filtered_products = []
        return (
            "Okay, closing the assistant.",
            all_products, None, False, None, False, None, False, None, None, None,
            "close_assistant", True, False
        )

    DISABLE_HANDS_FREE_PHRASES = {
        "turn off hands-free", "turn off hands free", "disable hands-free",
        "disable hands free", "stop hands-free", "turn off voice", "disable voice",
        "stop listening", "turn off listening"
    }
    if msg_stripped in DISABLE_HANDS_FREE_PHRASES or "turn off hands free" in msg_lower or "turn off hands-free" in msg_lower or "stop listening" in msg_lower:
        state.reset_selection()
        state.stage = "disabled"
        state.filtered_products = []
        return (
            "Hands-free mode turned off.",
            all_products, None, False, None, False, None, False, None, None, None,
            "disable_hands_free", False, True
        )

    # ── INTENT PRIORITY 2: Cancellation & Conversation-Ending Commands ──────────────
    # Utterances combined with replacement/correction info ("No, I want Red", "No, make it Navy")
    # MUST be processed as corrections, NOT cancellations.

    PURE_CANCEL_EXACT = {
        "no", "no thanks", "no thank you", "cancel", "cancel it", "cancel that",
        "stop", "never mind", "nevermind", "that's all", "thats all", "that is all",
        "nothing else", "i'm done", "im done", "leave it", "forget it", "don't add",
        "don't add it", "don't buy", "nothing", "i don't want anything", "i don't want this",
        "i don't want it", "no cancel", "no cancel it", "nope", "nah"
    }

    # Indicators that the utterance contains replacement / correction / search info
    REPLACEMENT_TRIGGERS = (
        "show", "find", "search", "looking for", "want to buy", "get me", "show me", "find me",
        "instead", "make it", "change", "give me", "give", "switch", "prefer", "rather", "actually",
        "red", "black", "blue", "navy", "white", "green", "yellow", "pink", "grey", "gray", "brown", "purple", "orange",
        "small", "medium", "large", "extra large", "xl", "l", "m", "s", "xs", "xxl",
        "size", "color", "colour", "jeans", "shirt", "t-shirt", "tshirt", "shoes", "pants", "dress",
        "two", "three", "four", "five", "quantity", "qty"
    )

    has_replacement_info = any(rt in msg_lower for rt in REPLACEMENT_TRIGGERS)

    is_pure_cancel = (msg_stripped in PURE_CANCEL_EXACT) or (
        any(msg_lower.startswith(p) for p in ("cancel", "stop", "never mind", "nevermind", "i don't want anything", "i dont want anything", "don't add", "dont add"))
        and not has_replacement_info
    )

    if msg_stripped.startswith("no") and not has_replacement_info:
        is_pure_cancel = True

    if is_pure_cancel:
        state.reset_selection()
        state.stage = "idle"
        state.filtered_products = []
        return (
            "Okay, cancelled.",
            all_products, None, False, None, False, None, False, None, None, None,
            "end_conversation", False, False
        )

    # Check if user is combining cancellation with a NEW product search (e.g. "No, show me jeans instead")
    SEARCH_TRIGGERS = ("show", "find", "search", "looking for", "want to buy", "get me", "show me", "find me")
    if any(st in msg_lower for st in SEARCH_TRIGGERS) and any(kw in msg_lower for kw in ("jeans", "shirt", "t-shirt", "tshirt", "shoes", "pants")):
        state.reset_selection()

    # ── PRIORITY 2.45: Handle awaiting_cart_quantity ──────────────────────────
    if state.stage == "awaiting_cart_quantity":
        op = state.pending_operations[0] if state.pending_operations else None
        q_mode, q_delta = _extract_quantity_delta(message)
        has_number = bool(re.search(r"\b\d+\b", msg_lower)) or any(w in msg_lower for w in WORD_TO_NUM.keys())

        YES_WORDS_SET = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "correct", "right", "confirm"}

        if not has_number or msg_stripped in YES_WORDS_SET:
            # "Yes" is NOT a valid quantity while awaiting cart quantity! Ask again.
            answer = "Please tell me the quantity you would like."
            return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

        if op:
            op["quantity_delta"] = q_delta
            op["quantity_missing"] = False
            op["operation_version"] = op.get("operation_version", 1) + 1
            if op.get("quantity_mode") == "increment":
                op["final_quantity"] = op.get("current_quantity", 0) + q_delta
            else:
                op["final_quantity"] = q_delta
            op["status"] = "ready"
            state.pending_operations[0] = op

            # If op still missing options (e.g. Size missing), collect options; else go to confirmation
            missing = op.get("missing_options", [])
            if missing:
                state.stage = "awaiting_variant"
                state.active_operation_id = op["operation_id"]
                next_missing = missing[0]
                opts_so_far = ", ".join(v for v in op.get("selected_options", {}).values())
                prefix = f"{opts_so_far} selected. " if opts_so_far else ""
                answer = f"{prefix}Which {next_missing.lower()} would you like for **{op['product_title']}**?"
                return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

            state.stage = "awaiting_cart_confirm"
            state.active_operation_id = None

            opts_str = ", ".join(f"{v}" for k, v in op.get("selected_options", {}).items())
            vt = op.get("variant_title") or opts_str
            p_title = op.get("product_title", "")

            if op.get("current_quantity", 0) == 0:
                answer = f"I'll add {q_delta} **{p_title}** ({vt}) to your cart. Shall I continue?"
            else:
                answer = f"I'll increase your **{p_title}** ({vt}) quantity by {q_delta}, making the total {op['final_quantity']}. Shall I continue?"
            return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # ── PRIORITY 2.5: Continue collecting options for a pending multi-op ──────
    # This must run before the cart-quantity classifier so that follow-up answers
    # ("Size XL", "Color Blue") are routed to the correct pending operation.
    if state.pending_operations and state.active_operation_id:
        collect_res = _collect_operation_options(
            message, state, all_products, session_id, shop_domain, admin_token
        )
        if collect_res is not None:
            return collect_res

    # ── PRIORITY 2.6: Handle awaiting_cart_confirm (confirmation of qty update) ─
    # Run before YES_WORDS block so "yes" / "yes, add three" reaches the right handler.
    if state.stage == "awaiting_cart_confirm":
        confirm_res = _handle_awaiting_cart_confirm(
            message, state, all_products, session_id, shop_domain, admin_token
        )
        if confirm_res is not None:
            return confirm_res
        # If _handle_awaiting_cart_confirm returned None, neither yes nor no was detected
        # → fall through to let the user type something more specific

    # ── PRIORITY 3: Cart quantity intent classifier ───────────────────────────
    # Must run BEFORE the generic "is_add" fast-path so that "add two more jeans"
    # is treated as a cart update, not a new product search.
    if not state.pending_operations:
        qty_intent = _classify_cart_quantity_intent(message, state, all_products, session_id)
        if qty_intent is not None:
            qty_res = _handle_cart_quantity_intent(
                qty_intent, state, all_products, session_id, shop_domain, admin_token
            )
            if qty_res is not None:
                return qty_res

    # ── Single-word yes/no fast-path ──────────────────────────────────────

    YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "correct",
                 "right", "go", "add", "confirm", "please", "definitely",
                 "absolutely", "affirmative", "do it", "do", "great",
                 "yes add to cart", "yes add it to cart", "add to cart", "add it to cart", "yes add to my cart", "add it", "yes add it"}

    if msg_stripped in YES_WORDS and state.stage == "awaiting_confirm":
        # Fast-track confirmation → same as handle_awaiting_confirm with intent=confirm_yes
        product = state.selected_product
        variant = state.selected_variant
        if product and variant:
            if not _is_variant_in_stock(variant):
                opts_summary = _get_product_options_summary(product)
                answer = f"Sorry, **{variant.get('title')}** is currently out of stock for **{product['title']}**. Available options: {opts_summary}."
                state.stage = "awaiting_variant"
                return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None
            try:
                quantity = state.quantity or 1
                cart = add_to_draft_order(
                    session_id=session_id,
                    shop_domain=shop_domain,
                    product=product,
                    variant=variant,
                    quantity=quantity,
                    admin_token=admin_token,
                )
                state.cart_id = cart.get("cart_id", state.cart_id)
                qty_str = f"{quantity}x " if quantity > 1 else ""
                answer = (
                    f"✅ Added {qty_str}**{product['title']}** "
                    f"({variant.get('title')}) to your cart!"
                )
                state.stage = "browse"
                state.selected_product = None
                state.selected_variant = None
                state.quantity = 1
                return answer, state.filtered_products or all_products, None, False, cart, False, None, False, None, variant.get("id"), quantity
            except Exception as exc:
                log.log_error("Fast yes-confirm", exc)

    # ── Option Selection Fast-Path (Color, Size, Style, Material) ──────────
    if (state.stage in ("awaiting_variant", "awaiting_confirm") or state.stage.startswith("awaiting_")) and state.selected_product:
        product = state.selected_product

        # Check for incomplete quantity phrase ("quantity should be")
        if _has_incomplete_quantity_phrase(message):
            state.stage = "awaiting_quantity"
            opts_str = ", ".join(f"{v}" for k, v in state.selected_options.items() if v)
            prefix = f"{opts_str} selected. " if opts_str else ""
            answer = f"{prefix}What quantity would you like?"
            return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

        valid_opts, invalid_opts, missing_options = _validate_and_apply_options(product, message, state)

        # 1) If an invalid option was requested (e.g. Red for Everyday Cotton T-Shirt)
        if invalid_opts:
            inv_key, inv_val = list(invalid_opts.items())[0]
            opt_map = _get_product_option_values(product)
            allowed = opt_map.get(inv_key, [])
            if len(allowed) <= 2:
                allowed_str = " and ".join(allowed)
            else:
                allowed_str = ", ".join(allowed[:-1]) + f", and {allowed[-1]}"
            p_title = product.get("title", "this item")
            answer = f"{inv_val} is not available for the **{p_title}**. Available {inv_key.lower()}s are {allowed_str}. Which {inv_key.lower()} would you like?"
            state.stage = f"awaiting_{inv_key.lower()}"
            return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

        # 2) If valid options were provided or exist in state
        if valid_opts or state.selected_options:
            variant = _resolve_variant_by_options(product, state.selected_options)
            has_explicit_add = any(w in msg_lower for w in ("add", "buy", "cart", "checkout", "check out", "basket", "bag", "put in", "place in"))

            if not missing_options:
                if variant and _is_variant_in_stock(variant):
                    if has_explicit_add:
                        try:
                            extracted_q = extract_quantity(message)
                            quantity = extracted_q if extracted_q > 1 else (state.quantity or 1)
                            cart = add_to_draft_order(
                                session_id=session_id,
                                shop_domain=shop_domain,
                                product=product,
                                variant=variant,
                                quantity=quantity,
                                admin_token=admin_token,
                            )
                            state.cart_id = cart.get("cart_id", state.cart_id)
                            qty_str = f"{quantity}x " if quantity > 1 else ""
                            answer = (
                                f"✅ Added {qty_str}**{product['title']}** "
                                f"({variant.get('title')}) to your cart!"
                            )
                            state.stage = "browse"
                            state.reset_selection()
                            return answer, state.filtered_products or all_products, None, False, cart, False, None, False, None, variant.get("id"), quantity
                        except Exception as exc:
                            log.log_error("Fast option add-to-cart", exc)

                    state.selected_variant = variant
                    state.stage = "awaiting_confirm"
                    opts_speech = ", ".join(f"{k.lower()} {v}" for k, v in state.selected_options.items() if v)
                    answer = (
                        f"You selected the **{product['title']}** in {opts_speech}. "
                        f"Would you like me to add it to your cart?"
                    )
                    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None
                else:
                    # Check if color and size exist separately but combination is unavailable
                    sel_color = state.selected_options.get("Color")
                    sel_size = state.selected_options.get("Size")
                    if sel_color and sel_size:
                        avail_sizes = []
                        for v in product.get("variants", {}).get("nodes", []):
                            opts = {o.get("name", "").lower(): o.get("value", "") for o in v.get("selectedOptions", [])}
                            if opts.get("color", "").lower() == sel_color.lower() and _is_variant_in_stock(v):
                                avail_sizes.append(opts.get("size", ""))
                        if avail_sizes:
                            sizes_str = " and ".join(avail_sizes) if len(avail_sizes) <= 2 else ", ".join(avail_sizes)
                            answer = f"{sel_color} and {sel_size} are available separately, but the {sel_color} / {sel_size} combination is currently unavailable. Available {sel_color} sizes are {sizes_str}."
                            state.stage = "awaiting_size"
                            return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

                    opts_fmt = ", ".join(f"{v}" for k, v in state.selected_options.items() if v)
                    available_summary = _get_product_options_summary(product)
                    state.stage = "awaiting_variant"
                    answer = (
                        f"Sorry, **{product['title']}** in {opts_fmt} is currently out of stock. "
                        f"Available options: {available_summary}."
                    )
                    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None
            else:
                next_missing = missing_options[0]
                state.stage = f"awaiting_{next_missing.lower()}"
                opts_fmt = ", ".join(f"{v}" for k, v in state.selected_options.items() if v)
                variants = product.get("variants", {}).get("nodes", [])
                available_vals = set()
                for v in variants:
                    if _is_variant_in_stock(v):
                        for o in v.get("selectedOptions", []):
                            if o.get("name", "").strip().lower() == next_missing.lower():
                                available_vals.add(o.get("value", "").strip())
                vals_str = ", ".join(sorted(available_vals))
                prefix = f"{opts_fmt} selected. " if opts_fmt else ""
                answer = f"{prefix}Which {next_missing.lower()} would you like for **{product['title']}**? Available: {vals_str}."
                return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # 0. Fast Checkout Only (e.g. "go to checkout", "proceed to checkout")
    CHECKOUT_WORDS = {"checkout", "check out", "go to checkout", "proceed to checkout", "take me to checkout", "open checkout", "pay now", "cart checkout"}
    is_checkout_only = any(w in msg_lower for w in CHECKOUT_WORDS) and not any(w in msg_lower for w in ("remove", "delete", "clear", "add", "select", "take", "buy", "pick", "get"))
    if is_checkout_only:
        cart = get_draft_order(session_id)
        if cart and cart.get("lines"):
            answer = "Taking you to checkout now!"
    # 0. Search & Filter Fast-Path (e.g. "show me T-shirts under $50", "show snowboards", "find shoes")
    search_verbs = ("show", "find", "search", "list", "look for", "bring up", "browse", "catalog", "products", "do you have")
    is_search_cmd = any(w in msg_lower for w in search_verbs) or any(pt in msg_lower for pt in ("t-shirt", "tshirt", "snowboard", "hoodie", "jeans", "shoes", "sneakers", "jacket", "shirt", "wax", "gift card"))
    is_not_cart_action = not any(w in msg_lower for w in ("remove", "delete", "clear", "cart", "card", "checkout", "add to", "put in", "buy"))

    if is_search_cmd and is_not_cart_action:
        state.reset_selection()

        max_price = None
        min_price = None
        m_max = re.search(r"\b(under|below|less than|max)\s+\$?(\d+(?:\.\d+)?)\b", msg_lower)
        if m_max:
            max_price = float(m_max.group(2))
        m_min = re.search(r"\b(over|above|more than|min)\s+\$?(\d+(?:\.\d+)?)\b", msg_lower)
        if m_min:
            min_price = float(m_min.group(2))

        req_type = None
        for pt, canonical in [
            ("t-shirt", "T-Shirt"), ("tshirt", "T-Shirt"), ("shirt", "Shirt"),
            ("snowboard", "snowboard"), ("hoodie", "Hoodie"), ("jeans", "Jeans"),
            ("shoes", "Shoes"), ("sneakers", "Shoes"), ("wax", "accessories"),
            ("gift card", "Gift Cards"), ("giftcard", "Gift Cards")
        ]:
            if pt in msg_lower:
                req_type = canonical
                break

        filters = {}
        if req_type:
            filters["product_type"] = req_type
        if max_price is not None:
            filters["max_price"] = max_price
        if min_price is not None:
            filters["min_price"] = min_price

        if not req_type:
            for p in all_products:
                title_l = p.get("title", "").lower()
                if any(w in msg_lower for w in title_l.split() if len(w) > 3):
                    filters["product_name"] = p.get("title")
                    break

        if filters:
            normalized = normalize_filters(filters, all_products)
            filtered = filter_products(all_products, filters)
            storefront_url = build_storefront_url(normalized)
            state.filtered_products = filtered
            state.previous_filters = filters

            if not filtered:
                state.stage = "browse"
                answer = "I couldn't find any products matching your request. Try adjusting your filters."
            elif len(filtered) == 1:
                p = filtered[0]
                state.selected_product = p

                # Check for incomplete quantity phrase ("and the quantity should be")
                if _has_incomplete_quantity_phrase(message):
                    _validate_and_apply_options(p, message, state)
                    state.stage = "awaiting_quantity"
                    opts_str = ", ".join(f"{v}" for k, v in state.selected_options.items() if v)
                    prefix = f"{opts_str} selected. " if opts_str else ""
                    answer = f"{prefix}What quantity would you like?"
                    return answer, filtered, storefront_url, False, None, False, None, False, None, None, None

                valid_opts, invalid_opts, missing_options = _validate_and_apply_options(p, message, state)

                if invalid_opts:
                    inv_key, inv_val = list(invalid_opts.items())[0]
                    opt_map = _get_product_option_values(p)
                    allowed = opt_map.get(inv_key, [])
                    allowed_str = " and ".join(allowed) if len(allowed) <= 2 else ", ".join(allowed[:-1]) + f", and {allowed[-1]}"
                    p_title = p.get("title", "this item")
                    answer = f"{inv_val} is not available for the **{p_title}**. Available {inv_key.lower()}s are {allowed_str}. Which {inv_key.lower()} would you like?"
                    state.stage = f"awaiting_{inv_key.lower()}"
                    return answer, filtered, storefront_url, False, None, False, None, False, None, None, None

                if not missing_options:
                    variant = _resolve_variant_by_options(p, state.selected_options)
                    if variant and _is_variant_in_stock(variant):
                        state.selected_variant = variant
                        state.stage = "awaiting_confirm"
                        opts_speech = ", ".join(f"{k.lower()} {v}" for k, v in state.selected_options.items() if v)
                        answer = f"You selected the **{p['title']}** in {opts_speech}. Would you like me to add it to your cart?"
                        return answer, filtered, storefront_url, False, None, False, None, False, None, None, None

                next_missing = missing_options[0] if missing_options else "variant"
                state.stage = f"awaiting_{next_missing.lower()}"
                opts_summary = _available_variants_text(p)
                answer = f"Found **{p['title']}**. Options: {opts_summary}. Which would you like?"
            else:
                state.stage = "awaiting_variant"
                answer = f"Found {len(filtered)} matching products. Which size or color would you like?"

            return answer, filtered, storefront_url, False, None, False, None, False, None, None, None

    # 0. Options / Variant Inquiry Fast-Path
    options_inquiry = any(w in msg_lower for w in (
        "what colors", "which colors", "what sizes", "which sizes", "available options",
        "which available", "available t-shirts", "available shirts", "tell me more about",
        "options in", "colors in", "sizes in", "what options", "which options", "what variants"
    ))
    if options_inquiry:
        target_prod = state.selected_product
        if not target_prod:
            for p in all_products:
                title_l = p.get("title", "").lower()
                if title_l in msg_lower or any(w in msg_lower for w in title_l.split() if len(w) > 3):
                    target_prod = p
                    break
        if not target_prod and all_products:
            target_prod = all_products[0]

        if target_prod:
            state.selected_product = target_prod
            state.stage = "awaiting_variant"
            opts_summary = _get_product_options_summary(target_prod)
            answer = f"**{target_prod['title']}** is available in {opts_summary}. Which option would you like?"
            prod_url = f"/products/{target_prod['handle']}" if target_prod.get("handle") else None
            return answer, state.filtered_products or all_products, prod_url, False, None, False, None, False, None, None, None

    # 0. Fast Cart Inquiry — broad matching to catch all STT variants and phrasings
    # Catches: "what's in my cart", "which product is in my cart", "provide cart details",
    # "tell me what is in my cart", STT errors like "card"/"rand", etc.
    cart_keywords = {"cart", "card"}  # STT: "card" = "cart", "rand" misheard
    cart_inquiry_words = {"what", "which", "show", "tell", "list", "view", "check",
                          "see", "give", "provide", "details", "contents", "items",
                          "products", "have", "there", "anything", "empty", "how many",
                          "current", "my", "in", "inside"}
    msg_words = set(re.split(r'\W+', msg_lower))

    # Match if message contains a cart keyword AND an inquiry-type word
    # OR if the message is one of the simple exact cart commands
    is_cart_inquiry = (
        bool(msg_words & cart_keywords) and bool(msg_words & cart_inquiry_words)
    ) or msg_stripped in {"cart", "my cart", "show cart", "view cart", "check cart",
                           "whats in my cart", "what is in my cart"}

    if is_cart_inquiry and not re.search(r'\b(add|remove|delete|clear|drop|empty|buy|checkout)\b', msg_lower):
        cart = get_draft_order(session_id)
        lines = cart.get("lines", []) if cart else []
        if not lines:
            answer = "Your cart is currently empty."
        else:
            items_str = ", ".join(
                f"**{l.get('product_title', '?')}**"
                + (f" ({l.get('variant_title', '')})" if l.get("variant_title", "").lower() not in ("", "default title", "default") else "")
                + (f" x{l.get('quantity', 1)}" if l.get("quantity", 1) > 1 else "")
                for l in lines
            )
            total_qty = cart.get("total_quantity", sum(l.get("quantity", 1) for l in lines))
            answer = f"Your cart currently has {total_qty} item(s): {items_str}."
        return answer, state.filtered_products or all_products, None, False, cart, False, None, False, None, None, None

    # 0. Fast Clear Entire Cart
    clear_all_pattern = r"\b((empty|clear|remove|delete|take out|drop|get rid of)\s+(all|everything)(\s+items?|\s+products?|\s+from\s+(the\s+)?(cart|card))?|(empty|clear)\s+(the\s+|my\s+)?(cart|card)|remove\s+all|delete\s+all|drop\s+all|empty\s+all|clear\s+all)\b"
    if re.search(clear_all_pattern, msg_lower):
        clear_draft_order(session_id)
        empty_cart = {"cart_id": "", "total_quantity": 0, "lines": []}
        return "I've emptied your cart.", state.filtered_products or all_products, None, False, empty_cart, False, None, True, None, None, None

    # 1. Fast Remove from Cart
    remove_pattern = r"\b(remove|delete|deleted|deleting|take out|took out|drop|dropped|clear|empty|get rid of|discard|cancel)\b"
    if re.search(remove_pattern, msg_lower):
        action_stop_words = {
            "remove", "removed", "removes", "delete", "deleted", "deleting",
            "take", "out", "took", "drop", "dropped", "clear", "empty", "get",
            "rid", "of", "from", "cart", "my", "the", "a", "an", "this", "that",
            "please", "item", "product", "in", "to", "for", "one", "two", "three",
            "four", "five", "1", "2", "3", "4", "5", "quantity", "qty", "amount",
            "count", "number", "unit", "units", "items", "card", "all"
        }
        words = [w for w in re.split(r'\W+', msg_lower) if len(w) >= 1 and w not in action_stop_words]
        prod_ref = " ".join(words) if words else None

        answer, removed_variant_id, cart, updated_qty = _handle_remove_from_cart(
            message=message,
            state=state,
            session_id=session_id,
            shop_domain=shop_domain,
            admin_token=admin_token,
            product_ref=prod_ref,
            all_products=all_products,
        )
        return answer, state.filtered_products or all_products, None, False, cart, False, removed_variant_id, False, updated_qty, None, None

    # 2. Fast Add to Cart & Selection
    has_action = any(w in msg_lower for w in ("add", "select", "buy", "take", "pick", "get", "bring", "put", "place", "play", "pay"))
    has_target = any(w in msg_lower for w in ("cart", "checkout", "check out", "basket", "bag"))
    is_checkout_cmd = "checkout" in msg_lower or "check out" in msg_lower
    has_explicit_add = any(w in msg_lower for w in ("add", "buy", "cart", "checkout", "check out", "basket", "bag", "put in", "place in"))

    is_add = (has_action and has_target) or msg_lower.startswith("add ") or msg_lower.startswith("select ") or is_checkout_cmd or state.stage == "awaiting_variant"
    if is_add:
        full_options_text = (state.selected_options_text + " " + msg_lower).strip() if state.selected_options_text else msg_lower
        quantity = extract_quantity(full_options_text)

        size_hint = None
        for s in ("xxl", "2xl", "xl", "xs", "small", "medium", "large", " size s ", " size m ", " size l ", " s ", " m ", " l "):
            if s in msg_lower:
                size_hint = s.strip()
                break

        action_stop_words = {
            "add", "added", "adds", "put", "place", "buy", "want", "get", "select", "selected",
            "take", "took", "pick", "bring", "to", "in", "into", "my", "the", "a", "an", "this", "that",
            "please", "item", "product", "cart", "basket", "bag", "checkout", "check", "out", "page",
            "size", "color", "colour", "more", "quantity", "qty", "amount", "count",
            "number", "unit", "units", "items", "card", "and", "go"
        } | set(WORD_TO_NUM.keys())
        words = [w for w in re.split(r'\W+', msg_lower) if len(w) > 2 and w not in action_stop_words and not w.isdigit()]

        matched_prod = None
        if words:
            best_score = 0
            for prod in all_products:
                title_lower = prod.get("title", "").lower()
                score = sum(1 for w in words if w in title_lower)
                if score > best_score:
                    best_score = score
                    matched_prod = prod

        if not matched_prod and state.selected_product:
            matched_prod = state.selected_product
        elif not matched_prod and state.filtered_products and len(state.filtered_products) == 1:
            matched_prod = state.filtered_products[0]

        # Ambiguity check: if user says "add it" or generic add with multiple products and no product matched
        if not matched_prod and state.filtered_products and len(state.filtered_products) > 1 and not words:
            titles = ", ".join(p['title'] for p in state.filtered_products[:3])
            answer = f"Which product did you mean? I can see: {titles}."
            return answer, state.filtered_products, None, True, None, False, None, False, None, None, None

        if matched_prod:
            # If we're already in awaiting_confirm stage and user is saying yes/add,
            # trust the saved variant — don't re-run _check_missing_options
            already_confirmed = (
                state.stage == "awaiting_confirm"
                and state.selected_product == matched_prod
                and state.selected_variant is not None
            )

            # Check if required options (like Size or Color) are missing in user request
            if not already_confirmed:
                is_complete, missing_opt_name, missing_vals = _check_missing_options(matched_prod, full_options_text)
                if not is_complete and missing_opt_name:
                    state.selected_product = matched_prod
                    state.selected_options_text = full_options_text
                    state.stage = "awaiting_variant"
                    vals_str = ", ".join(missing_vals)
                    answer = f"Which {missing_opt_name.lower()} would you like for **{matched_prod['title']}**? Available: {vals_str}."
                    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

            variant = None
            if already_confirmed or (matched_prod == state.selected_product and state.selected_variant and not size_hint):
                variant = state.selected_variant
            else:
                variant = _find_variant(matched_prod, full_options_text)

            if not variant:
                vars_list = matched_prod.get("variants", {}).get("nodes", [])
                in_stock_vars = [v for v in vars_list if _is_variant_in_stock(v)]
                variant = in_stock_vars[0] if in_stock_vars else (vars_list[0] if vars_list else None)

            if variant and not _is_variant_in_stock(variant):
                opts_summary = _get_product_options_summary(matched_prod)
                answer = f"Sorry, **{variant.get('title')}** is currently out of stock for **{matched_prod['title']}**. Available options: {opts_summary}."
                state.stage = "awaiting_variant"
                return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

            if variant:
                inv_qty = variant.get("inventoryQuantity")
                if inv_qty is not None and isinstance(inv_qty, int) and inv_qty >= 0 and quantity > inv_qty:
                    if inv_qty == 0:
                        opts_summary = _get_product_options_summary(matched_prod)
                        answer = f"Sorry, **{variant.get('title')}** is currently out of stock for **{matched_prod['title']}**. Available options: {opts_summary}."
                        state.stage = "awaiting_variant"
                        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None
                    else:
                        quantity = inv_qty

                state.selected_product = matched_prod
                state.selected_variant = variant
                state.quantity = quantity

                # Skip confirmation if user explicitly said "add", "add to cart", "checkout", "buy", etc.
                # Only ask for confirmation when user is just selecting/specifying a variant (e.g. "size M colour blue")
                skip_confirm = has_explicit_add or is_checkout_cmd
                if not skip_confirm:
                    state.stage = "awaiting_confirm"
                    price = float(variant.get("price", 0))
                    qty_str = f"{quantity}x " if quantity > 1 else ""
                    answer = f"Got it! Just to confirm: {qty_str}**{matched_prod['title']}** — {variant.get('title')} at ${price:.2f}. Shall I add this to your cart?"
                    return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

                # Otherwise perform direct add
                try:
                    cart = add_to_draft_order(
                        session_id=session_id,
                        shop_domain=shop_domain,
                        product=matched_prod,
                        variant=variant,
                        quantity=quantity,
                        admin_token=admin_token,
                    )
                    state.selected_options_text = ""
                    qty_str = f"{quantity}x " if quantity > 1 else ""
                    checkout_text = " Taking you to checkout now!" if is_checkout_cmd else ""
                    answer = f"✅ Added {qty_str}**{matched_prod['title']}** ({variant.get('title')}) to your cart!{checkout_text}"
                    target_url = "/cart" if is_checkout_cmd else None
                    state.stage = "browse"
                    state.selected_product = None
                    state.selected_variant = None
                    state.quantity = 1
                    return answer, all_products, target_url, False, cart, is_checkout_cmd, None, False, None, variant.get("id"), quantity
                except Exception as e:
                    log.log_warn("Fast add exception", str(e))

    return None


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
    admin_token: Optional[str] = None,
    transcription: Optional[str] = None,
    cart_context: Optional[dict] = None,
    sequence_num: Optional[int] = None,
) -> dict:
    state = session_store.get(session_id)

    # Sync backend cart with live Shopify native store cart (/cart.js) if provided
    if cart_context:
        sync_native_cart(session_id, cart_context)

    # Fetch live product catalog (cached 60s)
    all_products = get_products(
        shop_domain=shop_domain,
        admin_token=admin_token if admin_token else None,
    )

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
    fast_res = _fast_parse_message(message, state, all_products, session_id, shop_domain, admin_token)
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

        session_store.save(session_id, state)
        log.log_info(f"⚡ Fast-path matched: stage={state.stage}")
        if storefront_url:
            log.log_storefront_url(storefront_url)
        if shown_products:
            log.log_products(len(shown_products), shown_products)
        return _build_response(
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

    elif is_add_request or state.stage == "awaiting_variant":
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
            cart = get_draft_order(session_id)
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

    session_store.save(session_id, state)

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


    return _build_response(
        state=state,
        answer=answer,
        audio_b64=audio_b64,
        products=shown_products,
        needs_clarification=needs_clarification,
        cart=cart,
        storefront_url=storefront_url,
        transcription=transcription or message,
        auto_checkout=auto_checkout,
        removed_variant_id=removed_variant_id,
        updated_quantity=updated_quantity,
        added_variant_id=added_variant_id,
        added_quantity=added_quantity,
        sequence_num=sequence_num,
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
    try:
        shop_domain  = _resolve_domain(request.shop_domain)
        admin_token  = _resolve_admin_token(request.admin_token)

        log.log_request_start(request.session_id, shop_domain)
        log.log_info(f"💬 Text Input (Browser STT): \"{request.message}\"")

        result = _process_message(
            message=request.message,
            session_id=request.session_id,
            shop_domain=shop_domain,
            admin_token=admin_token,
            cart_context=request.cart_context,
            sequence_num=request.sequence_num,
        )

        log.log_stage(result.get("stage", "browse"))
        log.log_answer(result.get("answer", ""))

        return result
    except Exception as exc:
        log.log_error("voice_text endpoint error", exc)
        state = session_store.get(request.session_id) if request and request.session_id else ConversationState()
        if not state:
            state = ConversationState()
        return _build_response(
            state=state,
            answer="Sorry, I ran into a temporary issue. Could you please say that again?",
            needs_clarification=True,
            sequence_num=getattr(request, "sequence_num", 0),
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