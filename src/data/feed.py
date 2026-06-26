"""
Kraken Futures public data feed.
Fetches OHLCV candles via REST — no authentication needed for market data.
"""
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from loguru import logger

import config


@dataclass
class Bar:
    timestamp: datetime   # UTC, bar open time
    open: float
    high: float
    low: float
    close: float
    volume: float


class KrakenFeed:
    """Public REST client for OHLCV and ticker data."""

    def __init__(self) -> None:
        self._base = config.KRAKEN_BASE_URL
        self._chart_path = config.KRAKEN_CHART_PATH
        self._tick = config.CHART_TICK_TYPE
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ──────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────

    def fetch_ohlcv(
        self,
        symbol: str,
        resolution: str = "1m",
        count: int = 300,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> list[Bar]:
        """Return up to `count` closed 1-min bars, newest last."""
        url = f"{self._base}{self._chart_path}/{self._tick}/{symbol}/{resolution}"
        params: dict = {}
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts
        if not from_ts and not to_ts:
            params["count"] = count

        data = self._get(url, params)
        if not data:
            return []

        candles_raw = data.get("candles") or data.get("result", {})
        if isinstance(candles_raw, dict):
            candles_raw = candles_raw.get("candles", [])
        if not candles_raw:
            logger.warning(f"Empty candles in response for {symbol}")
            return []

        bars: list[Bar] = []
        for c in candles_raw:
            if isinstance(c, (list, tuple)):
                ts_ms, o, h, l, cl, vol = c[0], c[1], c[2], c[3], c[4], c[5]
            else:
                ts_ms = c.get("time") or c.get("timestamp")
                o  = c.get("open",   c.get("o"))
                h  = c.get("high",   c.get("h"))
                l  = c.get("low",    c.get("l"))
                cl = c.get("close",  c.get("c"))
                vol = c.get("volume", c.get("v", 0))

            bars.append(Bar(
                timestamp=datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(cl),
                volume=float(vol),
            ))

        bars.sort(key=lambda b: b.timestamp)
        return bars

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Best-bid/ask mid price from the ticker endpoint."""
        url = f"{self._base}{config.KRAKEN_REST_PATH}/tickers/{symbol}"
        data = self._get(url, {})
        if not data:
            return None
        ticker = data.get("ticker") or {}
        bid = ticker.get("bid") or ticker.get("bestBid")
        ask = ticker.get("ask") or ticker.get("bestAsk")
        last = ticker.get("last") or ticker.get("markPrice")
        if bid and ask:
            return (float(bid) + float(ask)) / 2
        if last:
            return float(last)
        return None

    # ──────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────

    def _get(self, url: str, params: dict) -> dict:
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                r = self._session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_S)
                r.raise_for_status()
                data = r.json()
                if data.get("result") not in ("success", None) and "error" in data:
                    logger.error(f"API error from {url}: {data}")
                    return {}
                return data
            except requests.exceptions.RequestException as exc:
                logger.warning(f"GET {url} attempt {attempt} failed: {exc}")
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_DELAY_S * attempt)
        logger.error(f"All retries exhausted for GET {url}")
        return {}
