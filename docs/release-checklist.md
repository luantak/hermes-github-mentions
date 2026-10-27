# Before a public release

The project is a standalone extraction of a personal watcher. The original
integration has been used with a live agent. This extracted version has offline
regression coverage; it has not yet been tested end to end against a separate,
unmodified Hermes installation.

- Test one issue comment, an inline PR review reply, and a Discussion reply with
  a separate bot account and an isolated Hermes home.
- Exercise a gateway restart while a request is queued, then check recovery and
  that the agent posts one final reply.
- Check generated routes on supported Hermes releases and record exact versions.
- Run the tests on Python 3.11 through 3.14 and on macOS. Initial local validation
  covers Python 3.14 on Linux.
- Confirm the included GitHub Actions matrix passes after the repository is
  published. It covers those Python versions on Linux and Python 3.14 on macOS.
- Review the agent's GitHub identity and public reply behavior in the live smoke
  test. Prompt instructions do not guarantee publishing behavior.
- Decide whether to replace optional SQLite progress tracking with a supported
  Hermes status API when one is available.

Keep the example credentials fictional. Never add real state, notification
payloads, generated route fragments, tokens, or service logs to the repository.
