"""
store_credentials_db.py

SQLite persistent database storing per-shop credentials (Storefront and Admin access tokens)
keyed by normalized shop domain. Multi-worker safe with WAL mode.
"""

import os
import sqlite3
import time
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "store_credentials.sqlite")


def _get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_credentials (
                shop_domain TEXT PRIMARY KEY,
                storefront_token TEXT,
                admin_token TEXT,
                created_at REAL,
                updated_at REAL
            )
        """)
        conn.commit()


# Initialize table on import
init_db()


def get_store_credentials(shop_domain: str) -> Optional[dict]:
    if not shop_domain:
        return None
    normalized_domain = shop_domain.strip().lower()
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT shop_domain, storefront_token, admin_token, created_at, updated_at FROM store_credentials WHERE shop_domain = ?",
            (normalized_domain,)
        )
        row = cursor.fetchone()
        if row:
            return {
                "shop_domain": row[0],
                "storefront_token": row[1],
                "admin_token": row[2],
                "created_at": row[3],
                "updated_at": row[4],
            }
    return None


def set_store_credentials(shop_domain: str, storefront_token: Optional[str] = None, admin_token: Optional[str] = None):
    if not shop_domain:
        return
    normalized_domain = shop_domain.strip().lower()
    now = time.time()
    with _get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT created_at FROM store_credentials WHERE shop_domain = ?", (normalized_domain,))
        row = cursor.fetchone()
        created_at = row[0] if row else now
        cursor.execute(
            """
            INSERT OR REPLACE INTO store_credentials (shop_domain, storefront_token, admin_token, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (normalized_domain, storefront_token, admin_token, created_at, now)
        )
        conn.commit()


def delete_store_credentials(shop_domain: str):
    if not shop_domain:
        return
    normalized_domain = shop_domain.strip().lower()
    with _get_db() as conn:
        conn.execute("DELETE FROM store_credentials WHERE shop_domain = ?", (normalized_domain,))
        conn.commit()
