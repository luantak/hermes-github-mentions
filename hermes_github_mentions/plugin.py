"""Native Hermes settings and CLI registration; imports no Hermes code at registration."""

import json
from pathlib import Path

from .config import Config
from .routes import build_routes
from .storage import atomic_write
from .watcher import Watcher

PLUGIN_ID = "hermes-github-mentions"
TOKEN_ENV = "GITHUB_MENTIONS_TOKEN"
SECRET_ENV = "GITHUB_MENTIONS_WEBHOOK_SECRET"
DEFAULTS = {"bot": "", "allowed_authors": [], "repositories": [], "discord_chat_id": "",
            "webhook_url": "http://127.0.0.1:8644/webhooks", "profile": "default",
            "poll_seconds": 60, "progress": False}


def get_settings(ctx):
    return {key: ctx.get_config(key, default=value) for key, value in DEFAULTS.items()}


def discord_target(ctx):
    value = ctx.get_config("discord_chat_id", default="")
    if not isinstance(value, str) or (value and not value.isdecimal()):
        raise ValueError("Discord channel override must contain digits")
    return value


def can_set_destination(source):
    """Require a stable Discord user ID explicitly allowed in the owning profile."""
    platform = getattr(source, "platform", None)
    if (source is None or getattr(source, "is_bot", False)
            or str(getattr(platform, "value", platform)) != "discord"):
        return False
    user_id = str(getattr(source, "user_id", "") or "")
    if not user_id.isdecimal():
        return False
    from agent.secret_scope import get_secret
    from gateway.config import Platform, load_gateway_config

    raw = get_secret("DISCORD_ALLOWED_USERS")
    if raw is None:
        config = load_gateway_config().platforms.get(Platform.DISCORD)
        extra = config.extra if config else {}
        raw = extra.get("allow_from", extra.get("allowed_users", []))
    entries = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(entries, (list, tuple)):
        return False
    allowed = {str(x).strip().removeprefix("<@").removeprefix("!").removesuffix(">") for x in entries}
    return user_id in allowed


def handle_chat_command(ctx, raw_args, *, source=None, **kwargs):
    action = raw_args.strip().lower() or "status"
    if action not in ("set", "reset", "status"):
        return "Use /github-mentions set, reset, or status in Discord."
    if not can_set_destination(source):
        return "Run this in Discord as a user explicitly listed in this profile's Discord allowed users."
    if action == "set":
        # Source IDs come from the adapter, never from command arguments or display names.
        chat_id = str(getattr(source, "thread_id", "") or getattr(source, "chat_id", ""))
        if not chat_id.isdecimal():
            return "Could not determine this Discord channel."
        ctx.set_config("discord_chat_id", chat_id)
        return "GitHub updates will use this channel for new requests and status notices."
    if action == "reset":
        ctx.set_config("discord_chat_id", "")
        return "GitHub updates will use this profile's Discord home channel."
    target = discord_target(ctx)
    return f"GitHub updates use <#{target}>." if target else "GitHub updates use this profile's Discord home channel."


def make_config(settings, home, data):
    for key in ("allowed_authors", "repositories"):
        if not isinstance(settings[key], list) or any(not isinstance(x, str) for x in settings[key]):
            raise ValueError(f"{key} must be a list of strings")
    for key in ("bot", "discord_chat_id", "webhook_url", "profile"):
        if not isinstance(settings[key], str):
            raise ValueError(f"{key} must be a string")
    if settings["discord_chat_id"] and not settings["discord_chat_id"].isdecimal():
        raise ValueError("Discord channel override must contain digits")
    return Config(
        bot=settings["bot"], allowed_authors=tuple(x.lower() for x in settings["allowed_authors"]),
        repositories=tuple(x.lower() for x in settings["repositories"]),
        state_file=data / "state.json", secret_file=data / "unused.secret",
        webhook_url=settings["webhook_url"], poll_seconds=settings["poll_seconds"],
        progress=settings["progress"], hermes_home=home, profile=settings["profile"],
    )


def scoped_config(ctx):
    from hermes_constants import get_hermes_home
    from plugins.plugin_storage import plugin_data_dir

    settings = get_settings(ctx)
    home = get_hermes_home()
    # Validate before creating the data directory.
    config = make_config(settings, home, home / "plugin-data" / PLUGIN_ID)
    plugin_data_dir(PLUGIN_ID)
    return config


