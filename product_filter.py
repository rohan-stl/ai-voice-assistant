import re
from dataclasses import dataclass, field
from typing import Optional


# ── Synonym map ─────────────────────────────────────────────────
# Maps informal user words to the set of tokens that may appear in product
# titles or productType fields. Used by filter_products product_name matching
# to bridge synonym gaps without touching the LLM prompt.
_PRODUCT_SYNONYMS: dict[str, list[str]] = {
    "hoodie":      ["hoodie", "hooded", "sweatshirt", "pullover"],
    "sweatshirt":  ["sweatshirt", "hoodie", "pullover", "crewneck", "fleece"],
    "pullover":    ["pullover", "sweatshirt", "hoodie", "hooded"],
    "sneakers":    ["sneakers", "sneaker", "trainers", "trainer", "runners", "running", "athletic"],
    "trainers":    ["trainers", "trainer", "sneakers", "runners", "running"],
    "runners":     ["runners", "running", "trainers", "sneakers"],
    "pants":       ["pants", "trousers", "chinos", "chino", "joggers", "jeans", "bottoms"],
    "trousers":    ["trousers", "pants", "chinos", "bottoms"],
    "jeans":       ["jeans", "denim", "pants", "trousers"],
    "shorts":      ["shorts", "short"],
    "tee":         ["tee", "t-shirt", "tshirt", "shirt", "top"],
    "t-shirt":     ["t-shirt", "tshirt", "tee", "shirt", "top"],
    "tshirt":      ["tshirt", "t-shirt", "tee", "shirt", "top"],
    "shirt":       ["shirt", "tee", "t-shirt", "tshirt", "top", "blouse"],
    "blouse":      ["blouse", "shirt", "top"],
    "dress":       ["dress", "gown", "frock", "midi", "maxi", "mini"],
    "jacket":      ["jacket", "coat", "blazer", "windbreaker", "parka", "anorak"],
    "coat":        ["coat", "jacket", "parka", "overcoat"],
    "blazer":      ["blazer", "jacket", "suit"],
    "skirt":       ["skirt", "midi", "mini", "maxi"],
    "leggings":    ["leggings", "tights", "yoga"],
    "shoes":       ["shoes", "shoe", "sneakers", "trainers", "boots", "loafers", "heels"],
    "boots":       ["boots", "boot", "ankle", "knee"],
    "sandals":     ["sandals", "sandal", "slides", "flip"],
    "bag":         ["bag", "tote", "handbag", "backpack", "clutch", "purse"],
    "backpack":    ["backpack", "bag", "rucksack", "pack"],
    "hat":         ["hat", "cap", "beanie", "snapback", "bucket"],
    "cap":         ["cap", "hat", "snapback", "baseball"],
    "scarf":       ["scarf", "wrap", "shawl"],
    "gloves":      ["gloves", "glove", "mittens"],
    "socks":       ["socks", "sock"],
    "underwear":   ["underwear", "briefs", "boxers", "lingerie"],
    "swimwear":    ["swimwear", "swimsuit", "bikini", "trunks", "swim"],
}


# ── StructuredQuery ────────────────────────────────────────────
# Single validated object that flows through the entire pipeline.
# Built from the LLM output by _coerce_llm_filters (in main.py).
@dataclass
class StructuredQuery:
    intent: str = "general_question"
    product_keywords: list = field(default_factory=list)  # lowercased, tokenized
    color: Optional[str] = None
    size: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    collection_handle: Optional[str] = None
    vendor: Optional[str] = None
    product_type: Optional[str] = None
    quantity: int = 1
    product_handle: Optional[str] = None  # resolved stable Shopify handle
    variant_id: Optional[str] = None      # resolved stable variant ID

    def to_filter_dict(self) -> dict:
        """Convert to the dict format expected by filter_products()."""
        return {
            "product_name": ", ".join(self.product_keywords) if self.product_keywords else None,
            "product_type": self.product_type,
            "color": self.color,
            "size": self.size,
            "vendor": self.vendor,
            "min_price": self.price_min,
            "max_price": self.price_max,
            "collection_handle": self.collection_handle,
        }


