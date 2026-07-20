# -*- coding: utf-8 -*-
""" message utilities """


import re
import unicodedata


def encoded_len(text: str) -> int:
    """Return the UTF-8 byte length used by Meshtastic payload limits."""

    return len(text.encode('utf-8'))


def _split_raw_by_encoded_len(text: str, max_len: int):
    """Split continuous text without cutting through a UTF-8 character."""

    chunks = []
    current = []
    current_len = 0
    for char in text:
        char_len = encoded_len(char)
        if current and current_len + char_len > max_len:
            chunks.append(''.join(current))
            current = []
            current_len = 0
        current.append(char)
        current_len += char_len
    if current:
        chunks.append(''.join(current))
    return chunks


def _split_text_by_encoded_len(text: str, max_len: int):
    """Split text without cutting through a UTF-8 character."""

    if max_len <= 0:
        raise ValueError('max_len must be positive')

    chunks = []
    current = []
    current_len = 0
    for token in re.findall(r'\S+\s*|\s+', text):
        token_len = encoded_len(token)
        if current and current_len + token_len > max_len:
            chunks.append(''.join(current).rstrip())
            current = []
            current_len = 0
        if token_len > max_len:
            chunks.extend(_split_raw_by_encoded_len(token.rstrip(), max_len))
            continue
        current.append(token)
        current_len += token_len
    if current:
        chunks.append(''.join(current).rstrip())
    return chunks


def split_message(msg, chunk_len, callback, **kwargs) -> None:
    """
    split_message - split message into smaller parts and invoke callback on each one

    :return:
    """
    parts = []
    part = []
    for line in msg.split('\n'):
        if len(line) == 0:
            continue
        candidate = '\n'.join(part + [line]) if part else line
        if encoded_len(candidate) <= chunk_len:
            part.append(line)
        else:
            if part:
                parts.append(part)
            part = [line]

    if part:
        parts.append(part)

    for part in parts:
        if len(part) == 0:
            continue
        line = '\n'.join(part)
        if encoded_len(line) <= chunk_len:
            callback(line, **kwargs)
        else:
            for chunk in _split_text_by_encoded_len(line, chunk_len):
                callback(chunk, **kwargs)


def split_user_message(sender: str, msg: str, chunk_len: int):
    """Split user's message into chunks with sender prefix and counters."""

    prefix = f"{sender}: "
    parts_count = 1
    while True:
        counter = f"[{parts_count}/{parts_count}] "
        available = chunk_len - encoded_len(prefix) - encoded_len(counter)
        parts = [p.strip() for p in _split_text_by_encoded_len(msg, available) if p.strip()]
        if len(parts) == parts_count:
            break
        parts_count = len(parts)

    return [f"{prefix}[{idx}/{parts_count}] {part}" for idx, part in enumerate(parts, start=1)]


def is_emoji_reaction(text: str) -> bool:
    """Return True if the provided text looks like a standalone emoji reaction."""

    if text is None:
        return False
    candidate = text.strip()
    if not candidate or len(candidate) > 8:
        return False
    has_emoji = False
    for char in candidate:
        if char in ('\u200d', '\ufe0f'):
            continue
        category = unicodedata.category(char)
        if category.startswith('S') or category in ('Mn', 'Cf'):
            has_emoji = True
            continue
        return False
    return has_emoji


def first_emoji_codepoint(text: str):
    """Return the integer codepoint of the first emoji-like character in text."""

    if text is None:
        return None
    for char in text.strip():
        if char in ('\u200d', '\ufe0f'):
            continue
        category = unicodedata.category(char)
        if category.startswith('S') or category in ('Mn', 'Cf'):
            return ord(char)
        break
    return None
