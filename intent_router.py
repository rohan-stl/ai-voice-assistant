import re
import time
import uuid
from typing import Optional

from shopify_auth import ShopContext
from conversation_state import ConversationState
from constants import (
    WORD_TO_NUM, 
    _QTY_INCREMENT_PATTERNS, 
    _QTY_SET_PATTERNS, 
    _SAME_CART_PATTERNS
)
from product_filter import filter_products, normalize_filters, merge_filters
from cart_service import (
    save_pending_cart_selection, 
    get_session_cart_context, 
    clear_session_cart_context
)
from llm_service import ask_llm_cart, ask_llm, _coerce_llm_filters
from storefront_url import build_storefront_url
import logger as log

from variant_resolver import (
    _extract_options_from_text,
    _extract_all_unvalidated_options,
    _find_product_by_ref,
    _is_variant_in_stock,
    _get_product_options_summary,
    get_remaining_option_values,
    _has_incomplete_quantity_phrase,
    _validate_and_apply_options,
    _resolve_variant_by_options,
    _find_variant,
    _available_variants_text,
    _format_product_search_summary,
    _check_missing_options,
    _get_product_option_values
)


def extract_quantity(text: str) -> int:
    text_lower = text.lower()
    clean_text = re.sub('\\b(size|sz|waist|length|number|no|#)\\s*:?\\s*#?\\d+\\b', '', text_lower)
    m_action = re.search('\\b(?:add|get|buy|want|need|select)\\s+(\\d+)\\b', clean_text)
    if m_action:
        val = int(m_action.group(1))
        if 1 <= val <= 99:
            return val
    m = re.search('\\b(\\d+)\\b', clean_text)
    if m:
        val = int(m.group(1))
        if 1 <= val <= 99:
            return val
    for word, val in WORD_TO_NUM.items():
        if re.search('\\b' + word + '\\b', clean_text):
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
    clean = re.sub('\\b(size|sz|waist|length|number|no|#)\\s*:?\\s*#?\\d+\\b', '', text_lower)
    m = re.search('\\bchange\\s+(?:it\\s+)?from\\s+\\d+\\s+to\\s+(\\d+)\\b', clean)
    if m:
        return ('set', max(1, int(m.group(1))))
    for pat in ['\\b(?:set|make|change)\\s+(?:the\\s+)?(?:quantity|qty)\\s+(?:to\\s+)?(\\w+)\\b', '\\b(?:set|make)\\s+it\\s+(?:to\\s+)?(?:exactly\\s+)?(\\w+)\\b', '\\bmake\\s+that\\s+(\\w+)\\b', '\\bquantity\\s+to\\s+(\\w+)\\b', '\\bqty\\s+to\\s+(\\w+)\\b']:
        m = re.search(pat, clean)
        if m:
            val_str = m.group(1)
            val = WORD_TO_NUM.get(val_str) or (int(val_str) if val_str.isdigit() else None)
            if val and 1 <= val <= 99:
                return ('set', val)
    for pat in ['\\badd\\s+(\\w+)\\s+more\\b', '\\bgive\\s+me\\s+(\\w+)\\s+more\\b', '\\bincrease\\s+(?:it\\s+|the\\s+)?by\\s+(\\w+)\\b', '\\b(\\d+|one|two|three|four|five|six|seven|eight|nine|ten)\\s+more\\b', '\\badd\\s+(?:another|additional)\\s+(\\w+)\\b']:
        m = re.search(pat, clean)
        if m:
            val_str = m.group(1)
            val = WORD_TO_NUM.get(val_str) or (int(val_str) if val_str.isdigit() else None)
            if val and 1 <= val <= 99:
                return ('increment', val)
    has_more_lang = bool(re.search('\\b(more|increase|additional|another|extra|again|add)\\b', clean))
    has_set_lang = bool(re.search('\\b(set|make|change|final|total|exactly)\\b', clean))
    qty = extract_quantity(text)
    if has_more_lang:
        return ('increment', qty)
    if has_set_lang:
        return ('set', qty)
    return ('add_new', qty)

def _handle_remove_from_cart(message: str, state: ConversationState, session_id: str, shop_domain: str, admin_token: Optional[str]=None, product_ref: Optional[str]=None, variant_ref: Optional[str]=None, all_products: Optional[list]=None) -> tuple[str, Optional[str], Optional[dict], Optional[int]]:
    """
    Check if the product/variant is in the session cart.
    If present: reduce quantity or remove line item, return (answer, removed_variant_id, updated_cart, new_qty).
    If absent: return clear message with actual cart contents.
    """
    current_cart = get_session_cart_context(session_id)
    lines = current_cart.get('lines', []) if current_cart else []
    target_line = None
    ref_prod = (product_ref or '').strip().lower()
    ref_var = (variant_ref or '').strip().lower()
    msg_lower = message.lower()
    remove_qty = None
    num_map = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'single': 1}
    for w, val in num_map.items():
        if re.search('\\b' + w + '\\b', msg_lower):
            remove_qty = val
            break
    if not remove_qty:
        m = re.search('\\b(\\d+)\\b', msg_lower)
        if m:
            remove_qty = int(m.group(1))
    noise_words = {'one', '1', 'two', '2', 'three', '3', 'four', '4', 'five', '5', 'quantity', 'qty', 'amount', 'count', 'number', 'unit', 'units', 'items', 'card', 'board', 'product', 'item', 'from', 'the', 'cart', 'my', 'a', 'an'}
    raw_words = [w for w in re.split('\\W+', ref_prod) if len(w) >= 2] if ref_prod else []
    clean_words = [w for w in raw_words if w not in noise_words]
    if lines:
        best_score = 0
        best_line = None
        for line in lines:
            score = _score_line_match(msg_lower, line, noise_words)
            if score > best_score:
                best_score = score
                best_line = line
        if best_line and best_score > 0:
            target_line = best_line
        elif len(lines) == 1:
            target_line = lines[0]
    if target_line:
        var_id = str(target_line.get('variant_id', ''))
        p_title = target_line.get('product_title', 'Product')
        v_title = target_line.get('variant_title', '')
        line_key = target_line.get('line_id', target_line.get('key', ''))
        curr_qty = int(target_line.get('quantity', 1))
        new_qty = 0
        op_type = 'remove'
        if remove_qty and curr_qty > remove_qty:
            new_qty = curr_qty - remove_qty
            op_type = 'change'
        action_id = str(uuid.uuid4())
        state.set_pending_cart_action(action_id=action_id, action_type=op_type, product_handle=target_line.get('product_handle', ''), product_title=p_title, variant_id=var_id, variant_title=v_title, line_key=line_key, quantity=new_qty, quantity_mode='set')
        log.log_info(f'Generated {op_type} action {action_id} for {p_title} ({v_title})')
        var_str = f' ({v_title})' if v_title and v_title.lower() not in ('default title', 'default') else ''
        if new_qty > 0:
            answer = f'Updating **{p_title}**{var_str} quantity to {new_qty} in your cart...'
        else:
            answer = f'Removing **{p_title}**{var_str} from your cart...'
        return (answer, var_id, current_cart, new_qty)
    if not lines:
        answer = "Your cart is currently empty — there's nothing to remove."
    else:
        cart_contents = ', '.join((f"**{l.get('product_title', '?')}**" + (f" ({l.get('variant_title', '')})" if l.get('variant_title', '').lower() not in ('', 'default title', 'default') else '') for l in lines))
        product_mention = ' '.join(clean_words) if clean_words else ref_prod or 'that product'
        answer = f"I couldn't find **{product_mention}** in your cart. Your cart currently has: {cart_contents}."
    return (answer, None, current_cart, None)

def _build_precise_product_url(normalized_filters: dict, filtered_products: list) -> str:
    """
    Build a Shopify storefront URL that returns ONLY the filtered products.

    Strategy:
    - If collection_handle was resolved → use /collections/<handle>?filter.*
    - If 1-6 specific products matched → search by their exact titles joined with
      a space, which Shopify's search treats as an AND query across title tokens.
      This avoids over-broad results (e.g. 'T-Shirt' also matching 'Linen Shirt').
    - If no products (zero results) or too many (>6, generic browse) → fall back
      to the generic build_storefront_url() so the user sees category-level results.
    """
    if not filtered_products:
        return build_storefront_url(normalized_filters)
    if 1 <= len(filtered_products) <= 6:
        from urllib.parse import urlencode
        exact_titles = [p.get('title', '') for p in filtered_products if p.get('title')]
        if exact_titles:
            query = ' OR '.join((f'"{t}"' for t in exact_titles))
            params = {'q': query, 'type': 'product', 'sort_by': 'title-ascending'}
            return f'/search?{urlencode(params)}'
    return build_storefront_url(normalized_filters)

def _handle_browse(message: str, state: ConversationState, all_products: list, shop_domain: str) -> tuple[str, list, Optional[str], bool]:
    """
    Browse stage: run LLM to extract filters, filter products, return results.
    Returns (answer, shown_products, storefront_url, needs_clarification).
    """
    result = ask_llm(message, all_products)
    raw_filters = _coerce_llm_filters(result.get('filters', {}) or {})
    intent = result.get('intent', 'general_question')
    answer = result.get('answer', '')
    filtered = all_products
    storefront_url = None
    if intent == 'filter_products':
        if state.previous_filters:
            raw_filters = merge_filters(raw_filters, state.previous_filters)
        normalized = normalize_filters(raw_filters, all_products)
        filtered = filter_products(all_products, normalized)
        if len(filtered) > 1:
            best_match = _find_product_by_ref(filtered, message)
            if best_match:
                filtered = [best_match]
        storefront_url = _build_precise_product_url(normalized, filtered)
        state.filtered_products = filtered
        state.previous_filters = raw_filters
        if len(filtered) == 0:
            state.stage = 'awaiting_product_clarification'
            answer = f"{answer} I couldn't find any products matching your request. Could you clarify what you're looking for?".strip()
            return (answer, filtered, storefront_url, True)
        elif len(filtered) == 1:
            state.selected_product = filtered[0]
            state.stage = 'awaiting_variant'
            hint = ' '.join(filter(None, [raw_filters.get('color'), raw_filters.get('size')])).strip()
            if hint:
                state.selected_options_text = hint
                fast_variant = _find_variant(filtered[0], hint)
                if fast_variant and fast_variant.get('availableForSale'):
                    state.selected_variant = fast_variant
                    state.quantity = 1
                    state.stage = 'awaiting_confirm'
                    price = float(fast_variant.get('price', 0))
                    answer = f"Got it! Just to confirm: **{filtered[0]['title']}** — {fast_variant.get('title')} at ${price:.2f}. Shall I add this to your cart?"
                else:
                    available = _available_variants_text(filtered[0])
                    answer = f"Found **{filtered[0]['title']}**. Options: {available}. Which would you like?"
            else:
                available = _available_variants_text(filtered[0])
                answer = f"Found **{filtered[0]['title']}**. Options: {available}. Which would you like?"
        else:
            state.stage = 'awaiting_variant'
            answer = _format_product_search_summary(filtered)
    else:
        state.filtered_products = all_products
    return (answer, filtered, storefront_url, False)

