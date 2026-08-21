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
    - The "subject" of a search is whichever of product_type / product_name
      is set (a turn only ever pins down one of these — a specific named
      product, OR a general category, not both). If the new turn names a
      subject that differs from the previous turn's subject (e.g. "the
      complete snowboard" -> "T-shirt", or snowboards -> t-shirts), treat
      it as a brand new search and don't carry anything over. This has to
      compare across both fields, not each field to itself, because a
      turn that names a product (product_name set) followed by a turn
      that names a category (product_type set) is still a subject change
      even though neither field individually "conflicts" with itself.
    - Otherwise, any non-null value in new_filters overrides the
      previous value for that field; null values fall back to whatever
      the previous turn had.
    """
    if not previous_filters:
        return new_filters

    def _norm(value):
        return (value or "").strip().lower()

    new_subject = _norm(new_filters.get("product_type")) or _norm(new_filters.get("product_name"))
    prev_subject = _norm(previous_filters.get("product_type")) or _norm(previous_filters.get("product_name"))

    if new_subject and prev_subject and new_subject != prev_subject:
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
            # Extract color and size from variant title
            # Example: "White / M"
            # -----------------------------------------

            variant_parts = [
                part.strip()
                for part in variant_title.split("/")
            ]

            variant_color = (
                variant_parts[0]
                if len(variant_parts) >= 1
                else ""
            )

            variant_size = (
                variant_parts[1]
                if len(variant_parts) >= 2
                else ""
            )

            # -----------------------------------------
            # Color filter (matches ANY requested color)
            # -----------------------------------------

            if colors_lower:
                if variant_color.lower() not in colors_lower:
                    continue

            # -----------------------------------------
            # Size filter (matches ANY requested size)
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