def scoped_token():
    from agent.secret_scope import get_secret

    value = (get_secret(TOKEN_ENV) or "").strip()
    if not value:
        raise ValueError(f"Set {TOKEN_ENV} in this Hermes profile's secrets")
    return value


def scoped_secret():
    from agent.secret_scope import get_secret

    value = (get_secret(SECRET_ENV) or "").strip()
    if len(value) < 32 or not value.isascii():
        raise ValueError(f"Set {SECRET_ENV} to an ASCII secret of at least 32 characters")
    return value.encode("ascii")


def setup_parser(parser):
    subs = parser.add_subparsers(dest="github_mentions_command", required=True)
    configure = subs.add_parser("configure", help="Save non-secret settings in Hermes")
    configure.add_argument("--bot", required=True)
    configure.add_argument("--author", action="append", required=True)
    configure.add_argument("--discord-chat-id", default=None,
                           help="Override Hermes' Discord home channel; omit to keep the current setting")
    configure.add_argument("--repository", action="append", default=[])
    configure.add_argument("--webhook-url", default=DEFAULTS["webhook_url"])
    configure.add_argument("--profile", default="default")
    configure.add_argument("--poll-seconds", type=int, default=60)
    configure.add_argument("--progress", action="store_true")
    routes = subs.add_parser("routes", help="Export a private webhook config fragment")
    routes.add_argument("--output", type=Path, required=True)
    run = subs.add_parser("run", help="Run in the foreground without enabling the gateway adapter")
    run.add_argument("--once", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    subs.add_parser("status", help="Show the profile's cursor and queue counts")


def handle_command(ctx, args):
    try:
        if args.github_mentions_command == "configure":
            from hermes_constants import get_hermes_home

            settings = {"bot": args.bot, "allowed_authors": args.author,
                        "discord_chat_id": (args.discord_chat_id if args.discord_chat_id is not None
                                            else ctx.get_config("discord_chat_id", default="")),
                        "repositories": args.repository,
                        "webhook_url": args.webhook_url, "profile": args.profile,
                        "poll_seconds": args.poll_seconds, "progress": args.progress}
            home = get_hermes_home()
            config = make_config(settings, home, home / "plugin-data" / PLUGIN_ID)
            # Reconfiguration must not reset the existing cursor or change its identity.
            Watcher(config).load_state()
            for key, value in settings.items():
                ctx.set_config(key, value)
            print("Saved plugin settings. Configure profile secrets, export routes, and enable the gateway adapter.")
            return 0
        config = scoped_config(ctx)
        if args.github_mentions_command == "routes":
            if args.output.exists():
                raise ValueError("Output already exists; choose a new file")
            if args.output.resolve() == config.state_file.resolve():
                raise ValueError("Output must be separate from watcher state")
            routes = build_routes(config, secret=scoped_secret(), dynamic_chat_id=True)
            routes["platforms"]["github_mentions"] = {"enabled": True}
            atomic_write(args.output, json.dumps(routes, indent=2) + "\n")
            print(f"Wrote {args.output}. It contains a webhook secret; keep it private.")
            return 0
        if args.github_mentions_command == "status":
            state = Watcher(config).load_state()
            print(json.dumps({"initialized": config.state_file.exists(), "cursor": state["cursor"],
                              **{key: len(state.get(key, {})) for key in ("seen", "pending", "active")},
                              "state_file": str(config.state_file)}, indent=2))
            return 0
        from .cli import run

        return run(config, args.once, args.dry_run, token=scoped_token(), secret_provider=scoped_secret,
                   chat_id_provider=lambda: discord_target(ctx))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f"GitHub mentions: {exc}")
        return 1


def register(ctx):
    def adapter_factory(config):
        from .adapter import GitHubMentionsAdapter

        return GitHubMentionsAdapter(config, ctx)

    ctx.register_platform(
        name="github_mentions", label="GitHub mentions", adapter_factory=adapter_factory,
        check_fn=lambda: True, install_hint="No additional Python dependencies are required.",
    )
    ctx.register_cli_command(
        name="github-mentions", help="Configure and inspect the GitHub mention watcher",
        setup_fn=setup_parser, handler_fn=lambda args: handle_command(ctx, args),
    )
    def chat_command(raw_args, *, source=None, **kwargs):
        return handle_chat_command(ctx, raw_args, source=source, **kwargs)

    ctx.register_command("github-mentions", handler=chat_command,
                         description="Choose the Discord destination for GitHub updates",
                         args_hint="set | reset | status")
