"""
conversation_state.py

In-memory and SQLite-backed conversation state store, keyed by session_id.
Each session tracks the multi-turn shopping flow stage and the user's
current selections. Sessions expire after IDLE_TTL_SECONDS of inactivity.
"""

import time
import uuid
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
    selection_revision: int = 0

    # -------------------------------------------------------
    # Cart quantity multi-operation state
    # -------------------------------------------------------
    pending_cart_intent: Optional[str] = None
    pending_operations: list = field(default_factory=list)
    active_operation_id: Optional[str] = None

    # -------------------------------------------------------
    # Client Cart Action State Machine (v2.1.9)
    # -------------------------------------------------------
    pending_cart_action: Optional[dict] = None

    # -------------------------------------------------------
    # Idempotency / dedup
    # -------------------------------------------------------
    processed_request_ids: list = field(default_factory=list)

    def touch(self):
        self.last_active = time.time()

    def set_pending_cart_action(
        self,
        action: dict = None,
        action_id: str = None,
        action_type: str = "add",
        product_handle: str = "",
        product_title: str = "",
        variant_id: str = None,
        variant_title: str = "",
        selected_options: dict = None,
        quantity: int = 1,
        quantity_mode: str = "set",
        line_key: str = None,
    ) -> dict:
        if isinstance(action, dict):
            self.pending_cart_action = action
        else:
            self.pending_cart_action = {
                "action_id": action_id or str(uuid.uuid4()),
                "status": "pending",
                "conversation_id": self.session_id,
                "type": action_type,
                "expected_operation": action_type,
                "product_handle": product_handle,
                "product_title": product_title,
                "variant_id": variant_id,
                "variant_title": variant_title,
                "selected_options": selected_options or {},
                "quantity": quantity,
                "quantity_mode": quantity_mode,
                "line_key": line_key,
                "created_at": time.time(),
                "completed_at": None,
            }
        self.stage = "awaiting_client_cart_action"
        return self.pending_cart_action

    def get_pending_cart_action(self) -> Optional[dict]:
        return self.pending_cart_action

    def update_pending_cart_action_status(self, status: str):
        if self.pending_cart_action:
            self.pending_cart_action["status"] = status
            if status in ("succeeded", "failed"):
                self.pending_cart_action["completed_at"] = time.time()

    def clear_pending_cart_action(self):
        self.pending_cart_action = None

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
        self.pending_cart_action = None
        self.is_fetching_more = False

        if not preserve_collection:
            self.active_collection = None
            self.collection_cursor = None
            self.collection_has_next_page = False
            self.collection_candidates = []


import sqlite3
import json

class SessionStore:
    """Thread-safe SQLite store for ConversationState objects."""

    def __init__(self, db_path: str = "sessions.sqlite"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    state_json TEXT,
                    last_active REAL
                )
            ''')
            conn.commit()

    def get(self, session_id: str) -> ConversationState:
        """Return existing state from SQLite or create a fresh one."""
        self._evict_expired()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT state_json FROM sessions WHERE session_id = ?", (session_id,))
            row = cursor.fetchone()
            
        if row:
            try:
                data = json.loads(row[0])
                state = ConversationState(**data)
            except Exception:
                state = ConversationState(session_id=session_id)
        else:
            state = ConversationState(session_id=session_id)
            
        state.session_id = session_id
        state.touch()
        return state

    def save(self, session_id: str, state: ConversationState):
        state.touch()
        import dataclasses
        state_dict = dataclasses.asdict(state)
        state_json = json.dumps(state_dict)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO sessions (session_id, state_json, last_active)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    last_active=excluded.last_active
            ''', (session_id, state_json, state.last_active))
            conn.commit()

    def delete(self, session_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()

    def _evict_expired(self):
        now = time.time()
        cutoff = now - IDLE_TTL_SECONDS
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff,))
            conn.commit()


# Singleton used by main.py
session_store = SessionStore()
