"""Forecast candles after the last timestamp in a market-data CSV file."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import torch

from model import Kronos, KronosPredictor, KronosTokenizer


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data" / "market.csv"
DEFAULT_OUTPUT = ROOT / "future_prediction.csv"
PRICE_COLUMNS = ["open", "high", "low", "close"]
MODEL_COLUMNS = [*PRICE_COLUMNS, "volume", "amount"]
MODEL_ID = "NeoQuasar/Kronos-base"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use the latest candles in a CSV file to forecast future candles."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--lookback", type=int, default=400)
    parser.add_argument("--pred-len", type=int, default=24)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--device", choices=("auto", "cpu", "mps"), default="auto")
    parser.add_argument(
        "--frequency",
        help="Optional pandas frequency such as 15min, 1h, or 1D. By default it is inferred.",
    )
    return parser.parse_args()


def resolve_device(requested: str) -> str:
    if requested == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if requested == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS was requested but is not available. Use --device cpu.")
    return requested


def load_history(path: Path, lookback: int) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Input file does not exist: {path.resolve()}")

    df = pd.read_csv(path)
    timestamp_column = next(
        (name for name in ("timestamps", "timestamp", "date") if name in df.columns),
        None,
    )
    if timestamp_column is None:
        raise ValueError("CSV needs a timestamps, timestamp, or date column.")

    if timestamp_column != "timestamps":
        df = df.rename(columns={timestamp_column: "timestamps"})
    df["timestamps"] = pd.to_datetime(df["timestamps"], errors="raise")
    df = df.sort_values("timestamps").drop_duplicates("timestamps").reset_index(drop=True)

    missing = [column for column in PRICE_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    for column in PRICE_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="raise")
    for column in ("volume", "amount"):
        if column not in df.columns:
            df[column] = 0.0
        else:
            df[column] = pd.to_numeric(df[column], errors="raise").fillna(0.0)

    if len(df) < lookback:
        raise ValueError(f"Need at least {lookback} rows, but the CSV has {len(df)}.")
    return df.iloc[-lookback:].copy()


def make_future_timestamps(
    history: pd.DataFrame, pred_len: int, frequency: str | None
) -> pd.Series:
    if frequency:
        interval = pd.tseries.frequencies.to_offset(frequency)
    else:
        differences = history["timestamps"].diff().dropna()
        if differences.empty:
            raise ValueError("At least two timestamps are required to infer frequency.")
        interval = differences.mode().iloc[0]

    last_timestamp = history["timestamps"].iloc[-1]
    values = pd.date_range(
        start=last_timestamp + interval,
        periods=pred_len,
        freq=interval,
    )
    return pd.Series(values, name="timestamps")


def main() -> None:
    args = parse_args()
    if args.lookback < 1 or args.lookback > 512:
        raise ValueError("--lookback must be between 1 and 512.")
    if args.pred_len < 1:
        raise ValueError("--pred-len must be at least 1.")
    if args.samples < 1:
        raise ValueError("--samples must be at least 1.")

    history = load_history(args.input, args.lookback)
    future_timestamps = make_future_timestamps(history, args.pred_len, args.frequency)
    device = resolve_device(args.device)

    print(f"Input: {args.input.resolve()}")
    print(f"Device: {device}")
    print(f"History: {history['timestamps'].iloc[0]} -> {history['timestamps'].iloc[-1]}")
    print(f"Forecast: {future_timestamps.iloc[0]} -> {future_timestamps.iloc[-1]}")
    print(f"Loading {MODEL_ID}...")

    tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_ID)
    model = Kronos.from_pretrained(MODEL_ID)
    tokenizer.eval()
    model.eval()

    predictor = KronosPredictor(
        model=model,
        tokenizer=tokenizer,
        device=device,
        max_context=512,
    )

    with torch.no_grad():
        prediction = predictor.predict(
            df=history[MODEL_COLUMNS].reset_index(drop=True),
            x_timestamp=history["timestamps"].reset_index(drop=True),
            y_timestamp=future_timestamps,
            pred_len=args.pred_len,
            T=args.temperature,
            top_p=args.top_p,
            sample_count=args.samples,
            verbose=True,
        )

    prediction.index = pd.DatetimeIndex(future_timestamps, name="timestamps")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    prediction.to_csv(args.output)

    print("\nFuture forecast:")
    print(prediction)
    print(f"\nSaved to: {args.output.resolve()}")
    print(
        "Note: generated timestamps are continuous and do not exclude market breaks, "
        "weekends, or holidays."
    )


if __name__ == "__main__":
    main()
