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
    # browse              – showing product results, waiting for refinement/selection
    # awaiting_variant    – user must pick a specific variant (size/color/qty)
    # awaiting_color      – user must select a valid color (after selecting product/size)
    # awaiting_size       – user must select a valid size (after selecting product/color)
    # awaiting_quantity   – user specified quantity phrase; waiting for number
    # awaiting_product    – multiple products match; user must clarify which one
    # awaiting_collection – user specified ambiguous/unavailable collection; waiting for choice
    # awaiting_confirm    – about to add NEW item to cart; waiting for user's "yes"
    # awaiting_cart_confirm – about to update EXISTING cart line quantity; waiting for "yes"
    # awaiting_cart_quantity – updating cart quantity; waiting for numeric quantity
    # collection_browse   – browsing inside an active collection
    # done                – item added to cart; flow complete (user may start over)
    stage: str = "browse"

    # Invalid option requests tracked for debugging/responses (e.g. {"Color": "Red"})
    invalid_requested_options: dict = field(default_factory=dict)

    # -------------------------------------------------------
    # Collection Context & Cursor State
    # -------------------------------------------------------
    active_collection: Optional[dict] = None  # {"id": ..., "title": ..., "handle": ..., "url": ...}
    collection_cursor: Optional[str] = None
    collection_has_next_page: bool = False
    collection_candidates: list = field(default_factory=list)
    is_fetching_more: bool = False

    # -------------------------------------------------------
    # Product context
    # -------------------------------------------------------
    filtered_products: list = field(default_factory=list)
    selected_product: Optional[dict] = None
    selected_variant: Optional[dict] = None
    quantity: int = 1

    # -------------------------------------------------------
    # Filter carry-forward (for multi-turn refinement)
    # -------------------------------------------------------
    previous_filters: dict = field(default_factory=dict)

    # -------------------------------------------------------
    # Cart
    # -------------------------------------------------------
    cart_id: Optional[str] = None
    cart_lines: list = field(default_factory=list)

    # -------------------------------------------------------
    # Timing (for TTL expiry)
    # -------------------------------------------------------
    last_active: float = field(default_factory=time.time)
    session_id: str = "default"

    selected_options: dict = field(default_factory=dict)
    selected_options_text: str = ""

    # -------------------------------------------------------
    # Cart quantity multi-operation state
    # -------------------------------------------------------
    pending_cart_intent: Optional[str] = None
    pending_operations: list = field(default_factory=list)
    active_operation_id: Optional[str] = None

    # -------------------------------------------------------
    # Idempotency / dedup
    # -------------------------------------------------------
    processed_request_ids: list = field(default_factory=list)

    def touch(self):
        self.last_active = time.time()

    def reset_selection(self, preserve_collection: bool = False):
        """
        Clear product/variant selection.
        If preserve_collection is False: also clears active_collection, collection_cursor, and candidates.
        """
        self.stage = "browse" if not (preserve_collection and self.active_collection) else "collection_browse"
        self.filtered_products = []
        self.selected_product = None
        self.selected_variant = None
        self.quantity = 1
        self.previous_filters = {}
        self.selected_options_text = ""
        self.selected_options = {}
        self.invalid_requested_options = {}
        self.pending_cart_intent = None
        self.pending_operations = []
        self.active_operation_id = None
        self.is_fetching_more = False

        if not preserve_collection:
            self.active_collection = None
            self.collection_cursor = None
            self.collection_has_next_page = False
            self.collection_candidates = []


class SessionStore:
    """Thread-safe in-memory store for ConversationState objects."""

    def __init__(self):
        self._store: dict[str, ConversationState] = {}

    def get(self, session_id: str) -> ConversationState:
        """Return existing state or create a fresh one."""
        self._evict_expired()
        if session_id not in self._store:
            self._store[session_id] = ConversationState(session_id=session_id)
        state = self._store[session_id]
        state.session_id = session_id
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
