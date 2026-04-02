import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from meshtastic import BROADCAST_ADDR as MESHTASTIC_BROADCAST_ADDR

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if 'imghdr' not in sys.modules:
    imghdr = types.ModuleType('imghdr')
    imghdr.what = lambda *_args, **_kwargs: None
    sys.modules['imghdr'] = imghdr

from mtg.bot.meshtastic import MeshtasticBot


def build_bot():
    bot = MeshtasticBot.__new__(MeshtasticBot)
    bot.database = MagicMock()
    bot.database.get_node_record.return_value = (False, None)
    bot.config = SimpleNamespace(
        enforce_type=lambda _type, value: _type(value),
        Telegram=SimpleNamespace(NotificationsEnabled=False, Room=12345),
        Meshtastic=SimpleNamespace(MaxHopCount=7),
    )
    bot.filter = MagicMock()
    bot.filter.banned.return_value = False
    bot.logger = logging.getLogger('test-meshtastic-rangetest')
    bot.telegram_connection = MagicMock()
    bot.telegram_connection.send_message_sync = MagicMock()
    bot.meshtastic_connection = MagicMock()
    bot.bot_handler = MagicMock()
    bot.bot_handler.get_response.return_value = None
    bot.memcache = MagicMock()
    bot.writer = MagicMock()
    bot.aprs = None
    bot.notify_on_new_node = MagicMock()
    bot.notify_low_battery = MagicMock()
    bot.process_pong = MagicMock()
    bot.process_meshtastic_command = MagicMock()
    return bot


def build_interface():
    return SimpleNamespace(
        nodes={
            '!12345678': {
                'user': {'longName': 'Range Tester'},
            }
        }
    )


def build_packet(*, portnum, text, to_id=MESHTASTIC_BROADCAST_ADDR):
    return {
        'fromId': '!12345678',
        'toId': to_id,
        'hopLimit': 1,
        'decoded': {
            'portnum': portnum,
            'text': text,
        },
    }


def test_broadcast_rangetest_app_sends_direct_nag():
    bot = build_bot()
    interface = build_interface()
    packet = build_packet(portnum='RANGE_TEST_APP', text='seq 1')
    bot._is_broadcast_target = lambda _target: True  # pylint:disable=protected-access

    handled = bot._process_rangetest(  # pylint:disable=protected-access
        packet,
        packet['decoded'],
        packet['fromId'],
        packet['toId'],
        interface,
    )

    assert handled is True
    bot.meshtastic_connection.send_text.assert_called_once_with(
        "Got your rangetest packet (seq 1). Please remember to turn rangetest off when you're done.",
        destinationId='!12345678',
    )
    bot.telegram_connection.send_message_sync.assert_called_once_with(
        chat_id=12345,
        text='Range Tester: seq 1 [rangetest]',
    )
    bot.telegram_connection.send_message.assert_not_called()


def test_each_broadcast_rangetest_packet_triggers_a_nag():
    bot = build_bot()
    interface = build_interface()
    bot._is_broadcast_target = lambda _target: True  # pylint:disable=protected-access

    packet1 = build_packet(portnum='RANGE_TEST_APP', text='seq 1')
    packet2 = build_packet(portnum='RANGE_TEST_APP', text='seq 2')

    bot._process_rangetest(packet1, packet1['decoded'], packet1['fromId'], packet1['toId'], interface)  # pylint:disable=protected-access
    bot._process_rangetest(packet2, packet2['decoded'], packet2['fromId'], packet2['toId'], interface)  # pylint:disable=protected-access

    assert bot.meshtastic_connection.send_text.call_count == 2
    assert bot.telegram_connection.send_message_sync.call_count == 2


def test_non_broadcast_rangetest_is_ignored_for_dm():
    bot = build_bot()
    interface = build_interface()
    packet = build_packet(portnum='RANGE_TEST_APP', text='seq 3', to_id='!abcdef01')
    bot._is_broadcast_target = lambda _target: False  # pylint:disable=protected-access

    bot.on_receive(packet, interface)

    bot.meshtastic_connection.send_text.assert_not_called()
    bot.telegram_connection.send_message_sync.assert_not_called()
    bot.telegram_connection.send_message.assert_not_called()


def test_non_seq_rangetest_payload_does_not_nag():
    bot = build_bot()
    interface = build_interface()
    packet = build_packet(portnum='RANGE_TEST_APP', text='hello there')
    bot._is_broadcast_target = lambda _target: True  # pylint:disable=protected-access

    bot.on_receive(packet, interface)

    bot.meshtastic_connection.send_text.assert_not_called()
    bot.telegram_connection.send_message_sync.assert_not_called()
    bot.telegram_connection.send_message.assert_not_called()


def test_text_message_seq_fallback_still_sends_direct_nag():
    bot = build_bot()
    interface = build_interface()
    packet = build_packet(portnum='TEXT_MESSAGE_APP', text='seq 9')
    bot._is_broadcast_target = lambda _target: True  # pylint:disable=protected-access

    handled = bot._process_rangetest(  # pylint:disable=protected-access
        packet,
        packet['decoded'],
        packet['fromId'],
        packet['toId'],
        interface,
    )

    assert handled is True
    bot.meshtastic_connection.send_text.assert_called_once_with(
        "Got your rangetest packet (seq 9). Please remember to turn rangetest off when you're done.",
        destinationId='!12345678',
    )
    bot.telegram_connection.send_message_sync.assert_called_once_with(
        chat_id=12345,
        text='Range Tester: seq 9 [rangetest]',
    )
    bot.telegram_connection.send_message.assert_not_called()
