"""
conversation_state.py

In-memory conversation state store, keyed by session_id.
Each session tracks the multi-turn shopping flow stage and the user's
current selections. Sessions expire after IDLE_TTL_SECONDS of inactivity.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

# Sessions expire after 30 minutes of idle
IDLE_TTL_SECONDS = 30 * 60


@dataclass
class ConversationState:
    # -------------------------------------------------------
    # Flow stage
    # -------------------------------------------------------
    # browse            – showing product results, waiting for refinement or selection
    # awaiting_variant  – user must pick a specific variant (size/color/qty)
    # awaiting_product  – multiple products match; user must clarify which one
    # awaiting_confirm  – about to add to cart; waiting for user's "yes"
    # done              – item added to cart; flow complete (user may start over)
    stage: str = "browse"

    # -------------------------------------------------------
    # Product context
    # -------------------------------------------------------
    # All products currently visible to the user (after filter)
    filtered_products: list = field(default_factory=list)

    # The single product the user has zeroed in on (or None)
    selected_product: Optional[dict] = None

    # The exact variant chosen (title, price, variantId, etc.)
    selected_variant: Optional[dict] = None

    # How many units the user wants
    quantity: int = 1

    # -------------------------------------------------------
    # Filter carry-forward (for multi-turn refinement)
    # -------------------------------------------------------
    previous_filters: dict = field(default_factory=dict)

    # -------------------------------------------------------
    # Cart
    # -------------------------------------------------------
    # Shopify Storefront cart GID (created on first add)
    cart_id: Optional[str] = None

    # Human-readable cart line items: [{title, variant_title, qty, price}]
    cart_lines: list = field(default_factory=list)

    # -------------------------------------------------------
    # Timing (for TTL expiry)
    # -------------------------------------------------------
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()

    # Accumulated option text across turns (e.g. "white t-shirt size m")
    selected_options_text: str = ""

    def reset_selection(self):
        """Clear product/variant selection without clearing the cart."""
        self.stage = "browse"
        self.filtered_products = []
        self.selected_product = None
        self.selected_variant = None
        self.quantity = 1
        self.previous_filters = {}
        self.selected_options_text = ""


class SessionStore:
    """Thread-safe in-memory store for ConversationState objects."""

    def __init__(self):
        self._store: dict[str, ConversationState] = {}

    def get(self, session_id: str) -> ConversationState:
        """Return existing state or create a fresh one."""
        self._evict_expired()
        if session_id not in self._store:
            self._store[session_id] = ConversationState()
        state = self._store[session_id]
        state.touch()
        return state

    def save(self, session_id: str, state: ConversationState):
        state.touch()
        self._store[session_id] = state

    def delete(self, session_id: str):
        self._store.pop(session_id, None)

    def _evict_expired(self):
        now = time.time()
        expired = [
            sid
            for sid, state in self._store.items()
            if now - state.last_active > IDLE_TTL_SECONDS
        ]
        for sid in expired:
            del self._store[sid]


# Singleton used by main.py
session_store = SessionStore()
