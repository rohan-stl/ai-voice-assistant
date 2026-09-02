"""
logger.py — Beautiful, structured console logging for AI Shopping Assistant.
Uses ANSI colors, icons, and clear sections. No external dependencies.
"""

import sys
from datetime import datetime

# ── ANSI color codes ─────────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"

BLACK   = "\033[30m"
RED     = "\033[31m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
BLUE    = "\033[34m"
MAGENTA = "\033[35m"
CYAN    = "\033[36m"
WHITE   = "\033[37m"

BG_GREEN  = "\033[42m"
BG_RED    = "\033[41m"
BG_BLUE   = "\033[44m"
BG_YELLOW = "\033[43m"

# ── Support for no-color environments ────────────────────────────
def _no_color() -> bool:
    return not sys.stdout.isatty() or "--no-color" in sys.argv


def _c(code: str, text: str) -> str:
    if _no_color():
        return text
    return f"{code}{text}{RESET}"


# ── Timestamp ────────────────────────────────────────────────────
def _ts() -> str:
    return _c(DIM, datetime.now().strftime("%H:%M:%S"))


# ── Divider ──────────────────────────────────────────────────────
def _divider(char: str = "─", width: int = 60, color: str = DIM) -> str:
    return _c(color, char * width)


# ─────────────────────────────────────────────────────────────────
# Public log functions
# ─────────────────────────────────────────────────────────────────

def log_request_start(session_id: str, shop: str):
    print()
    print(_divider("━", 64, CYAN))
    print(
        f"  {_c(BOLD + CYAN, '🎙  NEW REQUEST')}  "
        f"{_ts()}  "
        f"{_c(DIM, 'session=')} {_c(YELLOW, session_id[:12])}  "
        f"{_c(DIM, 'shop=')} {_c(BLUE, shop)}"
    )
    print(_divider("─", 64))


def log_stt(file_bytes: int, text: str):
    size_kb = file_bytes / 1024
    print(
        f"  {_c(MAGENTA, '🎤 STT')}   "
        f"{_c(DIM, f'{size_kb:.1f} KB')}  \u2192  "
        f'{_c(BOLD, chr(34) + text + chr(34))}'
    )


def log_llm_call(model: str, attempt: int):
    print(
        f"  {_c(BLUE, '🤖 LLM')}   "
        f"Calling {_c(CYAN, model)}  "
        f"{_c(DIM, f'(attempt {attempt})')}"
    )


def log_intent(intent: str, filters: dict):
    filter_str = "  ".join(
        f"{_c(DIM, k + '=')} {_c(YELLOW, str(v))}"
        for k, v in filters.items() if v
    ) if filters else _c(DIM, "none")
    print(
        f"  {_c(CYAN, '🎯 Intent')}  "
        f"{_c(BOLD, intent)}  "
        f"{filter_str}"
    )


def log_products(count: int, shown: list):
    titles = ", ".join(p.get("title", "?") for p in shown[:3])
    more   = f" + {len(shown) - 3} more" if len(shown) > 3 else ""
    print(
        f"  {_c(GREEN, '📦 Products')} "
        f"{_c(BOLD, str(count))} matched  "
        f"{_c(DIM, titles + more)}"
    )


def log_stage(stage: str):
    colors = {
        "browse":           CYAN,
        "awaiting_variant": YELLOW,
        "awaiting_confirm": MAGENTA,
    }
    color = colors.get(stage, WHITE)
    print(
        f"  {_c(DIM, '🔄 Stage')}   "
        f"{_c(color + BOLD, stage)}"
    )


def log_answer(text: str):
    # Truncate long answers for readability
    display = text[:120] + "…" if len(text) > 120 else text
    display = display.replace("**", "")  # strip markdown bold
    print(
        f"  {_c(GREEN, '💬 Answer')}  "
        f"{_c(BOLD, display)}"
    )


def log_cart_success(item_count: int, checkout_url: str):
    short_url = checkout_url[:60] + "…" if len(checkout_url) > 60 else checkout_url
    print()
    print(f"  {_c(BG_GREEN + BLACK + BOLD, ' 🛒 CART UPDATED ')}  {_c(GREEN, f'{item_count} item(s)')}")
    print(f"  {_c(DIM, '↳ Checkout:')} {_c(CYAN, short_url)}")


def log_storefront_url(url: str):
    print(
        f"  {_c(BLUE, '🔗 Filter URL')} "
        f"{_c(DIM, url[:80] + ('…' if len(url) > 80 else ''))}"
    )


def log_http(method: str, path: str, status: int, duration_ms: float):
    color = GREEN if status < 300 else (YELLOW if status < 500 else RED)
    print(
        f"\n  {_c(BOLD, method):6}  "
        f"{_c(DIM, path[:55])}  "
        f"{_c(color + BOLD, str(status))}  "
        f"{_c(DIM, f'{duration_ms:.0f}ms')}"
    )
    print(_divider("─", 64))


def log_error(context: str, error: Exception):
    print()
    print(f"  {_c(BG_RED + WHITE + BOLD, ' ❌ ERROR ')}  {_c(RED, context)}")
    print(f"  {_c(DIM, '↳')} {_c(RED, str(error))}")


def log_tts(success: bool):
    if success:
        print(f"  {_c(DIM, '🔊 TTS')}    Generated audio")
    else:
        print(f"  {_c(DIM, '🔇 TTS')}    Skipped (no audio)")


def log_warn(context: str, msg: str):
    print(
        f"  {_c(YELLOW, '⚠️  WARN')}  "
        f"{_c(YELLOW, context)}: {_c(DIM, msg)}"
    )


def log_info(msg: str):
    print(f"  {_c(DIM, 'ℹ')}  {msg}")
