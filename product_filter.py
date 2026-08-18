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
            # Color filter
            # -----------------------------------------

            if color:
                if variant_color.lower() != color.lower():
                    continue

            # -----------------------------------------
            # Size filter
            # -----------------------------------------

            if size:
                if variant_size.lower() != size.lower():
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
            color
            or size
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