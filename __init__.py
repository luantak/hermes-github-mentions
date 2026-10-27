"""Hermes directory-plugin entry point. Registration performs no I/O."""


def register(ctx):
    from .hermes_github_mentions.plugin import register as register_plugin

    register_plugin(ctx)
