"""Test-only server: LIVE mode (no demo flag) with a mock PriceCharting transport.

Used by the browser tests so records arrive exactly as in production (demo=False,
no watermark) without paid API calls or a running OBS.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402

from gameoverlay.config import AppConfig, Environment  # noqa: E402
from gameoverlay.demo.mock_pricecharting import DEMO_TOKEN, MockPriceCharting  # noqa: E402
from gameoverlay.logging_setup import setup_logging  # noqa: E402
from gameoverlay.server import create_app  # noqa: E402
from gameoverlay.services import Services  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data", required=True)
    a = ap.parse_args()
    os.environ["PRICECHARTING_TOKEN"] = DEMO_TOKEN
    setup_logging("WARNING")
    cfg = AppConfig(env=Environment.TEST, data_dir=Path(a.data), port=a.port)
    svc = Services(cfg, pc_transport=MockPriceCharting().transport())
    # No OBS in the test environment: point at a closed port; the client just keeps retrying.
    svc.update_settings({"obs": {"port": 1}})
    uvicorn.run(create_app(cfg, svc), host="127.0.0.1", port=a.port, log_config=None, log_level="warning")


if __name__ == "__main__":
    main()