def _handle_awaiting_variant(message: str, state: ConversationState, all_products: list, shop_domain: str, session_id: str, admin_token: Optional[str]=None) -> tuple[str, list, Optional[str], bool, Optional[dict], bool]:
    """
    The user has seen product results and is now specifying a variant or saying add to cart.
    Returns (answer, shown_products, storefront_url, needs_clarification, cart, auto_checkout).
    """
    result = ask_llm_cart(message, state.filtered_products or all_products, state.selected_product)
    intent = result.get('intent', 'general_question')
    product_ref = result.get('product_ref')
    variant_ref = result.get('variant_ref')
    quantity = result.get('quantity', 1)
    extracted_qty = extract_quantity(message)
    if extracted_qty > quantity:
        quantity = extracted_qty
    answer = result.get('answer', '')
    go_to_checkout = result.get('go_to_checkout', False) or intent == 'checkout'
    msg_lower = message.lower()
    is_search_query = any((w in msg_lower for w in ('list', 'catalog', 'show', 'find', 'search', 'browse', 'products'))) or intent == 'filter_products'
    if is_search_query:
        state.reset_selection()
        browse_answer, filtered, storefront_url, _ = _handle_browse(message, state, all_products, shop_domain)
        return (browse_answer, filtered, storefront_url, False, None, False)
    if intent == 'confirm_no':
        state.reset_selection()
        return ("No problem! Let me know what you'd like to look for.", all_products, None, False, None, False)
    search_pool = state.filtered_products or all_products
    product = state.selected_product
    if product_ref:
        referenced = _find_product_by_ref(search_pool, product_ref)
        if referenced and (not product or referenced.get('id') != product.get('id')):
            product = referenced
            state.selected_product = product
            state.selected_variant = None
            state.selected_options = {}
            state.selected_options_text = ''
    if not product and product_ref:
        product = _find_product_by_ref(search_pool, product_ref)
    if not product:
        action_stop_words = {'add', 'added', 'adds', 'put', 'place', 'buy', 'want', 'get', 'to', 'in', 'into', 'my', 'the', 'a', 'an', 'this', 'that', 'please', 'item', 'product', 'cart', 'basket', 'bag'}
        msg_words = [w for w in re.split('\\W+', message.lower()) if len(w) > 2 and w not in action_stop_words]
        if msg_words:
            best_score = 0
            for prod in search_pool:
                title_lower = prod.get('title', '').lower()
                score = sum((1 for w in msg_words if w in title_lower))
                if score > best_score:
                    best_score = score
                    product = prod
    if not product:
        if len(search_pool) > 1:
            titles = ', '.join((p['title'] for p in search_pool[:5]))
            return (f'Which product did you mean? I can see: {titles}.', search_pool, None, True, None, False)
        elif len(search_pool) == 1:
            product = search_pool[0]
        else:
            return ("I'm not sure which product you're referring to. Could you tell me the name?", search_pool, None, True, None, False)
    state.selected_product = product
    new_opts = _extract_options_from_text(product, message)
    if new_opts:
        state.selected_options.update(new_opts)
    variants = product.get('variants', {}).get('nodes', [])
    all_option_names = []
    for v in variants:
        for opt in v.get('selectedOptions', []):
            name = opt.get('name', '').strip()
            if name and name not in all_option_names:
                all_option_names.append(name)
    missing_options = [name for name in all_option_names if not state.selected_options.get(name)]
    if missing_options:
        state.stage = 'awaiting_variant'
        next_missing = missing_options[0]
        opts_fmt = ', '.join((f'{v}' for k, v in state.selected_options.items())) if state.selected_options else 'Option'
        available_vals = set()
        for v in variants:
            if _is_variant_in_stock(v):
                for o in v.get('selectedOptions', []):
                    if o.get('name', '').strip().lower() == next_missing.lower():
                        available_vals.add(o.get('value', '').strip())
        vals_str = ', '.join(sorted(available_vals))
        answer = f"{opts_fmt} selected. Which {next_missing.lower()} would you like for **{product['title']}**? Available: {vals_str}."
        return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    variant = _resolve_variant_by_options(product, state.selected_options) or _find_variant(product, message)
    if not variant or not _is_variant_in_stock(variant):
        state.stage = 'awaiting_variant'
        opts_fmt = ', '.join((f'{v}' for k, v in state.selected_options.items()))
        available_summary = _get_product_options_summary(product)
        answer = f"Sorry, **{product['title']}** in {opts_fmt} is currently out of stock. Available options: {available_summary}."
        return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    state.selected_variant = variant
    state.quantity = quantity
    has_explicit_add = any((w in msg_lower for w in ('add', 'buy', 'cart', 'checkout', 'check out', 'basket', 'bag', 'put in', 'place in')))
    if has_explicit_add:
        checkout_words = {'checkout', 'check out', 'go to checkout', 'proceed to checkout', 'take me to checkout', 'open checkout', 'pay now', 'cart checkout'}
        wants_checkout = any(w in msg_lower for w in checkout_words)
        action_id = str(uuid.uuid4())
        v_id = variant.get('id')
        v_title = variant.get('title', '')
        p_handle = product.get('handle', '')
        p_title = product.get('title', '')
        state.set_pending_cart_action(action_id=action_id, action_type='add', product_handle=p_handle, product_title=p_title, variant_id=v_id, variant_title=v_title, selected_options=state.selected_options or {}, quantity=quantity, quantity_mode='increment')
        cart = save_pending_cart_selection(session_id, product, variant, quantity)
        answer = f'Adding **{p_title}** to your cart...'
        if wants_checkout:
            answer += ' Taking you to checkout.'
        return (answer, state.filtered_products or all_products, None, False, cart, wants_checkout, None, False, None, v_id, quantity)
    state.stage = 'awaiting_confirm'
    opts_speech = ', '.join((f'{k.lower()} {v}' for k, v in state.selected_options.items()))
    answer = f"You selected the **{product['title']}** in {opts_speech}. Would you like me to add it to your cart?"
    return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)

def _handle_awaiting_confirm(message: str, state: ConversationState, all_products: list, shop_domain: str, session_id: str, admin_token: Optional[str]=None) -> tuple[str, list, Optional[str], bool, Optional[dict]]:
    """
    User should be saying yes/no to the pending add-to-cart.
    Cart is created/updated via Admin API Draft Orders — no Storefront token needed.
    Returns (answer, shown_products, storefront_url, needs_clarification, cart).
    """
    result = ask_llm_cart(message, state.filtered_products or all_products, state.selected_product)
    intent = result.get('intent', 'general_question')
    msg_lower = message.lower()
    is_search_intent = any((w in msg_lower for w in ('list', 'catalog', 'show', 'find', 'search', 'browse', 'products'))) or intent == 'filter_products'
    has_confirm_word = any((w in msg_lower for w in ('yes', 'yeah', 'yep', 'sure', 'ok', 'okay', 'add', 'buy', 'confirm', 'checkout', 'do it')))
    # Use the actual product's option catalog — avoids hardcoded color/size assumptions.
    # If the message contains valid option values for the current product, treat it
    # as an option selection rather than a yes/no confirmation.
    if state.selected_product:
        has_option_word = bool(_extract_options_from_text(state.selected_product, msg_lower))
    else:
        has_option_word = bool(_extract_all_unvalidated_options(msg_lower, all_products))
    if has_option_word and (not has_confirm_word):
        return _handle_awaiting_variant(message, state, all_products, shop_domain, session_id, admin_token)
    if is_search_intent and (not has_confirm_word):
        state.reset_selection()
        browse_answer, filtered, storefront_url, _ = _handle_browse(message, state, all_products, shop_domain)
        return (browse_answer, filtered, storefront_url, False, None, False)
    go_to_checkout = result.get('go_to_checkout', False) or intent in ('checkout',)
    has_affirmative = bool(re.search('\\b(yes|yeah|yep|yup|sure|ok|okay|add|buy|confirm|checkout|do it|definitely|absolutely)\\b', msg_lower)) and (not bool(re.search("\\b(no|not|don't|dont|cancel|stop|never|change|different)\\b", msg_lower)))
    if intent in ('confirm_yes', 'add_to_cart', 'checkout') or has_affirmative:
        product = state.selected_product
        variant = state.selected_variant
        if not product or not variant:
            state.stage = 'browse'
            return ('Something went wrong with my memory of your selection. Could you start over?', all_products, None, False, None, False)
        if not _is_variant_in_stock(variant):
            available = _available_variants_text(product)
            state.stage = 'awaiting_variant'
            return (f"Sorry, **{variant.get('title')}** is currently out of stock for **{product['title']}**. Available options: {available}.", state.filtered_products or all_products, None, False, None, False)
        try:
            quantity = state.quantity
            extracted_qty = extract_quantity(message)
            if extracted_qty > 1:
                quantity = extracted_qty
            elif result.get('quantity', 1) > 1:
                quantity = result.get('quantity')
            action_id = str(uuid.uuid4())
            v_id = str(variant.get('id', ''))
            v_title = variant.get('title', '')
            p_handle = product.get('handle', '')
            p_title = product.get('title', '')
            state.set_pending_cart_action(action_id=action_id, action_type='add', product_handle=p_handle, product_title=p_title, variant_id=v_id, variant_title=v_title, selected_options=state.selected_options or {}, quantity=quantity, quantity_mode='increment')
            cart = save_pending_cart_selection(session_id, product, variant, quantity)
            answer = f'Adding **{p_title}** to your cart...' + (' Taking you to checkout now.' if go_to_checkout else '')
            state.stage = 'awaiting_client_cart_action'
            state.selected_product = None
            state.selected_variant = None
            state.quantity = 1
            return (answer, state.filtered_products, None, False, cart, go_to_checkout, variant.get('id'), quantity)
        except Exception as exc:
            log.log_error('Cart add failed', exc)
            return ('Sorry, I had trouble adding that to your cart. Please try again.', state.filtered_products, None, False, None, False)
    if intent == 'confirm_no':
        state.stage = 'awaiting_variant'
        return ('No problem! Would you like to choose a different size or colour?', state.filtered_products, None, False, None, False)
    if intent == 'filter_products':
        state.reset_selection()
        browse_answer, filtered, storefront_url, _ = _handle_browse(message, state, all_products, shop_domain)
        return (browse_answer, filtered, storefront_url, False, None, False)
    product = state.selected_product
    variant = state.selected_variant
    answer = f"Shall I add **{product['title']} ({variant.get('title')})** to your cart?"
    return (answer, state.filtered_products, None, False, None, False)

