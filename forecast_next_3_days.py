"""Run and upload one three-day M15 forecast each for GOLD and BTC."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta

from forecast_tomorrow import ASSETS, ROOT, TIMEZONE, upload_forecast


def main() -> None:
    api_key = os.environ.get("FORECAST_UPLOAD_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FORECAST_UPLOAD_API_KEY environment variable is required to upload forecasts."
        )

    target_date = datetime.now(TIMEZONE).date() + timedelta(days=3)
    target_date_text = target_date.strftime("%d/%m/%Y")
    target_date_iso = target_date.isoformat()

    for asset, symbol_slug in ASSETS.items():
        command = [
            sys.executable,
            str(ROOT / "forecast_by_date.py"),
            target_date_text,
            "M15",
            asset,
        ]
        print(
            f"\nRunning {asset} forecast through {target_date_text}...",
            flush=True,
        )
        subprocess.run(command, cwd=ROOT, check=True)

        output_path = (
            ROOT
            / "forecast_result"
            / target_date_iso
            / f"future_prediction_{symbol_slug}_{target_date_iso}_15m.csv"
        )
        if not output_path.is_file():
            raise FileNotFoundError(
                "Forecast completed but expected CSV was not found: "
                f"{output_path}"
            )
        upload_forecast(output_path, asset, api_key)


if __name__ == "__main__":
    main()
