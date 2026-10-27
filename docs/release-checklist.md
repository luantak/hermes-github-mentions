# Release checks

The plugin has offline regression coverage and checks through Hermes' real
validator, Plugin Doctor, discovery, CLI registration, and adapter lifecycle.
These checks use a temporary Hermes home, blocked network access, and fake
credentials. A live GitHub-to-agent
test against a separate Hermes installation is still outstanding.

- Test one issue comment, an inline PR review reply, and a Discussion reply with
  a separate bot account and an isolated Hermes home.
- Run with Discord entirely unconfigured, then test optional Discord notifications
  separately. A failed Discord notice must not block GitHub work.
- Exercise a gateway restart while a request is queued, then check recovery and
  that the agent posts one final reply.
- Check generated routes on supported Hermes releases and record exact versions.
- Run the tests on Python 3.12, 3.13, and macOS. Local validation covers Python
  3.11 and 3.14 on Linux, with the native Hermes checks on Python 3.14.
- Confirm the included GitHub Actions matrix passes for the release commit.
  It covers Python 3.11 through 3.14 on Linux and Python 3.14 on macOS.
- Review the agent's GitHub identity and public reply behavior in the live smoke
  test. Prompt instructions do not guarantee publishing behavior.
- Decide whether to replace optional SQLite progress tracking with a supported
  Hermes status API when one is available.
- Verify the native plugin's optional progress mode against supported Hermes
  versions. The native adapter uses default in-process plugin execution; Hermes'
  plugin-host isolation mode does not support gateway adapters.

Keep the example credentials fictional. Never add real state, notification
payloads, generated route fragments, tokens, or service logs to the repository.
