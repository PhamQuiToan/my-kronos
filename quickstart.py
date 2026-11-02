"""Run a small Kronos forecast using the sample data bundled with the repo."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import torch

from model import Kronos, KronosPredictor, KronosTokenizer


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "tests" / "data" / "regression_input.csv"
DEFAULT_OUTPUT = ROOT / "prediction_result.csv"
FEATURES = ["open", "high", "low", "close", "volume", "amount"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Kronos sample forecast.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--lookback", type=int, default=256)
    parser.add_argument("--pred-len", type=int, default=8)
    parser.add_argument("--device", choices=("auto", "cpu", "mps"), default="auto")
    return parser.parse_args()


def resolve_device(requested: str) -> str:
    if requested == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if requested == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("MPS was requested but is not available on this machine.")
    return requested


def main() -> None:
    args = parse_args()
    device = resolve_device(args.device)

    df = pd.read_csv(args.input, parse_dates=["timestamps"])
    required_rows = args.lookback + args.pred_len
    if len(df) < required_rows:
        raise ValueError(
            f"Input needs at least {required_rows} rows, but {args.input} has {len(df)}."
        )

    missing = [column for column in FEATURES if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    history = df.iloc[: args.lookback]
    future = df.iloc[args.lookback : required_rows]

    print(f"Loading Kronos-small on {device}...")
    tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
    model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
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
            df=history[FEATURES].reset_index(drop=True),
            x_timestamp=history["timestamps"].reset_index(drop=True),
            y_timestamp=future["timestamps"].reset_index(drop=True),
            pred_len=args.pred_len,
            T=1.0,
            top_p=0.9,
            sample_count=1,
            verbose=True,
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    prediction.to_csv(args.output, index_label="timestamps")

    print("\nForecast:")
    print(prediction)
    print(f"\nSaved to: {args.output.resolve()}")


if __name__ == "__main__":
    main()
