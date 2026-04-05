"""Meshtastic Telegram Gateway package initializer."""

from typing import Any


def cmd(*args: Any, **kwargs: Any):
    """Lazily import the CLI entrypoint to avoid eager app dependencies."""

    from .mesh import cmd as mesh_cmd

    return mesh_cmd(*args, **kwargs)
