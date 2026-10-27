"""Receive-only gateway adapter. Hermes owns its startup and shutdown."""

import asyncio
import logging

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .network import request_json
from .plugin import scoped_config, scoped_secret, scoped_token
from .runtime import WatcherRuntime

LOG = logging.getLogger(__name__)


class GitHubMentionsAdapter(BasePlatformAdapter):
    def __init__(self, config, ctx):
        super().__init__(config, Platform("github_mentions"))
        self.ctx = ctx
        self.runtime = None
        self.worker = None

    async def connect(self, *, is_reconnect=False):
        if self.worker and self.worker.is_alive():
            return True
        try:
            config = scoped_config(self.ctx)
            token, secret = scoped_token(), scoped_secret()
            identity, _ = await asyncio.to_thread(request_json, "https://api.github.com/user", token)
            if str(identity.get("login", "")).lower() != config.bot.lower():
                raise ValueError("GitHub token identity does not match the configured bot")
            self.runtime = WatcherRuntime(config, token, secret)
            from agent.memory_provider import spawn_context_thread

            self.worker = spawn_context_thread(
                target=self.runtime.run, name="github-mentions", daemon=True,
            )
            self.worker.start()
            ready = await asyncio.to_thread(self.runtime.ready.wait, 5)
            if not ready:
                self.runtime.stop.set()
                raise RuntimeError("GitHub mention worker did not start within five seconds")
            if self.runtime.error:
                raise RuntimeError(str(self.runtime.error))
            self._mark_connected()
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            LOG.error("GitHub mentions could not start: %s", exc)
            self._set_fatal_error("github_mentions_start", str(exc), retryable=isinstance(exc, OSError))
            return False

    async def disconnect(self):
        if self.runtime:
            self.runtime.stop.set()
        if self.worker:
            # Do not cancel a thread doing a POST or release its state lock early.
            await asyncio.to_thread(self.worker.join, 25)
            if self.worker.is_alive():
                LOG.warning("GitHub mention worker is still finishing its current request")
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="GitHub mentions is receive-only; use the work route's delivery target")

    async def get_chat_info(self, chat_id):
        return {"name": "GitHub mentions", "type": "channel"}