def _parse_variant_title_to_options(variant_title: str) -> dict:
    """
    Best-effort parse "XL / Blue" → {"Size": "XL", "Color": "Blue"}.
    Used to pre-fill selected_options from an existing cart line.
    """
    if not variant_title or variant_title.lower() in ('default title', 'default', ''):
        return {}
    parts = [p.strip() for p in re.split('\\s*/\\s*|\\s+/\\s+', variant_title)]
    SIZE_CANONICAL = {'XS', 'S', 'M', 'L', 'XL', 'XXL', '2XL', 'Small', 'Medium', 'Large', 'Extra Large', 'Extra Small'}
    opts: dict = {}
    for part in parts:
        if part.upper() in {v.upper() for v in SIZE_CANONICAL}:
            opts.setdefault('Size', part)
        else:
            opts.setdefault('Color', part)
    return opts

def _score_line_match(text: str, line: dict, noise: set, active_options: Optional[dict]=None) -> int:
    """Score how well a user's text matches a cart line product and variant title."""
    p_title = line.get('product_title', '').lower()
    v_title = line.get('variant_title', '').lower()
    words = [w for w in re.split('\\W+', text.lower()) if len(w) > 2 and w not in noise]
    for size in (' s ', ' m ', ' l ', ' xl ', ' xs ', ' xxl '):
        if size in f' {text.lower()} ':
            words.append(size.strip())
    score = 0
    for w in words:
        if w in p_title or w in v_title:
            score += 1
            if w in v_title:
                score += 1
    if active_options:
        l_opts = _parse_variant_title_to_options(line.get('variant_title', ''))
        if all((l_opts.get(k, '').lower() == v.lower() for k, v in active_options.items() if v)):
            score += 10
        elif any((l_opts.get(k, '').lower() == v.lower() for k, v in active_options.items() if v)):
            score += 3
    return score

def _classify_cart_quantity_intent(message: str, state: ConversationState, all_products: list, session_id: str) -> Optional[dict]:
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
    current_cart = get_session_cart_context(session_id)
    cart_lines = current_cart.get('lines', []) if current_cart else []
    if not cart_lines or state.stage == 'awaiting_confirm':
        return None
    is_increment = any((re.search(p, msg_lower) for p in _QTY_INCREMENT_PATTERNS))
    is_set = any((re.search(p, msg_lower) for p in _QTY_SET_PATTERNS))
    has_qty_phrase = _has_incomplete_quantity_phrase(message)
    if not is_increment and (not is_set) and (not has_qty_phrase):
        return None
    mode, delta = _extract_quantity_delta(message)
    if mode == 'add_new':
        mode = 'increment' if is_increment else 'set'
    quantity_missing = has_qty_phrase and delta == 1 and (not re.search('\\b(one|1)\\b', msg_lower))
    is_same_ref = any((re.search(p, msg_lower) for p in _SAME_CART_PATTERNS))
    noise = {'add', 'more', 'increase', 'set', 'make', 'the', 'my', 'same', 'quantity', 'qty', 'to', 'by', 'of', 'a', 'an', 'in', 'cart', 'order', 'please', 'i', 'want', 'need', 'give', 'me', 'like', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'just', 'also', 'and', 'or', 'from', 'change', 'update', 'another', 'additional', 'extra', 'again', 'into', 'for', 'is', 'should', 'be'}
    best_line = None
    best_score = 0
    active_opts = state.selected_options or {}
    for line in cart_lines:
        score = _score_line_match(msg_lower, line, noise, active_options=active_opts)
        if score > best_score:
            best_score = score
            best_line = line
    if not best_line and len(cart_lines) == 1:
        best_line = cart_lines[0]
        best_score = 1
    if not best_line or best_score == 0:
        return None
    matched_product = _find_product_by_ref(all_products, best_line.get('product_title', ''))
    explicit_options = {}
    if matched_product:
        explicit_options = _extract_options_from_text(matched_product, message)
    matched_cart_line = None
    if explicit_options and (not is_same_ref):
        for line in cart_lines:
            line_opts = _parse_variant_title_to_options(line.get('variant_title', ''))
            if all((line_opts.get(k, '').lower() == v.lower() for k, v in explicit_options.items())):
                matched_cart_line = line
                break
    op_version = 1
    if explicit_options and (not is_same_ref) and (not matched_cart_line):
        resolved_var = _resolve_variant_by_options(matched_product, explicit_options) if matched_product else None
        var_id = resolved_var.get('id', '') if resolved_var else ''
        var_title = resolved_var.get('title', '') if resolved_var else '/'.join(explicit_options.values())
        curr_qty = 0
        final_qty = delta if mode == 'set' or quantity_missing else delta
        intent = 'cart_add_new_variant'
        missing = []
        if matched_product:
            variants = matched_product.get('variants', {}).get('nodes', [])
            all_opt_names = []
            for v in variants:
                for oe in v.get('selectedOptions', []):
                    n = oe.get('name', '').strip()
                    if n and n not in all_opt_names:
                        all_opt_names.append(n)
            missing = [n for n in all_opt_names if not explicit_options.get(n)]
        status = 'missing_quantity' if quantity_missing else 'ready' if not missing else 'collecting_options'
        return {'intent': intent, 'operations': [{'operation_id': 'op-1', 'operation_version': op_version, 'product_title': matched_product.get('title', best_line.get('product_title', '')) if matched_product else best_line.get('product_title', ''), 'source_line_id': None, 'variant_id': var_id, 'variant_title': var_title, 'selected_options': explicit_options, 'missing_options': missing, 'quantity_mode': mode, 'quantity_delta': delta if not quantity_missing else 1, 'quantity_missing': quantity_missing, 'current_quantity': curr_qty, 'final_quantity': final_qty, 'reuse_cart_variant': False, 'status': status}], 'reuse_cart_variant': False, 'mode': mode, 'delta': delta}
    target_line = matched_cart_line or best_line
    curr_qty = target_line.get('quantity', 1)
    final_qty = curr_qty + delta if mode in ('increment', 'add_new') else delta
    intent = 'cart_set_line_quantity' if mode == 'set' else 'cart_increment_existing_line'
    cart_line_opts = _parse_variant_title_to_options(target_line.get('variant_title', ''))
    merged_opts = {**cart_line_opts, **explicit_options}
    return {'intent': intent, 'operations': [{'operation_id': 'op-1', 'operation_version': op_version, 'product_title': target_line.get('product_title', ''), 'source_line_id': target_line.get('line_id', target_line.get('key', '')), 'variant_id': target_line.get('variant_id', ''), 'variant_title': target_line.get('variant_title', ''), 'selected_options': merged_opts, 'missing_options': [], 'quantity_mode': mode, 'quantity_delta': delta if not quantity_missing else 1, 'quantity_missing': quantity_missing, 'current_quantity': curr_qty, 'final_quantity': final_qty, 'reuse_cart_variant': is_same_ref or not explicit_options, 'status': 'missing_quantity' if quantity_missing else 'ready'}], 'reuse_cart_variant': is_same_ref or not explicit_options, 'mode': mode, 'delta': delta}

def _handle_cart_quantity_intent(intent_result: dict, state: ConversationState, all_products: list, session_id: str, shop_domain: str, admin_token: Optional[str]=None) -> tuple:
    """
    Handle a classified cart quantity intent.
    For 'same as cart' (reuse_cart_variant=True) or known cart-line ops:
      show a confirmation message → stage = awaiting_cart_confirm.
    For generic product references (reuse_cart_variant=False, missing options):
      ask for missing options → stage = awaiting_variant.
    """
    operations = intent_result.get('operations', [])
    if not operations:
        return None
    intent = intent_result.get('intent', 'cart_increment_existing_line')
    state.pending_cart_intent = intent
    state.pending_operations = [dict(o) for o in operations]
    EMPTY = ('', 'default title', 'default')

    def _variant_display(o: dict) -> str:
        vt = o.get('variant_title', '')
        return f' {vt}' if vt.lower() not in EMPTY else ''
    op = next((o for o in state.pending_operations if o.get('operation_id') == (state.active_operation_id or 'op-1')), state.pending_operations[0])
    if op.get('quantity_missing'):
        state.stage = 'awaiting_cart_quantity'
        state.active_operation_id = op['operation_id']
        product = _find_product_by_ref(all_products, op.get('product_title')) or state.selected_product
        if product:
            state.selected_product = product
        opts_str = ', '.join((f'{v}' for k, v in op.get('selected_options', {}).items()))
        p_title = product.get('title', op.get('product_title', '')) if product else op.get('product_title', '')
        if opts_str:
            answer = f'You selected the **{p_title}** in {opts_str}. What quantity would you like?'
        else:
            answer = f'For **{p_title}**, what quantity would you like?'
        return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    if op.get('reuse_cart_variant') or op.get('source_line_id'):
        state.active_operation_id = None
        state.stage = 'awaiting_cart_confirm'
        vd = _variant_display(op)
        if op['quantity_mode'] in ('increment', 'add_new'):
            if op.get('current_quantity', 0) > 0:
                op['quantity_mode'] = 'increment'
            answer = f"Your{vd} **{op['product_title']}** currently has quantity {op['current_quantity']}. I'll increase it by {op['quantity_delta']}, making the total {op['final_quantity']}. Shall I continue?"
        else:
            answer = f"I'll set your{vd} **{op['product_title']}** quantity to {op['final_quantity']}. Shall I continue?"
        return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    product = _find_product_by_ref(all_products, op.get('product_title'))
    if not product:
        return None
    state.selected_product = product
    state.selected_options = dict(op.get('selected_options', {}))
    state.stage = 'awaiting_variant'
    state.active_operation_id = op['operation_id']
    variants = product.get('variants', {}).get('nodes', [])
    all_option_names = []
    for v in variants:
        for oe in v.get('selectedOptions', []):
            n = oe.get('name', '').strip()
            if n and n not in all_option_names:
                all_option_names.append(n)
    missing = [n for n in all_option_names if not state.selected_options.get(n)]
    op['missing_options'] = missing
    if not missing:
        variant = _resolve_variant_by_options(product, state.selected_options)
        if variant:
            op['variant_id'] = variant.get('id', '')
            op['variant_title'] = variant.get('title', '')
            op['status'] = 'ready'
        state.stage = 'awaiting_cart_confirm'
        state.active_operation_id = None
        opts_str = ', '.join((f'{v}' for k, v in state.selected_options.items()))
        p_title = product.get('title', '')
        if op.get('current_quantity', 0) == 0:
            answer = f"You selected the {p_title} in {opts_str}. I'll add one to your cart. Shall I continue?"
        else:
            qty_word = 'add' if op['quantity_mode'] in ('increment', 'add_new') else 'set'
            answer = f"Got it — I'll {qty_word} {op['quantity_delta']} to your **{p_title}** ({opts_str}). Shall I continue?"
        return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    next_missing = missing[0]
    available_vals: set = set()
    all_colors: set = set()
    all_sizes: set = set()
    for v in variants:
        if _is_variant_in_stock(v):
            for oe in v.get('selectedOptions', []):
                n = oe.get('name', '').strip()
                val = oe.get('value', '').strip()
                if 'color' in n.lower() or 'colour' in n.lower():
                    all_colors.add(val)
                elif 'size' in n.lower():
                    all_sizes.add(val)
                if n.lower() == next_missing.lower():
                    available_vals.add(val)
    options_lines = []
    if all_colors:
        options_lines.append(f"Colors: {' or '.join(sorted(all_colors))}.")
    if all_sizes:
        options_lines.append(f"Sizes: {', '.join(sorted(all_sizes))}.")
    qty_desc = f"{op['quantity_delta']} more" if op['quantity_mode'] in ('increment', 'add_new') else f"quantity to {op['quantity_delta']}"
    answer = f"Which option would you like to {qty_desc} for **{product['title']}**?\n" + '\n'.join(options_lines)
    return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)

