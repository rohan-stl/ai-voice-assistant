"""
constants.py — Shared constants used across main.py, variant_resolver.py, and other modules.
Import from here; do NOT redefine these in individual files.
"""

WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "single": 1, "a": 1, "an": 1,
}

# ─────────────────────────────────────────────────────────
# Cart Quantity Intent — pattern constants
# ─────────────────────────────────────────────────────────

# Messages that indicate adding more (increment)
_QTY_INCREMENT_PATTERNS = [
    r"\badd\s+(?:\w+\s+)?more\b",          # "add two more", "add more"
    r"\badd\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b", # "add two T-shirts"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+more\b",
    r"\bincrease\s+(?:it\s+|the\s+)?by\b",  # "increase by", "increase it by"
    r"\bgive\s+me\s+(?:\w+\s+)?more\b",     # "give me two more"
    r"\badd\s+(?:another|additional)\b",    # "add another", "add additional"
    r"\bmore\s+of\s+(?:the\s+)?same\b",     # "more of the same"
    r"\bmore\s+of\s+(?:that|those|them)\b", # "more of those"
]

# Messages that indicate setting to an absolute quantity
_QTY_SET_PATTERNS = [
    r"\bset\s+(?:the\s+)?(?:quantity|qty)\b",
    r"\bmake\s+(?:the\s+)?(?:quantity|qty)\b",
    r"\bchange\s+(?:the\s+)?(?:quantity|qty)\b",
    r"\bchange\s+(?:it\s+)?from\s+\d+\s+to\s+\d+\b",
    r"\bfinal\s+quantity\b",
]

# Language that means "the exact same item already in my cart"
_SAME_CART_PATTERNS = [
    r"\bsame\b",                                 # "same jeans", "the same"
    r"\blike\s+the\s+one\s+in\s+(?:my\s+)?cart\b",
    r"\balready\s+in\s+(?:my\s+)?cart\b",
    r"\bmore\s+of\s+(?:the\s+)?same\b",
    r"\bthat\s+(?:same\s+)?(?:product|item|one)\b",
    r"\bthe\s+one\s+(?:i\s+have|in\s+(?:my\s+)?cart)\b",
    r"\bjust\s+like\b",
    r"\blike\s+in\s+(?:my\s+)?cart\b",
    r"\bin\s+my\s+cart\b",                       # "like the jeans in my cart"
]
