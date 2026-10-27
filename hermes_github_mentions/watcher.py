"""Mention detection, durable handoff, and optional Hermes progress tracking.

Extracted from the original personal watcher. No Hermes modules are imported.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .network import open_request, request_json
from .storage import save_state

API = "https://api.github.com"
DISCUSSION = re.compile(r"^https://api\.github\.com/repos/([^/]+)/([^/]+)/discussions/(\d+)$")
SUBJECT = re.compile(r"^https://api\.github\.com/repos/[^/]+/[^/]+/(issues|pulls)/(\d+)$")
DISCUSSION_FIELDS = "id url body author { login } createdAt updatedAt"
PAGE_INFO = "pageInfo { hasNextPage endCursor }"

GERMAN_WORDS = {"aber", "bitte", "das", "den", "der", "die", "doch", "du", "ein", "eine", "für",
                "ich", "ist", "kann", "kannst", "mit", "nicht", "noch", "oder", "prüf", "soll",
                "sollte", "und", "warum", "wir", "würde"}
ENGLISH_WORDS = {"and", "can", "could", "do", "does", "for", "how", "is", "it", "look", "need",
                 "not", "please", "should", "the", "this", "to", "we", "what",
                 "why", "would", "you"}
MESSAGES = {
    "en": {
        "mention": "GitHub mention from @{author} in {repo}\nTopic: {topic}\nRequest: {request}\n{url}",
        "started": "I picked up the GitHub request in {repo}.\nTopic: {topic}\nRequest: {request}\n{url}",
        "working": "GitHub request in {repo}\nTopic: {topic}\nRequest: {request}\n{steps} tool steps so far.\n{url}",
        "idle": "GitHub request in {repo}\nTopic: {topic}\nRequest: {request}\n{steps} tool steps; no activity for {minutes} min.\n{url}",
        "waiting": "The GitHub request in {repo} is waiting for the Hermes agent.\nTopic: {topic}\nRequest: {request}\n{url}",
        "no_request": "No instruction in the mention",
    },
    "de": {
        "mention": "GitHub-Erwähnung von @{author} in {repo}\nThema: {topic}\nAuftrag: {request}\n{url}",
        "started": "Ich habe den GitHub-Auftrag in {repo} übernommen.\nThema: {topic}\nAuftrag: {request}\n{url}",
        "working": "GitHub-Auftrag in {repo}\nThema: {topic}\nAuftrag: {request}\nBisher {steps} Arbeitsschritte.\n{url}",
        "idle": "GitHub-Auftrag in {repo}\nThema: {topic}\nAuftrag: {request}\n{steps} Arbeitsschritte; seit {minutes} Min. keine Aktivität.\n{url}",
        "waiting": "Der GitHub-Auftrag in {repo} wartet auf den Hermes-Agenten.\nThema: {topic}\nAuftrag: {request}\n{url}",
        "no_request": "Keine Anweisung in der Erwähnung",
    },
}
LOG = logging.getLogger("github-mention-watcher")


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(raw):
    return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None


def iso(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact(text, limit):
    """Keep GitHub text readable in Discord without repeating a whole comment."""
    value = re.sub(r"\s+", " ", text or "").strip()
    value = value.replace("@", "@\u200b")  # A quoted request should not ping Discord users.
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0].rstrip() + "…"
    return value


class WatcherStopped(RuntimeError):
    """Stop between network requests without advancing the saved cursor."""


class Watcher:
    def __init__(self, config, *, secret_provider=None, should_stop=None, chat_id_provider=None):
        self.config = config
        self.secret = secret_provider or config.secret
        self.should_stop = should_stop or (lambda: False)
        self.chat_id_provider = chat_id_provider
        self.mention = re.compile(
            r"(?<![A-Za-z0-9-])@" + re.escape(config.bot) + r"(?![A-Za-z0-9-])", re.IGNORECASE,
        )
        self.session_db = config.hermes_home / "state.db"
        self.gateway_state = config.hermes_home / "gateway_state.json"

    def check_running(self):
        if self.should_stop():
            raise WatcherStopped("Watcher is stopping")

    def request_json(self, *args, **kwargs):
        self.check_running()
        return request_json(*args, **kwargs)

    def pages(self, url, token, max_pages=10):
        results = []
        headers = None
        for _ in range(max_pages):
            data, headers = self.request_json(url, token)
            if not isinstance(data, list):
                raise ValueError("Expected a GitHub API list")
            results.extend(data)
            link = headers.get("Link", "")
            next_match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            if not next_match:
                return results, headers
            url = next_match.group(1)
        raise RuntimeError("GitHub result exceeded pagination limit; keeping cursor for retry")

    def matches(self, item, since):
        if not isinstance(item, dict):
            return False
        if str((item.get("user") or {}).get("login", "")).lower() not in self.config.allowed_authors:
            return False
        if not self.mention.search(item.get("body") or ""):
            return False
        changed = parse_time(item.get("updated_at") or item.get("submitted_at") or item.get("created_at"))
        return changed is not None and changed >= since

    def describe_mention(self, body, title):
        """Summarize the paragraph containing the configured bot mention."""
        mention = self.mention.search(body or "")
        if mention:
            start = (body.rfind("\n\n", 0, mention.start()) + 2) if "\n\n" in body[:mention.start()] else 0
            end = body.find("\n\n", mention.end())
            paragraph = body[start:end if end >= 0 else None]
            request = compact(self.mention.sub("", paragraph).strip(" ,:;\t\n"), 180)
        else:
            request = ""
        words = re.findall(r"[\wÄÖÜäöüß']+", request.lower())
        german = sum(word in GERMAN_WORDS for word in words)
        english = sum(word in ENGLISH_WORDS for word in words)
        language = "de" if german > english or (german == english and re.search(r"[äöüß]", request.lower())) else "en"
        topic = compact(title, 120) or "GitHub"
        return language, topic, request or MESSAGES[language]["no_request"]

    def format_message(self, kind, job, **extra):
        language = job.get("language", "en")
        phrases = MESSAGES.get(language, MESSAGES["en"])
        template = phrases[kind]
        return template.format(author=job.get("author", "owner"), repo=job["repo"], topic=job.get("topic") or "GitHub",
                               request=job.get("request") or phrases["no_request"],
                               url=job["url"], **extra)

    def mention_event(self, item, notification, kind):
        repo = notification["repository"]["full_name"]
        url = item.get("html_url") or item.get("url") or ""
        body = item.get("body") or ""
        subject_url = (notification.get("subject") or {}).get("url") or ""
        thread = SUBJECT.fullmatch(subject_url)
        thread_number = thread.group(2) if thread else ""
        if not thread_number:
            permalink = re.match(r"https://github\.com/[^/]+/[^/]+/(?:issues|pull)/(\d+)", url)
            thread_number = permalink.group(1) if permalink else ""
        review_comment_id = str(item.get("in_reply_to_id") or item.get("id") or "") if kind == "review_comment" else ""
        key = hashlib.sha256((url + "\n" + body).encode()).hexdigest()
        language, topic, request = self.describe_mention(body, notification.get("subject", {}).get("title", ""))
        return key, {
            "author": item["user"]["login"],
            "bot": self.config.bot,
            "repo": repo,
            "kind": kind,
            "url": url,
            "issue_number": thread_number,
            "review_comment_id": review_comment_id,
            "discussion_number": item.get("discussion_number", ""),
            "discussion_node_id": item.get("discussion_node_id", ""),
            "discussion_comment_id": item.get("id", "") if kind.startswith("discussion_") else "",
            "body": body[:6000],
            "thread_title": notification.get("subject", {}).get("title", ""),
            "language": language,
            "topic": topic,
            "request": request,
            "notice": self.format_message("mention", {"author": item["user"]["login"],
                                                  "repo": repo, "url": url, "language": language,
                                                  "topic": topic, "request": request}),
            "changed_at": item.get("updated_at") or item.get("submitted_at") or item.get("created_at"),
        }

    def graphql(self, query, variables, token):
        data, _ = self.request_json(f"{API}/graphql", token, {"query": query, "variables": variables})
        if data.get("errors"):
            raise RuntimeError("GitHub discussion query failed: " + json.dumps(data["errors"]))
        return data["data"]

    def discussion_connection(self, node_id, node_type, field, token):
        # Discussions have paginated comments; each comment has separately paginated replies.
        query = ("query($id:ID!,$after:String) { node(id:$id) { ... on " + node_type
                 + " { " + field + "(first:100,after:$after) { nodes { " + DISCUSSION_FIELDS
                 + " } " + PAGE_INFO + " } } } }")
        after = None
        for _ in range(100):
            data = self.graphql(query, {"id": node_id, "after": after}, token)
            if not data.get("node"):
                raise RuntimeError("GitHub discussion node is unavailable")
            connection = data["node"][field]
            yield from connection["nodes"]
            page = connection["pageInfo"]
            if not page["hasNextPage"]:
                return
            if not page["endCursor"] or page["endCursor"] == after:
                raise RuntimeError("GitHub discussion pagination did not advance")
            after = page["endCursor"]
        raise RuntimeError("GitHub discussion exceeded pagination limit; keeping cursor for retry")

    def discussion_candidates(self, subject_url, token, since):
        match = DISCUSSION.fullmatch(subject_url)
        if not match:
            raise ValueError("Unsupported GitHub discussion URL: " + subject_url)
        owner, name, number = match.groups()
        query = ("query($owner:String!,$name:String!,$number:Int!) { "
                 "repository(owner:$owner,name:$name) { discussion(number:$number) { "
                 + DISCUSSION_FIELDS + " } } }")
        data = self.graphql(query, {"owner": owner, "name": name, "number": int(number)}, token)
        discussion = (data.get("repository") or {}).get("discussion")
        if not discussion:
            raise RuntimeError("GitHub discussion is unavailable")

        def normalized(item):
            return {"id": item["id"], "html_url": item["url"], "body": item["body"],
                    "user": item.get("author"), "created_at": item["createdAt"],
                    "updated_at": item["updatedAt"], "discussion_number": number,
                    "discussion_node_id": discussion["id"]}

        # Comment activity must not make an old discussion body look newly mentioned.
        if parse_time(discussion["updatedAt"]) >= since:
            yield normalized(discussion), "discussion"
        for comment in self.discussion_connection(discussion["id"], "Discussion", "comments", token):
            yield normalized(comment), "discussion_comment"
            # Parent timestamps need not change when a new reply arrives.
            for reply in self.discussion_connection(comment["id"], "DiscussionComment", "replies", token):
                yield normalized(reply), "discussion_reply"

    def find_mentions(self, notification, token, since):
        if notification.get("reason") != "mention":
            return []
        repo = notification["repository"]["full_name"].lower()
        if self.config.repositories and repo not in self.config.repositories:
            return []
        subject = notification.get("subject") or {}
        subject_url = subject.get("url") or ""
        match = SUBJECT.fullmatch(subject_url)
        candidates = []
        if subject.get("type") == "Discussion":
            candidates.extend(self.discussion_candidates(subject_url, token, since))
        elif match:
            kind, number = match.groups()
            repo = notification["repository"]["full_name"]
            quoted_repo = "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/"))
            base = f"{API}/repos/{quoted_repo}"
            params = urllib.parse.urlencode({"since": iso(since), "per_page": 100})
            issue_comments, _ = self.pages(f"{base}/issues/{number}/comments?{params}", token)
            candidates.extend((comment, "issue_comment") for comment in issue_comments)
            if kind == "pulls":
                review_comments, _ = self.pages(f"{base}/pulls/{number}/comments?{params}", token)
                candidates.extend((comment, "review_comment") for comment in review_comments)
                reviews, _ = self.pages(f"{base}/pulls/{number}/reviews?per_page=100", token)
                candidates.extend((review, "review") for review in reviews)
            # New issue/PR bodies can mention the bot directly. The issue's updated_at
            # also changes when comments arrive, so use created_at to avoid old bodies.
            item, _ = self.request_json(subject_url, token)
            created = parse_time(item.get("created_at"))
            if created and created >= since:
                candidates.append((item, kind.rstrip("s")))
        elif subject.get("latest_comment_url"):
            # Other notification types may provide a REST comment URL.
            item, _ = self.request_json(subject["latest_comment_url"], token)
            candidates.append((item, subject.get("type", "comment")))
        return [self.mention_event(item, notification, kind) for item, kind in candidates if self.matches(item, since)]

    def load_state(self):
        identity = {"bot": self.config.bot.lower(), "profile": self.config.profile}
        if self.config.state_file.exists():
            data = json.loads(self.config.state_file.read_text())
            if (not isinstance(data, dict) or not parse_time(data.get("cursor"))
                    or any(not isinstance(data.get(k, {}), dict) for k in ("seen", "pending", "active"))):
                raise ValueError("Invalid state file; preserve it for recovery")
            if data.get("identity") != identity:
                raise ValueError("State belongs to a different bot/profile; choose a new state_file")
            return data
        return {"identity": identity, "cursor": iso(utc_now()), "seen": {}}

    def save_state(self, state):
        save_state(self.config.state_file, state)

    def gateway_draining(self):
        try:
            return bool(json.loads(self.gateway_state.read_text()).get("restart_requested"))
        except (OSError, ValueError):
            return False

    def post_route(self, route, event_id, payload, secret, event_type="mention"):
        self.check_running()
        if self.chat_id_provider is not None:
            # Resolve the plugin setting at delivery time, including for queued work.
            payload = {**payload, "discord_chat_id": self.chat_id_provider()}
        body = json.dumps(payload, ensure_ascii=False).encode()
        stamp = str(int(time.time()))
        signature = hmac.new(secret, stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"{self.config.webhook_url.rstrip('/')}/{route}", data=body, headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event_type,
            "X-Request-ID": event_id,
            "X-Webhook-Timestamp": stamp,
            "X-Webhook-Signature-V2": signature,
        })
        with open_request(req) as response:
            if response.status not in (200, 202):
                raise RuntimeError(f"Hermes webhook returned {response.status}")
            result = json.load(response)
            if result.get("status") not in ("accepted", "delivered", "duplicate"):
                raise RuntimeError("Hermes did not accept the event; check route configuration")

    def start_work(self, state, key, payload, secret):
        """Accept one saved mention for agent work, then record it atomically."""
        if payload["author"].lower() not in self.config.allowed_authors:
            raise ValueError("Queued request author is no longer allowed")
        if self.config.repositories and payload["repo"].lower() not in self.config.repositories:
            raise ValueError("Queued request repository is no longer allowed")
        seen = state["seen"].setdefault(key, {})
        if "github-mention-work" in seen:
            state.setdefault("pending", {}).pop(key, None)
            return False
        work_route = (self.config.work_route if payload["issue_number"]
                      else self.config.other_route)
        work_id = hashlib.sha256((key + "github-mention-work").encode()).hexdigest()
        self.post_route(work_route, work_id, payload, secret)
        accepted_at = iso(utc_now())
        seen["github-mention-work"] = accepted_at
        job = {
            "work_id": work_id,
            "repo": payload["repo"], "url": payload["url"],
            "route": work_route,
            "language": payload["language"], "topic": payload["topic"], "request": payload["request"],
            "started": accepted_at,
            "last_reported": None,
        }
        if self.config.progress:
            state.setdefault("active", {})[key] = job
        state.setdefault("pending", {}).pop(key, None)
        self.save_state(state)
        return True

    def poll(self, token, state, dry_run=False, bootstrap=False, allow_work=True):
        self.check_running()
        started = utc_now()
        cursor = parse_time(state["cursor"])
        since = cursor - timedelta(minutes=5)
        secret = self.secret() if not dry_run else b""
        pending = state.setdefault("pending", {})
        active = state.setdefault("active", {})
        delivered = 0
        if allow_work and not dry_run:
            # A GitHub notification may disappear or change reason during a long gateway restart.
            # Deliver saved requests before relying on the current notification list.
            for key, payload in list(pending.items()):
                if self.start_work(state, key, payload, secret):
                    delivered += 1
                else:
                    self.save_state(state)
        query = urllib.parse.urlencode({"all": "true", "participating": "true", "since": iso(since), "per_page": 50})
        notifications, headers = self.pages(f"{API}/notifications?{query}", token)
        interval = max(self.config.poll_seconds, int(headers.get("X-Poll-Interval", "60")))
        for notification in reversed(notifications):
            self.check_running()
            for key, payload in self.find_mentions(notification, token, since):
                if bootstrap and parse_time(payload["changed_at"]) < cursor:
                    continue
                if dry_run:
                    LOG.info("Would process %s in %s", payload["kind"], payload["repo"])
                    continue
                seen = state["seen"].setdefault(key, {})
                # Save the request before trying delivery. Recover even if its
                # GitHub notification changes or disappears after a failed POST.
                if "github-mention-work" not in seen and key not in pending:
                    pending[key] = payload
                    self.save_state(state)
                started_now = False
                if allow_work and "github-mention-work" not in seen:
                    started_now = self.start_work(state, key, payload, secret)
                    delivered += int(started_now)
                notice_sent = False
                if self.config.notice_route and "github-mention-notice" not in seen:
                    # After work is accepted, one Discord message covers both the
                    # mention and the start. During gateway drain it only announces
                    # the mention; the work route is retried later.
                    payload["notice"] = self.format_message("started" if allow_work else "mention", payload)
                    event_id = hashlib.sha256((key + "github-mention-notice").encode()).hexdigest()
                    self.post_route(self.config.notice_route, event_id, payload, secret)
                    seen["github-mention-notice"] = iso(utc_now())
                    self.save_state(state)
                    notice_sent = True
                if not allow_work:
                    continue
                if started_now and notice_sent and key in active:
                    active[key]["last_reported"] = iso(utc_now())
                    self.save_state(state)
        if not dry_run:
            if allow_work:
                state["cursor"] = iso(started)
            cutoff = started - timedelta(days=90)
            state["seen"] = {k: v for k, v in state["seen"].items()
                             if max((parse_time(t) for t in v.values()), default=cutoff) >= cutoff}
            self.save_state(state)
        LOG.info("Checked %d notifications, delivered %d mentions; next check in %ds",
                 len(notifications), delivered, interval)
        return interval

    def report_active(self, state):
        """Show bounded progress for webhook jobs, whose native tool progress is disabled."""
        if not self.config.progress:
            return
        active = state.get("active") or {}
        if not active:
            return
        now = utc_now()
        secret = self.secret()
        with closing(sqlite3.connect(self.session_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            for key, job in list(active.items()):
                route = job.get("route", "github-mention-work")
                # Hermes now encodes (profile, route, delivery) in the chat ID.
                # Keep the legacy lookup for work accepted before that change.
                identity = json.dumps((self.config.profile, route, job["work_id"]),
                                      ensure_ascii=False, separators=(",", ":")).encode()
                chat_id = "webhook:v2:" + base64.urlsafe_b64encode(identity).decode("ascii").rstrip("=")
                legacy_id = f"webhook:{route}:{job['work_id']}"
                row = db.execute(
                    "SELECT id, ended_at, last_activity_at, tool_call_count FROM sessions "
                    "WHERE chat_id IN (?, ?) OR session_key LIKE ? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (chat_id, legacy_id, f"%{legacy_id}%"),
                ).fetchone()
                # Some webhook sessions leave ended_at empty after a final response.
                # Check the latest turn so an earlier reply cannot hide resumed work.
                latest = db.execute(
                    "SELECT role, finish_reason FROM messages WHERE session_id = ? "
                    "AND role IN ('user', 'assistant', 'tool') ORDER BY id DESC LIMIT 1",
                    (row[0],),
                ).fetchone() if row and row[1] is None else None
                finished = row and (row[1] is not None or latest == ("assistant", "stop"))
                if finished:
                    active.pop(key)
                    self.save_state(state)
                    continue
                last_reported = parse_time(job.get("last_reported"))
                if last_reported and now - last_reported < timedelta(minutes=2):
                    continue
                if not last_reported and now - parse_time(job["started"]) < timedelta(seconds=30):
                    continue
                if row:
                    steps = row[3] or 0
                    if job.get("stale_reported") and row[2] == job.get("last_reported_activity"):
                        continue
                    if last_reported and steps == job.get("last_reported_steps") \
                            and now - last_reported < timedelta(minutes=5):
                        continue
                    idle_minutes = max(0, int((now.timestamp() - (row[2] or now.timestamp())) // 60))
                    message = self.format_message("idle" if idle_minutes >= 2 else "working", job,
                                             steps=steps, minutes=idle_minutes)
                    job["stale_reported"] = idle_minutes >= 30
                    job["last_reported_activity"] = row[2]
                else:
                    # Missing sessions must not generate a new waiting notice forever.
                    if job.get("waiting_reported"):
                        continue
                    steps = 0
                    message = self.format_message("waiting", job)
                event_id = hashlib.sha256((key + "progress" + iso(now)).encode()).hexdigest()
                self.post_route(self.config.progress_route, event_id, {"message": message}, secret, event_type="progress")
                if not row:
                    job["waiting_reported"] = True
                job["last_reported"] = iso(now)
                job["last_reported_steps"] = steps
                self.save_state(state)
                LOG.info("Reported progress for %s", job["repo"])
