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
from cart_service import add_to_draft_order, get_draft_order, remove_from_cart_by_variant, clear_draft_order, sync_native_cart
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
    m = re.search(r"\b(\d+)\b", text_lower)
    if m:
        return int(m.group(1))
    for word, val in WORD_TO_NUM.items():
        if re.search(r"\b" + word + r"\b", text_lower):
            return val
    return 1


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
        "auto_checkout": auto_checkout,
        "removed_variant_id": removed_variant_id,
        "clear_cart": clear_cart,
        "updated_quantity": updated_quantity,
        "added_variant_id": added_variant_id,
        "added_quantity": added_quantity,
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

    raw_words = [w.strip() for w in re.split(r'\W+', ref) if w.strip() and not w.strip().isdigit()]
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
            for size_term in ("xxl", "2xl", "xl", "xs", "small", "medium", "large", " size s ", " size m ", " size l ", " s ", " m ", " l "):
                if size_term in ref_lower:
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
                    if val.lower() in words:
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
    full_options_text = (state.selected_options_text + " " + message).strip() if state.selected_options_text else message

    # Check if required options (like Size or Color) are missing in user request
    is_complete, missing_opt_name, missing_vals = _check_missing_options(product, full_options_text)
    if not is_complete and missing_opt_name:
        state.selected_product = product
        state.selected_options_text = full_options_text
        state.stage = "awaiting_variant"
        vals_str = ", ".join(missing_vals)
        answer = f"Which {missing_opt_name.lower()} would you like for **{product['title']}**? Available: {vals_str}."
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # ── Resolve the variant ──────────────────────────────────────────────
    variant = _find_variant(product, full_options_text)

    if not variant:
        variants = product.get("variants", {}).get("nodes", [])
        if len(variants) == 1:
            variant = variants[0]
        elif variants:
            available_vars = [v for v in variants if _is_variant_in_stock(v)]
            if available_vars:
                variant = available_vars[0]
            else:
                variant = variants[0]

    if not variant:
        available = _available_variants_text(product)
        return (
            f"I couldn't find size/option '{variant_ref}' for "
            f"**{product['title']}**. Available options: {available}.",
            state.filtered_products,
            None,
            True,
            None,
            False,
        )

    if not _is_variant_in_stock(variant):
        available = _available_variants_text(product)
        return (
            f"Sorry, **{variant.get('title')}** is currently out of stock for "
            f"**{product['title']}**. Available options: {available}.",
            state.filtered_products,
            None,
            False,
            None,
            False,
        )

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


def _fast_parse_message(
    message: str,
    state: ConversationState,
    all_products: list,
    session_id: str,
    shop_domain: str,
    admin_token: Optional[str] = None,
) -> Optional[tuple[str, list, Optional[str], bool, Optional[dict], bool, Optional[str], bool]]:
    """
    Ultra-fast 0ms deterministic parser for common voice commands (add, remove, search, clear).
    Bypasses LLM network calls when intent & entity can be extracted directly.
    """
    msg_lower = message.lower().strip()
    msg_stripped = msg_lower.strip(".,!? ")

    # ── Greetings & Help Fast-Path ─────────────────────────────────────────
    if msg_stripped in {"hi", "hello", "hey", "help", "who are you", "what can you do", "start"}:
        answer = "Hello! I'm your AI Shopping Assistant. I can help you find products, check options, add items to your cart, or proceed to checkout. What are you looking for today?"
        return answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None

    # ── Single-word yes/no fast-path ──────────────────────────────────────
    # Handles the most common voice replies: "yes", "no", "yeah", "ok", etc.
    YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "correct",
                 "right", "go", "add", "confirm", "please", "definitely",
                 "absolutely", "affirmative", "do it", "do", "great",
                 "yes add to cart", "yes add it to cart", "add to cart", "add it to cart", "yes add to my cart", "add it"}
    NO_WORDS  = {"no", "nope", "nah", "cancel", "stop", "back", "nevermind",
                 "never", "negative", "dont", "don't", "skip", "quit", "exit"}

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

    # Also catch cancel phrases like "no, cancel", "no thanks", "don't add" when confirming
    NO_PHRASE_PATTERN = r"^(no\b|nope|nah|cancel|stop|nevermind|don'?t|skip|back|quit|exit)"
    is_no = msg_stripped in NO_WORDS or bool(re.match(NO_PHRASE_PATTERN, msg_stripped))
    if is_no and state.stage in ("awaiting_confirm", "awaiting_variant"):
        state.stage = "awaiting_variant" if state.filtered_products else "browse"
        state.selected_product = None
        state.selected_variant = None
        return (
            "No problem! What else can I help you find?",
            state.filtered_products or all_products, None, False, None, False, None, False, None, None, None
        )

    # ── Single-word size fast-path ────────────────────────────────────────
    # Handles replies like "M", "L", "XL", "small", "medium", "large"
    SIZE_MAP_VOICE = {
        "s": "S", "small": "S",
        "m": "M", "medium": "M",
        "l": "L", "large": "L",
        "xl": "XL", "extra large": "XL", "extralarge": "XL",
        "xs": "XS", "extra small": "XS",
        "xxl": "XXL", "2xl": "XXL",
    }
    if msg_stripped in SIZE_MAP_VOICE and state.stage in ("awaiting_variant", "awaiting_confirm"):
        canonical_size = SIZE_MAP_VOICE[msg_stripped]
        product = state.selected_product
        if product:
            variant = _find_variant(product, canonical_size)
            if variant and variant.get("availableForSale"):
                state.selected_variant = variant
                state.quantity = 1
                state.stage = "awaiting_confirm"
                price = float(variant.get("price", 0))
                answer = (
                    f"Got it! **{product['title']}** — {variant.get('title')} "
                    f"at ${price:.2f}. Shall I add this to your cart?"
                )
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
    is_search_cmd = any(w in msg_lower for w in search_verbs) or any(pt in msg_lower for pt in ("t-shirt", "tshirt", "snowboard", "hoodie", "jeans", "shoes", "sneakers", "wax", "gift card"))
    is_not_cart_action = not any(w in msg_lower for w in ("remove", "delete", "clear", "cart", "card", "checkout", "add to", "put in", "buy"))
    is_variant_selection = bool(state.selected_product) or (state.stage in ("awaiting_variant", "awaiting_confirm") and not any(w in msg_lower for w in search_verbs))

    if is_search_cmd and is_not_cart_action and not is_variant_selection:
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
                state.selected_product = filtered[0]
                state.stage = "awaiting_variant"
                opts_summary = _available_variants_text(filtered[0])
                answer = f"Found **{filtered[0]['title']}**. Options: {opts_summary}. Which would you like?"
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
        ) = fast_res

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

    log.log_request_start(request.session_id, shop_domain)
    log.log_info(f"💬 Text Input (Browser STT): \"{request.message}\"")

    result = _process_message(
        message=request.message,
        session_id=request.session_id,
        shop_domain=shop_domain,
        admin_token=admin_token,
        cart_context=request.cart_context,
    )

    log.log_stage(result.get("stage", "browse"))
    log.log_answer(result.get("answer", ""))

    return result


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