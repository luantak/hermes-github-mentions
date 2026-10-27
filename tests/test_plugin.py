import argparse
import asyncio
import contextvars
import importlib.util
import io
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_github_mentions import plugin
from hermes_github_mentions.runtime import WatcherRuntime
from hermes_github_mentions.storage import state_lock
from hermes_github_mentions.watcher import Watcher, WatcherStopped


class Context:
    def __init__(self):
        self.settings = {**plugin.DEFAULTS, "bot": "helper-bot", "allowed_authors": ["owner"],
                         "discord_chat_id": "1234"}
        self.platforms = {}
        self.commands = {}
        self.chat_commands = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value

    def register_platform(self, **kwargs):
        self.platforms[kwargs["name"]] = kwargs

    def register_cli_command(self, **kwargs):
        self.commands[kwargs["name"]] = kwargs

    def register_command(self, name, **kwargs):
        self.chat_commands[name] = kwargs


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ctx = Context()
        self.config = plugin.make_config(self.ctx.settings, self.home, self.home / "data")
        for name in ("hermes_github_mentions.network.open_request", "hermes_github_mentions.watcher.open_request"):
            blocker = patch(name, side_effect=AssertionError("Unmocked network request"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def modules(self):
        home = types.ModuleType("hermes_constants")
        home.get_hermes_home = lambda: self.home
        secrets = types.ModuleType("agent.secret_scope")
        secrets.get_secret = lambda key: {plugin.TOKEN_ENV: "fake-token", plugin.SECRET_ENV: "s" * 64}.get(key)
        storage = types.ModuleType("plugins.plugin_storage")

        def data_dir(name):
            path = self.home / "plugin-data" / name
            path.mkdir(parents=True, exist_ok=True)
            return path

        storage.plugin_data_dir = data_dir
        return {"hermes_constants": home, "agent.secret_scope": secrets, "plugins.plugin_storage": storage}

    def test_directory_entry_loads_namespaced_and_registration_has_no_side_effects(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("test_native_entry", root / "__init__.py",
                                                     submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"test_native_entry": module}):
            spec.loader.exec_module(module)
            module.register(self.ctx)
        self.assertIn("github_mentions", self.ctx.platforms)
        self.assertIn("github-mentions", self.ctx.commands)
        self.assertIn("github-mentions", self.ctx.chat_commands)
        self.assertTrue(self.ctx.platforms["github_mentions"]["check_fn"]())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_scoped_secrets_and_state_follow_the_active_home(self):
        with patch.dict(sys.modules, self.modules()):
            first = plugin.scoped_config(self.ctx)
            self.assertEqual(plugin.scoped_token(), "fake-token")
            self.assertEqual(plugin.scoped_secret(), b"s" * 64)
            self.home = self.home / "another-profile"
            second = plugin.scoped_config(self.ctx)
        self.assertNotEqual(first.state_file, second.state_file)
        self.assertEqual(second.hermes_home, self.home)
        self.assertEqual(first.state_file.parent.name, plugin.PLUGIN_ID)
        self.assertFalse(first.secret_file.exists())

    def test_missing_secrets_fail_instead_of_using_another_identity(self):
        modules = self.modules()
        modules["agent.secret_scope"].get_secret = lambda _: None
        with patch.dict(sys.modules, modules):
            with self.assertRaises(ValueError):
                plugin.scoped_token()
            with self.assertRaises(ValueError):
                plugin.scoped_secret()

    def test_native_configure_and_routes_use_hermes_settings(self):
        parser = argparse.ArgumentParser()
        plugin.setup_parser(parser)
        args = parser.parse_args(["configure", "--bot", "new-bot", "--author", "requester",
                                  "--discord-chat-id", "999"])
        with patch.dict(sys.modules, self.modules()), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(plugin.handle_command(self.ctx, args), 0)
            self.assertEqual(self.ctx.settings["bot"], "new-bot")
            output = self.home / "routes.json"
            args = parser.parse_args(["routes", "--output", str(output)])
            self.assertEqual(plugin.handle_command(self.ctx, args), 0)
        routes = json.loads(output.read_text())
        self.assertEqual(routes["platforms"]["github_mentions"], {"enabled": True})
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / "config.yaml").exists())
        self.assertFalse((self.home / "plugin-data" / plugin.PLUGIN_ID / "state.json").exists())

    def test_invalid_configuration_does_not_save_partial_settings(self):
        parser = argparse.ArgumentParser()
        plugin.setup_parser(parser)
        args = parser.parse_args(["configure", "--bot", "new-bot", "--author", "requester",
                                  "--discord-chat-id", "invalid"])
        with patch.dict(sys.modules, self.modules()), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(plugin.handle_command(self.ctx, args), 1)
        self.assertEqual(self.ctx.settings["bot"], "helper-bot")

    def test_configure_can_use_home_channel_without_an_id_and_keeps_existing_override(self):
        parser = argparse.ArgumentParser()
        plugin.setup_parser(parser)
        args = parser.parse_args(["configure", "--bot", "helper-bot", "--author", "owner"])
        self.ctx.settings["discord_chat_id"] = ""
        with patch.dict(sys.modules, self.modules()), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(plugin.handle_command(self.ctx, args), 0)
            self.assertEqual(self.ctx.settings["discord_chat_id"], "")
            self.ctx.settings["discord_chat_id"] = "888"
            self.assertEqual(plugin.handle_command(self.ctx, args), 0)
            self.assertEqual(self.ctx.settings["discord_chat_id"], "888")

    def test_discord_set_and_reset_take_channel_from_authenticated_source(self):
        source = types.SimpleNamespace(platform="discord", chat_id="888", thread_id=None, user_id="111", is_bot=False)
        secrets = self.modules()["agent.secret_scope"]
        secrets.get_secret = lambda _: "111,222"
        config = types.ModuleType("gateway.config")
        config.Platform = types.SimpleNamespace(DISCORD="discord")
        config.load_gateway_config = lambda: types.SimpleNamespace(platforms={})
        with patch.dict(sys.modules, {"agent.secret_scope": secrets, "gateway.config": config}):
            self.assertIn("this channel", plugin.handle_chat_command(self.ctx, "set", source=source))
            self.assertEqual(self.ctx.settings["discord_chat_id"], "888")
            self.assertIn("<#888>", plugin.handle_chat_command(self.ctx, "status", source=source))
            source.thread_id = "999"
            plugin.handle_chat_command(self.ctx, "set", source=source)
            self.assertEqual(self.ctx.settings["discord_chat_id"], "999")
            self.assertIn("home channel", plugin.handle_chat_command(self.ctx, "reset", source=source))
            self.assertEqual(self.ctx.settings["discord_chat_id"], "")
            self.assertTrue(self.ctx.settings["discord_notifications"])

    def test_discord_is_automatic_only_for_a_connected_profile_with_a_destination(self):
        modules = self.modules()
        config = types.ModuleType("gateway.config")
        config.Platform = types.SimpleNamespace(DISCORD="discord")
        platform = types.SimpleNamespace(enabled=True, home_channel="configured-home")
        config.load_gateway_config = lambda: types.SimpleNamespace(platforms={"discord": platform})
        status = types.ModuleType("gateway.status")
        live = types.SimpleNamespace(running=True, source="pid", runtime=None)
        snapshot = {"platforms": {"discord": {"state": "connected"}}}
        status.resolve_gateway_liveness = lambda **kwargs: live
        status.read_runtime_status = lambda path: snapshot
        status.runtime_status_is_stale = lambda value: False
        status.profile_platforms_from_multiplexer = lambda value, profile: {}
        modules.update({"gateway.config": config, "gateway.status": status})
        self.ctx.settings["discord_chat_id"] = ""
        with patch.dict(sys.modules, modules):
            self.assertTrue(plugin.discord_available(self.ctx))
            snapshot["platforms"]["discord"]["state"] = "disconnected"
            self.assertFalse(plugin.discord_available(self.ctx))
            snapshot["platforms"]["discord"]["state"] = "connected"
            platform.home_channel = None
            self.assertFalse(plugin.discord_available(self.ctx))
            self.ctx.settings["discord_chat_id"] = "999"
            self.assertTrue(plugin.discord_available(self.ctx))
            live.running = False
            self.assertFalse(plugin.discord_available(self.ctx))
            live.running = True
            self.ctx.settings["discord_notifications"] = False
            self.assertFalse(plugin.discord_available(self.ctx))

    def test_discord_auto_detection_does_not_borrow_another_multiplex_profile(self):
        modules = self.modules()
        config = types.ModuleType("gateway.config")
        config.Platform = types.SimpleNamespace(DISCORD="discord")
        config.load_gateway_config = lambda: types.SimpleNamespace(platforms={
            "discord": types.SimpleNamespace(enabled=True, home_channel="home")})
        status = types.ModuleType("gateway.status")
        snapshot = {"default": {"discord": {"state": "connected"}},
                    "coder": {"discord": {"state": "disconnected"}}}
        status.resolve_gateway_liveness = lambda **kwargs: types.SimpleNamespace(
            running=True, source="multiplexer", runtime=snapshot)
        status.read_runtime_status = lambda path: self.fail("Named profile must use its multiplexer snapshot")
        status.runtime_status_is_stale = lambda value: False
        status.profile_platforms_from_multiplexer = lambda value, profile: value[profile]
        modules.update({"gateway.config": config, "gateway.status": status})
        self.ctx.settings["profile"] = "coder"
        with patch.dict(sys.modules, modules):
            self.assertFalse(plugin.discord_available(self.ctx))
            snapshot["coder"]["discord"]["state"] = "connected"
            self.assertTrue(plugin.discord_available(self.ctx))

    def test_configure_can_explicitly_disable_and_restore_automatic_discord(self):
        parser = argparse.ArgumentParser()
        plugin.setup_parser(parser)
        base = ["configure", "--bot", "helper-bot", "--author", "owner"]
        with patch.dict(sys.modules, self.modules()), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(plugin.handle_command(self.ctx, parser.parse_args(base + ["--no-discord"])), 0)
            self.assertFalse(self.ctx.settings["discord_notifications"])
            self.assertEqual(plugin.handle_command(self.ctx, parser.parse_args(base + ["--discord"])), 0)
            self.assertTrue(self.ctx.settings["discord_notifications"])

    def test_discord_command_fails_closed_for_missing_bot_and_unlisted_sources(self):
        secrets = self.modules()["agent.secret_scope"]
        secrets.get_secret = lambda _: "111"
        config = types.ModuleType("gateway.config")
        config.Platform = types.SimpleNamespace(DISCORD="discord")
        config.load_gateway_config = lambda: types.SimpleNamespace(platforms={})
        source = types.SimpleNamespace(platform="discord", chat_id="888", user_id="222", is_bot=False)
        with patch.dict(sys.modules, {"agent.secret_scope": secrets, "gateway.config": config}):
            for value in (None, source, types.SimpleNamespace(),
                          types.SimpleNamespace(platform="discord", chat_id="888", user_id="111", is_bot=True)):
                self.assertIn("explicitly listed", plugin.handle_chat_command(self.ctx, "set", source=value))
            self.assertEqual(self.ctx.settings["discord_chat_id"], "1234")

    def test_discord_authorization_uses_profile_yaml_when_env_allowlist_is_absent(self):
        secrets = self.modules()["agent.secret_scope"]
        secrets.get_secret = lambda _: None
        config = types.ModuleType("gateway.config")
        config.Platform = types.SimpleNamespace(DISCORD="discord")
        config.load_gateway_config = lambda: types.SimpleNamespace(platforms={
            "discord": types.SimpleNamespace(extra={"allow_from": ["<@111>"]})})
        source = types.SimpleNamespace(platform="discord", user_id="111", is_bot=False)
        with patch.dict(sys.modules, {"agent.secret_scope": secrets, "gateway.config": config}):
            self.assertTrue(plugin.can_set_destination(source))
            source.user_id = "222"
            self.assertFalse(plugin.can_set_destination(source))

    def test_configure_keeps_existing_bot_state_identity(self):
        with patch.dict(sys.modules, self.modules()):
            config = plugin.scoped_config(self.ctx)
            watcher = Watcher(config)
            watcher.save_state(watcher.load_state())
            original = config.state_file.read_bytes()
            parser = argparse.ArgumentParser()
            plugin.setup_parser(parser)
            args = parser.parse_args(["configure", "--bot", "new-bot", "--author", "owner",
                                      "--discord-chat-id", "1234"])
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(plugin.handle_command(self.ctx, args), 1)
        self.assertEqual(config.state_file.read_bytes(), original)

    def test_runtime_stops_and_releases_lock(self):
        runtime = WatcherRuntime(self.config, "fake-token", b"s" * 64)

        def poll(*args, **kwargs):
            runtime.stop.set()
            return 60

        with patch.object(runtime.watcher, "poll", side_effect=poll):
            runtime.run()
        self.assertTrue(runtime.ready.is_set())
        self.assertIsNone(runtime.error)
        with state_lock(self.config.state_file):
            pass

    def test_runtime_duplicate_writer_is_reported_at_startup(self):
        runtime = WatcherRuntime(self.config, "fake-token", b"s" * 64)
        with state_lock(self.config.state_file), self.assertLogs(level="ERROR"):
            runtime.run()
        self.assertTrue(runtime.ready.is_set())
        self.assertIsInstance(runtime.error, RuntimeError)

    def test_runtime_progress_error_does_not_stop_polls(self):
        runtime = WatcherRuntime(self.config, "fake-token", b"s" * 64)

        def progress(_):
            runtime.stop.set()
            raise ValueError("unsupported schema")

        with patch.object(runtime.watcher, "poll", return_value=60) as poll, \
                patch.object(runtime.watcher, "report_active", side_effect=progress), self.assertLogs(level="WARNING"):
            runtime.run()
        self.assertEqual(poll.call_count, 1)
        self.assertIsNone(runtime.error)

    def test_stop_between_requests_preserves_the_cursor(self):
        watcher = Watcher(self.config, should_stop=lambda: True)
        state = watcher.load_state()
        original = state["cursor"]
        with self.assertRaises(WatcherStopped):
            watcher.poll("fake", state)
        self.assertEqual(state["cursor"], original)
        self.assertFalse(self.config.state_file.exists())

    def load_adapter(self):
        class Base:
            def __init__(self, config, platform):
                self.config, self.platform, self._running = config, platform, False

            def _mark_connected(self):
                self._running = True

            def _mark_disconnected(self):
                self._running = False

            def _set_fatal_error(self, *args, **kwargs):
                self._running = False

        config = types.ModuleType("gateway.config")
        config.Platform = str
        base = types.ModuleType("gateway.platforms.base")
        base.BasePlatformAdapter = Base
        base.SendResult = lambda **kwargs: types.SimpleNamespace(**kwargs)
        memory = types.ModuleType("agent.memory_provider")

        def spawn(target, **kwargs):
            context = contextvars.copy_context()
            return threading.Thread(target=lambda: context.run(target), **kwargs)

        memory.spawn_context_thread = spawn
        spec = importlib.util.spec_from_file_location(
            "hermes_github_mentions._test_adapter", Path(plugin.__file__).with_name("adapter.py"))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"gateway.config": config, "gateway.platforms.base": base}):
            spec.loader.exec_module(module)
        return module, memory

    def test_adapter_starts_context_worker_and_disconnect_waits_for_handoff(self):
        module, memory = self.load_adapter()
        profile_context = contextvars.ContextVar("test_profile", default="wrong")
        started, release = threading.Event(), threading.Event()
        observed = []

        def poll(*_, **__):
            observed.append(profile_context.get())
            started.set()
            if not release.wait(2):
                raise AssertionError("Test did not release worker")
            return 60

        async def exercise():
            profile_context.set("right-profile")
            adapter = module.GitHubMentionsAdapter(None, self.ctx)
            try:
                self.assertTrue(await adapter.connect())
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertTrue(adapter._running)
                disconnect = asyncio.create_task(adapter.disconnect())
                await asyncio.sleep(0)
                with self.assertRaises(RuntimeError):
                    with state_lock(self.config.state_file):
                        self.fail("Lock released while poll was still running")
                release.set()
                await disconnect
                self.assertFalse(adapter.worker.is_alive())
                self.assertFalse(adapter._running)
                with state_lock(self.config.state_file):
                    pass
            finally:
                release.set()
                await adapter.disconnect()

        with patch.dict(sys.modules, {"agent.memory_provider": memory}), \
                patch.object(module, "scoped_config", return_value=self.config), \
                patch.object(module, "scoped_token", return_value="fake-token"), \
                patch.object(module, "scoped_secret", return_value=b"s" * 64), \
                patch.object(module, "request_json", return_value=({"login": "helper-bot"}, {})), \
                patch("hermes_github_mentions.runtime.Watcher.poll", side_effect=poll):
            asyncio.run(exercise())
        self.assertEqual(observed, ["right-profile"])

    def test_adapter_refuses_wrong_github_identity_before_starting_worker(self):
        module, _ = self.load_adapter()
        adapter = module.GitHubMentionsAdapter(None, self.ctx)
        with patch.object(module, "scoped_config", return_value=self.config), \
                patch.object(module, "scoped_token", return_value="fake-token"), \
                patch.object(module, "scoped_secret", return_value=b"s" * 64), \
                patch.object(module, "request_json", return_value=({"login": "wrong-user"}, {})), \
                self.assertLogs(level="ERROR"):
            self.assertFalse(asyncio.run(adapter.connect()))
        self.assertIsNone(adapter.worker)
