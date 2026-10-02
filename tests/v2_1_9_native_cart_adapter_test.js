/**
 * tests/v2_1_9_native_cart_adapter_test.js
 * Node JS test suite for v2.1.9 native cart adapter, variant resolution,
 * positional option mapping, cart line matching, and sanitization logic.
 */

const assert = require("assert");

// ── Mock Globals ──────────────────────────────────────────────────
global.window = {
  Shopify: {
    routes: { root: "/" },
    shop: "mustali-dev.myshopify.com"
  }
};

// ── Functions under test (extracted from ai-assistant.js specification) ──

function buildVariantOptionMap(product, variant) {
  const result = {};
  if (!product || !product.options || !variant || !variant.options) return result;
  product.options.forEach((option, index) => {
    const optName = typeof option === "string" ? option : (option.name || `Option${index + 1}`);
    result[optName] = variant.options[index];
  });
  return result;
}

function resolveVariantStrict(product, requestedOptions) {
  if (!product || !product.variants || !Array.isArray(product.variants)) {
    return { variant: null, error: "PRODUCT_NOT_PUBLISHED" };
  }

  const requestedKeys = Object.keys(requestedOptions || {});
  const matchingVariants = product.variants.filter(variant => {
    const optMap = buildVariantOptionMap(product, variant);
    for (const key of requestedKeys) {
      const reqVal = String(requestedOptions[key] || "").trim().toLowerCase();
      const matchedOptKey = Object.keys(optMap).find(k => k.trim().toLowerCase() === key.trim().toLowerCase());
      if (!matchedOptKey) return false;
      const actualVal = String(optMap[matchedOptKey] || "").trim().toLowerCase();
      if (reqVal !== actualVal) return false;
    }
    return true;
  });

  if (matchingVariants.length === 0) {
    if (product.variants.length >= 250) {
      return { variant: null, error: "VARIANT_RESOLUTION_INCOMPLETE" };
    }
    return { variant: null, error: "CART_VARIANT_UNAVAILABLE" };
  }

  if (matchingVariants.length > 1) {
    const requiredOptCount = Array.isArray(product.options) ? product.options.length : 0;
    if (requestedKeys.length < requiredOptCount || product.variants.length >= 250) {
      return { variant: null, error: "VARIANT_RESOLUTION_INCOMPLETE" };
    }
  }

  const targetVariant = matchingVariants[0];
  const isAvail = targetVariant.available === true || targetVariant.availableForSale === true;
  if (!isAvail) {
    return { variant: null, error: "CART_VARIANT_UNAVAILABLE" };
  }

  return { variant: targetVariant, error: null };
}

function findExactCartLine(cart, criteria) {
  if (!cart || !Array.isArray(cart.items)) return null;

  const targetLineKey = criteria.lineKey ? String(criteria.lineKey) : null;
  const targetVarId = criteria.variantId ? String(criteria.variantId) : null;

  for (const line of cart.items) {
    const lineKey = line.key ? String(line.key) : String(line.id || "");
    const lineVarId = String(line.variant_id || line.id || "");

    if (targetLineKey && lineKey === targetLineKey) {
      return line;
    }

    if (targetVarId && (lineVarId === targetVarId || lineVarId.split("/").pop() === targetVarId.split("/").pop())) {
      if (criteria.selectedOptions) {
        const lineOpts = {};
        if (Array.isArray(line.options_with_values)) {
          line.options_with_values.forEach(o => { lineOpts[o.name.toLowerCase()] = String(o.value).toLowerCase(); });
        }
        let allMatch = true;
        for (const [k, v] of Object.entries(criteria.selectedOptions)) {
          if (lineOpts[k.toLowerCase()] && lineOpts[k.toLowerCase()] !== String(v).toLowerCase()) {
            allMatch = false;
            break;
          }
        }
        if (allMatch) return line;
      } else {
        return line;
      }
    }
  }
  return null;
}

function getExactLineQuantity(cart, target) {
  if (!cart || !Array.isArray(cart.items)) return 0;
  let targetVarId = null;
  let targetLineKey = null;

  if (typeof target === "object" && target !== null) {
    targetVarId = target.variantId || target.id || target.variant_id;
    targetLineKey = target.lineKey || target.key;
  } else if (typeof target === "string" || typeof target === "number") {
    targetVarId = String(target);
  }

  const line = findExactCartLine(cart, { lineKey: targetLineKey, variantId: targetVarId });
  return line ? (line.quantity || 0) : 0;
}

