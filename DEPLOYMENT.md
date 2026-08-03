# VPS Deployment Checklist

Operational steps to run through before and during the first deploy of
EntryA-Bot to a VPS. This is a checklist, not automation — work through it
top to bottom.

## 1. Prerequisites

- A domain name with an A record pointing at the VPS's IP.
- Docker + Docker Compose installed (runs `main.py`, the trading engine).
- Python 3.11 + venv installed (runs the Streamlit dashboard directly on the
  host — see `deploy/entrya-dashboard.service`).
- Caddy installed from its official apt repo (handles TLS termination).

## 2. SSH hardening

In `/etc/ssh/sshd_config`:

```
PasswordAuthentication no
PermitRootLogin no
```

Then `systemctl restart sshd`. Make sure your SSH key already works before
disabling password auth, or you'll lock yourself out.

## 3. Firewall (ufw)

```
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp    # required for Caddy's ACME HTTP-01 challenge/renewal
ufw allow 443/tcp
ufw enable
```

Do **not** open 8501/tcp externally — the dashboard binds to `127.0.0.1`
only (`.streamlit/config.toml`) and is reached exclusively through Caddy.

## 4. Bitget API key permissions

Manually verify in Bitget → API Management that the **live** key has only
**Trade** + **Read** permissions, with withdrawal/transfer OFF. The bot's
code (`src/broker/bitget.py`) never calls withdrawal endpoints, so this
should already be safe to leave disabled — just confirm it on Bitget's side
too, since that's the layer that actually enforces it.

## 5. Dashboard login setup

```
python scripts/generate_login_secret.py
```

Paste the two printed lines (`DASHBOARD_PASSWORD_SALT`, `DASHBOARD_PASSWORD_HASH`)
into the VPS's `.env`.

## 6. Rotate the leaked Discord webhook

`.env.example` previously had a real webhook URL committed to git history
(fixed going forward, but the old URL is still exposed in past commits).
Regenerate the webhook in Discord (Channel Settings → Integrations →
Webhooks) and put the new URL only in the untracked `.env` on the VPS.

## 7. Start services

```
docker-compose up -d                              # main.py trading engine (restart: unless-stopped)
systemctl daemon-reload
systemctl enable --now entrya-dashboard           # Streamlit, from deploy/entrya-dashboard.service
systemctl enable --now caddy                      # reverse proxy, from deploy/Caddyfile copied to /etc/caddy/Caddyfile
```

Known gap: `discord_bot/` still has no service definition and is expected
to be started manually (`python -m discord_bot`) — not addressed here.

## 8. Backups

Periodically back up (cron + off-box copy, mechanism is up to you):
- `state/state.json`
- `discord_bot/data/bot.db` (path from `DISCORD_BOT_DB_PATH` in `.env`)

## 9. Go-live smoke test

- [ ] `http://<vps-ip>:8501` from an external machine fails/times out (confirms loopback-only bind).
- [ ] `https://your-domain.example.com` serves the dashboard with a valid cert.
- [ ] Loading the dashboard shows the login form; wrong password is rejected.
- [ ] Correct password logs in; "Log out" button returns to the login form.
- [ ] `TRADING_MODE=demo` end-to-end (bot start/stop, order placement) verified working before ever switching to `live`.
