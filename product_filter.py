def filter_products(products: list, filters: dict) -> list:
    filtered = []

    product_type = filters.get("product_type")
    color = filters.get("color")
    size = filters.get("size")
    vendor = filters.get("vendor")
    max_price = filters.get("max_price")
    min_price = filters.get("min_price")

    for product in products:

        # Product type filter
        if product_type:
            actual_type = product.get("productType", "")

            if product_type.lower() not in actual_type.lower():
                continue

        # Vendor filter
        if vendor:
            actual_vendor = product.get("vendor", "")

            if vendor.lower() not in actual_vendor.lower():
                continue

        # Variant-level filtering
        variants = product.get("variants", {}).get("nodes", [])

        matching_variants = []

        for variant in variants:

            selected_options = variant.get("selectedOptions", [])

            options = {
                option.get("name", "").lower(): option.get("value", "")
                for option in selected_options
            }

            # Color filter
            if color:
                actual_color = options.get("color", "")

                if actual_color.lower() != color.lower():
                    continue

            # Size filter
            if size:
                actual_size = options.get("size", "")

                if actual_size.lower() != size.lower():
                    continue

            # Price filter
            price = variant.get("price")

            if price is not None:

                try:
                    price = float(price)
                except (ValueError, TypeError):
                    continue

                if max_price is not None and price > float(max_price):
                    continue

                if min_price is not None and price < float(min_price):
                    continue

            matching_variants.append(variant)

        # If variant-level filters exist but no variants match,
        # skip the product
        if (
            color
            or size
            or max_price is not None
            or min_price is not None
        ):
            if not matching_variants:
                continue

        # Keep product but replace variants
        # with only matching variants
        product_copy = product.copy()

        if matching_variants:
            product_copy["variants"] = {
                **product.get("variants", {}),
                "nodes": matching_variants
            }

        filtered.append(product_copy)

    return filtered