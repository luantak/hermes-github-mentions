# Project instructions

This is a standalone companion to Hermes, with no runtime dependencies beyond
Python 3.11's standard library. Keep Hermes imports out of the watcher.

- Never read or change a developer's live Hermes config, state, credentials, or
  services during tests. All network calls must be mocked and temporary files
  must live on disk, not a memory-backed filesystem.
- Preserve saved requests and cursor behavior when changing polling or delivery.
  Add behavior tests for changes that can lose or duplicate requests.
- Keep author checks and the optional repository allowlist before agent work.
- Database progress tracking is optional and must not block mention admission.
- Example accounts and credentials must be fictional. Generated config fragments
  contain a webhook secret and must not be committed.
- Run `python3 -m unittest discover -s tests -v` with `TMPDIR` set to a checked,
  disk-backed directory. The suite has no external dependencies or network needs.
- Follow the shared server's RAM and disk budgets. Do not build or install Hermes
  to validate this project.
