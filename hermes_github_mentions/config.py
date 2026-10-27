"""Non-secret settings live in TOML; credentials live in the environment or files."""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
ROUTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")


@dataclass(frozen=True)
class Config:
    bot: str
    allowed_authors: tuple[str, ...]
    state_file: Path
    secret_file: Path
    webhook_url: str = "http://127.0.0.1:8644/webhooks"
    token_env: str = "GITHUB_TOKEN"
    token_file: Path | None = None
    work_route: str = "github-mention-work"
    other_route: str = "github-mention-work-other"
    notice_route: str = "github-mention-notice"
    progress_route: str = "github-mention-progress"
    poll_seconds: int = 60
    discord_notifications: bool = False
    progress: bool = False
    hermes_home: Path = field(default_factory=lambda: Path.home() / ".hermes")
    profile: str = "default"

    def __post_init__(self):
        if not LOGIN.fullmatch(self.bot):
            raise ValueError("bot must be a GitHub username")
        if not self.allowed_authors or any(not LOGIN.fullmatch(x) for x in self.allowed_authors):
            raise ValueError("allowed_authors must contain GitHub usernames")
        for name in ("work_route", "other_route", "notice_route", "progress_route"):
            value = getattr(self, name)
            if not value and name == "notice_route":
                continue
            if not ROUTE.fullmatch(value):
                raise ValueError(f"Invalid {name}")
        derived_routes = [self.work_route + "-discord", self.other_route + "-discord"]
        if any(not ROUTE.fullmatch(name) for name in derived_routes):
            raise ValueError("Work route names must leave room for the -discord suffix")
        routes = [self.work_route, self.other_route, self.progress_route, *derived_routes]
        if self.notice_route:
            routes.append(self.notice_route)
        if len(set(routes)) != len(routes):
            raise ValueError("Route names must be distinct")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.profile):
            raise ValueError("profile must contain letters, digits, underscores, or hyphens")
        url = urlsplit(self.webhook_url)
        expected_path = "/webhooks" if self.profile == "default" else f"/p/{self.profile}/webhooks"
        if (url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1")
                or url.username or url.password or url.query or url.fragment
                or url.path.rstrip("/") != expected_path):
            raise ValueError(f"webhook_url must be a local HTTP {expected_path} endpoint")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("Invalid webhook port")
        if type(self.poll_seconds) is not int or self.poll_seconds < 60:
            raise ValueError("poll_seconds must be an integer of at least 60")
        if type(self.progress) is not bool:
            raise ValueError("progress must be true or false")
        if type(self.discord_notifications) is not bool:
            raise ValueError("discord_notifications must be true or false")

    def token(self):
        value = os.environ.get(self.token_env, "").strip()
        if not value and self.token_file:
            value = self.token_file.read_text().strip()
        if not value:
            raise ValueError(f"Set {self.token_env} or configure token_file")
        return value

    def secret(self):
        value = self.secret_file.read_bytes().strip()
        if len(value) < 32:
            raise ValueError("Webhook secret must contain at least 32 bytes")
        return value


def default_config_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "hermes-github-mentions" / "config.toml"


def load_config(path):
    path = Path(path).expanduser().resolve()
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    unknown = set(data) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError("Unknown config keys: " + ", ".join(sorted(unknown)))
    for name in ("bot", "token_env", "webhook_url", "work_route", "other_route", "notice_route",
                 "progress_route", "profile"):
        if name in data and not isinstance(data[name], str):
            raise ValueError(f"{name} must be a string")
    if "allowed_authors" in data:
        authors = data["allowed_authors"]
        if not isinstance(authors, list) or any(not isinstance(x, str) for x in authors):
            raise ValueError("allowed_authors must be an array of strings")
        data["allowed_authors"] = tuple(x.lower() for x in authors)
    for name in ("state_file", "secret_file", "token_file", "hermes_home"):
        if name in data:
            if not isinstance(data[name], str) or not data[name]:
                raise ValueError(f"{name} must be a nonempty path")
            value = Path(data[name]).expanduser()
            data[name] = value if value.is_absolute() else path.parent / value
    missing = {"bot", "allowed_authors", "state_file", "secret_file"} - data.keys()
    if missing:
        raise ValueError("Missing config keys: " + ", ".join(sorted(missing)))
    return Config(**data)
