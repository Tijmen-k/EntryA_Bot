# Entry A — Bitget Futures Trading Bot

Algorithmic implementation of the **Entry A** liquidity-sweep fade strategy on Bitget Futures (`ETHUSD`).

## Quick start

### 1. Clone & configure
```bash
git clone https://github.com/YOUR_USERNAME/entry-a-bot.git
cd entry-a-bot
cp .env.example .env
# Edit .env — add your Bitget API keys and set TRADING_MODE=demo
```

### 2. Get Bitget demo credentials
- Register at https://www.bitget.com → Demo Trading → API Management
- Create an API key (needs: Trading + Account permissions)
- Paste the key, secret, and passphrase into `.env`

### 3. Run locally (Python)
```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Dry-run (reads live data, places no orders):
```bash
python main.py --dry-run
```

### 4. Deploy to VPS (Docker)
```bash
# On your VPS
git clone https://github.com/YOUR_USERNAME/entry-a-bot.git
cd entry-a-bot
cp .env.example .env && nano .env   # fill in credentials
docker-compose up -d
docker-compose logs -f              # tail live logs
```

## Configuration

All settings live in `.env` (copy from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `demo` | `demo` or `live` — selects the matching `BITGET_DEMO_*`/`BITGET_LIVE_*` key set |
| `BITGET_DEMO_API_KEY` / `_SECRET` / `_PASSPHRASE` | — | From Bitget demo trading account |
| `BITGET_LIVE_API_KEY` / `_SECRET` / `_PASSPHRASE` | — | From Bitget live account |
| `SYMBOL` | `ETHUSD` | Instrument (coin-M inverse perpetual) |
| `RISK_PER_TRADE_PCT` | `0.01` | 1% of account balance per trade |
| `LEVERAGE` | `5.0` | Position leverage (5x recommended to start) |
| `DRY_RUN` | `false` | `true` = no real orders |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose output |

## Project structure

```
entry-a-bot/
├── main.py              # Bot orchestrator & main loop
├── config.py            # All configuration (reads .env)
├── src/
│   ├── data/feed.py     # Bitget public REST — OHLCV & ticker
│   ├── broker/bitget.py # Authenticated REST — orders, positions, account
│   ├── strategy/entry_a.py  # Entry A state machine + bias calculator
│   ├── risk/sizing.py   # Risk-based position sizing
│   └── utils/logger.py  # Loguru setup with file rotation
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## VPS deployment checklist

- [ ] VPS (1 CPU / 1 GB RAM is plenty)
- [ ] Docker + Docker Compose installed
- [ ] Port 22 open for SSH only
- [ ] `.env` configured with correct API keys
- [ ] `TRADING_MODE=demo` for initial validation
- [ ] Run `--dry-run` for one full day to confirm logic fires correctly
- [ ] Switch to `TRADING_MODE=live` only after demo validation passes


