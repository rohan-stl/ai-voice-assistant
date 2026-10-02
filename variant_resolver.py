import re
import threading
from typing import Optional
from conversation_state import ConversationState
from shopify_service import extract_variant_nodes, normalize_shopify_variant
import logger as log
from constants import WORD_TO_NUM

# Module-level cache for global option vocabulary, keyed by frozenset of product ids.
_options_cache: dict = {}
_options_cache_lock = threading.Lock()

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

def _extract_all_unvalidated_options(text: str, all_products: list) -> dict[str, str]:
    """
    Extract any option (color, size, etc.) mentioned in user text dynamically based on the full catalog.
    Caches the global option vocabulary per unique product set to avoid O(N*V) per message.
    Returns dict like {"Color": "Charcoal", "Size": "L"}
    """
    text_lower = text.lower()
    found = {}

    # Build a stable cache key from the set of product ids
    cache_key = frozenset(p.get("id", p.get("handle", "")) for p in all_products)
    
    with _options_cache_lock:
        if cache_key not in _options_cache:
            global_options: dict[str, set] = {}
            for product in all_products:
                opts = _get_product_option_values(product)
                for opt_name, vals in opts.items():
                    if opt_name not in global_options:
                        global_options[opt_name] = set()
                    for val in vals:
                        global_options[opt_name].add(str(val))
            _options_cache[cache_key] = global_options
        global_options = _options_cache[cache_key]

    for opt_name, vals in global_options.items():
        for val in vals:
            if opt_name.lower() == "size":
                # Handle size canonicalization synonyms mapping
                for syn, canonical in _SIZE_CANONICAL.items():
                    if canonical.lower() == val.lower():
                        pattern = r"\b" + re.escape(syn) + r"\b"
                        if re.search(pattern, text_lower):
                            found["Size"] = canonical
                            break
            
            # Standard value matching
            pattern = r"\b" + re.escape(val.lower()) + r"\b"
            if re.search(pattern, text_lower):
                # Don't overwrite canonical size match with raw size match if we already found a synonym
                if opt_name.lower() == "size" and "Size" in found:
                    pass
                else:
                    found[opt_name] = val.capitalize() if opt_name.lower() == "color" else val
                break

    return found


def invalidate_options_cache():
    """Call this when the product catalog is refreshed to clear the cached option vocabulary."""
    with _options_cache_lock:
        _options_cache.clear()


