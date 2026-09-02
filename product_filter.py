def _as_list(value):
    """Normalize a filter value into a list of trimmed strings."""
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
    Resolve product_type / vendor / product_name to exact Shopify catalog casing.
    Shopify collection filter URLs are case-sensitive.
    """
    normalized = dict(filters or {})

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


def align_filters_for_url(filters: dict, filtered_products: list) -> dict:
    """
    Align URL facets with the widget match set so redirect shows the same products.
    Drops vendor when matches span multiple vendors; never invents vendor if LLM omitted it.
    """
    aligned = dict(filters or {})
    if not filtered_products:
        return aligned

    types = {p.get("productType", "").strip() for p in filtered_products if p.get("productType")}
    if types:
        aligned["product_type"] = ", ".join(sorted(types))

    had_vendor = bool(_as_list(filters.get("vendor")))
    if not had_vendor:
        aligned["vendor"] = None
    else:
        vendors = {p.get("vendor", "").strip() for p in filtered_products if p.get("vendor")}
        if len(vendors) != 1:
            aligned["vendor"] = None
        else:
            aligned["vendor"] = next(iter(vendors))

    return aligned


def filter_products(products: list, filters: dict) -> list:
    filtered = []

    product_names = _as_list(filters.get("product_name"))
    product_type = filters.get("product_type")
    product_types = _as_list(product_type) if product_type else []
    colors = _as_list(filters.get("color"))
    sizes = _as_list(filters.get("size"))
    vendor = filters.get("vendor")
    vendors = _as_list(vendor) if vendor else []
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")

    colors_lower = [c.lower() for c in colors]
    sizes_lower = [s.lower() for s in sizes]

    for product in products:

        # Product name filter (specific product title)
        if product_names:
            actual_title = product.get("title", "").lower()
            if not any(name.lower() in actual_title for name in product_names):
                continue

        # Product type filter
        if product_types:
            actual_type = product.get("productType", "").lower()
            if not any(pt.lower() in actual_type for pt in product_types):
                continue

        # Vendor filter
        if vendors:
            actual_vendor = product.get("vendor", "").lower()
            if not any(v.lower() in actual_vendor for v in vendors):
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

            # Color filter — substring match
            if colors_lower:
                vc = variant_color.lower()
                if not any(c in vc or vc in c for c in colors_lower):
                    continue

            # Size filter
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