def _collect_operation_options(message: str, state: ConversationState, all_products: list, session_id: str, shop_domain: str, admin_token: Optional[str]=None) -> Optional[tuple]:
    """
    Handle a follow-up message when state.pending_operations is active and an
    operation is still collecting missing options.
    """
    if not state.pending_operations or not state.active_operation_id:
        return None
    active_op: Optional[dict] = None
    active_idx: Optional[int] = None
    for idx, op in enumerate(state.pending_operations):
        if op.get('operation_id') == state.active_operation_id:
            active_op = op
            active_idx = idx
            break
    if active_op is None or active_idx is None:
        return None
    msg_lower = message.lower()
    product = state.selected_product
    if product:
        new_opts = _extract_options_from_text(product, msg_lower)
        if new_opts:
            active_op['selected_options'] = {**active_op.get('selected_options', {}), **new_opts}
            active_op['operation_version'] = active_op.get('operation_version', 1) + 1
    q_mode, q_delta = _extract_quantity_delta(message)
    if q_mode in ('increment', 'set') and q_delta >= 1:
        active_op['quantity_mode'] = q_mode
        active_op['quantity_delta'] = q_delta
        active_op['quantity_missing'] = False
        active_op['operation_version'] = active_op.get('operation_version', 1) + 1
    elif q_mode == 'add_new' and q_delta > 1:
        active_op['quantity_delta'] = q_delta
        active_op['quantity_missing'] = False
        active_op['operation_version'] = active_op.get('operation_version', 1) + 1
    if active_op['quantity_mode'] in ('increment', 'add_new'):
        active_op['final_quantity'] = active_op.get('current_quantity', 0) + active_op['quantity_delta']
        if active_op.get('current_quantity', 0) > 0:
            active_op['quantity_mode'] = 'increment'
    else:
        active_op['final_quantity'] = active_op['quantity_delta']
    state.pending_operations[active_idx] = active_op
    variants = product.get('variants', {}).get('nodes', []) if product else []
    all_option_names: list = []
    for v in variants:
        for oe in v.get('selectedOptions', []):
            n = oe.get('name', '').strip()
            if n and n not in all_option_names:
                all_option_names.append(n)
    still_missing = [n for n in all_option_names if not active_op.get('selected_options', {}).get(n)]
    active_op['missing_options'] = still_missing
    state.pending_operations[active_idx] = active_op
    EMPTY = ('', 'default title', 'default')
    if still_missing:
        next_missing = still_missing[0]
        available_vals: set = set()
        for v in variants:
            if _is_variant_in_stock(v):
                for oe in v.get('selectedOptions', []):
                    if oe.get('name', '').strip().lower() == next_missing.lower():
                        available_vals.add(oe.get('value', '').strip())
        opts_so_far = ', '.join((v for v in active_op.get('selected_options', {}).values()))
        prefix = f'{opts_so_far} selected. ' if opts_so_far else ''
        answer = f"{prefix}Which {next_missing.lower()} would you like for **{active_op['product_title']}**? Available: {', '.join(sorted(available_vals))}."
        return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    if product:
        variant = _resolve_variant_by_options(product, active_op.get('selected_options', {}))
        if variant:
            active_op['variant_id'] = variant.get('id', '')
            active_op['variant_title'] = variant.get('title', '')
            active_op['status'] = 'ready'
            state.pending_operations[active_idx] = active_op
    state.stage = 'awaiting_cart_confirm'
    state.active_operation_id = None
    ops = state.pending_operations
    if len(ops) == 1:
        op = ops[0]
        opts_str = ', '.join((f'{v}' for k, v in op.get('selected_options', {}).items()))
        vt = op.get('variant_title', '')
        vd = vt if vt.lower() not in EMPTY else opts_str
        p_title = op.get('product_title', '')
        if op.get('current_quantity', 0) == 0:
            answer = f"You selected the **{p_title}** in {vd}. I'll add {op['quantity_delta']} to your cart. Shall I continue?"
        elif op['quantity_mode'] in ('increment', 'add_new'):
            answer = f"Got it — I'll add {op['quantity_delta']} more **{p_title}** ({vd}), bringing the total to {op['final_quantity']}. Shall I continue?"
        else:
            answer = f"Got it — I'll set **{p_title}** ({vd}) quantity to {op['final_quantity']}. Shall I continue?"
    else:
        lines_desc = []
        for op in ops:
            opts_str = ', '.join((f'{k} {v}' for k, v in op.get('selected_options', {}).items()))
            vt = op.get('variant_title', '')
            vd = vt if vt.lower() not in EMPTY else opts_str
            if op['quantity_mode'] in ('increment', 'add_new'):
                lines_desc.append(f"• Add {op['quantity_delta']} more **{op['product_title']}** ({vd}), from {op['current_quantity']} to {op['final_quantity']}")
            else:
                lines_desc.append(f"• Set **{op['product_title']}** ({vd}) quantity to {op['final_quantity']}")
        answer = "I'll make these cart changes:\n" + '\n'.join(lines_desc) + '\nShall I continue?'
    return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)

