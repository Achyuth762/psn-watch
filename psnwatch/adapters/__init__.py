from . import page, shopify, variants_text, woo

ADAPTERS = {
    "woocommerce": woo.check,
    "shopify": shopify.check,
    "page": page.check,
    "variants_text": variants_text.check,
}

# Adapters that only need plain HTTP (safe for --no-browser / GitHub Actions)
HTTP_ONLY = {"woocommerce", "shopify"}
