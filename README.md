# hermes-github-mentions

Give your self-hosted [Hermes Agent](https://github.com/NousResearch/hermes-agent)
a task by mentioning its bot on GitHub. The plugin checks the bot's notifications,
accepts requests from approved users, and asks Hermes to reply in the original
thread. Discord is used automatically when available and is optional.

```text
GitHub @mention → approved author → signed local webhook → Hermes → GitHub reply
```

A native Hermes plugin with no extra Python dependencies. Supports issue and PR
comments, inline reviews, and Discussions. No public webhook endpoint or
repository allowlist is needed. Approved users can request work wherever the bot
has access and receives mention notifications.

## Requirements

- Hermes with native directory plugins, gateway adapters, and
  [webhooks](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks).
- Python 3.11+ on Linux or macOS, and [GitHub CLI](https://cli.github.com/)
  available in Hermes' terminal environment.
- A separate GitHub bot account with access to the repositories it will work on.

## Setup

Run these commands on the Hermes host, in the profile that should handle requests.
The examples use Bash. Replace `example-bot` with the bot's GitHub username and
`example-owner` with your own username, without `@`.

1. **Create the bot token.** Sign in to GitHub as the bot, then open
   **Settings → Developer settings → Personal access tokens → Tokens (classic)**.
   Create a token with `repo` and `notifications` scopes. Authorize organization
   SSO if required, and accept any repository invitations as the bot.
   [GitHub's notification API requires a classic token](https://docs.github.com/en/rest/activity/notifications);
   fine-grained tokens and GitHub App tokens do not work here. GitHub.com only.

2. **Install and choose who can give tasks.**

   ```bash
   hermes plugins install luantak/hermes-github-mentions --enable
   hermes github-mentions configure --bot example-bot --author example-owner
   ```

   Repeat `--author USERNAME` in the same command to approve more users.
   The plugin uses Hermes' default `in_process` mode; plugin-host isolation does
   not support gateway adapters. Configuration warnings during initial install
   are resolved by completing the remaining steps.

3. **Save the bot credentials.** Paste the token at the hidden prompt:

   ```bash
   read -r -s -p "Bot GitHub token: " github_mentions_token
   printf '\n'
   hermes config set GITHUB_TOKEN "$github_mentions_token"
   unset github_mentions_token
   hermes config set GITHUB_MENTIONS_WEBHOOK_SECRET "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
   ```

   The plugin and Hermes' GitHub tools use the same `GITHUB_TOKEN`.
   The last command creates the shared local webhook secret. Keep it unchanged
   unless rotating it deliberately. Secrets belong to the active Hermes profile;
   Desktop users can also fill the plugin's secret fields there.
   If your terminal runtime supplies `GH_TOKEN`, it must also belong to the bot:
   [it takes precedence over `GITHUB_TOKEN`](https://cli.github.com/manual/gh_help_environment).
   For Docker or SSH terminals, make sure `gh` and the bot credentials are
   available inside that environment too.

4. **Configure the local webhook and check access.**

   ```bash
   hermes github-mentions routes --apply
   hermes github-mentions run --dry-run
   ```

   `--apply` saves the routes and enables the `github_mentions` adapter in the
   active profile, preserving other platforms and routes. It grants work runs
   the `terminal`, `file`, `web`, `skills`, and `todo` toolsets. To review the
   generated configuration first, use `routes --output ./hermes-routes.json`
   instead; that file contains a secret and must not be committed.
   The default listener is `127.0.0.1:8644`. If your gateway uses another port,
   include `--webhook-url http://127.0.0.1:PORT/webhooks` in step 2 before applying.
   A successful dry run confirms bot identity and notification access; it starts
   no work and writes no state.

5. **Restart and verify.** For a gateway installed as a background service:

   ```bash
   hermes gateway restart
   hermes gateway status
   hermes github-mentions status
   ```

   For a foreground gateway, stop it and run `hermes gateway run` again.
   Check that `github_mentions` is connected and watcher status becomes
   `"initialized": true` with an advancing cursor. Hermes owns the worker;
   no separate watcher service is needed.

6. **Try a new mention.** From an approved account, post a comment in a repository
   the bot can access:

   ```text
   @example-bot Please reply here with "GitHub mentions are working". Do not change files.
   ```

   Create it after the gateway starts. Polling takes at least 60 seconds, plus
   the agent's response time. Success is a reply from the bot in that GitHub
   thread, not just a Discord notice or successful webhook handoff.

For a multiplexed profile, also pass `--profile NAME` and
`--webhook-url http://127.0.0.1:8644/p/NAME/webhooks` in step 2, using that profile's
Hermes context for every command. See the [settings example](examples/hermes-plugin.example.yaml).

## Optional Discord updates

If the same Hermes profile has Discord connected and a home channel configured,
it receives status notices and copies of replies. Otherwise work stays on GitHub.
In Discord, `/github-mentions set` chooses the current channel or thread,
`/github-mentions status` shows the destination, and `/github-mentions reset`
restores the home channel. Only explicitly allowed Discord users can change it.

Add `--no-discord` to the configure command to disable notices, or `--discord` to
restore automatic use. No channel ID is required. Changes affect new requests;
running requests keep their original destination. Failed notices do not block work.

## Troubleshooting and recovery

| Symptom | Check |
|---|---|
| Command not found | Confirm plugin installation and enablement with `hermes plugins list`. |
| Token identity mismatch or HTTP 401 | Use a valid classic token owned by the configured bot. |
| No mention detected | Create a new mention from an approved user; check repository access and the bot's GitHub notifications. |
| Local webhook rejected or unreachable | Check the configured URL, shared secret, applied routes, and gateway restart. |
| Work starts but no GitHub reply | Check `gh api user --jq .login` inside Hermes' terminal environment; it must show the bot. Check its write permissions and gateway logs. |
| Another watcher holds the lock | Run one worker per state file. Stop foreground polling or the separate service when using the native adapter. |

State is saved under `<Hermes home>/plugin-data/hermes-github-mentions/state.json`.
Don't delete it to fix delivery errors. Queued requests survive restarts; stable
IDs suppress duplicate deliveries, but exactly-once execution is not guaranteed.
A fresh state starts now, without replaying old mentions. Detection depends on
notifications whose current reason is `mention`, not a search of all comments.

The agent is instructed to verify work and post one final reply. Those are
instructions, not an enforced publishing gate. Repository content remains
untrusted; configure Hermes' execution permissions accordingly. Optional
`--progress` notices read Hermes' internal SQLite schema and can break after
upgrades. Disable them if incompatible. No Hermes source patches are required.

## Standalone CLI

From a checkout, without installing dependencies:

```bash
python3 -m hermes_github_mentions init --bot example-bot --author example-owner
python3 -m hermes_github_mentions routes --output ./hermes-routes.json
python3 -m hermes_github_mentions run --dry-run
python3 -m hermes_github_mentions run
```

Before running, set `GITHUB_TOKEN` in the watcher environment or `token_file` in
`~/.config/hermes-github-mentions/config.toml`, and merge the exported `platforms`
settings into Hermes' `config.yaml`, preserving existing routes, then restart it.
The standalone CLI exports configuration and does not apply it. Discord is off
by default there; set `discord_notifications = true` to opt in.
Use `--config PATH` before the subcommand for another config. For background use,
adapt the paths in the [systemd service](examples/hermes-github-mentions.service).
Choose either native or standalone operation.

## Development

```bash
mkdir -p .test-tmp
TMPDIR="$PWD/.test-tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Tests use fake responses and temporary state, with unmocked network access blocked.
For native integration checks, set `HERMES_TEST_ROOT` and `PYTHONPATH` to an existing
Hermes source root and run its Python, with a disk-backed `TMPDIR` outside this
checkout. No live credentials are needed. See the [release checks](docs/release-checklist.md).

[MIT licensed](LICENSE). Independent of Nous Research.