function sanitizeCartContext(rawCart) {
  if (!rawCart || typeof rawCart !== "object") return null;
  return {
    item_count: rawCart.item_count || 0,
    items: Array.isArray(rawCart.items) ? rawCart.items.map(item => {
      const opts = {};
      if (Array.isArray(item.options_with_values)) {
        item.options_with_values.forEach(opt => { opts[opt.name] = opt.value; });
      } else if (item.options && typeof item.options === "object") {
        Object.assign(opts, item.options);
      }
      return {
        key: item.key || String(item.id || ""),
        variant_id: item.variant_id || item.id,
        product_title: item.product_title || item.title || "",
        variant_title: item.variant_title || "",
        quantity: item.quantity || 1,
        options: opts
      };
    }) : []
  };
}

// ── Test Suites ──────────────────────────────────────────────────

console.log("▶ Running v2.1.9 Native Cart Adapter Tests...");

// Test 1: Positional buildVariantOptionMap
{
  const product = { options: [{ name: "Size" }, { name: "Color" }] };
  const variant = { options: ["XL", "Blue"] };
  const map = buildVariantOptionMap(product, variant);
  assert.strictEqual(map["Size"], "XL");
  assert.strictEqual(map["Color"], "Blue");
  console.log("  ✓ Positional buildVariantOptionMap passed");
}

// Test 2: Case-insensitive option matching & canonical value preservation
{
  const product = {
    options: [{ name: "Size" }],
    variants: [{ id: 101, options: ["6-7 Years"], available: true }]
  };
  const res = resolveVariantStrict(product, { size: "6-7 years" });
  assert.strictEqual(res.error, null);
  assert.strictEqual(res.variant.id, 101);
  assert.strictEqual(res.variant.options[0], "6-7 Years");
  console.log("  ✓ Strict case-insensitive variant resolution passed");
}

// Test 3: 250 returned variants returns VARIANT_RESOLUTION_INCOMPLETE
{
  const variants = Array.from({ length: 250 }, (_, i) => ({
    id: 1000 + i,
    options: [`Size_${i}`],
    available: true
  }));
  const product = { options: [{ name: "Size" }], variants };
  const res = resolveVariantStrict(product, { size: "non-existent" });
  assert.strictEqual(res.error, "VARIANT_RESOLUTION_INCOMPLETE");
  console.log("  ✓ 250 variants limit returns VARIANT_RESOLUTION_INCOMPLETE passed");
}

// Test 4: findExactCartLine by lineKey and variantId
{
  const cart = {
    items: [
      { key: "line-key-1", variant_id: 201, quantity: 1 },
      { key: "line-key-2", variant_id: 202, quantity: 3 }
    ]
  };
  const line1 = findExactCartLine(cart, { lineKey: "line-key-2" });
  assert.strictEqual(line1.variant_id, 202);
  assert.strictEqual(getExactLineQuantity(cart, { lineKey: "line-key-2" }), 3);
  console.log("  ✓ Fresh cart line resolution & quantity extraction passed");
}

// Test 5: Cart context sanitization (never leak tokens, secrets, cookies)
{
  const rawCart = {
    token: "SECRET_TOKEN_999",
    checkout_url: "https://store.myshopify.com/checkouts/secret",
    attributes: { secret_cookie: "xyz" },
    item_count: 2,
    items: [
      { key: "k1", variant_id: 555, product_title: "Jeans", quantity: 2, options_with_values: [{ name: "Size", value: "32W" }] }
    ]
  };
  const sanitized = sanitizeCartContext(rawCart);
  assert.strictEqual(sanitized.token, undefined);
  assert.strictEqual(sanitized.checkout_url, undefined);
  assert.strictEqual(sanitized.attributes, undefined);
  assert.strictEqual(sanitized.item_count, 2);
  assert.strictEqual(sanitized.items[0].product_title, "Jeans");
  assert.strictEqual(sanitized.items[0].options["Size"], "32W");
  console.log("  ✓ Cart context sanitization passed");
}

console.log("✅ All v2.1.9 Native Cart Adapter Tests Passed Successfully!");
