#!/usr/bin/env bash
# ============================================================
# AI Shopping Assistant – Dev Launcher
# Usage: ./start.sh
# Starts:  1) Python backend  2) Cloudflare tunnel  3) shopify app dev
# No ngrok, no hardcoded URLs — everything auto-configures.
# ============================================================

set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SHOPIFY_DIR="$REPO_ROOT/frontend/ai-shopping-assistant"
TOML="$SHOPIFY_DIR/shopify.app.toml"
BACKEND_PORT=8000
LOG_DIR="$REPO_ROOT/.dev-logs"

mkdir -p "$LOG_DIR"

# ── Kill stale processes on required ports ─────────────────────
echo "🧹 Cleaning up any stale processes on ports 8000 and 9293…"
lsof -ti :8000 | xargs kill -9 2>/dev/null || true
lsof -ti :9293 | xargs kill -9 2>/dev/null || true
pkill -9 ngrok 2>/dev/null || true
sleep 1
echo "   ✅ Ports cleared."


# ── Cleanup on exit ────────────────────────────────────────────
cleanup() {
  echo ""
  echo "⬇️  Shutting down all processes…"
  kill "$UVICORN_PID"  2>/dev/null || true
  kill "$CF_PID"       2>/dev/null || true
  kill "$SHOPIFY_PID"  2>/dev/null || true
  wait 2>/dev/null
  echo "✅ All processes stopped."
}
trap cleanup EXIT INT TERM

# ── 1. Activate venv & start Python backend ────────────────────
echo "🐍 Starting Python backend on port $BACKEND_PORT…"
source "$REPO_ROOT/venv/bin/activate"
python -m uvicorn main:app --reload --port "$BACKEND_PORT" \
  >"$LOG_DIR/uvicorn.log" 2>&1 &
UVICORN_PID=$!

echo "   Waiting for backend to be ready…"
for i in $(seq 1 20); do
  if curl -sf "http://localhost:$BACKEND_PORT/" > /dev/null 2>&1; then
    echo "   ✅ Backend ready."
    break
  fi
  sleep 0.5
done

# ── 2. Start Cloudflare Quick Tunnel ──────────────────────────
echo ""
echo "☁️  Starting Cloudflare Quick Tunnel (no account needed)…"
cloudflared tunnel --url "http://localhost:$BACKEND_PORT" \
  --no-autoupdate \
  >"$LOG_DIR/cloudflared.log" 2>&1 &
CF_PID=$!

echo "   Waiting for tunnel URL…"
TUNNEL_URL=""
for i in $(seq 1 30); do
  TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG_DIR/cloudflared.log" 2>/dev/null | head -1)
  if [ -n "$TUNNEL_URL" ]; then
    break
  fi
  sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
  echo "❌ Could not get tunnel URL — check $LOG_DIR/cloudflared.log"
  exit 1
fi

echo "   ✅ Tunnel: $TUNNEL_URL"

# ── 3. Patch shopify.app.toml with the live tunnel URL ─────────
echo ""
echo "📝 Patching shopify.app.toml with tunnel URL…"

# Update application_url and app_proxy.url using sed
# macOS sed requires '' for in-place edits
sed -i '' "s|^application_url = .*|application_url = \"$TUNNEL_URL\"|" "$TOML"
sed -i '' "/^\[app_proxy\]/,/^\[/ s|^url = .*|url = \"$TUNNEL_URL\"|" "$TOML"

echo "   ✅ shopify.app.toml updated."
echo ""
echo "   Tunnel URL: $TUNNEL_URL"
echo "   App Proxy : $TUNNEL_URL/apps/ai-assistant/voice"
echo ""

# ── 4. Start shopify app dev ──────────────────────────────────
echo "🛍️  Starting shopify app dev…"
cd "$SHOPIFY_DIR"
shopify app dev &
SHOPIFY_PID=$!

echo ""
echo "============================================================"
echo "✅ All services running!"
echo "   Backend     : http://localhost:$BACKEND_PORT"
echo "   Tunnel      : $TUNNEL_URL"
echo "   Shopify ext : http://127.0.0.1:9293"
echo "   Logs        : $LOG_DIR/"
echo "============================================================"
echo ""
echo "Press Ctrl+C to stop all services."

# Keep alive — wait for shopify app dev (interactive)
wait $SHOPIFY_PID
