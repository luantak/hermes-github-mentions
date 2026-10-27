"""Cooperatively stopped worker. The caller supplies its profile-scoped credentials."""

import logging
import sqlite3
import threading

from .cli import retry_seconds
from .storage import state_lock
from .watcher import Watcher, WatcherStopped

LOG = logging.getLogger(__name__)


class WatcherRuntime:
    def __init__(self, config, token, secret, *, chat_id_provider=None, discord_enabled_provider=None):
        self.config = config
        self.token = token
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.watcher = Watcher(config, secret_provider=lambda: secret, should_stop=self.stop.is_set,
                               chat_id_provider=chat_id_provider, discord_enabled_provider=discord_enabled_provider)

    def run(self):
        try:
            with state_lock(self.config.state_file):
                fresh = not self.config.state_file.exists()
                state = self.watcher.load_state()
                if fresh:
                    self.watcher.save_state(state)
                self.ready.set()
                while not self.stop.is_set():
                    try:
                        interval = self.watcher.poll(
                            self.token, state, bootstrap=fresh,
                            allow_work=not self.watcher.gateway_draining(),
                        )
                        fresh = False
                    except WatcherStopped:
                        break
                    except (OSError, ValueError, RuntimeError, KeyError) as exc:
                        LOG.warning("GitHub mention poll failed: %s", exc)
                        interval = retry_seconds(exc, self.config.poll_seconds)
                    try:
                        self.watcher.report_active(state)
                    except WatcherStopped:
                        break
                    except (sqlite3.Error, OSError, ValueError, RuntimeError, KeyError) as exc:
                        LOG.warning("GitHub mention progress unavailable: %s", exc)
                    self.stop.wait(interval)
        except Exception as exc:
            self.error = exc
            LOG.error("GitHub mention worker stopped: %s", exc)
        finally:
            self.ready.set()
