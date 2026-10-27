import copy
import gc
import hashlib
import hmac
import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request

from hermes_github_mentions.cli import main, retry_seconds
from hermes_github_mentions.config import Config, load_config
from hermes_github_mentions.network import NoRedirects, request_json
from hermes_github_mentions.routes import build_routes
from hermes_github_mentions.storage import state_lock
from hermes_github_mentions.watcher import Watcher, iso, utc_now


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(json.dumps(data).encode())
        self.status = status
        self.headers = headers or {}


class WatcherTests(unittest.TestCase):
    def setUp(self):
        # Missing mocks must fail before any network access, including localhost.
        for target in ("hermes_github_mentions.network.open_request",
                       "hermes_github_mentions.watcher.open_request"):
            blocker = patch(target, side_effect=AssertionError("Unmocked network request"))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(bot="helper-bot", allowed_authors=("owner",),
                             state_file=self.root / "state.json",
                             secret_file=self.root / "webhook.secret", hermes_home=self.root)
        self.config.secret_file.write_text("s" * 64)
        self.watcher = Watcher(self.config)
        self.now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        self.clock = patch("hermes_github_mentions.watcher.utc_now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.item = {"id": 123, "user": {"login": "owner"}, "body": "@helper-bot please rebase",
                     "html_url": "https://github.com/team/project/pull/42#issuecomment-123",
                     "updated_at": iso(self.now), "created_at": iso(self.now)}
        self.notification = {"reason": "mention", "repository": {"full_name": "team/project"},
                             "subject": {"type": "PullRequest", "title": "Fix a bug",
                                         "url": "https://api.github.com/repos/team/project/pulls/42"}}
        self.calls = []

    def transport(self, req):
        self.calls.append(req)
        parsed = urlsplit(req.full_url)
        if parsed.hostname == "127.0.0.1":
            return Response({"status": "accepted"}, status=202)
        if parsed.path == "/user":
            return Response({"login": self.config.bot})
        if parsed.path == "/notifications":
            return Response([self.notification], headers={"X-Poll-Interval": "90"})
        if parsed.path.endswith("/issues/42/comments"):
            return Response([self.item])
        if parsed.path.endswith(("/pulls/42/comments", "/pulls/42/reviews")):
            return Response([])
        if parsed.path.endswith("/pulls/42"):
            return Response({"created_at": "2025-01-01T00:00:00Z"})
        raise AssertionError(f"Unexpected request: {req.full_url}")

    def fake_network(self):
        stack = __import__("contextlib").ExitStack()
        stack.enter_context(patch("hermes_github_mentions.network.open_request", side_effect=self.transport))
        stack.enter_context(patch("hermes_github_mentions.watcher.open_request", side_effect=self.transport))
        return stack

    def test_only_allowed_authors_and_exact_bot_mentions_match(self):
        for author, text, expected in [
            ("OWNER", "@HELPER-BOT please fix", True),
            ("stranger", "@helper-bot please fix", False),
            ("owner", "@helper-bot-other please fix", False),
            ("owner", "prefix@helper-bot please fix", False),
            ("owner", "hello (@helper-bot), please fix", True),
        ]:
            with self.subTest(author=author, text=text):
                item = {**self.item, "body": text, "user": {"login": author}}
                self.assertEqual(self.watcher.matches(item, self.now - timedelta(minutes=1)), expected)

    def test_repository_allowlist_filters_before_fetching_comments(self):
        watcher = Watcher(replace(self.config, repositories=("another/repo",)))
        with patch("hermes_github_mentions.watcher.request_json") as request:
            self.assertEqual(watcher.find_mentions(self.notification, "fake", self.now), [])
        request.assert_not_called()

    def test_poll_handoff_is_signed_and_repeated_poll_does_not_duplicate_work(self):
        state = self.watcher.load_state()
        with self.fake_network():
            self.assertEqual(self.watcher.poll("fake", state), 90)
            self.watcher.poll("fake", state)
        posts = [r for r in self.calls if r.data is not None]
        self.assertEqual(len(posts), 2)  # Work and private notice.
        work = posts[0]
        headers = {k.lower(): v for k, v in work.header_items()}
        expected = hmac.new(self.config.secret(), headers["x-webhook-timestamp"].encode() + b"." + work.data,
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-webhook-signature-v2"], expected)
        self.assertEqual(json.loads(work.data)["bot"], "helper-bot")
        self.assertEqual(json.loads(work.data)["author"], "owner")
        self.assertEqual(state.get("pending"), {})
        self.assertEqual(state.get("active"), {})
        self.assertEqual(self.config.state_file.stat().st_mode & 0o777, 0o600)

    def test_failed_delivery_is_saved_before_post_and_retried_after_notification_disappears(self):
        state = self.watcher.load_state()
        cursor = state["cursor"]
        with self.fake_network(), patch.object(self.watcher, "post_route", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                self.watcher.poll("fake", state)
        saved = json.loads(self.config.state_file.read_text())
        self.assertEqual(saved["cursor"], cursor)
        self.assertEqual(len(saved["pending"]), 1)
        with patch.object(self.watcher, "pages", return_value=([], {})), \
                patch.object(self.watcher, "post_route") as post:
            self.watcher.poll("fake", saved)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(saved["pending"], {})

    def test_gateway_drain_saves_request_and_notice_then_recovers_work(self):
        state = self.watcher.load_state()
        with self.fake_network():
            self.watcher.poll("fake", state, allow_work=False)
        self.assertEqual(len(state["pending"]), 1)
        self.assertEqual([urlsplit(r.full_url).path for r in self.calls if r.data],
                         ["/webhooks/github-mention-notice"])
        self.calls.clear()
        with self.fake_network():
            self.watcher.poll("fake", state)
        self.assertEqual([urlsplit(r.full_url).path for r in self.calls if r.data],
                         ["/webhooks/github-mention-work"])

    def test_notice_failure_does_not_repeat_accepted_work(self):
        state = self.watcher.load_state()
        with self.fake_network(), patch.object(self.watcher, "post_route", side_effect=[None, OSError("offline")]):
            with self.assertRaises(OSError):
                self.watcher.poll("fake", state)
        state = self.watcher.load_state()
        with self.fake_network(), patch.object(self.watcher, "post_route") as post:
            self.watcher.poll("fake", state)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], self.config.notice_route)

    def test_dry_run_has_no_writes_even_on_fresh_bootstrap(self):
        self.item["updated_at"] = iso(self.now - timedelta(minutes=1))
        before = sorted(p.name for p in self.root.iterdir())
        with self.fake_network(), patch.object(self.watcher, "post_route") as post:
            self.watcher.poll("fake", self.watcher.load_state(), dry_run=True, bootstrap=True)
        post.assert_not_called()
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))

    def test_fresh_start_does_not_replay_older_mentions(self):
        self.item["updated_at"] = iso(self.now - timedelta(minutes=1))
        state = self.watcher.load_state()
        with self.fake_network(), patch.object(self.watcher, "post_route") as post:
            self.watcher.poll("fake", state, bootstrap=True)
        post.assert_not_called()

    def test_edited_request_gets_new_delivery_and_review_reply_uses_root(self):
        first, _ = self.watcher.mention_event(self.item, self.notification, "review_comment")
        changed = {**self.item, "body": "@helper-bot please fix tests", "in_reply_to_id": 99}
        second, payload = self.watcher.mention_event(changed, self.notification, "review_comment")
        self.assertNotEqual(first, second)
        self.assertEqual(payload["review_comment_id"], "99")

    def test_old_pr_body_is_not_replayed_for_new_comment_activity(self):
        with self.fake_network():
            found = self.watcher.find_mentions(self.notification, "fake", self.now)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1]["kind"], "issue_comment")

    def test_discussion_replies_are_scanned_even_when_parent_is_old(self):
        discussion = {"id": "D_1", "url": "https://github.com/team/project/discussions/42",
                      "body": "old topic", "author": {"login": "owner"},
                      "createdAt": "2025-01-01T00:00:00Z", "updatedAt": "2025-01-01T00:00:00Z"}
        comment = {**discussion, "id": "DC_1"}
        reply = {**discussion, "id": "DC_2", "body": self.item["body"], "updatedAt": iso(self.now)}
        with patch.object(self.watcher, "graphql", return_value={"repository": {"discussion": discussion}}), \
                patch.object(self.watcher, "discussion_connection", side_effect=[[comment], [reply]]):
            found = list(self.watcher.discussion_candidates(
                "https://api.github.com/repos/team/project/discussions/42", "fake", self.now))
        self.assertEqual([kind for item, kind in found if self.watcher.matches(item, self.now)],
                         ["discussion_reply"])

    def test_rest_pagination_follows_all_pages_and_limit_failure_preserves_cursor(self):
        with patch("hermes_github_mentions.watcher.request_json", side_effect=[
            ([1], {"Link": '<https://api.github.com/page2>; rel="next"'}), ([2], {}),
        ]):
            self.assertEqual(self.watcher.pages("https://api.github.com/page1", "fake")[0], [1, 2])
        state = self.watcher.load_state()
        original = state["cursor"]
        with patch("hermes_github_mentions.watcher.request_json", return_value=(
            [], {"Link": '<https://api.github.com/next>; rel="next"'},
        )):
            with self.assertRaises(RuntimeError):
                self.watcher.poll("fake", state)
        self.assertEqual(state["cursor"], original)

    def test_graphql_pagination_refuses_nonadvancing_cursor(self):
        data = {"node": {"comments": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "same"}}}}
        with patch.object(self.watcher, "graphql", return_value=data):
            with self.assertRaises(RuntimeError):
                list(self.watcher.discussion_connection("D_1", "Discussion", "comments", "fake"))

    def test_ignored_webhook_response_is_not_marked_as_accepted(self):
        state = self.watcher.load_state()
        key, payload = self.watcher.mention_event(self.item, self.notification, "issue_comment")
        with patch("hermes_github_mentions.watcher.open_request", return_value=Response({"status": "ignored"})):
            with self.assertRaises(RuntimeError):
                self.watcher.start_work(state, key, payload, self.config.secret())
        self.assertNotIn("github-mention-work", state["seen"][key])

    def test_corrupt_state_and_identity_change_fail_without_resetting_cursor(self):
        self.config.state_file.write_text("not json")
        with self.assertRaises(ValueError):
            self.watcher.load_state()
        self.config.state_file.unlink()
        self.watcher.save_state(self.watcher.load_state())
        with self.assertRaises(ValueError):
            Watcher(replace(self.config, bot="another-bot")).load_state()

    def test_pending_author_must_still_be_allowed(self):
        key, payload = self.watcher.mention_event(self.item, self.notification, "issue_comment")
        watcher = Watcher(replace(self.config, allowed_authors=("someone-else",)))
        with patch.object(watcher, "post_route") as post:
            with self.assertRaises(ValueError):
                watcher.start_work(watcher.load_state(), key, payload, self.config.secret())
        post.assert_not_called()

    def test_second_process_cannot_share_state(self):
        with state_lock(self.config.state_file):
            with self.assertRaises(RuntimeError):
                with state_lock(self.config.state_file):
                    self.fail("Second writer acquired state")

    def test_progress_completion_and_missing_session_notices(self):
        self.config = replace(self.config, progress=True)
        self.watcher = Watcher(self.config)
        db = sqlite3.connect(self.root / "state.db")
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE sessions (id TEXT, chat_id TEXT, session_key TEXT,
                ended_at REAL, last_activity_at REAL, tool_call_count INTEGER, started_at REAL);
            CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, finish_reason TEXT);
        """)
        state = self.watcher.load_state()
        key, payload = self.watcher.mention_event(self.item, self.notification, "issue_comment")
        with patch.object(self.watcher, "post_route"):
            self.watcher.start_work(state, key, payload, self.config.secret())
        state["active"][key]["started"] = iso(self.now - timedelta(minutes=10))
        with patch.object(self.watcher, "post_route") as post:
            self.watcher.report_active(state)
            state["active"][key]["last_reported"] = iso(self.now - timedelta(minutes=10))
            self.watcher.report_active(state)
        self.assertEqual(post.call_count, 1)
        work_id = state["active"][key]["work_id"]
        import base64
        chat_id = "webhook:v2:" + base64.urlsafe_b64encode(json.dumps(
            ("default", self.config.work_route, work_id), separators=(",", ":")
        ).encode()).decode().rstrip("=")
        db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                   ("session1", chat_id, "", None, self.now.timestamp(), 3, self.now.timestamp()))
        db.execute("INSERT INTO messages VALUES (1, 'session1', 'assistant', 'stop')")
        db.commit()
        job = copy.deepcopy(state["active"][key])
        self.watcher.report_active(state)
        self.assertEqual(state["active"], {})
        # Resumed work must not be hidden by an earlier assistant response.
        db.execute("INSERT INTO messages VALUES (2, 'session1', 'user', NULL)")
        db.commit()
        state["active"][key] = job
        with patch.object(self.watcher, "post_route"):
            self.watcher.report_active(state)
        self.assertIn(key, state["active"])
        # Exercise actual connections with cyclic GC disabled, reproducing the old leak.
        if Path("/proc/self/fd").exists():
            before = len(list(Path("/proc/self/fd").iterdir()))
            gc.disable()
            try:
                for _ in range(100):
                    self.watcher.report_active(state)
            finally:
                gc.enable()
            self.assertEqual(before, len(list(Path("/proc/self/fd").iterdir())))

    def test_progress_schema_failure_does_not_leak_connection(self):
        sqlite3.connect(self.root / "state.db").close()
        watcher = Watcher(replace(self.config, progress=True))
        state = {"active": {"job": {"route": "work", "work_id": "1"}}}
        with self.assertRaises(sqlite3.OperationalError):
            watcher.report_active(state)
        if Path("/proc/self/fd").exists():
            before = len(list(Path("/proc/self/fd").iterdir()))
            for _ in range(30):
                with self.assertRaises(sqlite3.OperationalError):
                    watcher.report_active(state)
            self.assertEqual(before, len(list(Path("/proc/self/fd").iterdir())))

    def test_network_rejects_other_hosts_plaintext_and_redirects(self):
        with patch("hermes_github_mentions.network.open_request") as send:
            for url in ("http://api.github.com/user", "https://evil.test/user", "https://api.github.com.evil.test"):
                with self.assertRaises(ValueError):
                    request_json(url, "fake")
        send.assert_not_called()
        with self.assertRaises(ValueError):
            NoRedirects().redirect_request(Request("https://api.github.com/user"), None, 302, "", {}, "https://evil.test")

    def test_config_paths_validation_and_token_precedence(self):
        path = self.root / "config.toml"
        path.write_text('bot="helper-bot"\nallowed_authors=["OWNER"]\nstate_file="queue/state.json"\n'
                        'secret_file="webhook.secret"\ntoken_file="token.secret"\n')
        config = load_config(path)
        self.assertEqual(config.state_file, self.root / "queue" / "state.json")
        self.assertEqual(config.allowed_authors, ("owner",))
        config.token_file.write_text("file-token")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "environment-token"}):
            self.assertEqual(config.token(), "environment-token")
        with self.assertRaises(ValueError):
            replace(config, webhook_url="https://public.example/webhooks")
        with self.assertRaises(ValueError):
            replace(config, allowed_authors=())
        with self.assertRaises(ValueError):
            replace(config, poll_seconds=1)
        path.write_text(path.read_text() + '\nmisspelled = true\n')
        with self.assertRaises(ValueError):
            load_config(path)

    def test_route_fragment_uses_private_delivery_and_separate_direct_routes(self):
        routes = build_routes(replace(self.config, progress=True), "1234")["platforms"]["webhook"]["extra"]["routes"]
        for name in (self.config.work_route, self.config.other_route):
            self.assertEqual(routes[name]["deliver"], "discord")
            self.assertNotIn("deliver_only", routes[name])
            self.assertIn("terminal", routes[name]["toolsets"])
            self.assertEqual(routes[name]["deliver_extra"]["chat_id"], "1234")
        self.assertTrue(routes[self.config.progress_route]["deliver_only"])
        self.assertTrue(routes[self.config.notice_route]["deliver_only"])
        self.assertEqual(routes[self.config.work_route]["secret"], self.config.secret().decode())

    def test_profile_and_custom_port_match_generated_routes(self):
        with self.assertRaises(ValueError):
            replace(self.config, profile="coder")
        config = replace(self.config, profile="coder", webhook_url="http://127.0.0.1:9123/p/coder/webhooks")
        extra = build_routes(config, "1234")["platforms"]["webhook"]["extra"]
        self.assertEqual(extra["port"], 9123)
        self.assertTrue(all(route["profile"] == "coder" for route in extra["routes"].values()))

    def test_routes_default_to_home_channel_and_native_routes_accept_dynamic_destination(self):
        config = replace(self.config, progress=True)
        routes = build_routes(config)["platforms"]["webhook"]["extra"]["routes"]
        self.assertTrue(all("deliver_extra" not in route for route in routes.values()))
        dynamic = build_routes(config, dynamic_chat_id=True)["platforms"]["webhook"]["extra"]["routes"]
        self.assertTrue(all(route["deliver_extra"]["chat_id"] == "{discord_chat_id}" for route in dynamic.values()))

    def test_destination_setting_is_resolved_for_each_post_without_mutating_saved_payload(self):
        destination = ["888"]
        watcher = Watcher(self.config, chat_id_provider=lambda: destination[0])
        payload = {"message": "test", "discord_chat_id": "stale"}
        with self.fake_network():
            watcher.post_route("route", "one", payload, self.config.secret())
            destination[0] = ""
            watcher.post_route("route", "two", payload, self.config.secret())
        self.assertEqual([json.loads(call.data)["discord_chat_id"] for call in self.calls], ["888", ""])
        self.assertEqual(payload["discord_chat_id"], "stale")

    def test_standalone_route_export_does_not_require_channel_id(self):
        path = self.root / "config.toml"
        path.write_text('bot="helper-bot"\nallowed_authors=["owner"]\nstate_file="state.json"\nsecret_file="webhook.secret"\n')
        output = self.root / "routes.json"
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--config", str(path), "routes", "--output", str(output)]), 0)
        routes = json.loads(output.read_text())["platforms"]["webhook"]["extra"]["routes"]
        self.assertTrue(all("deliver_extra" not in route for route in routes.values()))

    def test_cli_status_needs_neither_token_nor_secret(self):
        config_path = self.root / "config.toml"
        config_path.write_text('bot="helper-bot"\nallowed_authors=["owner"]\nstate_file="state.json"\nsecret_file="missing.secret"\n')
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["--config", str(config_path), "status"]), 0)
        self.assertFalse(json.loads(output.getvalue())["initialized"])
        self.assertFalse(self.config.state_file.exists())

    def test_cli_init_and_routes_are_separate_from_live_hermes(self):
        self.config.secret_file.unlink()
        config_path = self.root / "config.toml"
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--config", str(config_path), "init", "--bot", "helper-bot", "--author", "owner"]), 0)
            output = self.root / "routes.json"
            self.assertEqual(main(["--config", str(config_path), "routes", "--discord-chat-id", "1234",
                                   "--output", str(output)]), 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.config.state_file.exists())
        self.assertFalse((self.root / "config.yaml").exists())
        with patch("sys.stdout", new_callable=io.StringIO), self.assertLogs(level="ERROR"):
            self.assertEqual(main(["--config", str(config_path), "init", "--bot", "other", "--author", "owner"]), 1)

    def test_cli_dry_run_checks_identity_and_does_not_create_state_or_lock(self):
        config_path = self.root / "config.toml"
        config_path.write_text('bot="helper-bot"\nallowed_authors=["owner"]\nstate_file="state.json"\nsecret_file="missing.secret"\n')
        args = ["--config", str(config_path), "run", "--dry-run"]
        with self.fake_network(), patch.dict(os.environ, {"GITHUB_TOKEN": "fake"}):
            self.assertEqual(main(args), 0)
        self.assertFalse(self.config.state_file.exists())
        self.assertFalse(Path(str(self.config.state_file) + ".lock").exists())
        with patch("hermes_github_mentions.cli.request_json", return_value=({"login": "wrong-user"}, {})), \
                patch.dict(os.environ, {"GITHUB_TOKEN": "fake"}), self.assertLogs(level="ERROR"):
            self.assertEqual(main(args), 1)

    def test_rate_limit_retry_respects_server_delay(self):
        import urllib.error
        error = urllib.error.HTTPError("https://api.github.com/notifications", 429, "limited",
                                        {"Retry-After": "180"}, None)
        self.addCleanup(error.close)
        self.assertGreaterEqual(retry_seconds(error, 60), 180)


if __name__ == "__main__":
    unittest.main()
