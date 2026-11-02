"""Run M15 forecasts for GOLD and BTC through tomorrow."""

from __future__ import annotations

import os
import secrets
import ssl
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import certifi


ROOT = Path(__file__).resolve().parent
TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
UPLOAD_URL = "https://matsea.metatechsolutions.vn/api/forecast/upload"
UPLOAD_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
ASSETS = {
    "GOLD": "paxgusdt",
    "BTC": "btcusdt",
}


def upload_forecast(file_path: Path, category: str, api_key: str) -> None:
    boundary = f"----KronosForecast{secrets.token_hex(16)}"
    body = b"".join(
        (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="category"\r\n\r\n'
                f"{category}\r\n"
            ).encode(),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
                "Content-Type: text/csv\r\n\r\n"
            ).encode(),
            file_path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        )
    )
    request = Request(
        UPLOAD_URL,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-api-key": api_key,
        },
        method="POST",
    )
    with urlopen(request, timeout=60, context=UPLOAD_SSL_CONTEXT) as response:
        response_body = response.read().decode("utf-8", errors="replace")
        print(
            f"Uploaded {category} forecast (HTTP {response.status}).",
            flush=True,
        )
        if response_body:
            print(response_body)


def main() -> None:
    api_key = os.environ.get("FORECAST_UPLOAD_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FORECAST_UPLOAD_API_KEY environment variable is required to upload forecasts."
        )

    target_date = datetime.now(TIMEZONE).date() + timedelta(days=1)
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
        print(f"\nRunning {asset} forecast for {target_date_text}...", flush=True)
        subprocess.run(command, cwd=ROOT, check=True)

        output_path = (
            ROOT
            / "forecast_result"
            / target_date_iso
            / f"future_prediction_{symbol_slug}_{target_date_iso}_15m.csv"
        )
        if not output_path.is_file():
            raise FileNotFoundError(
                f"Forecast completed but expected CSV was not found: {output_path}"
            )
        upload_forecast(output_path, asset, api_key)


if __name__ == "__main__":
    main()
