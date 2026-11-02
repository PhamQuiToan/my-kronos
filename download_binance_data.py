"""Download recent Binance Spot candlesticks as a Kronos-compatible CSV."""

from __future__ import annotations

import argparse
import json
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd
import certifi


ROOT = Path(__file__).resolve().parent
API_URL = "https://data-api.binance.vision/api/v3/klines"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "btcusdt_15m.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 1 <= args.limit <= 1000:
        raise ValueError("--limit must be between 1 and 1000 for Binance Spot klines.")

    query = urlencode(
        {"symbol": args.symbol.upper(), "interval": args.interval, "limit": args.limit}
    )
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(f"{API_URL}?{query}", timeout=30, context=ssl_context) as response:
        payload = json.load(response)

    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Binance response: {payload}")

    rows = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for candle in payload:
        # Ignore the currently forming candle so every saved row is complete.
        if int(candle[6]) >= now_ms:
            continue
        rows.append(
            {
                "timestamps": pd.to_datetime(int(candle[0]), unit="ms", utc=True),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]),
                "amount": float(candle[7]),
            }
        )

    if not rows:
        raise RuntimeError("Binance returned no completed candles.")

    df = pd.DataFrame(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)

    print(f"Saved {len(df)} completed {args.interval} candles for {args.symbol.upper()}.")
    print(f"Range: {df['timestamps'].iloc[0]} -> {df['timestamps'].iloc[-1]}")
    print(f"Latest close: {df['close'].iloc[-1]:.8f} USDT")
    print(f"Output: {args.output.resolve()}")


if __name__ == "__main__":
    main()
