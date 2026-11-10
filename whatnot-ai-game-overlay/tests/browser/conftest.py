from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SHOT_DIR = Path(__file__).parent / "__screenshots__" / "actual"

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FixtureServer:
    def __init__(self, data_dir: Path, port: int | None = None) -> None:
        self.data_dir = data_dir
        self.port = port or _free_port()
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FixtureServer":
        env = {**os.environ, "WGO_DISABLE_KEYRING": "true"}
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "browser" / "serve_fixture.py"), "--port", str(self.port), "--data", str(self.data_dir)],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(self.base + "/health", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(self.proc.stderr.read().decode())
            time.sleep(0.15)
        raise RuntimeError("fixture server did not start")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def token(self) -> str:
        return (self.data_dir / "admin_token").read_text().strip()

    def api(self, method: str, path: str, json=None):
        r = httpx.request(method, self.base + path, json=json, headers={"Authorization": f"Bearer {self.token}"}, timeout=10)
        r.raise_for_status()
        return r.json()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    srv = FixtureServer(tmp_path_factory.mktemp("browser-data")).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def browser():
    # Module scope: Playwright's sync API runs its own event loop; it must be shut down
    # before pytest-asyncio tests in other modules run in the same process.
    if sync_playwright is None:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        exe = os.environ.get("WGO_CHROMIUM")
        try:
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception:
            fallback = "/opt/pw-browsers/chromium"
            if Path(fallback).exists():
                b = p.chromium.launch(executable_path=fallback)
            else:
                pytest.skip("Chromium for Playwright is not installed (run: python -m playwright install chromium)")
        yield b
        b.close()


@pytest.fixture
def page_factory(browser):
    contexts = []

    def make(width: int, height: int, reduced_motion: bool = False):
        ctx = browser.new_context(
            viewport={"width": width, "height": height},
            bypass_csp=True,  # lets the test harness evaluate JS; the app's CSP stays strict
            reduced_motion="reduce" if reduced_motion else "no-preference",
        )
        contexts.append(ctx)
        page = ctx.new_page()
        page.errors = []
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        return page

    yield make
    for c in contexts:
        c.close()
