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
            match = next(
                (
                    actual for actual in actual_values
                    if requested.lower() in actual.lower()
                ),
                None,
            )
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
    filtered = []

    product_names = _as_list(filters.get("product_name"))
    product_types = _as_list(filters.get("product_type"))
    colors = _as_list(filters.get("color"))
    sizes = _as_list(filters.get("size"))
    vendors = _as_list(filters.get("vendor"))
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")

    colors_lower = [c.lower() for c in colors]
    sizes_lower = [s.lower() for s in sizes]

    for product in products:

        # Product name filter (matches if ANY requested name is a substring
        # of the product's title, e.g. "complete snowboard" matches
        # "The Complete Snowboard")
        if product_names:
            actual_title = product.get("title", "").lower()

            if not any(
                name.lower() in actual_title for name in product_names
            ):
                continue

        # Product type filter (matches if ANY requested type is a substring)
        if product_types:
            actual_type = product.get("productType", "").lower()

            if not any(
                pt.lower() in actual_type for pt in product_types
            ):
                continue

        # Vendor filter (matches if ANY requested vendor is a substring)
        if vendors:
            actual_vendor = product.get("vendor", "").lower()

            if not any(
                v.lower() in actual_vendor for v in vendors
            ):
                continue

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

            for opt in selected_opts:
                opt_name = opt.get("name", "").lower()
                opt_val  = opt.get("value", "")
                if opt_name in ("color", "colour"):
                    variant_color = opt_val
                elif opt_name == "size":
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