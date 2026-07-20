# -*- coding: utf-8 -*-
# pylint: skip-file
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, call

from mtg.bot.telegram.telegram import TelegramBot


def test_mark_meshtastic_packets_sent_skips_packets_without_id():
    bot = TelegramBot.__new__(TelegramBot)
    bot.logger = MagicMock(spec=logging.Logger)
    database = MagicMock()

    bot._mark_meshtastic_packets_sent(
        database,
        123,
        [SimpleNamespace(), SimpleNamespace(id=77), SimpleNamespace(id=None), SimpleNamespace(id=78)],
        previous_packet_id=42,
    )

    database.mark_link_sent.assert_called_once_with(123, meshtastic_packet_id=77)
    database.add_link_alias.assert_has_calls([
        call(123, 77, previous_packet_id=42),
        call(123, 78, previous_packet_id=77),
    ])
    assert database.add_link_alias.call_count == 2


def test_mark_meshtastic_packets_sent_handles_all_packets_without_id():
    bot = TelegramBot.__new__(TelegramBot)
    bot.logger = MagicMock(spec=logging.Logger)
    database = MagicMock()

    bot._mark_meshtastic_packets_sent(
        database,
        123,
        [SimpleNamespace(), SimpleNamespace(id=None)],
    )

    database.mark_link_sent.assert_called_once_with(123, meshtastic_packet_id=None)
    database.add_link_alias.assert_not_called()
