"""Small CLI that never installs services or changes the live Hermes configuration."""

import argparse
import json
import logging
import os
import secrets
import signal
import sqlite3
import threading
import time
import urllib.error
from pathlib import Path

from . import __version__
from .config import Config, default_config_path, load_config
from .network import request_json
from .routes import build_routes
from .storage import atomic_write, state_lock
from .watcher import Watcher, WatcherStopped

LOG = logging.getLogger("hermes-github-mentions")


def initialize(path, bot, authors):
    path = path.expanduser().resolve()
    Config(bot=bot, allowed_authors=tuple(x.lower() for x in authors),
           state_file=path.parent / "state.json", secret_file=path.parent / "webhook.secret")
    if path.exists():
        raise ValueError("Config already exists; edit it instead of reinitializing")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret_path = path.parent / "webhook.secret"
    if secret_path.exists():
        raise ValueError("Secret already exists; preserve it and create the config manually")
    fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(secrets.token_hex(32) + "\n")
    text = (
        f'bot = {json.dumps(bot)}\nallowed_authors = {json.dumps(authors)}\n'
        'state_file = "state.json"\nsecret_file = "webhook.secret"\n'
        'token_env = "GITHUB_TOKEN"\n'
        '# token_file = "github-token.secret" # A raw token, used when token_env is unset.\n'
        'webhook_url = "http://127.0.0.1:8644/webhooks"\npoll_seconds = 60\n'
        'discord_notifications = false # Optional Discord copies of replies and notices.\n'
        'progress = false # Optional, depends on Hermes internal SQLite schema.\n'
        '# hermes_home = "~/.hermes"\n# profile = "default"\n'
    )
    atomic_write(path, text)
    print(f"Created {path} and {secret_path}. Configure Hermes routes before running.")


def retry_seconds(exc, minimum):
    delay = minimum
    if isinstance(exc, urllib.error.HTTPError):
        try:
            delay = max(delay, int(exc.headers.get("Retry-After", "0")))
            if exc.code in (403, 429):
                delay = max(delay, int(exc.headers.get("X-RateLimit-Reset", "0")) - int(time.time()))
        except (TypeError, ValueError):
            pass
    return delay


def run(config, once=False, dry_run=False, *, token=None, secret_provider=None, chat_id_provider=None,
        discord_enabled_provider=None):
    token = config.token() if token is None else token
    identity, _ = request_json("https://api.github.com/user", token)
    if str(identity.get("login", "")).lower() != config.bot.lower():
        raise ValueError("GitHub token identity does not match the configured bot")
    watcher = Watcher(config, secret_provider=secret_provider, chat_id_provider=chat_id_provider,
                      discord_enabled_provider=discord_enabled_provider)
    if dry_run:
        state = watcher.load_state()
        watcher.poll(token, state, dry_run=True, bootstrap=not config.state_file.exists())
        return 0
    watcher.secret()
    stop = threading.Event()
    watcher.should_stop = stop.is_set
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with state_lock(config.state_file):
        fresh = not config.state_file.exists()
        state = watcher.load_state()
        if fresh:
            watcher.save_state(state)
            LOG.info("Initialized cursor; earlier requests will not be replayed")
        while not stop.is_set():
            try:
                draining = watcher.gateway_draining()
                interval = watcher.poll(token, state, bootstrap=fresh, allow_work=not draining)
                fresh = False
            except WatcherStopped:
                break
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                LOG.error("Mention check failed: %s", exc)
                if once:
                    return 1
                interval = retry_seconds(exc, config.poll_seconds)
            # Optional progress is independent of notification admission and its cursor.
            try:
                watcher.report_active(state)
            except (sqlite3.Error, OSError, ValueError, RuntimeError, KeyError) as exc:
                LOG.warning("Progress unavailable: %s", exc)
            if once:
                return 0
            stop.wait(interval)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Forward trusted GitHub mentions to local Hermes.")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a new config and webhook secret")
    init.add_argument("--bot", required=True)
    init.add_argument("--author", action="append", required=True)
    routes = commands.add_parser("routes", help="Write a private Hermes config fragment")
    routes.add_argument("--discord-chat-id", default="",
                        help="Override Hermes' Discord home channel")
    routes.add_argument("--output", type=Path, required=True)
    watch = commands.add_parser("run", help="Poll GitHub and hand requests to Hermes")
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--dry-run", action="store_true", help="One read-only poll, no webhook or state writes")
    commands.add_parser("status", help="Show local cursor and queue counts without credentials")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "init":
            initialize(args.config, args.bot, args.author)
            return 0
        config = load_config(args.config)
        if args.command == "routes":
            if args.discord_chat_id and not config.discord_notifications:
                raise ValueError("Set discord_notifications = true to use a Discord channel override")
            if args.output.exists():
                raise ValueError("Output already exists; choose a new file")
            if args.output.resolve() in (args.config.resolve(), config.secret_file.resolve(),
                                         config.state_file.resolve(), config.token_file):
                raise ValueError("Output must be separate from config, credentials, and state")
            atomic_write(args.output, json.dumps(build_routes(config, args.discord_chat_id), indent=2) + "\n")
            print(f"Wrote {args.output}. It contains a webhook secret; keep it private.")
            return 0
        if args.command == "status":
            state = Watcher(config).load_state()
            print(json.dumps({"initialized": config.state_file.exists(), "cursor": state["cursor"],
                              **{k: len(state.get(k, {})) for k in ("pending", "active", "seen")}}, indent=2))
            return 0
        return run(config, args.once, args.dry_run)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        LOG.error("%s", exc)
        return 1
