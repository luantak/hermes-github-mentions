# Project instructions

This is a native Hermes plugin with a standalone CLI and no additional runtime
dependencies beyond Python 3.11's standard library. Keep Hermes imports out of
the watcher. The plugin and adapter modules use documented Hermes APIs.

- Never read or change a developer's live Hermes config, state, credentials, or
  services during tests. All network calls must be mocked and temporary files
  must live on disk, not a memory-backed filesystem.
- Preserve saved requests and cursor behavior when changing polling or delivery.
  Add behavior tests for changes that can lose or duplicate requests.
- Keep author approval checks before agent work. Do not restrict approved
  mentions to a configured list of repositories.
- Database progress tracking is optional and must not block mention admission.
- Plugin registration must not start workers, read secrets, or create state.
  The gateway adapter owns the context-bound worker's startup and shutdown.
- Read native credentials through Hermes' secret scope. Never mutate process
  environment variables to pass credentials to the standalone core.
- Example accounts and credentials must be fictional. Generated config fragments
  contain a webhook secret and must not be committed.
- Run `python3 -m unittest discover -s tests -v` with `TMPDIR` set to a checked,
  disk-backed directory. The suite has no external dependencies or network needs.
- Optional real-Hermes checks require an already installed environment and
  `HERMES_TEST_ROOT`. Their disk-backed `TMPDIR` must be outside the checkout,
  because Plugin Doctor copies this directory into its scratch space.
- Follow the shared server's RAM and disk budgets. Do not build or install Hermes
  to validate this project.