def _handle_awaiting_cart_confirm(message: str, state: ConversationState, all_products: list, session_id: str, shop_domain: str, admin_token: Optional[str]=None) -> Optional[tuple]:
    """
    The user is confirming (or cancelling or correcting) a pending cart quantity update.
    """
    if not state.pending_operations:
        state.stage = 'browse'
        return None
    msg_lower = message.lower()
    has_yes = bool(re.search('\\b(yes|yeah|yep|yup|sure|ok|okay|go|do\\s+it|continue|confirm|proceed|please|absolutely|correct|right|definitely)\\b', msg_lower))
    has_no = bool(re.search('\\b(no|nope|nah|cancel|stop|never\\s+mind|nevermind|don.?t|forget\\s+it|leave\\s+it)\\b', msg_lower))
    has_correction = any((rt in msg_lower for rt in ('instead', 'make it', 'change', 'give me', 'give', 'switch', 'prefer', 'rather', 'actually', 'red', 'black', 'blue', 'navy', 'white', 'green', 'yellow', 'pink', 'grey', 'gray', 'brown', 'purple', 'orange', 'small', 'medium', 'large', 'extra large', 'xl', 'l', 'm', 's', 'xs', 'xxl', 'size', 'color', 'colour', 'jeans', 'shirt', 't-shirt', 'tshirt', 'shoes', 'pants', 'dress', 'two', 'three', 'four', 'five', 'quantity', 'qty')))
    if has_no and (not has_yes) and (not has_correction):
        state.pending_cart_intent = None
        state.pending_operations = []
        state.active_operation_id = None
        state.stage = 'browse'
        return ('Okay, cancelled.', state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    q_mode, q_delta = _extract_quantity_delta(message)
    qty_override = q_mode in ('increment', 'set') and q_delta > 0
    if qty_override:
        for op in state.pending_operations:
            op['quantity_mode'] = q_mode
            op['quantity_delta'] = q_delta
            op['operation_version'] = op.get('operation_version', 1) + 1
            if q_mode in ('increment', 'add_new'):
                op['final_quantity'] = op.get('current_quantity', 0) + q_delta
                if op.get('current_quantity', 0) > 0:
                    op['quantity_mode'] = 'increment'
            else:
                op['final_quantity'] = q_delta
    if not has_yes:
        op = state.pending_operations[0] if state.pending_operations else None
        if op:
            product = _find_product_by_ref(all_products, op.get('product_title', '')) or state.selected_product
            if product:
                valid_opts, invalid_opts, missing_opts = _validate_and_apply_options(product, message, state, all_products)
                if invalid_opts:
                    inv_key, inv_val = list(invalid_opts.items())[0]
                    opt_map = _get_product_option_values(product)
                    allowed = opt_map.get(inv_key, [])
                    allowed_str = ' and '.join(allowed) if len(allowed) <= 2 else ', '.join(allowed[:-1]) + f', and {allowed[-1]}'
                    p_title = product.get('title', 'this item')
                    answer = f'{inv_val} is not available for the **{p_title}**. Available {inv_key.lower()}s are {allowed_str}. Which {inv_key.lower()} would you like?'
                    state.stage = f'awaiting_{inv_key.lower()}'
                    return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
                corr_opts = valid_opts or _extract_options_from_text(product, message)
                if corr_opts:
                    merged_opts = {**op.get('selected_options', {}), **corr_opts}
                    op['selected_options'] = merged_opts
                    op['source_line_id'] = None
                    op['variant_id'] = None
                    op['operation_version'] = op.get('operation_version', 1) + 1
                    new_var = _resolve_variant_by_options(product, merged_opts)
                    if new_var:
                        op['variant_id'] = new_var.get('id', '')
                        op['variant_title'] = new_var.get('title', '')
                        op['status'] = 'ready'
                    current_cart = get_session_cart_context(session_id)
                    cart_lines = current_cart.get('lines', []) if current_cart else []
                    matched_line = None
                    for line in cart_lines:
                        l_opts = _parse_variant_title_to_options(line.get('variant_title', ''))
                        if all((l_opts.get(k, '').lower() == v.lower() for k, v in merged_opts.items())):
                            matched_line = line
                            break
                    if matched_line:
                        op['source_line_id'] = matched_line.get('line_id', matched_line.get('key', ''))
                        op['current_quantity'] = matched_line.get('quantity', 1)
                        if op['quantity_mode'] in ('increment', 'add_new'):
                            op['final_quantity'] = op['current_quantity'] + op['quantity_delta']
                            if op['current_quantity'] > 0:
                                op['quantity_mode'] = 'increment'
                        else:
                            op['final_quantity'] = op['quantity_delta']
                    else:
                        op['source_line_id'] = None
                        op['current_quantity'] = 0
                        op['final_quantity'] = op['quantity_delta']
                    state.pending_operations[0] = op
                    state.stage = 'awaiting_cart_confirm'
                    opts_str = ', '.join((f'{v}' for k, v in merged_opts.items()))
                    p_title = product.get('title', op.get('product_title', ''))
                    vt = op.get('variant_title') or opts_str
                    if op['current_quantity'] == 0:
                        answer = f"You selected the {p_title} in {opts_str}. I'll add {op['quantity_delta']} to your cart. Shall I continue?"
                    else:
                        answer = f"Your {vt} **{p_title}** currently has quantity {op['current_quantity']}. I'll increase it by {op['quantity_delta']}, making the total {op['final_quantity']}. Shall I continue?"
                    return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
        if qty_override:
            ops = state.pending_operations
            EMPTY = ('', 'default title', 'default')
            if len(ops) == 1:
                op = ops[0]
                vt = op.get('variant_title', '')
                vd = f' {vt}' if vt.lower() not in EMPTY else ''
                if op['quantity_mode'] in ('increment', 'add_new'):
                    answer = f"Updated: I'll increase your{vd} **{op['product_title']}** by {op['quantity_delta']}, making the total {op['final_quantity']}. Shall I continue?"
                else:
                    answer = f"Updated: I'll set your{vd} **{op['product_title']}** quantity to {op['final_quantity']}. Shall I continue?"
            else:
                lines_desc = []
                for op in ops:
                    vt = op.get('variant_title', '')
                    vd = f' {vt}' if vt.lower() not in EMPTY else ''
                    lines_desc.append(f"• {op['product_title']}{vd}: quantity → {op['final_quantity']}")
                answer = 'Updated plan:\n' + '\n'.join(lines_desc) + '\nShall I continue?'
            return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
        return None
    checkout_words = {'checkout', 'check out', 'go to checkout', 'proceed to checkout', 'take me to checkout', 'open checkout', 'pay now', 'cart checkout'}
    wants_checkout = any(w in message.lower() for w in checkout_words)
    action_id = str(uuid.uuid4())
    if state.selected_product and state.selected_variant:
        product = state.selected_product
        variant = state.selected_variant
        quantity = state.quantity or 1
        v_id = str(variant.get('id', ''))
        v_title = variant.get('title', '')
        p_handle = product.get('handle', '')
        p_title = product.get('title', '')
        state.set_pending_cart_action(action_id=action_id, action_type='add', product_handle=p_handle, product_title=p_title, variant_id=v_id, variant_title=v_title, selected_options=state.selected_options or {}, quantity=quantity, quantity_mode='add_new')
        cart = save_pending_cart_selection(session_id, product, variant, quantity)
        answer = f'Adding **{p_title}** to your cart...'
        if wants_checkout:
            answer += ' Taking you to checkout.'
        return (answer, state.filtered_products or all_products, None, False, cart, wants_checkout, None, False, None, v_id, quantity)
    if state.pending_operations:
        op = state.pending_operations[0]
        op_type = 'add' if not op.get('source_line_id') else 'remove' if op.get('final_quantity', 1) == 0 else 'change'
        v_id = str(op.get('variant_id', ''))
        p_title = op.get('product_title', 'item')
        is_add_op = op_type == 'add'
        action_qty = op.get('quantity_delta', 1) if is_add_op else op.get('final_quantity', op.get('quantity_delta', 1))
        action_mode = 'increment' if is_add_op else 'set'
        state.set_pending_cart_action(action_id=action_id, action_type=op_type, product_handle=op.get('product_handle', ''), product_title=p_title, variant_id=v_id, variant_title=op.get('variant_title', ''), selected_options=op.get('selected_options') or {}, quantity=action_qty, quantity_mode=action_mode, line_key=op.get('source_line_id'))
        state.pending_operations = []
        state.stage = 'awaiting_client_cart_action'
        cart = get_session_cart_context(session_id)
        removed_var = v_id if op_type == 'remove' else None
        updated_var = v_id if op_type == 'change' else None
        updated_qty = op.get('final_quantity') if op_type == 'change' else None
        added_var = v_id if op_type == 'add' else None
        added_qty = action_qty if op_type == 'add' else None
        answer = f"{('Removing' if op_type == 'remove' else 'Updating' if op_type == 'change' else 'Adding')} **{p_title}** {('from' if op_type == 'remove' else 'in' if op_type == 'change' else 'to')} your cart..."
        if wants_checkout:
            answer += ' Taking you to checkout.'
        return (answer, state.filtered_products or all_products, None, False, cart, wants_checkout, removed_var, False, updated_qty, added_var, added_qty, None, False, False, updated_var)
    final_cart = get_session_cart_context(session_id)
    return ('No changes were made to your cart.', state.filtered_products or all_products, None, False, final_cart, False, None, False, None, None, None, None, False, False, None)

def route_message(message: str, state: ConversationState, all_products: list, session_id: str, shop_domain: str, admin_token: Optional[str]=None, shop_context: Optional[ShopContext]=None) -> Optional[tuple]:
    """
    Ultra-fast deterministic parser for common voice commands (add, remove, search, clear).
    Bypasses LLM network calls when intent & entity can be extracted directly.
    """
    msg_lower = message.lower().strip()
    msg_stripped = msg_lower.strip('.,!? ')
    if msg_stripped in {'hi', 'hello', 'hey', 'help', 'who are you', 'what can you do', 'start'}:
        answer = "Hello! I'm your AI Shopping Assistant. I can help you find products, check options, add items to your cart, or proceed to checkout. What are you looking for today?"
        return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    CLOSE_PHRASES = {'close', 'close it', 'close assistant', 'close panel', 'close widget', 'minimize', 'hide', 'hide assistant', 'exit assistant', 'close the assistant', 'close it please', 'exit', 'close chat'}
    if msg_stripped in CLOSE_PHRASES or bool(re.search('\\b(close|minimize|hide)\\b.*?\\b(assistant|widget|panel|chat)\\b', msg_lower)):
        state.reset_selection()
        state.stage = 'idle'
        state.filtered_products = []
        return ('Okay, closing the assistant.', all_products, None, False, None, False, None, False, None, None, None, 'cancel', True, False)
    DISABLE_HANDS_FREE_PHRASES = {'turn off hands-free', 'turn off hands free', 'disable hands-free', 'disable hands free', 'stop hands-free', 'turn off voice', 'disable voice', 'stop listening', 'turn off listening'}
    if msg_stripped in DISABLE_HANDS_FREE_PHRASES or 'turn off hands free' in msg_lower or 'turn off hands-free' in msg_lower or ('stop listening' in msg_lower):
        state.reset_selection()
        state.stage = 'disabled'
        state.filtered_products = []
        return ('Hands-free mode turned off.', all_products, None, False, None, False, None, False, None, None, None, 'disable_hands_free', False, True)
    PURE_CANCEL_EXACT = {'no', 'no thanks', 'no thank you', 'cancel', 'cancel it', 'cancel that', 'stop', 'never mind', 'nevermind', "that's all", 'thats all', 'that is all', 'nothing else', "i'm done", 'im done', 'leave it', 'forget it', "don't add", "don't add it", "don't buy", 'nothing', "i don't want anything", "i don't want this", "i don't want it", 'no cancel', 'no cancel it', 'nope', 'nah'}
    # Structural verbs that always signal user wants to change/search something.
    # Product attributes (colors, sizes, product types, product titles) are derived
    # live from the catalog so this works for ANY store without hardcoded assumptions.
    _STRUCTURAL_REPLACEMENT = (
        'show', 'find', 'search', 'looking for', 'want to buy', 'get me', 'show me',
        'find me', 'instead', 'make it', 'change', 'give me', 'give', 'switch',
        'prefer', 'rather', 'actually', 'quantity', 'qty',
    )
    _catalog_tokens: set = set()
    for _crp in all_products:
        for _cropt in _crp.get('options', []):
            for _crval in _cropt.get('values', []):
                _catalog_tokens.add(str(_crval).strip().lower())
        _crtype = (_crp.get('productType') or _crp.get('product_type') or '').strip().lower()
        if _crtype:
            _catalog_tokens.add(_crtype)
        for _crw in re.split(r'\W+', _crp.get('title', '').lower()):
            if len(_crw) > 2:
                _catalog_tokens.add(_crw)
    has_replacement_info = (
        any(rt in msg_lower for rt in _STRUCTURAL_REPLACEMENT)
        or any(ct in msg_lower for ct in _catalog_tokens)
    )
    is_pure_cancel = msg_stripped in PURE_CANCEL_EXACT or (any((msg_lower.startswith(p) for p in ('cancel', 'stop', 'never mind', 'nevermind', "i don't want anything", 'i dont want anything', "don't add", 'dont add'))) and (not has_replacement_info))
    if msg_stripped.startswith('no') and (not has_replacement_info):
        is_pure_cancel = True
    if is_pure_cancel:
        state.reset_selection()
        state.stage = 'idle'
        state.filtered_products = []
        return ('Okay, cancelled.', all_products, None, False, None, False, None, False, None, None, None, 'cancel', False, False)
    SEARCH_TRIGGERS = ('show', 'find', 'search', 'looking for', 'want to buy', 'get me', 'show me', 'find me')
    # Reset selection when user is searching for a different product.
    # Use the catalog instead of hardcoded product-type words ('jeans', 'shirt', etc.).
    if any(st in msg_lower for st in SEARCH_TRIGGERS):
        _search_reset_prod = _find_product_by_ref(all_products, message)
        if _search_reset_prod and (
            not state.selected_product
            or _search_reset_prod.get('id') != state.selected_product.get('id')
        ):
            state.reset_selection()
    if state.stage == 'awaiting_cart_quantity':
        op = state.pending_operations[0] if state.pending_operations else None
        q_mode, q_delta = _extract_quantity_delta(message)
        has_number = bool(re.search('\\b\\d+\\b', msg_lower)) or any((w in msg_lower for w in WORD_TO_NUM.keys()))
        YES_WORDS_SET = {'yes', 'yeah', 'yep', 'yup', 'sure', 'ok', 'okay', 'correct', 'right', 'confirm'}
        if not has_number or msg_stripped in YES_WORDS_SET:
            answer = 'Please tell me the quantity you would like.'
            return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
        if op:
            op['quantity_delta'] = q_delta
            op['quantity_missing'] = False
            op['operation_version'] = op.get('operation_version', 1) + 1
            if op.get('quantity_mode') in ('increment', 'add_new'):
                op['final_quantity'] = op.get('current_quantity', 0) + q_delta
                if op.get('current_quantity', 0) > 0:
                    op['quantity_mode'] = 'increment'
            else:
                op['final_quantity'] = q_delta
            op['status'] = 'ready'
            state.pending_operations[0] = op
            missing = op.get('missing_options', [])
            if missing:
                state.stage = 'awaiting_variant'
                state.active_operation_id = op['operation_id']
                next_missing = missing[0]
                opts_so_far = ', '.join((v for v in op.get('selected_options', {}).values()))
                prefix = f'{opts_so_far} selected. ' if opts_so_far else ''
                answer = f"{prefix}Which {next_missing.lower()} would you like for **{op['product_title']}**?"
                return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
            state.stage = 'awaiting_cart_confirm'
            state.active_operation_id = None
            opts_str = ', '.join((f'{v}' for k, v in op.get('selected_options', {}).items()))
            vt = op.get('variant_title') or opts_str
            p_title = op.get('product_title', '')
            if op.get('current_quantity', 0) == 0:
                answer = f"I'll add {q_delta} **{p_title}** ({vt}) to your cart. Shall I continue?"
            elif op.get('quantity_mode') == 'increment':
                answer = f"I'll increase your **{p_title}** ({vt}) quantity by {q_delta}, making the total {op['final_quantity']}. Shall I continue?"
            else:
                answer = f"I'll set your **{p_title}** ({vt}) quantity to {op['final_quantity']}. Shall I continue?"
            return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    if state.pending_operations and state.active_operation_id:
        collect_res = _collect_operation_options(message, state, all_products, session_id, shop_domain, admin_token)
        if collect_res is not None:
            return collect_res
    if state.stage == 'awaiting_cart_confirm':
        confirm_res = _handle_awaiting_cart_confirm(message, state, all_products, session_id, shop_domain, admin_token)
        if confirm_res is not None:
            return confirm_res
    YES_WORDS = {'yes', 'yeah', 'yep', 'yup', 'sure', 'ok', 'okay', 'correct', 'right', 'go', 'add', 'confirm', 'please', 'definitely', 'absolutely', 'affirmative', 'do it', 'do', 'great', 'yes sure', 'sure yes', 'yes please', 'yes add to cart', 'yes add it to cart', 'add to cart', 'add it to cart', 'yes add to my cart', 'add it', 'yes add it'}
    is_confirm_yes = state.stage == 'awaiting_confirm' and (msg_stripped in YES_WORDS or (bool(re.search('\\b(yes|yeah|yep|yup|sure|ok|okay|correct|right|add|confirm|please|definitely|do it)\\b', msg_lower)) and (not bool(re.search("\\b(no|not|don't|dont|cancel|stop|never|change|different)\\b", msg_lower)))))
    if is_confirm_yes:
        product = state.selected_product
        variant = state.selected_variant
        if product and variant:
            if not _is_variant_in_stock(variant):
                opts_summary = _get_product_options_summary(product)
                answer = f"Sorry, **{variant.get('title')}** is currently out of stock for **{product['title']}**. Available options: {opts_summary}."
                state.stage = 'awaiting_variant'
                return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
            q_mode, q_delta = _extract_quantity_delta(message)
            has_explicit_number = bool(re.search('\\b\\d+\\b', msg_lower)) or any((w in msg_lower for w in WORD_TO_NUM.keys()))
            quantity = q_delta if has_explicit_number else state.quantity or 1
            action_id = 'act_' + uuid.uuid4().hex[:12]
            action = {'action_id': action_id, 'type': 'add', 'status': 'pending', 'conversation_id': session_id, 'expected_operation': 'add', 'product_handle': product.get('handle', ''), 'product_title': product.get('title', ''), 'variant_id': str(variant.get('id', '')), 'variant_title': variant.get('title', ''), 'selected_options': state.selected_options or {}, 'quantity': quantity, 'quantity_mode': 'increment', 'line_key': None, 'created_at': time.time(), 'completed_at': None}
            state.set_pending_cart_action(action)
            save_pending_cart_selection(session_id, product, variant, quantity)
            # Check if the user also wants to go to checkout in the same utterance.
            # Handles: "yes add to cart and go to checkout", "yes, checkout", "add it and pay now", etc.
            CHECKOUT_WORDS = {'checkout', 'check out', 'go to checkout', 'proceed to checkout',
                              'take me to checkout', 'checkout page', 'pay now', 'cart checkout'}
            wants_checkout = any(w in msg_lower for w in CHECKOUT_WORDS)
            answer = f"Adding **{product.get('title')}** ({variant.get('title')}) to your cart..."
            if wants_checkout:
                answer += ' Taking you to checkout now.'
            state.stage = 'awaiting_client_cart_action'
            return (answer, state.filtered_products or all_products, None, False, None, wants_checkout, None, False, None, variant.get('id'), quantity)
    if not state.pending_operations:
        qty_intent = _classify_cart_quantity_intent(message, state, all_products, session_id)
        if qty_intent is not None:
            qty_res = _handle_cart_quantity_intent(qty_intent, state, all_products, session_id, shop_domain, admin_token)
            if qty_res is not None:
                return qty_res
    is_option_list_query = (any((kw in msg_lower for kw in ('sizes', 'colors', 'colours', 'options', 'available sizes', 'available colors'))) or (any((kw in msg_lower for kw in ('show', 'list', 'what', 'which', 'available', 'display'))) and any((kw in msg_lower for kw in ('size', 'color', 'colour', 'option'))))) and (not any((kw in msg_lower for kw in ('add', 'buy', 'cart', 'checkout', 'delete', 'remove', 'select'))))
    if is_option_list_query:
        target_product = state.selected_product
        product_ref = None
        for p in state.filtered_products or all_products:
            p_title = p.get('title', '').lower()
            words = [w for w in re.split('\\W+', p_title) if len(w) > 3]
            if any((w in msg_lower for w in words)):
                product_ref = p.get('title')
                break
        if product_ref:
            matching = [p for p in state.filtered_products or all_products if any((w in p.get('title', '').lower() for w in re.split('\\W+', product_ref.lower()) if len(w) > 3))]
            if len(matching) > 1:
                titles_str = ', '.join((p['title'] for p in matching[:3]))
                answer = f'Which product would you like to see sizes or options for? Found multiple: {titles_str}.'
                state.stage = 'awaiting_product'
                return (answer, matching, None, False, None, False, None, False, None, None, None)
            elif len(matching) == 1:
                target_product = matching[0]
                state.selected_product = target_product
        if not target_product:
            answer = 'Which product would you like to see sizes or options for?'
            state.stage = 'browse'
            return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
        # Detect which option type the user is asking about using the product's
        # actual option names — not hardcoded 'size'/'color' strings.
        # Works for any store: Material, Fit, Style, Scent, etc.
        req_opt = None
        if target_product:
            for _opt_def in target_product.get('options', []):
                _opt_name = _opt_def.get('name', '').strip()
                # Match option name or its plural (e.g. 'color' matches 'colors')
                if _opt_name.lower() in msg_lower or (_opt_name.lower() + 's') in msg_lower:
                    req_opt = _opt_name
                    break
        state.selected_product = target_product
        state.stage = 'awaiting_variant_option'
        summary = _get_product_options_summary(target_product, state.selected_options, requested_option_name=req_opt)
        return (summary, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    if state.stage in ('awaiting_variant', 'awaiting_variant_option', 'awaiting_confirm', 'awaiting_product_clarification') or state.stage.startswith('awaiting_'):
        if not state.selected_product and (state.filtered_products or all_products):
            pool = state.filtered_products or all_products
            best_p = _find_product_by_ref(pool, message)
            if best_p:
                state.selected_product = best_p
            elif len(pool) == 1:
                state.selected_product = pool[0]

    # ── Product-switch detection ─────────────────────────────────────────────
    # If the user is in a variant-selection stage but their message seems to
    # refer to a DIFFERENT product, switch to that product instead of trying
    # to apply the message as an option selection for the current product.
    #
    # Strategy (data-driven, no hardcoded word lists):
    #   1. Check if the message contains a "switch signal" phrase.
    #   2. If so, ask the CURRENT product's own option data whether the message
    #      contains any valid option values (size, color, etc.).
    #   3. Only attempt a product switch if the current product's options do NOT
    #      match — meaning the user is NOT just selecting a variant.
    #
    # Example:
    #   Current product = Casual Oxford Shirt (options: Size S/M/L/XL, Color Sky Blue/White)
    #   User says "I want size M color sky blue"
    #     → _extract_options_from_text finds Size:M, Color:Sky Blue for current product
    #     → valid options found → NO switch → applies M/Sky Blue correctly ✓
    #   User says "Okay then I want Oxford shirt"
    #     → no valid options extracted for Navy Blue Formal Shirt
    #     → tries product switch → finds Casual Oxford Shirt → switches ✓
    PRODUCT_SWITCH_SIGNALS = (
        'i want', 'i need', 'i would like', "i'd like", 'give me', 'show me',
        'get me', 'find me', 'instead', 'actually', 'switch to', 'change to',
        'switch', 'okay then', 'ok then', 'what about', 'how about',
    )
    if (
        state.stage in ('awaiting_variant', 'awaiting_variant_option', 'awaiting_confirm', 'awaiting_product_clarification')
        or state.stage.startswith('awaiting_')
    ) and state.selected_product:
        _has_switch_signal = any(sig in msg_lower for sig in PRODUCT_SWITCH_SIGNALS)
        if _has_switch_signal:
            # Ask the current product's own option catalogue whether the message
            # contains valid option values. If it does, the user is selecting an
            # option — not switching products.
            _current_valid_opts = _extract_options_from_text(state.selected_product, msg_lower)
            _has_valid_options_for_current = bool(_current_valid_opts)
            if not _has_valid_options_for_current:
                # No valid options for current product — try to find a different one
                switched_p = _find_product_by_ref(all_products, message)
                if switched_p and switched_p.get('id') != state.selected_product.get('id'):
                    # User is asking for a different product — reset and switch
                    state.reset_selection()
                    state.selected_product = switched_p
                    state.stage = 'awaiting_variant_option'
                    summary = _get_product_options_summary(switched_p, state.selected_options)
                    state.filtered_products = [switched_p]
                    return (summary, [switched_p], None, False, None, False, None, False, None, None, None)
    # ────────────────────────────────────────────────────────────────────────

    if (state.stage in ('awaiting_variant', 'awaiting_variant_option', 'awaiting_confirm') or state.stage.startswith('awaiting_')) and state.selected_product:
        product = state.selected_product
        opt_map = get_remaining_option_values(product, state.selected_options, shop_domain=shop_domain)
        if not opt_map:
            state.stage = 'awaiting_confirm'
            answer = f'This product does not require option selection. Would you like me to add one to your cart?'
            return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
        if _has_incomplete_quantity_phrase(message):
            state.stage = 'awaiting_quantity'
            opts_str = ', '.join((f'{v}' for k, v in state.selected_options.items() if v))
            prefix = f'{opts_str} selected. ' if opts_str else ''
            answer = f'{prefix}What quantity would you like?'
            return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
        valid_opts, invalid_opts, missing_options = _validate_and_apply_options(product, message, state, all_products)
        if valid_opts:
            state.selection_revision += 1
        if invalid_opts:
            inv_key, inv_val = list(invalid_opts.items())[0]
            allowed_items = opt_map.get(inv_key, [])
            allowed = [item['value'] for item in allowed_items if item['available']]
            if len(allowed) <= 2:
                allowed_str = ' and '.join(allowed) if allowed else 'none'
            else:
                allowed_str = ', '.join(allowed[:-1]) + f', and {allowed[-1]}'
            p_title = product.get('title', 'this item')
            answer = f'{inv_val} is not available for the **{p_title}**. Available {inv_key.lower()}s are {allowed_str}. Which {inv_key.lower()} would you like?'
            state.stage = 'awaiting_variant_option'
            return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
        auto_selected_msgs = []
        opt_map_updated = get_remaining_option_values(product, state.selected_options, shop_domain=shop_domain)
        for opt_name, items in opt_map_updated.items():
            if not any((item['selected'] for item in items)):
                avail_items = [item for item in items if item['available']]
                if len(avail_items) == 1:
                    auto_val = avail_items[0]['value']
                    state.selected_options[opt_name] = auto_val
                    state.selection_revision += 1
                    auto_selected_msgs.append(f'Only {opt_name.lower()} {auto_val} is available, so I selected {auto_val}.')
        opt_map_final = get_remaining_option_values(product, state.selected_options, shop_domain=shop_domain)
        missing_options = [name for name, items in opt_map_final.items() if not any((item['selected'] for item in items))]
        if not missing_options:
            variant = _resolve_variant_by_options(product, state.selected_options)
            has_explicit_add = any((w in msg_lower for w in ('add', 'buy', 'cart', 'checkout', 'check out', 'basket', 'bag', 'put in', 'place in')))
            checkout_words = {'checkout', 'check out', 'go to checkout', 'proceed to checkout', 'take me to checkout', 'open checkout', 'pay now', 'cart checkout'}
            wants_checkout = any(w in msg_lower for w in checkout_words)
            if variant and _is_variant_in_stock(variant):
                if has_explicit_add or wants_checkout:
                    extracted_q = extract_quantity(message)
                    quantity = extracted_q if extracted_q > 1 else state.quantity or 1
                    action_id = str(uuid.uuid4())
                    v_id = str(variant.get('id', ''))
                    v_title = variant.get('title', '')
                    p_handle = product.get('handle', '')
                    p_title = product.get('title', '')
                    state.set_pending_cart_action(action_id=action_id, action_type='add', product_handle=p_handle, product_title=p_title, variant_id=v_id, variant_title=v_title, selected_options=state.selected_options or {}, quantity=quantity, quantity_mode='increment')
                    cart = save_pending_cart_selection(session_id, product, variant, quantity)
                    answer = f'Adding **{p_title}** to your cart...'
                    if wants_checkout:
                        answer += ' Taking you to checkout.'
                    return (answer, state.filtered_products or all_products, None, False, cart, wants_checkout, None, False, None, v_id, quantity)
                state.selected_variant = variant
                state.stage = 'awaiting_confirm'
                opts_speech = ', '.join((f'{k.lower()} {v}' for k, v in state.selected_options.items() if v))
                prefix = ' '.join(auto_selected_msgs) + ' ' if auto_selected_msgs else ''
                answer = f"{prefix}You selected the **{product['title']}** in {opts_speech}. Would you like me to add it to your cart?"
                return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
            else:
                prefix = ' '.join(auto_selected_msgs) + ' ' if auto_selected_msgs else ''
                summary = _get_product_options_summary(product, state.selected_options)
                state.stage = 'awaiting_variant_option'
                answer = f'{prefix}Sorry, that combination is currently unavailable. {summary}'
                return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
        else:
            state.stage = 'awaiting_variant_option'
            prefix = ' '.join(auto_selected_msgs) + ' ' if auto_selected_msgs else ''
            summary = _get_product_options_summary(product, state.selected_options)
            answer = f'{prefix}{summary}'
            return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
    CHECKOUT_WORDS = {'checkout', 'check out', 'go to checkout', 'proceed to checkout', 'take me to checkout', 'open checkout', 'pay now', 'cart checkout'}
    is_checkout_only = any((w in msg_lower for w in CHECKOUT_WORDS)) and (not any((w in msg_lower for w in ('remove', 'delete', 'clear', 'add', 'select', 'take', 'buy', 'pick', 'get'))))
    if is_checkout_only:
        cart = get_session_cart_context(session_id)
        if cart and cart.get('lines'):
            answer = 'Taking you to checkout now!'
            return (answer, state.filtered_products or all_products, None, False, cart, True, None, False, None, None, None, 'checkout', False, False)
        else:
            answer = 'Your cart is currently empty. You need to add items before checking out.'
            return (answer, state.filtered_products or all_products, None, False, cart, False, None, False, None, None, None, 'checkout', False, False)
    search_verbs = ('show', 'find', 'search', 'list', 'look for', 'bring up', 'browse', 'catalog', 'products', 'do you have')
    # Derive product-type/title words from the live catalog so we never need a
    # hardcoded list of product types ('jeans', 'shirt', 'hoodie', etc.).
    _catalog_search_words: set = set()
    for _csp in all_products:
        _cstype = (_csp.get('productType') or _csp.get('product_type') or '').strip().lower()
        if _cstype:
            _catalog_search_words.add(_cstype)
        for _csw in re.split(r'\W+', _csp.get('title', '').lower()):
            if len(_csw) > 3:
                _catalog_search_words.add(_csw)
    is_search_cmd = (
        any(w in msg_lower for w in search_verbs)
        or any(pw in msg_lower for pw in _catalog_search_words)
    )
    is_not_cart_action = not any((w in msg_lower for w in ('remove', 'delete', 'clear', 'cart', 'card', 'checkout', 'add to', 'put in', 'buy')))
    if is_search_cmd and is_not_cart_action:
        state.reset_selection()
        max_price = None
        min_price = None
        m_max = re.search('\\b(under|below|less than|max)\\s+\\$?(\\d+(?:\\.\\d+)?)\\b', msg_lower)
        if m_max:
            max_price = float(m_max.group(2))
        m_min = re.search('\\b(over|above|more than|min)\\s+\\$?(\\d+(?:\\.\\d+)?)\\b', msg_lower)
        if m_min:
            min_price = float(m_min.group(2))
        req_type = None
        # Use live catalog product types instead of a hardcoded type→canonical map.
        _catalog_type_map: dict = {}
        for _ctp in all_products:
            _raw_type = (_ctp.get('productType') or _ctp.get('product_type') or '').strip()
            if _raw_type:
                _catalog_type_map[_raw_type.lower()] = _raw_type
        for _type_lower, _type_canonical in _catalog_type_map.items():
            if _type_lower in msg_lower:
                req_type = _type_canonical
                break
        filters = {}
        if req_type:
            filters['product_type'] = req_type
        if max_price is not None:
            filters['max_price'] = max_price
        if min_price is not None:
            filters['min_price'] = min_price
        # Extract option values (color, size, or any other option) from the live catalog.
        # Replaces hardcoded color lists ('black', 'navy', ...) and size lists ('xs', 'xl', ...).
        _search_filter_opts = _extract_all_unvalidated_options(msg_lower, all_products)
        for _sfopt_name, _sfopt_val in _search_filter_opts.items():
            _sfkey = _sfopt_name.lower()
            if _sfkey in ('color', 'colour'):
                if not filters.get('color'):
                    filters['color'] = _sfopt_val.lower()
            elif _sfkey == 'size':
                if not filters.get('size'):
                    filters['size'] = _sfopt_val.lower()
            else:
                # Support any other option type the store might have
                if _sfkey not in filters:
                    filters[_sfkey] = _sfopt_val.lower()
        if not req_type and (not filters.get('color')):
            for p in all_products:
                title_l = p.get('title', '').lower()
                if any((w in msg_lower for w in title_l.split() if len(w) > 3)):
                    filters['product_name'] = p.get('title')
                    break
        if filters:
            normalized = normalize_filters(filters, all_products)
            filtered = filter_products(all_products, filters)
            storefront_url = _build_precise_product_url(normalized, filtered)
            state.filtered_products = filtered
            state.previous_filters = filters
            if not filtered:
                state.stage = 'browse'
                answer = "I couldn't find any products matching your request. Try adjusting your filters."
            elif len(filtered) == 1:
                p = filtered[0]
                state.selected_product = p
                if _has_incomplete_quantity_phrase(message):
                    _validate_and_apply_options(p, message, state, all_products)
                    state.stage = 'awaiting_quantity'
                    opts_str = ', '.join((f'{v}' for k, v in state.selected_options.items() if v))
                    prefix = f'{opts_str} selected. ' if opts_str else ''
                    answer = f'{prefix}What quantity would you like?'
                    return (answer, filtered, storefront_url, False, None, False, None, False, None, None, None)
                valid_opts, invalid_opts, missing_options = _validate_and_apply_options(p, message, state, all_products)
                if invalid_opts:
                    inv_key, inv_val = list(invalid_opts.items())[0]
                    opt_map = _get_product_option_values(p)
                    allowed = opt_map.get(inv_key, [])
                    allowed_str = ' and '.join(allowed) if len(allowed) <= 2 else ', '.join(allowed[:-1]) + f', and {allowed[-1]}'
                    p_title = p.get('title', 'this item')
                    answer = f'{inv_val} is not available for the **{p_title}**. Available {inv_key.lower()}s are {allowed_str}. Which {inv_key.lower()} would you like?'
                    state.stage = f'awaiting_{inv_key.lower()}'
                    return (answer, filtered, storefront_url, False, None, False, None, False, None, None, None)
                if not missing_options:
                    variant = _resolve_variant_by_options(p, state.selected_options)
                    if variant and _is_variant_in_stock(variant):
                        state.selected_variant = variant
                        state.stage = 'awaiting_confirm'
                        opts_speech = ', '.join((f'{k.lower()} {v}' for k, v in state.selected_options.items() if v))
                        answer = f"You selected the **{p['title']}** in {opts_speech}. Would you like me to add it to your cart?"
                        return (answer, filtered, storefront_url, False, None, False, None, False, None, None, None)
                next_missing = missing_options[0] if missing_options else 'variant'
                state.stage = f'awaiting_{next_missing.lower()}'
                opts_summary = _available_variants_text(p)
                answer = f"Found **{p['title']}**. Options: {opts_summary}. Which would you like?"
            else:
                state.stage = 'awaiting_variant'
                answer = _format_product_search_summary(filtered)
            return (answer, filtered, storefront_url, False, None, False, None, False, None, None, None)
    # Build options-inquiry detector from live catalog option names.
    # Works for any store regardless of options (Color, Size, Material, Scent, Fit, etc.).
    _catalog_option_names_for_inquiry: set = set()
    for _oip in all_products:
        for _oiopt in _oip.get('options', []):
            _oin = _oiopt.get('name', '').strip().lower()
            if _oin and _oin != 'title':
                _catalog_option_names_for_inquiry.add(_oin)
                _catalog_option_names_for_inquiry.add(_oin + 's')  # plural (e.g. 'sizes', 'colors')
    _oiq_verbs = ('what', 'which', 'available', 'tell me', 'show me', 'list', 'display')
    options_inquiry = (
        (any(v in msg_lower for v in _oiq_verbs) and any(opt in msg_lower for opt in _catalog_option_names_for_inquiry))
        or any(w in msg_lower for w in ('available options', 'what variants', 'what options', 'which options'))
    )
    if options_inquiry:
        target_prod = state.selected_product
        if not target_prod:
            for p in all_products:
                title_l = p.get('title', '').lower()
                if title_l in msg_lower or any((w in msg_lower for w in title_l.split() if len(w) > 3)):
                    target_prod = p
                    break
        if not target_prod and all_products:
            target_prod = all_products[0]
        if target_prod:
            state.selected_product = target_prod
            state.stage = 'awaiting_variant'
            opts_summary = _get_product_options_summary(target_prod)
            answer = f"**{target_prod['title']}** is available in {opts_summary}. Which option would you like?"
            prod_url = f"/products/{target_prod['handle']}" if target_prod.get('handle') else None
            return (answer, state.filtered_products or all_products, prod_url, False, None, False, None, False, None, None, None)
    cart_keywords = {'cart', 'card'}
    cart_inquiry_words = {'what', 'which', 'show', 'tell', 'list', 'view', 'check', 'see', 'give', 'provide', 'details', 'contents', 'items', 'products', 'have', 'there', 'anything', 'empty', 'how many', 'current', 'my', 'in', 'inside'}
    msg_words = set(re.split('\\W+', msg_lower))
    is_cart_inquiry = bool(msg_words & cart_keywords) and bool(msg_words & cart_inquiry_words) or msg_stripped in {'cart', 'my cart', 'show cart', 'view cart', 'check cart', 'whats in my cart', 'what is in my cart'}
    if is_cart_inquiry and (not re.search('\\b(add|remove|delete|clear|drop|empty|buy|checkout)\\b', msg_lower)):
        cart = get_session_cart_context(session_id)
        lines = cart.get('lines', []) if cart else []
        if not lines:
            answer = 'Your cart is currently empty.'
        else:
            items_str = ', '.join((f"**{l.get('product_title', '?')}**" + (f" ({l.get('variant_title', '')})" if l.get('variant_title', '').lower() not in ('', 'default title', 'default') else '') + (f" x{l.get('quantity', 1)}" if l.get('quantity', 1) > 1 else '') for l in lines))
            total_qty = cart.get('total_quantity', sum((l.get('quantity', 1) for l in lines)))
            answer = f'Your cart currently has {total_qty} item(s): {items_str}.'
        return (answer, state.filtered_products or all_products, None, False, cart, False, None, False, None, None, None)
    clear_all_pattern = '\\b((empty|clear|remove|delete|take out|drop|get rid of)\\s+(all|everything)(\\s+items?|\\s+products?|\\s+from\\s+(the\\s+)?(cart|card))?|(empty|clear)\\s+(the\\s+|my\\s+)?(cart|card)|remove\\s+all|delete\\s+all|drop\\s+all|empty\\s+all|clear\\s+all)\\b'
    if re.search(clear_all_pattern, msg_lower):
        action_id = str(uuid.uuid4())
        state.set_pending_cart_action(action_id=action_id, action_type='clear', product_handle='', product_title='all items', variant_id='', quantity=0, quantity_mode='set')
        clear_session_cart_context(session_id)
        empty_cart = {'cart_id': '', 'total_quantity': 0, 'lines': []}
        return ('Clearing all items from your cart...', state.filtered_products or all_products, None, False, empty_cart, False, None, True, None, None, None)
    remove_pattern = '\\b(remove|delete|deleted|deleting|take out|took out|drop|dropped|clear|empty|get rid of|discard|cancel)\\b'
    if re.search(remove_pattern, msg_lower):
        action_stop_words = {'remove', 'removed', 'removes', 'delete', 'deleted', 'deleting', 'take', 'out', 'took', 'drop', 'dropped', 'clear', 'empty', 'get', 'rid', 'of', 'from', 'cart', 'my', 'the', 'a', 'an', 'this', 'that', 'please', 'item', 'product', 'in', 'to', 'for', 'one', 'two', 'three', 'four', 'five', '1', '2', '3', '4', '5', 'quantity', 'qty', 'amount', 'count', 'number', 'unit', 'units', 'items', 'card', 'all'}
        words = [w for w in re.split('\\W+', msg_lower) if len(w) >= 1 and w not in action_stop_words]
        prod_ref = ' '.join(words) if words else None
        answer, removed_variant_id, cart, updated_qty = _handle_remove_from_cart(message=message, state=state, session_id=session_id, shop_domain=shop_domain, admin_token=admin_token, product_ref=prod_ref, all_products=all_products)
        checkout_words = {'checkout', 'check out', 'go to checkout', 'proceed to checkout', 'take me to checkout', 'open checkout', 'pay now', 'cart checkout'}
        wants_checkout = any(w in msg_lower for w in checkout_words)
        legacy_removed_id = removed_variant_id if updated_qty is None or updated_qty == 0 else None
        if wants_checkout:
            answer += ' Taking you to checkout.'
        return (answer, state.filtered_products or all_products, None, False, cart, wants_checkout, legacy_removed_id, False, updated_qty, None, None)
    has_action = any((w in msg_lower for w in ('add', 'select', 'buy', 'take', 'pick', 'get', 'bring', 'put', 'place', 'play', 'pay')))
    has_target = any((w in msg_lower for w in ('cart', 'checkout', 'check out', 'basket', 'bag')))
    is_checkout_cmd = 'checkout' in msg_lower or 'check out' in msg_lower
    has_explicit_add = any((w in msg_lower for w in ('add', 'buy', 'cart', 'checkout', 'check out', 'basket', 'bag', 'put in', 'place in')))
    is_add = has_action and has_target or msg_lower.startswith('add ') or msg_lower.startswith('select ') or is_checkout_cmd or (state.stage in ('awaiting_variant', 'awaiting_variant_option', 'awaiting_size', 'awaiting_color', 'awaiting_product_clarification'))
    if is_add:
        full_options_text = (state.selected_options_text + ' ' + msg_lower).strip() if state.selected_options_text else msg_lower
        quantity = extract_quantity(full_options_text)
        # Derive size/variant hint from the catalog option values — no hardcoded sizes.
        size_hint = None
        _size_hint_opts = _extract_all_unvalidated_options(msg_lower, all_products)
        if _size_hint_opts.get('Size'):
            size_hint = _size_hint_opts['Size'].lower()
        action_stop_words = {'add', 'added', 'adds', 'put', 'place', 'buy', 'want', 'get', 'select', 'selected', 'take', 'took', 'pick', 'bring', 'to', 'in', 'into', 'my', 'the', 'a', 'an', 'this', 'that', 'please', 'item', 'product', 'cart', 'basket', 'bag', 'checkout', 'check', 'out', 'page', 'size', 'color', 'colour', 'more', 'quantity', 'qty', 'amount', 'count', 'number', 'unit', 'units', 'items', 'card', 'and', 'go'} | set(WORD_TO_NUM.keys())
        words = [w for w in re.split('\\W+', msg_lower) if len(w) > 2 and w not in action_stop_words and (not w.isdigit())]
        matched_prod = None
        if words:
            best_score = 0
            for prod in all_products:
                title_lower = prod.get('title', '').lower()
                score = sum((1 for w in words if w in title_lower))
                if score > best_score:
                    best_score = score
                    matched_prod = prod
        if not matched_prod and state.selected_product:
            matched_prod = state.selected_product
        elif not matched_prod and state.filtered_products and (len(state.filtered_products) == 1):
            matched_prod = state.filtered_products[0]
        if not matched_prod and state.filtered_products and (len(state.filtered_products) > 1) and (not words):
            titles = ', '.join((p['title'] for p in state.filtered_products[:3]))
            answer = f'Which product did you mean? I can see: {titles}.'
            return (answer, state.filtered_products, None, True, None, False, None, False, None, None, None)
        if matched_prod:
            already_confirmed = state.stage == 'awaiting_confirm' and state.selected_product == matched_prod and (state.selected_variant is not None)
            if not already_confirmed:
                is_complete, missing_opt_name, missing_vals = _check_missing_options(matched_prod, full_options_text)
                if not is_complete and missing_opt_name:
                    state.selected_product = matched_prod
                    state.selected_options_text = full_options_text
                    state.stage = 'awaiting_variant'
                    vals_str = ', '.join(missing_vals)
                    answer = f"Which {missing_opt_name.lower()} would you like for **{matched_prod['title']}**? Available: {vals_str}."
                    return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
            variant = None
            if already_confirmed or (matched_prod == state.selected_product and state.selected_variant and (not size_hint)):
                variant = state.selected_variant
            else:
                variant = _find_variant(matched_prod, full_options_text)
            if not variant:
                vars_list = matched_prod.get('variants', {}).get('nodes', [])
                in_stock_vars = [v for v in vars_list if _is_variant_in_stock(v)]
                variant = in_stock_vars[0] if in_stock_vars else vars_list[0] if vars_list else None
            if variant and (not _is_variant_in_stock(variant)):
                opts_summary = _get_product_options_summary(matched_prod)
                answer = f"Sorry, **{variant.get('title')}** is currently out of stock for **{matched_prod['title']}**. Available options: {opts_summary}."
                state.stage = 'awaiting_variant'
                return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
            if variant:
                inv_qty = variant.get('inventoryQuantity')
                if inv_qty is not None and isinstance(inv_qty, int) and (inv_qty >= 0) and (quantity > inv_qty):
                    if inv_qty == 0:
                        opts_summary = _get_product_options_summary(matched_prod)
                        answer = f"Sorry, **{variant.get('title')}** is currently out of stock for **{matched_prod['title']}**. Available options: {opts_summary}."
                        state.stage = 'awaiting_variant'
                        return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
                    else:
                        quantity = inv_qty
                state.selected_product = matched_prod
                state.selected_variant = variant
                state.quantity = quantity
                skip_confirm = has_explicit_add or is_checkout_cmd
                if not skip_confirm:
                    state.stage = 'awaiting_confirm'
                    price = float(variant.get('price', 0))
                    qty_str = f'{quantity}x ' if quantity > 1 else ''
                    answer = f"Got it! Just to confirm: {qty_str}**{matched_prod['title']}** — {variant.get('title')} at ${price:.2f}. Shall I add this to your cart?"
                    return (answer, state.filtered_products or all_products, None, False, None, False, None, False, None, None, None)
                action_id = str(uuid.uuid4())
                v_id = str(variant.get('id', ''))
                v_title = variant.get('title', '')
                p_handle = matched_prod.get('handle', '')
                p_title = matched_prod.get('title', '')
                state.set_pending_cart_action(action_id=action_id, action_type='add', product_handle=p_handle, product_title=p_title, variant_id=v_id, variant_title=v_title, selected_options=state.selected_options or {}, quantity=quantity, quantity_mode='add_new')
                cart = save_pending_cart_selection(session_id, matched_prod, variant, quantity)
                state.selected_options_text = ''
                checkout_text = ' Taking you to checkout now!' if is_checkout_cmd else ''
                answer = f'Adding **{p_title}** to your cart...{checkout_text}'
                target_url = '/cart' if is_checkout_cmd else None
                return (answer, all_products, target_url, False, cart, is_checkout_cmd, None, False, None, v_id, quantity)
    return None