def _as_list(value):
    """
    Normalize a filter value into a list of trimmed strings.

    Accepts None, a single string (possibly comma-separated, e.g.
    "white, blue"), or an already-iterable value like a list.
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]

    return [
        part.strip()
        for part in str(value).split(",")
        if part.strip()
    ]


def normalize_filters(filters: dict, products: list) -> dict:
    """
    Resolve product_type / vendor values against the exact casing used
    in the real Shopify data.

    The LLM's extracted filters (e.g. "T-shirt" vs the store's actual
    "T-Shirt") can drift in casing. Our own product_filter matching is
    case-insensitive so it isn't affected, but Shopify's native
    collection filter URLs (filter.p.product_type=...) are case-
    sensitive strings, so a casing mismatch silently returns zero
    results on the storefront even when we found correct matches.
    This resolves each requested value to the real string from the
    fetched product list before it's used to build that URL.
    """
    normalized = dict(filters)

    def _resolve(requested_values, actual_values):
        resolved = []
        for requested in requested_values:
            req_l = requested.lower()
            
            # 1. Exact match
            match = next((a for a in actual_values if req_l == a.lower()), None)
            
            # 2. Exact match ignoring spaces/hyphens and trailing 's'
            if not match:
                req_clean = re.sub(r'[\s\-]', '', req_l).rstrip('s')
                for a in actual_values:
                    a_clean = re.sub(r'[\s\-]', '', a.lower()).rstrip('s')
                    if req_clean == a_clean:
                        match = a
                        break

            # 3. Fallback to substring ONLY IF the substring is a standalone word, not part of a hyphenated word like t-shirt
            # Wait, no substring fallback for product_types to prevent "shirts" matching "t-shirts".
            if not match:
                # We will only allow substring if it's really needed, but it's risky. Let's just stick to the above.
                pass
                
            resolved.append(match if match else requested)
        return ", ".join(resolved)

    requested_types = _as_list(filters.get("product_type"))
    if requested_types:
        actual_types = {p.get("productType", "") for p in products}
        actual_types.discard("")
        normalized["product_type"] = _resolve(requested_types, actual_types)

    requested_vendors = _as_list(filters.get("vendor"))
    if requested_vendors:
        actual_vendors = {p.get("vendor", "") for p in products}
        actual_vendors.discard("")
        normalized["vendor"] = _resolve(requested_vendors, actual_vendors)

    requested_names = _as_list(filters.get("product_name"))
    if requested_names:
        actual_titles = {p.get("title", "") for p in products}
        actual_titles.discard("")
        normalized["product_name"] = _resolve(requested_names, actual_titles)

    return normalized


def merge_filters(new_filters: dict, previous_filters: dict | None) -> dict:
    """
    Merge freshly-extracted filters with the previous turn's filters so a
    follow-up like "blue ones" can refine an earlier "under $700" instead
    of silently dropping it.

    Rules:
    - No previous_filters -> return new_filters unchanged.
    - If the new turn specifies a primary subject filter (product_type, vendor, or product_name)
      that differs from or conflicts with the previous turn's primary filter, reset previous
      primary filters and treat as a new search.
    - Otherwise, merge non-null values into previous_filters.
    """
    if not previous_filters:
        return new_filters

    def _norm(value):
        return (value or "").strip().lower()

    new_type = _norm(new_filters.get("product_type"))
    new_name = _norm(new_filters.get("product_name"))
    new_vendor = _norm(new_filters.get("vendor"))

    prev_type = _norm(previous_filters.get("product_type"))
    prev_name = _norm(previous_filters.get("product_name"))
    prev_vendor = _norm(previous_filters.get("vendor"))

    # Determine if primary subject/category/vendor changed
    subject_changed = False

    if new_type and prev_type and new_type != prev_type:
        subject_changed = True
    elif new_name and prev_name and new_name != prev_name:
        subject_changed = True
    elif new_vendor and prev_vendor and new_vendor != prev_vendor:
        subject_changed = True
    elif new_type and prev_vendor and not new_vendor:
        subject_changed = True
    elif new_vendor and prev_type and not new_type:
        subject_changed = True
    elif new_name and (prev_type or prev_vendor):
        subject_changed = True

    if subject_changed:
        return new_filters

    merged = dict(previous_filters)

    for key, value in new_filters.items():
        if value is not None:
            merged[key] = value

    return merged


def filter_products(products: list, filters: dict) -> list:
    if not isinstance(products, list):
        products = []
    filtered = []

    product_names = _as_list(filters.get("product_name"))
    product_types = _as_list(filters.get("product_type"))
    colors = _as_list(filters.get("color"))
    sizes = _as_list(filters.get("size"))
    vendors = _as_list(filters.get("vendor"))
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")
    collection_handle = (filters.get("collection_handle") or "").strip().lower() or None

    colors_lower = [c.lower() for c in colors]
    sizes_lower = [s.lower() for s in sizes]

    for product in products:

        # Product name filter
        # Matches if ANY requested name keyword (or its synonyms) appears in the
        # product title tokens OR productType tokens. This bridges gaps like
        # "hoodie" → productType "Sweatshirt" or title "Pullover".
        if product_names:
            actual_title = product.get("title", "").lower()
            actual_type  = product.get("productType", "").lower()
            # Build a combined token set from title + productType
            title_tokens = set(w.rstrip('s') for w in re.split(r'\W+', actual_title) if w)
            type_tokens  = set(w.rstrip('s') for w in re.split(r'\W+', actual_type) if w)
            combined_tokens = title_tokens | type_tokens

            match_found = False
            for name in product_names:
                name_lower = name.lower()

                # Fast path: direct substring match on title
                if name_lower in actual_title:
                    match_found = True
                    break

                # Expand each word in the name through the synonym map,
                # then check whether any synonym token appears in the
                # combined title + productType token set.
                raw_words = [w for w in re.split(r'\W+', name_lower) if w]
                synonym_tokens: set[str] = set()
                for raw_word in raw_words:
                    word_stripped = raw_word.rstrip('s')
                    syns = _PRODUCT_SYNONYMS.get(raw_word) or _PRODUCT_SYNONYMS.get(word_stripped) or [word_stripped]
                    for syn in syns:
                        synonym_tokens.update(w.rstrip('s') for w in re.split(r'\W+', syn) if w)

                if synonym_tokens & combined_tokens:
                    match_found = True
                    break

                # Word-set subset match on title tokens (legacy exact path)
                if set(w.rstrip('s') for w in raw_words).issubset(title_tokens):
                    match_found = True
                    break

            if not match_found:
                continue

        # Product type filter (matches exactly or by cleaned string to avoid "shirts" matching "t-shirts")
        if product_types:
            actual_type = product.get("productType", "").lower()
            actual_clean = re.sub(r'[\s\-]', '', actual_type).rstrip('s')

            match_found = False
            for pt in product_types:
                pt_l = pt.lower()
                if pt_l == actual_type:
                    match_found = True
                    break
                
                pt_clean = re.sub(r'[\s\-]', '', pt_l).rstrip('s')
                if pt_clean == actual_clean:
                    match_found = True
                    break
                    
            if not match_found:
                continue

        # Vendor filter (matches if ANY requested vendor is a substring)
        if vendors:
            actual_vendor = product.get("vendor", "").lower()

            if not any(
                v.lower() in actual_vendor for v in vendors
            ):
                continue

        # Collection filter — uses embedded membership index (Phase 1).
        # Falls back to no-op if the `collections` key is absent (old cache).
        if collection_handle:
            product_collections = product.get("collections")
            if product_collections is not None:
                handles = [c.get("handle", "").lower() for c in product_collections]
                if collection_handle not in handles:
                    continue
            # If `collections` key is absent (pre-Phase-1 cache entry), allow through
            # so the UI does not go blank on a cache boundary.

        variants = product.get("variants", {}).get("nodes", [])

        matching_variants = []

        for variant in variants:

            variant_title = variant.get("title", "")

            # -----------------------------------------
            # Extract color and size from selectedOptions
            # (fall back to splitting title by "/" only as last resort)
            # -----------------------------------------

            selected_opts = variant.get("selectedOptions", [])
            variant_color = ""
            variant_size = ""

            # Match option values to filters dynamically by VALUE content, not by
            # hardcoded option names ("color", "size"). This way stores with
            # option names like "Colour", "Shade", "Fit", "Style" etc. also work.
            for opt in selected_opts:
                opt_name = opt.get("name", "").lower()
                opt_val  = opt.get("value", "")
                # Color filter: check if any requested color matches this option's value
                if colors_lower and not variant_color:
                    if any(c in opt_val.lower() or opt_val.lower() in c for c in colors_lower):
                        variant_color = opt_val
                # Size filter: check if any requested size matches this option's value
                if sizes_lower and not variant_size:
                    if opt_val.lower() in sizes_lower:
                        variant_size = opt_val
                # Legacy: also accept by option name for backward compatibility
                if not variant_color and opt_name in ("color", "colour"):
                    variant_color = opt_val
                if not variant_size and opt_name == "size":
                    variant_size = opt_val

            # Fallback: split title "Blue / M" when selectedOptions absent
            if not variant_color and not variant_size:
                variant_parts = [p.strip() for p in variant_title.split("/")]
                if len(variant_parts) >= 2:
                    variant_color = variant_parts[0]
                    variant_size  = variant_parts[1]
                elif len(variant_parts) == 1:
                    # Single-option product — leave color empty, treat as size
                    variant_size = variant_parts[0]

            # -----------------------------------------
            # Color filter — substring match so "dark blue" matches "blue"
            # -----------------------------------------

            if colors_lower:
                vc = variant_color.lower()
                if not any(c in vc or vc in c for c in colors_lower):
                    continue

            # -----------------------------------------
            # Size filter — exact match (M ≠ XL)
            # -----------------------------------------

            if sizes_lower:
                if variant_size.lower() not in sizes_lower:
                    continue

            # -----------------------------------------
            # Price filter
            # -----------------------------------------

            price = variant.get("price")

            if max_price is not None or min_price is not None:

                if price is None:
                    continue

                try:
                    price = float(price)
                except (ValueError, TypeError):
                    continue

                if (
                    max_price is not None
                    and price > float(max_price)
                ):
                    continue

                if (
                    min_price is not None
                    and price < float(min_price)
                ):
                    continue

            matching_variants.append(variant)

        # -----------------------------------------
        # If variant filters exist but nothing matched
        # -----------------------------------------

        if (
            colors
            or sizes
            or max_price is not None
            or min_price is not None
        ):
            if not matching_variants:
                continue

        # -----------------------------------------
        # Keep only matching variants
        # -----------------------------------------

        product_copy = product.copy()

        if matching_variants:
            product_copy["variants"] = {
                **product.get("variants", {}),
                "nodes": matching_variants
            }

        filtered.append(product_copy)

    return filtered