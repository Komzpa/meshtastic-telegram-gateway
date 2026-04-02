# -*- coding: utf-8 -*-
"""Compatibility helpers for Telegram reaction updates."""

from telegram.ext import MessageReactionHandler


def ensure_reaction_update_support() -> None:
    """No-op compatibility shim for older code paths."""

    return None


__all__ = ["MessageReactionHandler", "ensure_reaction_update_support"]
