# -*- coding: utf-8 -*-
# pylint: skip-file
import pytest
import time
import logging
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, Mock, call, mock_open, PropertyMock, ANY
from threading import Event, Thread

from mtg.connection.meshtastic.meshtastic import MeshtasticConnection


class TestMeshtasticConnection:
    """Test MeshtasticConnection class"""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration"""
        config = MagicMock()
        config.Meshtastic.FIFOEnabled = True
        config.Meshtastic.FIFOPath = "/tmp/test.fifo"
        config.Meshtastic.FIFOCmdPath = "/tmp/test.cmd.fifo"
        config.MQTT.Enabled = False
        config.enforce_type.return_value = True
        return config

    @pytest.fixture
    def mock_logger(self):
        """Create mock logger"""
        return MagicMock(spec=logging.Logger)

    @pytest.fixture
    def mock_filter(self):
        """Create mock filter class"""
        filter_class = MagicMock()
        filter_class.banned.return_value = False
        return filter_class

    @pytest.fixture
    def meshtastic_connection(self, mock_config, mock_logger, mock_filter):
        """Create MeshtasticConnection instance"""
        connection = MeshtasticConnection(
            dev_path="/dev/ttyUSB0",
            logger=mock_logger,
            config=mock_config,
            filter_class=mock_filter,
            startup_ts=1234567890.0
        )
        connection.chunk_send_interval = 0
        return connection

    def test_init(self, mock_config, mock_logger, mock_filter):
        """Test MeshtasticConnection initialization"""
        connection = MeshtasticConnection(
            dev_path="/dev/ttyUSB0",
            logger=mock_logger,
            config=mock_config,
            filter_class=mock_filter,
            startup_ts=1234567890.0
        )

        assert connection.dev_path == "/dev/ttyUSB0"
        assert connection.interface is None
        assert connection.logger == mock_logger
        assert connection.config == mock_config
        assert connection.startup_ts == 1234567890.0
        assert connection.mqtt_nodes == {}
        assert connection.name == 'Meshtastic Connection'
        assert connection.filter == mock_filter
        assert connection.fifo == "/tmp/test.fifo"
        assert connection.fifo_cmd == "/tmp/test.cmd.fifo"
        assert connection.exit is False
        assert connection.lock is not None
        assert connection.fifo_lock is not None

    def test_init_default_fifo_paths(self, mock_logger, mock_filter):
        """Test initialization with default FIFO paths when config doesn't provide them"""
        config = MagicMock()
        # Mock getattr to raise KeyError for missing attributes
        config.Meshtastic = MagicMock()
        type(config.Meshtastic).FIFOPath = PropertyMock(side_effect=KeyError)
        type(config.Meshtastic).FIFOCmdPath = PropertyMock(side_effect=KeyError)

        connection = MeshtasticConnection(
            dev_path="/dev/ttyUSB0",
            logger=mock_logger,
            config=config,
            filter_class=mock_filter
        )

        assert connection.fifo == '/tmp/mtg.fifo'
        assert connection.fifo_cmd == '/tmp/mtg.cmd.fifo'

    def test_get_startup_ts(self, meshtastic_connection):
        """Test get_startup_ts property"""
        assert meshtastic_connection.get_startup_ts == 1234567890.0

    @patch('mtg.connection.meshtastic.meshtastic.meshtastic_serial_interface')
    def test_connect_serial(self, mock_serial_module, meshtastic_connection):
        """Test connect with serial interface"""
        mock_interface = MagicMock()
        mock_serial_module.SerialInterface.return_value = mock_interface

        meshtastic_connection.connect()

        mock_serial_module.SerialInterface.assert_called_once_with(
            devPath="/dev/ttyUSB0",
            debugOut=ANY
        )
        assert meshtastic_connection.interface == mock_interface

    @patch('mtg.connection.meshtastic.meshtastic.meshtastic_tcp_interface')
    def test_connect_tcp(self, mock_tcp_module, mock_config, mock_logger, mock_filter):
        """Test connect with TCP interface"""
        mock_interface = MagicMock()
        mock_tcp_module.TCPInterface.return_value = mock_interface

        connection = MeshtasticConnection(
            dev_path="tcp:192.168.1.100:4403",
            logger=mock_logger,
            config=mock_config,
            filter_class=mock_filter
        )

        connection.connect()

        mock_tcp_module.TCPInterface.assert_called_once_with(
            "192.168.1.100:4403",
            debugOut=ANY
        )
        assert connection.interface == mock_interface

    @patch('mtg.connection.meshtastic.meshtastic.MQTTInterface')
    def test_connect_mqtt(self, mock_mqtt_interface, mock_config, mock_logger, mock_filter):
        """Test connect with MQTT interface"""
        mock_interface = MagicMock()
        mock_mqtt_interface.return_value = mock_interface

        connection = MeshtasticConnection(
            dev_path="mqtt",
            logger=mock_logger,
            config=mock_config,
            filter_class=mock_filter
        )

        connection.connect()

        mock_mqtt_interface.assert_called_once_with(
            debugOut=ANY,
            cfg=mock_config,
            logger=mock_logger
        )
        assert connection.interface == mock_interface

    @patch('mtg.connection.meshtastic.meshtastic.Thread')
    def test_lost_event_starts_one_recovery_and_established_is_noop(
        self, mock_thread, meshtastic_connection
    ):
        """A post-start loss recovers once; established events do not recover."""
        old_interface = MagicMock()
        meshtastic_connection.interface = old_interface

        meshtastic_connection.handle_connection_event(
            old_interface, 'meshtastic.connection.established'
        )
        mock_thread.assert_not_called()

        meshtastic_connection.handle_connection_event(
            old_interface, 'meshtastic.connection.lost'
        )
        meshtastic_connection.handle_connection_event(
            old_interface, 'meshtastic.connection.lost'
        )
        mock_thread.assert_called_once_with(
            target=meshtastic_connection._recover_connection,
            args=(old_interface,),
            daemon=True,
            name='MeshtasticReconnect',
        )
        mock_thread.return_value.start.assert_called_once()

    def test_recovery_closes_lost_interface_and_reconnects(self, meshtastic_connection):
        """Recovery clears the stale interface before invoking normal retries."""
        old_interface = MagicMock()
        new_interface = MagicMock()
        meshtastic_connection.interface = old_interface

        def reconnect():
            assert meshtastic_connection.interface is None
            meshtastic_connection.interface = new_interface

        meshtastic_connection.connect = MagicMock(side_effect=reconnect)
        callback = MagicMock()
        meshtastic_connection.add_reconnect_callback(callback)
        meshtastic_connection._recover_connection(old_interface)

        old_interface.close.assert_called_once_with()
        meshtastic_connection.connect.assert_called_once_with()
        assert meshtastic_connection.interface is new_interface
        assert meshtastic_connection._reconnect_in_progress is False
        callback.assert_called_once_with()

    @patch('mtg.connection.meshtastic.meshtastic.Thread')
    def test_stale_loss_after_recovery_does_not_replace_new_interface(
        self, mock_thread, meshtastic_connection
    ):
        """A delayed callback from the old interface cannot start recovery again."""
        old_interface = MagicMock()
        new_interface = MagicMock()
        meshtastic_connection.interface = new_interface

        meshtastic_connection.handle_connection_event(
            old_interface, 'meshtastic.connection.lost'
        )

        mock_thread.assert_not_called()
        assert meshtastic_connection.interface is new_interface

    def test_health_watchdog_recovers_when_lost_event_is_not_delivered(
        self, meshtastic_connection
    ):
        """A cleared interface state starts normal recovery without pubsub."""
        disconnected_interface = MagicMock()
        disconnected_interface.isConnected = Event()
        meshtastic_connection.interface = disconnected_interface
        meshtastic_connection._health_watchdog_stop = Event()
        meshtastic_connection._health_watchdog_stop.set()
        meshtastic_connection.handle_connection_event = MagicMock()

        meshtastic_connection._watch_connection_health()

        meshtastic_connection.handle_connection_event.assert_called_once_with(
            disconnected_interface, 'meshtastic.connection.lost'
        )

    def test_health_watchdog_leaves_connected_interface_alone(
        self, meshtastic_connection
    ):
        """A connected interface must not begin a duplicate recovery."""
        connected_interface = MagicMock()
        connected_interface.isConnected = Event()
        connected_interface.isConnected.set()
        meshtastic_connection.interface = connected_interface
        meshtastic_connection._health_watchdog_stop = Event()
        meshtastic_connection._health_watchdog_stop.set()
        meshtastic_connection.handle_connection_event = MagicMock()

        meshtastic_connection._watch_connection_health()

        meshtastic_connection.handle_connection_event.assert_not_called()

    @patch('mtg.connection.meshtastic.meshtastic.Thread')
    def test_health_watchdog_starts_once(self, mock_thread, meshtastic_connection):
        """Reconnects keep one watchdog instead of accumulating threads."""
        meshtastic_connection._start_connection_health_watchdog()
        meshtastic_connection._start_connection_health_watchdog()

        mock_thread.assert_called_once_with(
            target=meshtastic_connection._watch_connection_health,
            daemon=True,
            name='MeshtasticHealth',
        )
        mock_thread.return_value.start.assert_called_once_with()

    def test_send_text_no_interface(self, meshtastic_connection):
        """Test send_text when interface is None"""
        meshtastic_connection.interface = None

        # Should not raise exception
        meshtastic_connection.send_text("Test message")

    @patch('mtg.connection.meshtastic.meshtastic.mesh_pb2')
    def test_send_text_short_message(self, mock_mesh_pb2, meshtastic_connection):
        """Test send_text with short message"""
        mock_mesh_pb2.Constants.DATA_PAYLOAD_LEN = 256
        mock_interface = MagicMock()
        meshtastic_connection.interface = mock_interface

        meshtastic_connection.send_text("Short message")

        mock_interface.sendText.assert_called_once_with("Short message")

    @patch('mtg.connection.meshtastic.meshtastic.mesh_pb2')
    def test_send_text_long_message(self, mock_mesh_pb2, meshtastic_connection):
        """Test send_text with long message that needs splitting"""
        mock_mesh_pb2.Constants.DATA_PAYLOAD_LEN = 20
        mock_interface = MagicMock()
        sent_packets = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        mock_interface.sendText.side_effect = sent_packets
        meshtastic_connection.interface = mock_interface

        result = meshtastic_connection.send_text(
            "abcdefghijklmnopqrstuvwxyz",
            destinationId="12345",
        )

        assert result == sent_packets
        assert mock_interface.sendText.call_args_list == [
            call("abcdefghijklmnopqrst", destinationId="12345"),
            call("uvwxyz", destinationId="12345"),
        ]

    @patch('mtg.connection.meshtastic.meshtastic.mesh_pb2')
    def test_send_text_multipart_reply_only_marks_first_part(self, mock_mesh_pb2, meshtastic_connection):
        """Only the first part should render a native reply preview."""
        mock_mesh_pb2.Constants.DATA_PAYLOAD_LEN = 20
        mock_interface = MagicMock()
        sent_packets = [SimpleNamespace(id=101), SimpleNamespace(id=102)]
        meshtastic_connection.interface = mock_interface
        meshtastic_connection._send_rich_text = MagicMock(return_value=sent_packets[0])
        mock_interface.sendText.return_value = sent_packets[1]

        result = meshtastic_connection.send_text(
            "abcdefghijklmnopqrstuvwxyz",
            reply_id=42,
            destinationId="12345",
        )

        assert result == sent_packets
        meshtastic_connection._send_rich_text.assert_called_once_with(
            "abcdefghijklmnopqrst",
            reply_id=42,
            emoji=None,
            destinationId="12345",
        )
        mock_interface.sendText.assert_called_once_with("uvwxyz", destinationId="12345")

    @patch('mtg.connection.meshtastic.meshtastic.mesh_pb2')
    def test_send_user_text_multipart_is_atomic_and_only_first_part_replies(
        self, mock_mesh_pb2, meshtastic_connection
    ):
        """A user message is one paced send group with one reply preview."""
        mock_mesh_pb2.Constants.DATA_PAYLOAD_LEN = 60
        packets = [SimpleNamespace(id=10), SimpleNamespace(id=11), SimpleNamespace(id=12)]
        meshtastic_connection._send_parts = MagicMock(return_value=packets)

        result = meshtastic_connection.send_user_text(
            "Telegram User",
            "abcdefghijklmnopqrstuvwxyz" * 4,
            reply_id=77,
            destinationId="12345",
        )

        assert result == packets
        parts = meshtastic_connection._send_parts.call_args.args[0]
        assert len(parts) >= 2
        assert all(len(part.encode('utf-8')) <= 60 for part in parts)
        assert meshtastic_connection._send_parts.call_args.kwargs == {
            'reply_id': 77,
            'destinationId': '12345',
        }

    @patch('mtg.connection.meshtastic.meshtastic.mesh_pb2')
    def test_multipart_send_paces_every_following_packet(
        self, mock_mesh_pb2, meshtastic_connection
    ):
        """Packets enter the radio queue at the configured interval."""
        mock_mesh_pb2.Constants.DATA_PAYLOAD_LEN = 20
        meshtastic_connection.chunk_send_interval = 2.0
        meshtastic_connection._chunk_sleep = MagicMock()
        meshtastic_connection.interface = MagicMock()
        meshtastic_connection.interface.sendText.side_effect = [
            SimpleNamespace(id=1),
            SimpleNamespace(id=2),
        ]

        meshtastic_connection.send_text('abcdefghijklmnopqrstuvwxyz')

        meshtastic_connection._chunk_sleep.assert_called_once_with(2.0)

    @patch('mtg.connection.meshtastic.meshtastic.mesh_pb2')
    def test_concurrent_multipart_messages_cannot_interleave(
        self, mock_mesh_pb2, meshtastic_connection
    ):
        """The message lock covers every part of one logical message."""
        mock_mesh_pb2.Constants.DATA_PAYLOAD_LEN = 30
        first_packet_started = Event()
        release_first_packet = Event()
        sent = []

        def send_text(part, **_kwargs):
            sent.append(part)
            if len(sent) == 1:
                first_packet_started.set()
                assert release_first_packet.wait(timeout=1)
            return SimpleNamespace(id=len(sent))

        meshtastic_connection.interface = MagicMock()
        meshtastic_connection.interface.sendText.side_effect = send_text
        first = Thread(
            target=meshtastic_connection.send_user_text,
            args=('First', 'a' * 100),
        )
        second = Thread(
            target=meshtastic_connection.send_user_text,
            args=('Second', 'b' * 100),
        )

        first.start()
        assert first_packet_started.wait(timeout=1)
        second.start()
        release_first_packet.set()
        first.join(timeout=1)
        second.join(timeout=1)

        assert not first.is_alive()
        assert not second.is_alive()
        first_count = sum(part.startswith('First:') for part in sent)
        assert first_count >= 2
        assert all(part.startswith('First:') for part in sent[:first_count])
        assert all(part.startswith('Second:') for part in sent[first_count:])

    def test_send_data_no_interface(self, meshtastic_connection):
        """Test send_data when interface is None"""
        meshtastic_connection.interface = None

        # Should not raise exception
        meshtastic_connection.send_data(b"data")

    def test_send_data_with_interface(self, meshtastic_connection):
        """Test send_data with interface"""
        mock_interface = MagicMock()
        meshtastic_connection.interface = mock_interface

        meshtastic_connection.send_data(b"data", destinationId="12345")

        mock_interface.sendData.assert_called_once_with(b"data", destinationId="12345")

    def test_node_info_no_interface(self, meshtastic_connection):
        """Test node_info when interface is None"""
        result = meshtastic_connection.node_info("12345")
        assert result == {}

    def test_node_info_with_interface(self, meshtastic_connection):
        """Test node_info with interface"""
        mock_interface = MagicMock()
        mock_nodes = {"12345": {"id": "12345", "user": {"longName": "Test Node"}}}
        mock_interface.nodes = mock_nodes
        meshtastic_connection.interface = mock_interface

        result = meshtastic_connection.node_info("12345")
        assert result == {"id": "12345", "user": {"longName": "Test Node"}}

    def test_node_info_not_found(self, meshtastic_connection):
        """Test node_info when node not found"""
        mock_interface = MagicMock()
        mock_interface.nodes = {}
        meshtastic_connection.interface = mock_interface

        result = meshtastic_connection.node_info("12345")
        assert result == {}

    @patch('mtg.connection.meshtastic.meshtastic.time.sleep')
    @patch('mtg.connection.meshtastic.meshtastic.MESHTASTIC_LOCAL_ADDR', 0xFFFFFFFF)
    def test_reboot(self, mock_sleep, meshtastic_connection):
        """Test reboot method"""
        mock_interface = MagicMock()
        mock_node = MagicMock()
        mock_interface.getNode.return_value = mock_node
        meshtastic_connection.interface = mock_interface

        with patch.object(meshtastic_connection, 'connect') as mock_connect:
            meshtastic_connection.reboot()

        meshtastic_connection.logger.info.assert_any_call("Reboot requested...")
        mock_interface.getNode.assert_called_once_with(0xFFFFFFFF)
        mock_node.reboot.assert_called_once_with(10)
        mock_interface.close.assert_called_once()
        mock_sleep.assert_called_once_with(20)
        mock_connect.assert_called_once()
        meshtastic_connection.logger.info.assert_any_call("Reboot completed...")

    @patch('mtg.connection.meshtastic.meshtastic.MESHTASTIC_LOCAL_ADDR', 0xFFFFFFFF)
    def test_reset_db(self, meshtastic_connection):
        """Test reset_db method"""
        mock_interface = MagicMock()
        mock_node = MagicMock()
        mock_interface.getNode.return_value = mock_node
        meshtastic_connection.interface = mock_interface

        meshtastic_connection.reset_db()

        meshtastic_connection.logger.info.assert_any_call('Reset node DB requested...')
        mock_interface.getNode.assert_called_once_with(0xFFFFFFFF)
        mock_node.resetNodeDb.assert_called_once()
        meshtastic_connection.logger.info.assert_any_call('Reset node DB completed...')

    def test_on_mqtt_node(self, meshtastic_connection):
        """Test on_mqtt_node callback"""
        meshtastic_connection.on_mqtt_node("node123", "online")

        assert meshtastic_connection.mqtt_nodes["node123"] == "online"
        meshtastic_connection.logger.debug.assert_called_with("node123 is online")

    def test_nodes_mqtt_property(self, meshtastic_connection):
        """Test nodes_mqtt property"""
        meshtastic_connection.mqtt_nodes = {
            "node1": "online",
            "node2": "offline",
            "node3": "online"
        }

        result = meshtastic_connection.nodes_mqtt
        assert set(result) == {"node1", "node2", "node3"}

    def test_node_has_mqtt(self, meshtastic_connection):
        """Test node_has_mqtt method"""
        meshtastic_connection.mqtt_nodes = {"node1": "online"}

        assert meshtastic_connection.node_has_mqtt("node1") is True
        assert meshtastic_connection.node_has_mqtt("node2") is False

    def test_node_mqtt_status(self, meshtastic_connection):
        """Test node_mqtt_status method"""
        meshtastic_connection.mqtt_nodes = {"node1": "online"}

        assert meshtastic_connection.node_mqtt_status("node1") == "online"
        assert meshtastic_connection.node_mqtt_status("node2") == "N/A"

    def test_nodes_property_no_interface(self, meshtastic_connection):
        """Test nodes property when interface is None"""
        meshtastic_connection.interface = None

        assert meshtastic_connection.nodes == {}

    def test_nodes_property_with_interface(self, meshtastic_connection):
        """Test nodes property with interface"""
        mock_interface = MagicMock()
        mock_nodes = {"node1": {"id": "node1"}, "node2": {"id": "node2"}}
        mock_interface.nodes = mock_nodes
        meshtastic_connection.interface = mock_interface

        assert meshtastic_connection.nodes == mock_nodes

    def test_nodes_property_with_interface_none_nodes(self, meshtastic_connection):
        """Test nodes property when interface.nodes is None"""
        mock_interface = MagicMock()
        mock_interface.nodes = None
        meshtastic_connection.interface = mock_interface

        assert meshtastic_connection.nodes == {}

    def test_nodes_with_info_property(self, meshtastic_connection):
        """Test nodes_with_info property"""
        mock_interface = MagicMock()
        mock_nodes = {
            "node1": {"id": "node1", "user": {"longName": "Node 1"}},
            "node2": {"id": "node2", "user": {"longName": "Node 2"}}
        }
        mock_interface.nodes = mock_nodes
        meshtastic_connection.interface = mock_interface

        result = meshtastic_connection.nodes_with_info
        assert len(result) == 2
        assert {"id": "node1", "user": {"longName": "Node 1"}} in result
        assert {"id": "node2", "user": {"longName": "Node 2"}} in result

    def test_nodes_with_position_property(self, meshtastic_connection):
        """Test nodes_with_position property"""
        mock_interface = MagicMock()
        mock_nodes = {
            "node1": {"id": "node1", "position": {"latitude": 40.7128}},
            "node2": {"id": "node2"},  # No position
            "node3": {"id": "node3", "position": {"latitude": 41.0}}
        }
        mock_interface.nodes = mock_nodes
        meshtastic_connection.interface = mock_interface

        result = meshtastic_connection.nodes_with_position
        assert len(result) == 2
        assert all("position" in node for node in result)

    def test_nodes_with_user_property(self, meshtastic_connection):
        """Test nodes_with_user property"""
        mock_interface = MagicMock()
        mock_nodes = {
            "node1": {"id": "node1", "position": {"latitude": 40.7128}, "user": {"longName": "Node 1"}},
            "node2": {"id": "node2", "position": {"latitude": 41.0}},  # No user
            "node3": {"id": "node3", "user": {"longName": "Node 3"}},  # No position
            "node4": {"id": "node4", "position": {"latitude": 42.0}, "user": {"longName": "Node 4"}}
        }
        mock_interface.nodes = mock_nodes
        meshtastic_connection.interface = mock_interface

        result = meshtastic_connection.nodes_with_user
        assert len(result) == 2
        assert all("position" in node and "user" in node for node in result)

    def test_format_nodes_no_table(self, meshtastic_connection):
        """Test format_nodes when showNodes returns empty"""
        mock_interface = MagicMock()
        mock_interface.showNodes.return_value = ""
        meshtastic_connection.interface = mock_interface

        result = meshtastic_connection.format_nodes()
        assert result == "No other nodes"

    def test_format_nodes_with_table(self, meshtastic_connection):
        """Test format_nodes with table data"""
        mock_interface = MagicMock()
        table_data = """╒══════════╤═══════╤═════════╤═══════════╤══════════╕
│ User     │ AKA   │ ID      │ !ID       │ SNR      │
╞══════════╪═══════╪═════════╪═══════════╪══════════╡
│ TestUser │ TU    │ abc123  │ !abc123   │ -5.25    │
│ Node2    │ N2    │ def456  │ !def456   │ -3.0     │
╘══════════╧═══════╧═════════╧═══════════╧══════════╛"""

        mock_interface.showNodes.return_value = table_data
        meshtastic_connection.interface = mock_interface
        meshtastic_connection.filter.banned.return_value = False

        result = meshtastic_connection.format_nodes()

        # Verify formatting
        lines = result.split('\n')
        assert len(lines) > 0
        assert "User" in lines[0]
        assert "TestUser" in result
        assert "!abc123" in result

        # Verify filter.banned was called
        meshtastic_connection.filter.banned.assert_any_call("!abc123")
        meshtastic_connection.filter.banned.assert_any_call("!def456")

    def test_format_nodes_with_banned_nodes(self, meshtastic_connection):
        """Test format_nodes filters out banned nodes"""
        mock_interface = MagicMock()
        table_data = """╒══════════╤═══════╤═════════╤═══════════╤══════════╕
│ User     │ AKA   │ ID      │ !ID       │ SNR      │
╞══════════╪═══════╪═════════╪═══════════╪══════════╡
│ TestUser │ TU    │ abc123  │ !abc123   │ -5.25    │
│ Banned   │ BN    │ ban456  │ !ban456   │ -3.0     │
╘══════════╧═══════╧═════════╧═══════════╧══════════╛"""

        mock_interface.showNodes.return_value = table_data
        meshtastic_connection.interface = mock_interface

        # Mock filter to ban the second node
        def banned_side_effect(node_id):
            return node_id == "!ban456"

        meshtastic_connection.filter.banned.side_effect = banned_side_effect

        result = meshtastic_connection.format_nodes(include_self=True)

        # Verify banned node is logged
        meshtastic_connection.logger.debug.assert_called_with("Node !ban456 is in a blacklist...")

    @patch('mtg.connection.meshtastic.meshtastic.create_fifo')
    @patch('mtg.connection.meshtastic.meshtastic.setthreadtitle')
    @patch('builtins.open', new_callable=mock_open, read_data="Test message\nAnother message\n")
    @patch('mtg.connection.meshtastic.meshtastic.MESHTASTIC_BROADCAST_ADDR', 0xFFFFFFFF)
    def test_run_loop(self, mock_file, mock_setthreadtitle, mock_create_fifo, meshtastic_connection):
        """Test run_loop method"""
        # Set exit flag after first iteration
        def side_effect(*args, **kwargs):
            if mock_file.return_value.__enter__.call_count >= 1:
                meshtastic_connection.exit = True
            return mock_file.return_value

        mock_file.side_effect = side_effect

        meshtastic_connection.run_loop()

        mock_setthreadtitle.assert_called_once_with('Meshtastic Connection')
        mock_create_fifo.assert_called_once_with('/tmp/test.fifo')
        mock_file.assert_called_with('/tmp/test.fifo', encoding='utf-8')

    @patch('mtg.connection.meshtastic.meshtastic.create_fifo')
    @patch('mtg.connection.meshtastic.meshtastic.setthreadtitle')
    @patch('builtins.open', new_callable=mock_open, read_data="reboot\n")
    def test_run_cmd_loop_reboot(self, mock_file, mock_setthreadtitle, mock_create_fifo, meshtastic_connection):
        """Test run_cmd_loop with reboot command"""
        # Set exit flag after first iteration
        def side_effect(*args, **kwargs):
            if mock_file.return_value.__enter__.call_count >= 1:
                meshtastic_connection.exit = True
            return mock_file.return_value

        mock_file.side_effect = side_effect

        with patch.object(meshtastic_connection, 'reboot') as mock_reboot:
            meshtastic_connection.run_cmd_loop()

        mock_setthreadtitle.assert_called_once_with("MeshtasticCmd")
        mock_create_fifo.assert_called_once_with('/tmp/test.cmd.fifo')
        mock_file.assert_called_with('/tmp/test.cmd.fifo', encoding='utf-8')
        meshtastic_connection.logger.warning.assert_called_with("Reboot requested using CMD...")
        mock_reboot.assert_called_once()

    @patch('mtg.connection.meshtastic.meshtastic.create_fifo')
    @patch('mtg.connection.meshtastic.meshtastic.setthreadtitle')
    @patch('builtins.open', new_callable=mock_open, read_data="reset_db\n")
    def test_run_cmd_loop_reset_db(self, mock_file, mock_setthreadtitle, mock_create_fifo, meshtastic_connection):
        """Test run_cmd_loop with reset_db command"""
        # Set exit flag after first iteration
        def side_effect(*args, **kwargs):
            if mock_file.return_value.__enter__.call_count >= 1:
                meshtastic_connection.exit = True
            return mock_file.return_value

        mock_file.side_effect = side_effect

        with patch.object(meshtastic_connection, 'reset_db') as mock_reset_db:
            meshtastic_connection.run_cmd_loop()

        mock_setthreadtitle.assert_called_once_with("MeshtasticCmd")
        mock_create_fifo.assert_called_once_with('/tmp/test.cmd.fifo')
        mock_file.assert_called_with('/tmp/test.cmd.fifo', encoding='utf-8')
        meshtastic_connection.logger.warning.assert_called_with("Reset DB requested using CMD...")
        mock_reset_db.assert_called_once()

    def test_shutdown(self, meshtastic_connection):
        """Test shutdown method"""
        meshtastic_connection.shutdown()
        assert meshtastic_connection.exit is True

    @patch('mtg.connection.meshtastic.meshtastic.Thread')
    def test_run_fifo_enabled(self, mock_thread, meshtastic_connection):
        """Test run method when FIFO is enabled"""
        meshtastic_connection.config.enforce_type.return_value = True

        mock_thread_instance1 = MagicMock()
        mock_thread_instance2 = MagicMock()
        mock_thread.side_effect = [mock_thread_instance1, mock_thread_instance2]

        meshtastic_connection.run()

        # Verify two threads were created
        assert mock_thread.call_count == 2

        # First thread for run_loop
        mock_thread.assert_any_call(
            target=meshtastic_connection.run_loop,
            daemon=True,
            name='Meshtastic Connection'
        )
        mock_thread_instance1.start.assert_called_once()

        # Second thread for run_cmd_loop
        mock_thread.assert_any_call(
            target=meshtastic_connection.run_cmd_loop,
            daemon=True,
            name="MeshtasticCmd"
        )
        mock_thread_instance2.start.assert_called_once()

    def test_thread_safety(self, meshtastic_connection):
        """Test thread safety locks are initialized"""
        # Test that locks are properly initialized
        assert meshtastic_connection.lock is not None
        assert meshtastic_connection.fifo_lock is not None

        # Verify they are RLock instances by checking type name
        assert 'RLock' in str(type(meshtastic_connection.lock))
        assert 'RLock' in str(type(meshtastic_connection.fifo_lock))

    def test_empty_interface_handling(self, meshtastic_connection):
        """Test various methods handle None interface gracefully"""
        meshtastic_connection.interface = None

        # Test all methods that check for None interface
        assert meshtastic_connection.node_info("test") == {}
        assert meshtastic_connection.nodes == {}
        assert meshtastic_connection.nodes_with_info == []
        assert meshtastic_connection.nodes_with_position == []
        assert meshtastic_connection.nodes_with_user == []

        # These should not raise exceptions
        meshtastic_connection.send_text("test")
        meshtastic_connection.send_data(b"test")

    def test_format_nodes_edge_cases(self, meshtastic_connection):
        """Test format_nodes with various edge cases"""
        mock_interface = MagicMock()
        meshtastic_connection.interface = mock_interface

        # Test with None return
        mock_interface.showNodes.return_value = None
        assert meshtastic_connection.format_nodes() == "No other nodes"

        # Test with empty lines
        table_data = """╒══════════╤═══════╤═════════╤═══════════╤══════════╕
│ User     │ AKA   │ ID      │ !ID       │ SNR      │
╞══════════╪═══════╪═════════╪═══════════╪══════════╡


╘══════════╧═══════╧═════════╧═══════════╧══════════╛"""

        mock_interface.showNodes.return_value = table_data
        result = meshtastic_connection.format_nodes()
        lines = [line for line in result.split('\n') if line.strip()]
        assert len(lines) >= 1  # At least header should be present
