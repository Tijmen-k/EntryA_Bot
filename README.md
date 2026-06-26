# Entry A — Kraken Futures Trading Bot

Algorithmic implementation of the **Entry A** liquidity-sweep fade strategy on Kraken Futures (`PF_ETHUSD`).

## Strategy summary

- **Sessions:** London 09:00–14:00 UTC (ORB 09:00–09:30), NY 14:00–23:00 UTC (ORB 14:00–14:30)
- **Active days:** Tuesday, Wednesday, Thursday only
- **Signal:** Price sweeps outside the Opening Range, then closes back inside → enter fade trade
- **Bias filter:** Only trade direction aligned with intraday bias (vs yesterday's open)
- **SL:** 0.7% hard stop from entry
- **TP:** Measured move — previous day's range projected from ORB boundary
- **No entry:** final 60 min before session close

## Quick start

### 1. Clone & configure
```bash
git clone https://github.com/YOUR_USERNAME/entry-a-bot.git
cd entry-a-bot
cp .env.example .env
# Edit .env — add your Kraken API keys and set TRADING_MODE=demo
```

### 2. Get Kraken demo credentials
- Register at https://demo-futures.kraken.com
- Create an API key under Settings → API (needs: Trading + Account permissions)
- Paste key and secret into `.env`

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
| `TRADING_MODE` | `demo` | `demo` or `live` |
| `KRAKEN_API_KEY` | — | From Kraken Futures account |
| `KRAKEN_API_SECRET` | — | From Kraken Futures account |
| `SYMBOL` | `PF_ETHUSD` | Instrument (linear ETH perpetual) |
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
│   ├── data/feed.py     # Kraken public REST — OHLCV & ticker
│   ├── broker/kraken.py # Authenticated REST — orders, positions, account
│   ├── strategy/entry_a.py  # Entry A state machine + bias calculator
│   ├── risk/sizing.py   # Risk-based position sizing
│   └── utils/logger.py  # Loguru setup with file rotation
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## VPS deployment checklist

- [ ] Ubuntu 22.04 LTS VPS (1 CPU / 1 GB RAM is plenty)
- [ ] Docker + Docker Compose installed
- [ ] Port 22 open for SSH only
- [ ] `.env` configured with correct API keys
- [ ] `TRADING_MODE=demo` for initial validation
- [ ] Run `--dry-run` for one full day to confirm logic fires correctly
- [ ] Switch to `TRADING_MODE=live` only after demo validation passes

## Risk warnings

This software is provided for educational and research purposes. Trading futures carries significant risk of loss. Always:
- Start on demo with `TRADING_MODE=demo`
- Validate with `DRY_RUN=true` before enabling real orders
- Use only capital you can afford to lose entirely
- Monitor positions and logs daily
