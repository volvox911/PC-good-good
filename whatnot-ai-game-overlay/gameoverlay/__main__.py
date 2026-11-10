"""Command line entry point.

    python -m gameoverlay serve [--demo] [--env development|production] [--open]
    python -m gameoverlay init            # create data dir, database and admin token
    python -m gameoverlay login-url       # print the one-click dashboard sign-in link
    python -m gameoverlay set-secret NAME # store a credential in the OS keyring (prompted)
    python -m gameoverlay check [--live]  # self-check (live: contacts OBS / PriceCharting / Anthropic)
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import sys
import threading
import time
import webbrowser

from . import __version__
from .config import AppConfig, Environment
from .logging_setup import setup_logging


def _config(args) -> AppConfig:
    overrides = {}
    if getattr(args, "env", None):
        overrides["env"] = Environment(args.env)
    if getattr(args, "demo", False):
        overrides["demo"] = True
    if getattr(args, "port", None):
        overrides["port"] = args.port
    if getattr(args, "host", None):
        overrides["host"] = args.host
    return AppConfig(**overrides)


def _admin_token(cfg: AppConfig) -> str:
    from .secrets_store import load_or_create_admin_token

    return load_or_create_admin_token(cfg.data_dir)


def cmd_serve(args) -> int:
    import uvicorn

    cfg = _config(args)
    from .secrets_store import SecretStore

    store = SecretStore(cfg.data_dir, use_keyring=not cfg.disable_keyring)
    token = _admin_token(cfg)
    level = "DEBUG" if cfg.env == Environment.DEVELOPMENT else cfg.log_level
    setup_logging(level, cfg.log_dir, json_console=False, secret_source=lambda: store.known_values() + [token])

    from .server import create_app

    if args.reload:
        code_url = "(printed by the reloading server below)"
    else:
        code = secrets.token_urlsafe(24)
        code_url = f"{cfg.base_url}/login?code={code}"
    print(f"\n  Whatnot AI Game Overlay v{__version__}  —  {'DEMO MODE (sample data)' if cfg.demo else 'LIVE MODE'}")
    print(f"  OBS Browser Source URL : {cfg.base_url}/overlay")
    print(f"  Operator dashboard     : {code_url}")
    print("  (one-time sign-in link, valid 15 minutes; afterwards open")
    print(f"   {cfg.base_url}/login and paste the token from 'python -m gameoverlay login-url')")
    print("  Press Ctrl+C to stop.\n")
    if args.open and not args.reload:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(code_url)), daemon=True).start()
    if args.reload:
        os.environ["WGO_ENV"] = cfg.env.value
        os.environ["WGO_PORT"] = str(cfg.port)
        os.environ["WGO_HOST"] = cfg.host
        if cfg.demo:
            os.environ["WGO_DEMO"] = "true"
        uvicorn.run("gameoverlay.server:app_factory", factory=True, host=cfg.host, port=cfg.port, reload=True,
                    log_config=None, ws_max_size=1 << 20)
        return 0
    app = create_app(cfg, login_code=code)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None, ws_max_size=1 << 20, timeout_graceful_shutdown=5)
    return 0


def cmd_init(args) -> int:
    cfg = _config(args)
    from .db import Database

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path)
    v = db.schema_version()
    db.close()
    print(f"Data directory : {cfg.data_dir}")
    print(f"Database       : {cfg.db_path} (schema v{v})")
    print(f"Dashboard      : {cfg.base_url}/login  (Start.cmd opens it with a one-time link)")
    return 0


def cmd_login_url(args) -> int:
    cfg = _config(args)
    print(f"Open {cfg.base_url}/login and paste this access token (keep it private):")
    print(_admin_token(cfg))
    return 0


def cmd_set_secret(args) -> int:
    from .secrets_store import ALL_SECRETS, SecretStore

    if args.name not in ALL_SECRETS:
        print(f"Unknown secret. Choose one of: {', '.join(ALL_SECRETS)}", file=sys.stderr)
        return 2
    cfg = _config(args)
    store = SecretStore(cfg.data_dir, use_keyring=not cfg.disable_keyring)
    value = getpass.getpass(f"Enter {args.name} (input hidden, empty to remove): ")
    store.set(args.name, value)
    print(f"Saved {args.name} to {store.backend}.")
    return 0


def cmd_check(args) -> int:
    cfg = _config(args)
    from .secrets_store import SecretStore

    store = SecretStore(cfg.data_dir, use_keyring=not cfg.disable_keyring)
    ok = True
    print(f"Python {sys.version.split()[0]}  |  data dir {cfg.data_dir}  |  secrets backend: {store.backend}")
    for name, st in store.all_status().items():
        print(f"  {name:22s} {'configured (' + st['source'] + ')' if st['configured'] else 'MISSING'}")
        ok &= st["configured"] or name == "obs_password"
    if args.live:
        ok &= asyncio.run(_live_check(cfg, store))
    print("RESULT:", "OK" if ok else "ATTENTION NEEDED")
    return 0 if ok else 1


async def _live_check(cfg: AppConfig, store) -> bool:
    from .config import RuntimeSettings
    from .db import Database
    from .obs.client import OBSClient
    from .pricing.client import PriceChartingClient, PriceChartingError
    from .pricing.ratelimit import SerialRateLimiter

    ok = True
    db = Database(cfg.db_path)
    raw = db.get_setting("runtime")
    s = RuntimeSettings.model_validate(raw) if raw else RuntimeSettings()
    db.close()
    obs = OBSClient(lambda: (s.obs.host, s.obs.port), lambda: store.get("obs_password"))
    obs.start()
    connected = await obs.wait_connected(6)
    print(f"  OBS {s.obs.host}:{s.obs.port}: {'connected' if connected else obs.state + ' ' + str(obs.last_error)}")
    if connected:
        inputs = await obs.list_inputs()
        print("    inputs:", ", ".join(f"{i['name']} [{i['kind']}]" for i in inputs) or "(none)")
    ok &= connected
    await obs.stop()
    pc = PriceChartingClient(lambda: store.get("pricecharting_token"), SerialRateLimiter(1.2))
    try:
        res = await pc.search("super mario 64 nintendo 64")
        print(f"  PriceCharting: OK ({len(res)} results for a test search)")
    except PriceChartingError as e:
        print(f"  PriceCharting: FAILED — {e}")
        ok = False
    finally:
        await pc.aclose()
    key = store.get("anthropic_api_key")
    if key:
        import anthropic

        try:
            client = anthropic.AsyncAnthropic(api_key=key, timeout=20, max_retries=0)
            m = await client.models.retrieve(s.recognition.model)
            print(f"  Anthropic: OK (model {m.id} available)")
        except anthropic.APIError as e:
            print(f"  Anthropic: FAILED — {type(e).__name__}")
            ok = False
    else:
        print("  Anthropic: key missing")
        ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gameoverlay", description="Whatnot AI Game Overlay")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--env", choices=[e.value for e in Environment])
        p.add_argument("--demo", action="store_true", help="demo mode with mock camera/APIs (sample data)")
        p.add_argument("--port", type=int)
        p.add_argument("--host")

    p = sub.add_parser("serve", help="run the overlay server and dashboard")
    common(p)
    p.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    p.add_argument("--reload", action="store_true", help="development auto-reload")
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("init")
    common(p)
    p.set_defaults(fn=cmd_init)
    p = sub.add_parser("login-url")
    common(p)
    p.set_defaults(fn=cmd_login_url)
    p = sub.add_parser("set-secret")
    common(p)
    p.add_argument("name")
    p.set_defaults(fn=cmd_set_secret)
    p = sub.add_parser("check")
    common(p)
    p.add_argument("--live", action="store_true")
    p.set_defaults(fn=cmd_check)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