def _validate_and_apply_options(
    product: dict,
    text: str,
    state: ConversationState,
    all_products: list
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

    unvalidated = _extract_options_from_text(product, text)
    generic = _extract_all_unvalidated_options(text, all_products)
    for k, v in generic.items():
        if k not in unvalidated:
            unvalidated[k] = v

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
    Check if a variant is available for sale.
    Returns True if available_for_sale is True.
    Does NOT mark as unavailable simply because inventoryQuantity is 0,
    because merchants frequently allow overselling/selling when inventory is 0.
    """
    if not variant:
        return False
    norm = normalize_shopify_variant(variant)
    return norm["available_for_sale"] is True


def is_shopify_default_title(name: str, value: str) -> bool:
    """
    Check if an option name/value pair is Shopify's placeholder for single-variant products.
    Returns True ONLY when normalize(name) == 'title' and normalize(value) == 'default title'.
    """
    if not name or not value:
        return False
    return name.strip().lower() == "title" and value.strip().lower() == "default title"


def get_remaining_option_values(
    product: dict,
    selected_options: dict,
    requested_option_name: Optional[str] = None,
    shop_domain: str = ""
) -> dict[str, list[dict]]:
    """
    Computes available and selected option values for a product given currently selected_options.
    Returns dict mapping option name -> list of dicts:
    [
        {"value": "S", "available": True, "selected": False},
        {"value": "M", "available": True, "selected": True}
    ]
    """
    if not product:
        return {}

    raw_variants = extract_variant_nodes(product.get("variants", {})) if isinstance(product.get("variants"), dict) else (product.get("variants") if isinstance(product.get("variants"), list) else [])
    variants = [normalize_shopify_variant(v) for v in raw_variants]
    in_stock_variants = [v for v in variants if v["available_for_sale"] is True]

    # Collect configured option names (preserving order)
    option_names_ordered = []

    for opt in product.get("options", []):
        opt_name = opt.get("name", "").strip()
        if opt_name and opt_name.lower() != "title":
            if opt_name not in option_names_ordered:
                option_names_ordered.append(opt_name)

    for v in variants:
        opts_list = v.get("selected_options") or v.get("selectedOptions") or []
        for o in opts_list:
            name = o.get("name", "").strip()
            val = o.get("value", "").strip()
            if is_shopify_default_title(name, val):
                continue
            if name and name not in option_names_ordered:
                option_names_ordered.append(name)

    all_values_per_option = {}
    for name in option_names_ordered:
        all_values_per_option[name] = []

    for v in variants:
        opts_list = v.get("selected_options") or v.get("selectedOptions") or []
        for o in opts_list:
            name = o.get("name", "").strip()
            val = o.get("value", "").strip()
            if is_shopify_default_title(name, val):
                continue
            if name.strip().lower() == "title" and val.strip().lower() == "available":
                continue
            if name in all_values_per_option and val:
                existing_lower = [v_val.lower() for v_val in all_values_per_option[name]]
                if val.lower() not in existing_lower:
                    all_values_per_option[name].append(val)

    result = {}
    for name in option_names_ordered:
        vals = all_values_per_option.get(name, [])
        if not vals:
            continue

        result[name] = []
        for val in vals:
            is_selected = selected_options.get(name, "").lower() == val.lower()

            matching = False
            for v in in_stock_variants:
                opts_list = v.get("selected_options") or v.get("selectedOptions") or []
                v_opts = {o.get("name", "").strip().lower(): o.get("value", "").strip() for o in opts_list}
                if v_opts.get(name.lower(), "").lower() != val.lower():
                    continue

                other_match = True
                for s_name, s_val in selected_options.items():
                    if s_name.lower() == name.lower() or not s_val:
                        continue
                    if v_opts.get(s_name.lower(), "").lower() != s_val.lower():
                        other_match = False
                        break
                if other_match:
                    matching = True
                    break

            result[name].append({
                "value": val,
                "available": matching,
                "selected": is_selected
            })

    log.log_info(f"[Variant Options] shop={shop_domain}")
    log.log_info(f"[Variant Options] product={product.get('title')}")
    log.log_info(f"[Variant Options] Shopify option names={list(result.keys())}")
    log.log_info(f"[Variant Options] available variants={len(in_stock_variants)}")
    log.log_info(f"[Variant Options] extracted selectable options={result}")
    log.log_info(f"[Variant Options] selected options={selected_options}")

    return result


def _get_product_options_summary(
    product: dict,
    selected_options: Optional[dict] = None,
    requested_option_name: Optional[str] = None
) -> str:
    """
    Builds a shopper-facing text summary of available options.
    Never returns "Available" or fallback metadata string.
    """
    if not product:
        return "This product is currently unavailable."

    variants = product.get("variants", {}).get("nodes", [])
    in_stock = [v for v in variants if _is_variant_in_stock(v)]
    if not in_stock:
        return f"The **{product.get('title', 'product')}** is currently sold out."

    opt_map = get_remaining_option_values(product, selected_options or {})
    if not opt_map:
        return f"This product does not require option selection. Would you like me to add one to your cart?"

    parts = []
    if requested_option_name:
        req_match = next((k for k in opt_map.keys() if k.lower() == requested_option_name.lower()), None)
        if req_match:
            avail_vals = [item["value"] for item in opt_map[req_match] if item["available"]]
            if avail_vals:
                if len(avail_vals) == 1:
                    vals_str = avail_vals[0]
                elif len(avail_vals) == 2:
                    vals_str = " and ".join(avail_vals)
                else:
                    vals_str = ", ".join(avail_vals[:-1]) + f" and {avail_vals[-1]}"
                return f"Available {req_match.lower()}s are {vals_str}. Which {req_match.lower()} would you like?"

    for name, items in opt_map.items():
        avail_vals = [item["value"] for item in items if item["available"]]
        if avail_vals:
            if len(avail_vals) == 1:
                vals_str = avail_vals[0]
            elif len(avail_vals) == 2:
                vals_str = " and ".join(avail_vals)
            else:
                vals_str = ", ".join(avail_vals[:-1]) + f" and {avail_vals[-1]}"
            parts.append(f"Available {name.lower()}s are {vals_str}.")

    if not parts:
        return f"This product does not require option selection. Would you like me to add one to your cart?"

    missing_names = [name for name, items in opt_map.items() if not any(item["selected"] for item in items)]
    if len(missing_names) == 1:
        prompt_str = f"Which {missing_names[0].lower()} would you like?"
    elif len(missing_names) > 1:
        missing_fmt = " and ".join(m.lower() for m in missing_names)
        prompt_str = f"Which {missing_fmt} would you like?"
    else:
        prompt_str = "Would you like me to add this to your cart?"

    return f"Found **{product.get('title')}**. " + " ".join(parts) + f" {prompt_str}"


def _available_variants_text(product: dict) -> str:
    return _get_product_options_summary(product)


def _format_product_search_summary(products: list) -> str:
    """
    Build a clear, rich, conversational response naming the matching products and prices.
    Example: "I found 3 matching products for you: **Everyday Cotton Tee** ($25.00), **Classic Slim Fit Tee** ($21.99), and **Vintage Wash Tee** ($34.00). Which size or color would you prefer?"
    """
    if not products:
        return "I couldn't find any products matching your request in the store right now."

    count = len(products)
    if count == 1:
        p = products[0]
        title = p.get("title", "product")
        opts_summary = _available_variants_text(p)
        return f"I found the **{title}**. {opts_summary}"

    top_items = products[:3]
    item_parts = []
    for p in top_items:
        title = p.get("title", "Product")
        price = p.get("price")
        if not price and isinstance(p.get("variants"), dict):
            nodes = p.get("variants", {}).get("nodes", [])
            if nodes and nodes[0].get("price"):
                price = nodes[0].get("price")
        elif not price and isinstance(p.get("variants"), list):
            if p.get("variants") and p["variants"][0].get("price"):
                price = p["variants"][0].get("price")

        price_str = ""
        if price:
            try:
                price_str = f" (${float(price):.2f})"
            except (ValueError, TypeError):
                pass
        item_parts.append(f"**{title}**{price_str}")

    if len(item_parts) == 1:
        items_str = item_parts[0]
    elif len(item_parts) == 2:
        items_str = f"{item_parts[0]} and {item_parts[1]}"
    else:
        items_str = f"{', '.join(item_parts[:-1])}, and {item_parts[-1]}"

    more_str = f" plus {count - len(top_items)} more" if count > len(top_items) else ""
    return f"I found {count} matching products for you: {items_str}{more_str}. Which size or color would you prefer?"


def _find_product_by_ref(products: list, product_ref: Optional[str]) -> Optional[dict]:
    """Match product_ref (handle, exact, highest token match, or substring) against products."""
    if not product_ref or not products:
        return None
    ref = product_ref.strip().lower()
    ref_words = [w for w in re.split(r'\W+', ref) if len(w) > 2]

    # Strategy 0: Exact handle match (stable Shopify ID — lossless round-trip)
    # The LLM is now instructed to return the handle, so this fires first.
    for p in products:
        if p.get("handle", "").lower() == ref:
            return p

    # Strategy 1: Exact title match or title equal to ref
    for p in products:
        title = p.get("title", "").lower()
        if ref == title:
            return p

    # Strategy 2: Token match scoring (rank products by matching words)
    best_product = None
    best_score = 0
    if ref_words:
        for p in products:
            title = p.get("title", "").lower()
            match_count = sum(1 for w in ref_words if w in title)
            title_words = [w for w in re.split(r'\W+', title) if len(w) > 2]
            title_match_count = sum(1 for w in title_words if w in ref)
            total_score = match_count * 2 + title_match_count
            if total_score > best_score:
                best_score = total_score
                best_product = p

    if best_product and best_score > 0:
        return best_product

    # Strategy 3: Substring match fallback
    for p in products:
        title = p.get("title", "").lower()
        if ref in title or title in ref:
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

        # Use the existing robust extractor instead of hardcoded strings.
        # Build a minimal synthetic product so _extract_options_from_text can scan it.
        synthetic_product = {
            "options": [{"name": name, "values": list(possible_vals)}],
            "variants": {"nodes": [
                {"selectedOptions": [{"name": name, "value": v}]}
                for v in possible_vals
            ]}
        }
        extracted = _extract_options_from_text(synthetic_product, ref_text)
        satisfied_val = extracted.get(name)

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
