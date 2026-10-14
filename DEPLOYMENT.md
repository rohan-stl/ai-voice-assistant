# AI Shopping Assistant — Staging Deployment Guide

How to put the app on a **staging server** with a fixed HTTPS URL, instead of the local dev setup (Cloudflare quick tunnel + `shopify app dev`). This guide is for staging only; production hardening is listed at the end as a separate, later step.

## 1. Architecture

| Part | What it is | Where it runs |
|---|---|---|
| Backend | FastAPI app (`main.py`), served by uvicorn on port 8000 | Your server |
| Storefront widget | Theme app extension under `frontend/ai-shopping-assistant` | Shopify (published with `shopify app deploy`) |
| App proxy | Shopify forwards `/apps/ai-assistant/*` on the storefront to the backend | Shopify to your server |
| State | `sessions.sqlite`, `store_credentials.sqlite` | Server disk |
| External APIs | Shopify (Admin + Storefront), OpenRouter (LLM), ElevenLabs (voice) | Internet |

In development `start.sh` starts a quick tunnel whose URL changes every run. Staging needs a fixed HTTPS domain instead, so Shopify can always reach the backend.

## 2. Prerequisites

**Server**
- Linux VPS or VM (Ubuntu 22.04+), 1 vCPU / 1–2 GB RAM minimum, or a PaaS (Render, Railway, Fly.io).
- A domain or subdomain (for example `staging-assistant.example.com`) with a DNS A record pointing to the server.
- HTTPS certificate (Let's Encrypt). Shopify requires HTTPS for the app and the app proxy.
- Python 3.11, nginx (or Caddy), systemd, git.

**Accounts and credentials**

| Variable | Purpose |
|---|---|
| `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` | App credentials from the Shopify Partner dashboard |
| `SHOPIFY_STORE_DOMAIN` | Store domain, for example `your-store.myshopify.com` |
| `SHOPIFY_STOREFRONT_TOKEN` | Storefront API token (add to cart / checkout) |
| `SHOPIFY_ADMIN_TOKEN` | Admin API token (draft orders) |
| `OPENROUTER_API_KEY` | LLM requests |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | Text-to-speech |
| `ALLOW_SINGLE_STORE_ENV_FALLBACK` | Leave `true` on staging (one demo store using the tokens above) |
| `APP_ENV` | Set `staging` (any value except `production` keeps localhost CORS origins for testing) |
| `ALLOWED_ORIGINS` | Optional extra CORS origins, e.g. a custom storefront domain |

Create a clean `.env` on the server (copy `.env.example`) with one key per service. Do not copy the local `.env`, which contains several commented-out personal keys. Use a **dev/demo store** for staging, never a live merchant store.

**Local machine:** Shopify CLI (`npm i -g @shopify/cli`) and access to the Partner dashboard.

**Recommended:** create a separate **staging app** in the Partner dashboard (own `client_id` / secret), so staging never fights with the dev tunnel app over URLs.

## 3. Staging checklist

Already done in the repo:
- [x] **CORS restricted** (`main.py`): only `*.myshopify.com`, `admin.shopify.com`, your `SHOPIFY_STORE_DOMAIN` and anything in `ALLOWED_ORIGINS` (comma-separated) can call the API from a browser. Localhost stays allowed unless `APP_ENV=production`.
- [x] **Runtime DB files untracked**: `store_credentials.sqlite-shm` / `-wal` removed from git, `.gitignore` covers `*.sqlite*`. Commit that change.
- [x] **`.env` is git-ignored**; `.env.example` lists every variable.

Do on the server:
- [ ] Run **without `--reload`** (the systemd unit below does). `start.sh` is dev-only; do not use it on the server.
- [ ] Run the tests before each deploy: `venv/bin/python -m pytest tests -q`
- [ ] Keep `.env` private: `chmod 600 .env`.

Not needed for staging (do before a real production launch): Postgres instead of SQLite, scheduled backups, `ALLOW_SINGLE_STORE_ENV_FALLBACK=false`, `APP_ENV=production`, OAuth install flow for other merchants, and **rotating the Shopify client secret** (a secret is hardcoded as a default in `shopify_service.py` and `get_shopify_token.py`, tracked in git — rotate it before the repo is shared more widely).

## 4. Deploy the backend (Ubuntu VPS)

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv nginx certbot python3-certbot-nginx git

sudo git clone <repo-url> /opt/ai-assistant
cd /opt/ai-assistant
sudo python3.11 -m venv venv
sudo venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env   # staging values (section 2)
sudo chown -R www-data:www-data /opt/ai-assistant
```

**systemd service** — `/etc/systemd/system/ai-assistant.service`:

```ini
[Unit]
Description=AI Shopping Assistant
After=network.target

[Service]
WorkingDirectory=/opt/ai-assistant
EnvironmentFile=/opt/ai-assistant/.env
ExecStart=/opt/ai-assistant/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-assistant
sudo systemctl status ai-assistant
```

**nginx** — `/etc/nginx/sites-available/ai-assistant`:

```nginx
server {
    server_name staging-assistant.example.com;

    client_max_body_size 20M;          # voice uploads

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;       # LLM + TTS can be slow
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/ai-assistant /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d staging-assistant.example.com
```

## 5. Point Shopify at the staging server

Create a staging config next to the dev one, `frontend/ai-shopping-assistant/shopify.app.staging.toml` (generate it with `shopify app config link` and choose the staging app), then set:

```toml
client_id = "<staging app client id>"
application_url = "https://staging-assistant.example.com"

[build]
automatically_update_urls_on_dev = false   # never let `shopify app dev` rewrite staging URLs

[app_proxy]
url = "https://staging-assistant.example.com"
subpath = "ai-assistant"
prefix = "apps"
```

Publish it from your local machine:

```bash
cd frontend/ai-shopping-assistant
shopify app deploy --config staging
```

Install the staging app on the dev/demo store, and enable the app embed in the theme editor if the widget does not appear. Put the staging app's client id and secret in the server `.env`.

Local development keeps using `./start.sh` with the default `shopify.app.toml`; the two never share URLs.

## 6. Verify

- [ ] `curl -I https://staging-assistant.example.com/` returns 200.
- [ ] The widget appears on the storefront.
- [ ] A voice or text request reaches `/voice-text` (`journalctl -u ai-assistant -f`).
- [ ] Search, variant selection, add to cart and checkout work end to end.
- [ ] Voice features work (the browser requires HTTPS and microphone permission).
- [ ] Restarting the service keeps sessions (`sessions.sqlite` persists).
- [ ] Staging URL is not the dev tunnel URL anywhere in `shopify.app.staging.toml`.

## 7. Operations

**Update**
```bash
cd /opt/ai-assistant && sudo git pull
sudo venv/bin/pip install -r requirements.txt
sudo systemctl restart ai-assistant
```

**Logs:** `journalctl -u ai-assistant -f` and `/var/log/nginx/error.log`.

**Data:** `sessions.sqlite` and `store_credentials.sqlite` live in the app folder. Staging data can be reset by deleting them (`store_credentials.sqlite` holds store tokens, so keep it private).

**Secrets:** rotate keys if they were ever shared or committed.

## 8. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Widget loads but requests fail | `[app_proxy] url` not updated, or `shopify app deploy --config staging` not run |
| 502 from nginx | Service not running — `systemctl status ai-assistant` |
| Signature / auth errors on `/voice-text` | Wrong `SHOPIFY_CLIENT_SECRET`, or server clock out of sync |
| CORS errors in the browser console | Origin not allowed — add it to `ALLOWED_ORIGINS` in `.env` and restart |
| No audio replies | `ELEVENLABS_API_KEY` or `ELEVENLABS_VOICE_ID` missing or out of quota |
| LLM answers fail | `OPENROUTER_API_KEY` invalid or out of credit |
