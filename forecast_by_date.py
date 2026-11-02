"""Download Binance data and run Kronos through the end of a target date."""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parent
DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
LOOKBACK = 400
RESULT_RETENTION_DAYS = 5

ASSETS = {
    "BTC": {
        "symbol": "BTCUSDT",
        "name": "Bitcoin / Tether",
    },
    "GOLD": {
        "symbol": "PAXGUSDT",
        "name": "PAX Gold / Tether (Binance gold proxy)",
    },
    "VANG": {
        "symbol": "PAXGUSDT",
        "name": "PAX Gold / Tether (Binance gold proxy)",
    },
}

# User-facing candle code: (Binance interval, pandas frequency, duration)
CANDLES = {
    "M1": ("1m", "1min", timedelta(minutes=1)),
    "M3": ("3m", "3min", timedelta(minutes=3)),
    "M5": ("5m", "5min", timedelta(minutes=5)),
    "M15": ("15m", "15min", timedelta(minutes=15)),
    "M30": ("30m", "30min", timedelta(minutes=30)),
    "H1": ("1h", "1h", timedelta(hours=1)),
    "H2": ("2h", "2h", timedelta(hours=2)),
    "H4": ("4h", "4h", timedelta(hours=4)),
    "H6": ("6h", "6h", timedelta(hours=6)),
    "H8": ("8h", "8h", timedelta(hours=8)),
    "H12": ("12h", "12h", timedelta(hours=12)),
    "D1": ("1d", "1D", timedelta(days=1)),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download recent Binance candles and forecast from the latest completed "
            "candle through the end of a target date."
        )
    )
    parser.add_argument(
        "date",
        nargs="?",
        help="Target date in DD/MM/YYYY format, for example 27/10/2026.",
    )
    parser.add_argument(
        "candle",
        nargs="?",
        help=f"Candle code: {', '.join(CANDLES)}.",
    )
    parser.add_argument(
        "asset",
        nargs="?",
        help="Asset to forecast: BTC or GOLD (VANG is also accepted).",
    )
    parser.add_argument(
        "--symbol",
        help="Advanced override for a Binance Spot symbol, for example ETHUSDT.",
    )
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--device", choices=("auto", "cpu", "mps"), default="cpu")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=0.9)
    return parser.parse_args()


def get_required_value(value: str | None, prompt: str) -> str:
    if value:
        return value.strip()
    return input(prompt).strip()


def run(command: list[str]) -> None:
    print("\n$", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def prune_old_forecast_results(results_root: Path) -> None:
    dated_directories: list[tuple[date, Path]] = []
    if not results_root.exists():
        return

    for path in results_root.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            folder_date = date.fromisoformat(path.name)
        except ValueError:
            continue
        if folder_date.isoformat() == path.name:
            dated_directories.append((folder_date, path))

    dated_directories.sort(key=lambda item: item[0], reverse=True)
    for _, old_directory in dated_directories[RESULT_RETENTION_DAYS:]:
        shutil.rmtree(old_directory)


def main() -> None:
    args = parse_args()
    date_text = get_required_value(args.date, "Ngày muốn dự báo (DD/MM/YYYY): ")
    candle_code = get_required_value(args.candle, "Khung nến (ví dụ M15): ").upper()
    asset_code = (
        (args.asset or "CUSTOM").upper()
        if args.symbol
        else get_required_value(args.asset, "Tài sản (BTC hoặc GOLD/VANG): ").upper()
    )

    try:
        target_date = datetime.strptime(date_text, "%d/%m/%Y").date()
    except ValueError as exc:
        raise ValueError("Ngày phải có định dạng DD/MM/YYYY, ví dụ 27/10/2026.") from exc

    if candle_code not in CANDLES:
        raise ValueError(
            f"Khung nến không hợp lệ: {candle_code}. Hỗ trợ: {', '.join(CANDLES)}"
        )
    if not args.symbol and asset_code not in ASSETS:
        raise ValueError("Tài sản không hợp lệ. Hãy chọn BTC hoặc GOLD/VANG.")
    if args.samples < 1:
        raise ValueError("--samples must be at least 1.")

    binance_interval, pandas_frequency, candle_duration = CANDLES[candle_code]
    asset_config = ASSETS.get(asset_code)
    symbol = args.symbol.upper() if args.symbol else asset_config["symbol"]
    asset_name = (
        f"Custom Binance symbol {symbol}" if args.symbol else asset_config["name"]
    )
    symbol_slug = symbol.lower()
    data_path = ROOT / "data" / f"{symbol_slug}_{binance_interval}.csv"
    results_root = ROOT / "forecast_result"
    result_directory = results_root / target_date.isoformat()
    output_path = result_directory / (
        f"future_prediction_{symbol_slug}_{target_date.isoformat()}_{binance_interval}.csv"
    )

    run(
        [
            sys.executable,
            "download_binance_data.py",
            "--symbol",
            symbol,
            "--interval",
            binance_interval,
            "--limit",
            "1000",
            "--output",
            str(data_path),
        ]
    )

    df = pd.read_csv(data_path, usecols=["timestamps"])
    latest_utc = pd.to_datetime(df["timestamps"].iloc[-1], utc=True)
    local_timezone = ZoneInfo(args.timezone)
    latest_local = latest_utc.tz_convert(local_timezone)

    next_midnight = datetime.combine(
        target_date + timedelta(days=1), time.min, tzinfo=local_timezone
    )
    target_last_candle = next_midnight - candle_duration
    remaining_seconds = (target_last_candle - latest_local).total_seconds()
    if remaining_seconds < candle_duration.total_seconds():
        raise ValueError(
            f"Ngày {date_text} không nằm sau nến hoàn chỉnh mới nhất "
            f"({latest_local.strftime('%d/%m/%Y %H:%M %Z')})."
        )

    pred_len = math.floor(remaining_seconds / candle_duration.total_seconds())
    if pred_len > 1000:
        raise ValueError(
            f"Cần dự báo {pred_len} nến, quá xa để chạy an toàn. "
            "Hãy chọn ngày gần hơn hoặc khung nến lớn hơn."
        )

    print("\nKế hoạch dự báo:")
    print(f"  Tài sản: {asset_name}")
    print(f"  Cặp Binance: {symbol}")
    print(f"  Nến: {candle_code} ({binance_interval})")
    print(f"  Nến hoàn chỉnh mới nhất: {latest_local.strftime('%d/%m/%Y %H:%M %Z')}")
    print(f"  Dự báo đến: {target_last_candle.strftime('%d/%m/%Y %H:%M %Z')}")
    print(f"  Số nến tương lai: {pred_len}")

    run(
        [
            sys.executable,
            "future_forecast.py",
            "--input",
            str(data_path),
            "--output",
            str(output_path),
            "--lookback",
            str(LOOKBACK),
            "--pred-len",
            str(pred_len),
            "--samples",
            str(args.samples),
            "--temperature",
            str(args.temperature),
            "--top-p",
            str(args.top_p),
            "--frequency",
            pandas_frequency,
            "--device",
            args.device,
        ]
    )
    prune_old_forecast_results(results_root)

    print("\nHoàn tất.")
    print(f"Dữ liệu Binance: {data_path}")
    print(f"Kết quả Kronos: {output_path}")


if __name__ == "__main__":
    main()
