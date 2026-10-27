"""Opt-in checks against an already installed Hermes, with an isolated home.

Set HERMES_TEST_ROOT to its source root and put it on PYTHONPATH. Keep TMPDIR
outside this checkout because Plugin Doctor copies the plugin into TMPDIR.
"""

import argparse
import asyncio
import importlib
import io
import json
import os
import socket
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


@unittest.skipUnless(os.environ.get("HERMES_TEST_ROOT"), "Requires an existing Hermes installation")
class HermesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = Path(__file__).resolve().parents[1]
        temp_root = Path(tempfile.gettempdir()).resolve()
        if temp_root.is_relative_to(self.root):
            self.fail("Use a disk-backed TMPDIR outside the checkout for Plugin Doctor")
        env = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HERMES_ENABLE_PROJECT_PLUGINS": "0"})
        env.start()
        self.addCleanup(env.stop)
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"):
            blocker = patch(target, side_effect=AssertionError("Unmocked network request"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def test_hermes_admission_and_plugin_doctor(self):
        from hermes_cli.plugin_dev import doctor_plugin
        from hermes_cli.plugin_validate import validate_plugin_dir

        report = validate_plugin_dir(self.root)
        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.warnings, [])
        self.assertEqual(report.isolation["verdict"], "in_process")
        doctor = doctor_plugin(self.root)
        self.assertTrue(doctor.ok, doctor.format_text())

    def test_real_discovery_cli_settings_and_gateway_lifecycle(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from gateway.config import GatewayConfig, Platform
        from gateway.platforms.webhook import WebhookAdapter
        from gateway.session import SessionSource
        from gateway.platform_registry import platform_registry
        from gateway.status import flush_runtime_status
        from hermes_cli.plugins import PluginManager
        import hermes_yaml

        plugins = self.home / "plugins"
        plugins.mkdir()
        (plugins / "hermes-github-mentions").symlink_to(self.root, target_is_directory=True)
        manager = PluginManager()
        self.addCleanup(manager.unload)
        manifests = manager._scan_directory(plugins, source="user")
        self.assertEqual(len(manifests), 1)
        manager._load_plugin(manifests[0])
        loaded = manager._plugins["hermes-github-mentions"]
        self.assertTrue(loaded.enabled, loaded.error)
        command = manager._cli_commands["github-mentions"]
        parser = argparse.ArgumentParser()
        command["setup_fn"](parser)
        args = parser.parse_args(["configure", "--bot", "helper-bot", "--author", "owner"])
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(command["handler_fn"](args), 0)

        secrets = set_secret_scope({"GITHUB_MENTIONS_TOKEN": "fake-token",
                                    "GITHUB_MENTIONS_WEBHOOK_SECRET": "s" * 64,
                                    "DISCORD_ALLOWED_USERS": "111"}, profile_home=str(self.home))
        self.addCleanup(reset_secret_scope, secrets)
        self.addCleanup(flush_runtime_status)
        output_path = self.home / "routes.json"
        args = parser.parse_args(["routes", "--output", str(output_path)])
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(command["handler_fn"](args), 0)
        settings = hermes_yaml.safe_load((self.home / "config.yaml").read_text())
        settings["platforms"] = json.loads(output_path.read_text())["platforms"]
        gateway_config = GatewayConfig.from_dict(settings)
        self.assertNotIn(Platform.DISCORD, gateway_config.platforms)
        work_routes = json.loads(output_path.read_text())["platforms"]["webhook"]["extra"]["routes"]
        self.assertEqual(work_routes["github-mention-work"]["deliver"], "log")
        webhook = WebhookAdapter(gateway_config.platforms[Platform.WEBHOOK])
        for name, route in work_routes.items():
            webhook._validate_route(name, route)
        adapter_config = gateway_config.platforms[Platform("github_mentions")]
        self.assertTrue(adapter_config.enabled)
        entry = platform_registry.get("github_mentions")
        self.assertIsNotNone(entry)
        adapter = entry.adapter_factory(adapter_config)
        module = importlib.import_module(type(adapter).__module__)
        observed = []
        polled = threading.Event()

        def poll(watcher, token, state, **kwargs):
            from agent.secret_scope import get_secret
            from hermes_constants import get_hermes_home

            observed.append((get_hermes_home(), get_secret("GITHUB_MENTIONS_TOKEN")))
            polled.set()
            adapter.runtime.stop.set()
            return 60

        watcher_module = importlib.import_module(module.WatcherRuntime.__module__.rsplit(".", 1)[0] + ".watcher")

        async def exercise():
            self.assertTrue(await adapter.connect())
            self.assertTrue(await asyncio.to_thread(polled.wait, 2))
            chat = manager._plugin_commands["github-mentions"]["handler"]
            source = SessionSource(platform=Platform.DISCORD, chat_id="888", user_id="111")
            self.assertIn("this channel", chat("set", source=source))
            self.assertEqual(adapter.runtime.watcher.chat_id_provider(), "888")
            self.assertFalse(adapter.runtime.watcher.discord_enabled())  # Discord is not configured.
            self.assertIn("home channel", chat("reset", source=source))
            self.assertEqual(adapter.runtime.watcher.chat_id_provider(), "")
            self.assertFalse(adapter.runtime.watcher.discord_enabled())
            await adapter.disconnect()
            self.assertFalse(adapter.worker.is_alive())

        with patch.object(module, "request_json", return_value=({"login": "helper-bot"}, {})), \
                patch.object(watcher_module.Watcher, "poll", autospec=True, side_effect=poll):
            asyncio.run(exercise())
        self.assertEqual(observed, [(self.home, "fake-token")])
        args = parser.parse_args(["status"])
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(command["handler_fn"](args), 0)
        self.assertTrue(json.loads(output.getvalue())["initialized"])

    def test_github_only_work_route_can_finish_without_a_discord_adapter(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.webhook import WebhookAdapter

        webhook = WebhookAdapter(PlatformConfig())
        webhook._delivery_info = {"job": {"deliver": "log", "deliver_extra": {}}}
        result = asyncio.run(webhook.send("job", "Replied in the GitHub thread.", metadata={"notify": True}))
        self.assertTrue(result.success)

    def test_apply_routes_preserves_existing_config_and_keeps_secret_in_profile_store(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from hermes_cli.config import save_config, require_readable_config_before_write
        from hermes_github_mentions import plugin

        existing_route = {"secret": "existing-secret", "events": ["mention"], "deliver": "log"}
        save_config({"terminal": {"timeout": 137}, "platforms": {
            "discord": {"enabled": True},
            "webhook": {"routes": {"direct-custom": existing_route},
                        "extra": {"routes": {"extra-custom": existing_route}}},
        }})
        settings = {**plugin.DEFAULTS, "bot": "helper-bot", "allowed_authors": ["owner"]}
        ctx = types.SimpleNamespace(get_config=lambda key, default=None: settings.get(key, default))
        scope = set_secret_scope({"GITHUB_MENTIONS_WEBHOOK_SECRET": "s" * 64}, profile_home=str(self.home))
        self.addCleanup(reset_secret_scope, scope)
        parser = argparse.ArgumentParser()
        plugin.setup_parser(parser)
        args = parser.parse_args(["routes", "--apply"])
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(plugin.handle_command(ctx, args), 0)
            first = require_readable_config_before_write()
            self.assertEqual(plugin.handle_command(ctx, args), 0)
            self.assertEqual(first, require_readable_config_before_write())
        routes = first["platforms"]["webhook"]["extra"]["routes"]
        self.assertEqual(routes["direct-custom"], existing_route)
        self.assertEqual(routes["extra-custom"], existing_route)
        self.assertEqual(first["terminal"]["timeout"], 137)
        self.assertTrue(first["platforms"]["discord"]["enabled"])
        self.assertTrue(first["platforms"]["github_mentions"]["enabled"])
        self.assertEqual(routes["github-mention-work"]["secret"], "${GITHUB_MENTIONS_WEBHOOK_SECRET}")
        self.assertNotIn("s" * 64, (self.home / "config.yaml").read_text())
        self.assertNotIn("s" * 64, output.getvalue())
        self.assertFalse((self.home / "plugin-data" / plugin.PLUGIN_ID / "state.json").exists())

    def test_auto_discord_uses_real_profile_projection_without_borrowing_default(self):
        from datetime import datetime, timezone
        from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
        from gateway.status import GatewayLiveness
        from hermes_github_mentions.plugin import discord_available

        ctx = types.SimpleNamespace(get_config=lambda key, default=None: "coder" if key == "profile" else default)
        config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(
            enabled=True, home_channel=HomeChannel(Platform.DISCORD, "5678", "GitHub updates"))})
        snapshot = {"updated_at": datetime.now(timezone.utc).isoformat(), "platforms": {
            "discord": {"state": "connected"}, "coder:discord": {"state": "disconnected"}}}
        live = GatewayLiveness(running=True, pid=123, source="multiplexer", runtime=snapshot)
        with patch("gateway.config.load_gateway_config", return_value=config), \
                patch("gateway.status.resolve_gateway_liveness", return_value=live) as probe:
            self.assertFalse(discord_available(ctx))
            snapshot["platforms"]["coder:discord"]["state"] = "connected"
            self.assertTrue(discord_available(ctx))
        self.assertEqual(probe.call_args.kwargs["profile_dir"], self.home)

    def test_hermes_resolves_dynamic_destination_and_home_channel(self):
        from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
        from gateway.platforms.base import SendResult
        from gateway.platforms.webhook import WebhookAdapter

        target = types.SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
        config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(
            enabled=True, home_channel=HomeChannel(Platform.DISCORD, "5678", "GitHub updates"))})
        webhook = WebhookAdapter(PlatformConfig())
        webhook.gateway_runner = types.SimpleNamespace(config=config, _authorization_adapter=lambda *_: target)

        async def exercise():
            for selected, expected in (("", "5678"), ("9999", "9999"), ("", "5678")):
                extra = webhook._render_delivery_extra({"chat_id": "{discord_chat_id}"},
                                                       {"discord_chat_id": selected})
                result = await webhook._deliver_cross_platform("discord", "update", {"deliver_extra": extra})
                self.assertTrue(result.success)
                self.assertEqual(target.send.call_args.args[0], expected)
            config.platforms[Platform.DISCORD].home_channel = None
            result = await webhook._deliver_cross_platform("discord", "update", {"deliver_extra": {"chat_id": ""}})
            self.assertFalse(result.success)
            self.assertIn("home channel", result.error)

        asyncio.run(exercise())
