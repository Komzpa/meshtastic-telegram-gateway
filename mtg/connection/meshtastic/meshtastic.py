# -*- coding: utf-8 -*-
""" Meshtastic connection module """

import configparser
import logging
import re
import time
import json
#
from threading import Event, RLock, Thread
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
)
#
from meshtastic import (
    LOCAL_ADDR as MESHTASTIC_LOCAL_ADDR,
    BROADCAST_ADDR as MESHTASTIC_BROADCAST_ADDR,
    serial_interface as meshtastic_serial_interface,
    tcp_interface as meshtastic_tcp_interface,
    mesh_pb2,
    portnums_pb2,
)
# pylint:disable=no-name-in-module
from meshtastic.protobuf import config_pb2
# pylint:disable=no-name-in-module,no-member
from setproctitle import setthreadtitle

from mtg.utils import create_fifo, encoded_len, split_message, split_user_message
from mtg.connection.mqtt import MQTTInterface

FIFO = '/tmp/mtg.fifo'
FIFO_CMD = '/tmp/mtg.cmd.fifo'
MESH_CHUNK_INTERVAL_SECONDS = 2.0
MESH_CONNECTION_HEALTH_INTERVAL_SECONDS = 5.0


# pylint:disable=too-many-instance-attributes,too-many-public-methods
class MeshtasticConnection:
    """
    Meshtastic device connection
    """
    fifo = FIFO
    fifo_cmd = FIFO_CMD

    # pylint:disable=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        dev_path: str,
        logger: logging.Logger,
        config: Any,
        filter_class: Any,
        startup_ts: float = time.time(),
    ):
        self.dev_path = dev_path
        self.interface: Optional[Any] = None
        self.logger = logger
        self.config = config
        self.startup_ts = startup_ts
        self.mqtt_nodes: Dict[str, Any] = {}
        self.name = 'Meshtastic Connection'
        self.lock = RLock()
        self._reconnect_lock = RLock()
        self._reconnect_in_progress = False
        self._reconnect_callbacks: List[Callable[[], None]] = []
        self._health_watchdog_lock = RLock()
        self._health_watchdog_started = False
        self._health_watchdog_stop = Event()
        self.connection_health_interval = MESH_CONNECTION_HEALTH_INTERVAL_SECONDS
        self.chunk_send_interval = MESH_CHUNK_INTERVAL_SECONDS
        self._chunk_sleep = time.sleep
        self.fifo_lock = RLock()
        self.filter = filter_class
        parser = getattr(config, 'config', None)
        if isinstance(parser, configparser.ConfigParser) and parser.has_section('Meshtastic'):
            meshtastic_config = parser['Meshtastic']
            self.fifo = meshtastic_config.get('FIFOPath', self.fifo)
            self.fifo_cmd = meshtastic_config.get('FIFOCmdPath', self.fifo_cmd)
        else:
            meshtastic_config = getattr(config, 'Meshtastic', None)
            if meshtastic_config is not None:
                try:
                    fifo_path = meshtastic_config.FIFOPath
                except (AttributeError, KeyError):
                    fifo_path = None
                if fifo_path:
                    self.fifo = fifo_path
                try:
                    fifo_cmd_path = meshtastic_config.FIFOCmdPath
                except (AttributeError, KeyError):
                    fifo_cmd_path = None
                if fifo_cmd_path:
                    self.fifo_cmd = fifo_cmd_path
        # exit
        self.exit = False

    @property
    def get_startup_ts(self):
        """
        Get startup timestamp

        :return:
        """
        return self.startup_ts

    def _connect_once(self):
        if self.dev_path.startswith('tcp:'):
            self.interface = meshtastic_tcp_interface.TCPInterface(
                self.dev_path.removeprefix('tcp:'), debugOut=None
            )
        elif self.dev_path == 'mqtt':
            self.interface = MQTTInterface(debugOut=None, cfg=self.config, logger=self.logger)
        else:
            self.interface = meshtastic_serial_interface.SerialInterface(
                devPath=self.dev_path, debugOut=None
            )

    def connect(self):
        """Connect to Meshtastic device with retries"""
        retries = 0
        last_exc = None
        while retries < 3:
            try:
                self._connect_once()
                self._start_connection_health_watchdog()
                return
            except Exception as exc:  # pylint:disable=broad-except
                last_exc = exc
                self.logger.error("Meshtastic connect error: %s", repr(exc))
                if self.interface:
                    try:
                        self.interface.close()
                    except Exception as close_exc:  # pylint:disable=broad-except
                        self.logger.warning("Failed to close interface: %s", repr(close_exc))
                    self.interface = None
                retries += 1
                time.sleep(5)
        if last_exc:
            raise last_exc

    def _start_connection_health_watchdog(self) -> None:
        """Watch the interface state when a library disconnect event is missed."""
        with self._health_watchdog_lock:
            if self.exit or self._health_watchdog_started:
                return
            self._health_watchdog_started = True
        thread = Thread(
            target=self._watch_connection_health,
            daemon=True,
            name='MeshtasticHealth',
        )
        thread.start()

    def _watch_connection_health(self) -> None:
        """Recover if Meshtastic marks the current interface disconnected."""
        while not self.exit:
            interface = self.interface
            connection_state = getattr(interface, 'isConnected', None)
            if isinstance(connection_state, Event) and not connection_state.is_set():
                self.handle_connection_event(interface, 'meshtastic.connection.lost')
            if self._health_watchdog_stop.wait(self.connection_health_interval):
                break

    def handle_connection_event(self, interface, topic) -> None:
        """Recover the connection after a post-start Meshtastic disconnect.

        Meshtastic emits connection events from its interface thread.  Keep
        recovery in this connection owner and run it in one background thread
        so the event publisher is not blocked by serial open retries.
        """
        if topic != 'meshtastic.connection.lost':
            return
        if self.exit:
            self.logger.debug('Ignoring connection loss during shutdown')
            return
        with self._reconnect_lock:
            if self.interface is not None and self.interface is not interface:
                self.logger.debug('Ignoring stale connection loss callback')
                return
            if self._reconnect_in_progress:
                self.logger.debug('Connection recovery already in progress')
                return
            self._reconnect_in_progress = True
        self.logger.warning('Meshtastic connection lost; starting recovery')
        thread = Thread(
            target=self._recover_connection,
            args=(interface,),
            daemon=True,
            name='MeshtasticReconnect',
        )
        thread.start()

    def add_reconnect_callback(self, callback: Callable[[], None]) -> None:
        """Run callback after a lost Meshtastic connection has recovered."""
        if callback not in self._reconnect_callbacks:
            self._reconnect_callbacks.append(callback)

    def _notify_reconnected(self) -> None:
        """Resume connection-dependent work after the interface is usable."""
        for callback in self._reconnect_callbacks:
            try:
                callback()
            except Exception as exc:  # pylint:disable=broad-except
                self.logger.error('Meshtastic reconnect callback failed: %s', repr(exc))

    def _recover_connection(self, lost_interface) -> None:
        """Close the lost interface and retry opening the configured device."""
        recovered = False
        try:
            with self.lock:
                if self.interface is lost_interface:
                    self.interface = None
            try:
                lost_interface.close()
            except Exception as exc:  # pylint:disable=broad-except
                self.logger.warning('Failed to close lost interface: %s', repr(exc))
            while not self.exit:
                try:
                    self.connect()
                    self.logger.info('Meshtastic connection recovery completed')
                    recovered = True
                    break
                except Exception as exc:  # pylint:disable=broad-except
                    self.logger.error('Meshtastic connection recovery failed: %s', repr(exc))
                    time.sleep(5)
        finally:
            with self._reconnect_lock:
                self._reconnect_in_progress = False
        if recovered:
            self._notify_reconnected()

    def _send_parts(self, parts, reply_id=None, emoji=None, **kwargs):
        """Send one logical message without letting other messages interleave."""
        send_kwargs = dict(kwargs)
        results = []
        with self.lock:
            for index, part in enumerate(parts):
                if self.interface is None:
                    break
                target_reply_id = reply_id if index == 0 else None
                target_emoji = emoji if index == 0 else None
                log_data = {
                    "event": "send_mesh",
                    "message": part,
                    "kwargs": send_kwargs,
                    "reply_id": target_reply_id,
                    "emoji": target_emoji,
                }
                self.logger.info(json.dumps(log_data))
                if target_reply_id is not None or target_emoji is not None:
                    packet = self._send_rich_text(
                        part,
                        reply_id=target_reply_id,
                        emoji=target_emoji,
                        **send_kwargs,
                    )
                else:
                    packet = self.interface.sendText(part, **send_kwargs)
                if packet:
                    results.append(packet)
                else:
                    break
                if index + 1 < len(parts) and self.chunk_send_interval > 0:
                    self._chunk_sleep(self.chunk_send_interval)
        return results

    def send_text(self, msg, reply_id=None, emoji=None, **kwargs):
        """Send a Meshtastic message, optionally as a reply or reaction."""
        if self.interface is None:
            return []

        chunk_len = mesh_pb2.Constants.DATA_PAYLOAD_LEN  # pylint:disable=no-member
        parts = []
        split_message(msg, chunk_len, lambda part, **_kwargs: parts.append(part))
        if not parts and (msg == '' or emoji is not None):
            parts = [msg]
        return self._send_parts(parts, reply_id=reply_id, emoji=emoji, **kwargs)

    def _send_rich_text(self, msg, reply_id=None, emoji=None, **kwargs):
        """Send text that needs extra metadata like reply IDs or emoji reactions."""

        destination_id = kwargs.pop('destinationId', MESHTASTIC_BROADCAST_ADDR)
        want_ack = kwargs.pop('wantAck', False)
        hop_limit = kwargs.pop('hopLimit', None)
        pki_encrypted = kwargs.pop('pkiEncrypted', False)
        public_key = kwargs.pop('publicKey', None)
        channel_index = kwargs.pop('channelIndex', 0)
        want_response = kwargs.pop('wantResponse', False)

        data = mesh_pb2.Data()
        data.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        data.payload = msg.encode('utf-8')
        data.want_response = want_response
        if reply_id is not None:
            data.reply_id = int(reply_id)
        if emoji is not None:
            data.emoji = int(emoji)

        mesh_packet = mesh_pb2.MeshPacket()
        mesh_packet.channel = channel_index
        mesh_packet.decoded.CopyFrom(data)

        return self.interface._sendPacket(  # pylint:disable=protected-access
            mesh_packet,
            destinationId=destination_id,
            wantAck=want_ack,
            hopLimit=hop_limit,
            pkiEncrypted=pki_encrypted,
            publicKey=public_key,
        )

    def send_user_text(self, sender: str, message: str, reply_id=None, **kwargs):
        """Send text message from a specific sender with automatic splitting."""

        chunk_len = mesh_pb2.Constants.DATA_PAYLOAD_LEN  # pylint:disable=no-member
        full = f"{sender}: {message}"
        if encoded_len(full) <= chunk_len:
            parts = [full]
        else:
            parts = split_user_message(sender, message, chunk_len)
        return self._send_parts(parts, reply_id=reply_id, **kwargs)

    def send_data(self, *args, **kwargs) -> None:
        """
        Send Meshtastic data message

        :param args:
        :param kwargs:
        :return:
        """
        if self.interface is None:
            return
        with self.lock:
            self.interface.sendData(*args, **kwargs)

    def node_info(self, node_id) -> Dict:
        """
        Return node information for a specific node ID

        :param node_id:
        :return:
        """
        if self.interface is None:
            return {}
        return self.interface.nodes.get(node_id, {})

    def reboot(self):
        """
        Execute Meshtastic device reboot

        :return:
        """
        self.logger.info("Reboot requested...")
        self.interface.getNode(MESHTASTIC_LOCAL_ADDR).reboot(10)
        self.interface.close()
        time.sleep(20)
        self.connect()
        self.logger.info("Reboot completed...")

    def reset_db(self):
        """
        Reset Meshtastic device DB

        :return:
        """
        self.logger.info('Reset node DB requested...')
        self.interface.getNode(MESHTASTIC_LOCAL_ADDR).resetNodeDb()
        self.logger.info('Reset node DB completed...')

    # pylint:disable=too-many-locals,too-many-branches,too-many-statements
    def reset_params(self):
        """Reset device parameters to configured values.

        The device reboots after configuration is applied, which is expected.
        """
        parser = getattr(self.config, 'config', None)
        reset_cfg = None
        telegram_cfg = None
        if parser is not None:
            if parser.has_section('MeshtasticReset'):
                reset_cfg = parser['MeshtasticReset']
            if parser.has_section('Telegram'):
                telegram_cfg = parser['Telegram']

        def _reset_value(name, default=None):
            if reset_cfg is None:
                return default
            return reset_cfg.get(name, default)

        if not self.config.enforce_type(bool, _reset_value('Enabled', 'false')):
            return
        try:
            self.interface.waitForConfig()
        except Exception as exc:  # pylint:disable=broad-except
            self.logger.error('Could not fetch device config: %s', repr(exc))
            return

        node = self.interface.getNode(MESHTASTIC_LOCAL_ADDR)
        diffs = []

        use_room = self.config.enforce_type(
            bool,
            _reset_value('LongNameFromRoomLink', 'true'),
        )
        desired_long = (
            telegram_cfg.get('RoomLink') if telegram_cfg is not None else None
            if use_room
            else _reset_value('LongName', None)
        )
        desired_short = _reset_value('ShortName', '🔗')
        current_long = self.interface.getLongName() or ''
        current_short = self.interface.getShortName() or ''
        if (desired_long and desired_long != current_long) or (
            desired_short and desired_short != current_short
        ):
            diffs.append(
                f'name {current_long}/{current_short} -> {desired_long}/{desired_short}'
            )
            node.setOwner(long_name=desired_long, short_name=desired_short)

        lora = node.localConfig.lora
        lora_changed = False
        hop_limit_value = _reset_value('HopLimit')
        if hop_limit_value is not None:
            hop_limit = self.config.enforce_type(
                int, hop_limit_value
            )
            if lora.hop_limit != hop_limit:
                diffs.append(f'hop_limit {lora.hop_limit}->{hop_limit}')
                lora.hop_limit = hop_limit
                lora_changed = True
        region_value = _reset_value('Region')
        if region_value is not None:
            try:
                region_enum = config_pb2.Config.LoRaConfig.RegionCode.Value(
                    region_value
                )
                if lora.region != region_enum:
                    diffs.append(
                        f'region {lora.region}->{region_value}'
                    )
                    lora.region = region_enum
                    lora_changed = True
            except Exception as exc:  # pylint:disable=broad-except
                self.logger.error('Invalid region %s: %s', region_value, repr(exc))
        duty_cycle_value = _reset_value('DutyCycle')
        if duty_cycle_value is not None:
            duty = self.config.enforce_type(
                bool, duty_cycle_value
            )
            if lora.override_duty_cycle != duty:
                diffs.append(f'duty_cycle {lora.override_duty_cycle}->{duty}')
                lora.override_duty_cycle = duty
                lora_changed = True
        ok_to_mqtt = _reset_value('OkToMQTT', None)
        if ok_to_mqtt is not None:
            ok_to_mqtt = self.config.enforce_type(bool, ok_to_mqtt)
            if lora.config_ok_to_mqtt != ok_to_mqtt:
                diffs.append(
                    f'ok_to_mqtt {lora.config_ok_to_mqtt}->{ok_to_mqtt}'
                )
                lora.config_ok_to_mqtt = ok_to_mqtt
                lora_changed = True
        ignore_mqtt = _reset_value('IgnoreMQTT', None)
        if ignore_mqtt is not None:
            ignore_mqtt = self.config.enforce_type(bool, ignore_mqtt)
            if lora.ignore_mqtt != ignore_mqtt:
                diffs.append(f'ignore_mqtt {lora.ignore_mqtt}->{ignore_mqtt}')
                lora.ignore_mqtt = ignore_mqtt
                lora_changed = True
        if lora_changed:
            node.writeConfig('lora')

        role_value = _reset_value('Role')
        if role_value is not None:
            try:
                role_enum = config_pb2.Config.DeviceConfig.Role.Value(
                    role_value
                )
                device_cfg = node.localConfig.device
                if device_cfg.role != role_enum:
                    diffs.append(
                        f'role {device_cfg.role}->{role_value}'
                    )
                    device_cfg.role = role_enum
                    node.writeConfig('device')
            except Exception as exc:  # pylint:disable=broad-except
                self.logger.error('Invalid role %s: %s', role_value, repr(exc))

        map_reporting_value = _reset_value('MapReporting')
        if map_reporting_value is not None:
            try:
                map_report = self.config.enforce_type(
                    bool, map_reporting_value
                )
                module_cfg = node.moduleConfig
                if module_cfg.mqtt.map_reporting_enabled != map_report:
                    diffs.append(
                        f'map_reporting {module_cfg.mqtt.map_reporting_enabled}->{map_report}'
                    )
                    module_cfg.mqtt.map_reporting_enabled = map_report
                    node.writeConfig('mqtt')
            except Exception as exc:  # pylint:disable=broad-except
                self.logger.error('Failed to set map reporting: %s', repr(exc))

        for diff in diffs:
            self.logger.info('Reset parameter: %s', diff)

    def on_mqtt_node(self, node_id, payload):
        """
        on_mqtt_node - callback for MQTT node status

        :param node_id:
        :param payload:
        :return:
        """
        self.logger.debug(f'{node_id} is {payload}')
        self.mqtt_nodes[node_id] = payload

    @property
    def nodes_mqtt(self) -> List:
        """
        Return list of nodes with MQTT status

        :return:
        """
        return list(self.mqtt_nodes)

    def node_has_mqtt(self, node_id):
        """
        node_has_mqtt - check if node has MQTT status

        :param node_id:
        :return:
        """
        return node_id in self.mqtt_nodes

    def node_mqtt_status(self, node_id):
        """
        node_mqtt_status - return MQTT status for a specific node ID

        :param node_id:
        :return:
        """
        return self.mqtt_nodes.get(node_id, 'N/A')

    @property
    def nodes(self) -> Dict:
        """
        Return dictionary of nodes

        :return:
        """
        if self.interface is None:
            return {}
        return self.interface.nodes or {}

    @property
    def nodes_with_info(self) -> List:
        """
        Return list of nodes with information

        :return:
        """
        return [self.nodes.get(node) for node in self.nodes]

    @property
    def nodes_with_position(self) -> List:
        """
        Filter out nodes without position

        :return:
        """
        return [
            node_info
            for node_info in self.nodes_with_info
            if node_info.get('position')
        ]

    @property
    def nodes_with_user(self) -> List:
        """
        Filter out nodes without position or user

        :return:
        """
        return [
            node_info
            for node_info in self.nodes_with_position
            if node_info.get('user')
        ]

    # pylint:disable=too-many-branches
    def format_nodes(self, include_self=False):
        """
        Formats node list to be more compact

        :param include_self:
        :param nodes:
        :return:
        """
        table = self.interface.showNodes(includeSelf=include_self)
        if not table:
            return "No other nodes"

        nodes = re.sub(r'[╒═╤╕╘╧╛╞╪╡├─┼┤]', '', table)
        nodes = nodes.replace('│', ',')
        new_nodes = []
        header = True
        for line in nodes.split('\n'):
            line = line.lstrip(',').rstrip(',').rstrip('\n')
            if not line:
                continue
            # clear column value
            i = 0
            new_line = []
            for column in line.split(','):
                column = column.strip()
                if i == 0:
                    column = f'**{column}**'.replace('.', r'\.') if header else f'**{column}**`'
                new_line.append(f'{column}, ')
                if not header:
                    i += 1
            reassembled_line = ''.join(new_line).rstrip(', ')
            reassembled_line = f'{reassembled_line}' if header else f'{reassembled_line}`'
            header = False
            new_nodes.append(reassembled_line)
        filtered_nodes = []
        for line in new_nodes:
            node_id = line.split(', ')[3]
            if not node_id.startswith('!'):
                continue
            if self.filter.banned(node_id):
                self.logger.debug(f"Node {node_id} is in a blacklist...")
                continue
            filtered_nodes.append(line)
        return '\n'.join(new_nodes)

    def run_loop(self):
        """
        Meshtastic loop runner. Used for messages

        :return:
        """
        setthreadtitle(self.name)

        self.logger.debug("Opening FIFO...")
        create_fifo(self.fifo)
        while not self.exit:
            with open(self.fifo, encoding='utf-8') as fifo:
                for line in fifo:
                    line = line.rstrip('\n')
                    self.send_text(line, destinationId=MESHTASTIC_BROADCAST_ADDR)

    def run_cmd_loop(self):
        """
        Meshtastic loop runner. Used for commands

        :return:
        """
        setthreadtitle("MeshtasticCmd")

        self.logger.debug("Opening FIFO...")
        create_fifo(self.fifo_cmd)
        while not self.exit:
            with open(self.fifo_cmd, encoding='utf-8') as fifo:
                for line in fifo:
                    line = line.rstrip('\n')
                    if line.startswith("reboot"):
                        self.logger.warning("Reboot requested using CMD...")
                        self.reboot()
                    if line.startswith("reset_db"):
                        self.logger.warning("Reset DB requested using CMD...")
                        self.reset_db()

    def shutdown(self):
        """
        Stop Meshtastic connection
        """
        self.exit = True
        self._health_watchdog_stop.set()

    def run(self):
        """
        Meshtastic connection runner

        :return:
        """
        if self.config.enforce_type(bool, self.config.Meshtastic.FIFOEnabled):
            thread = Thread(target=self.run_loop, daemon=True, name=self.name)
            thread.start()
            cmd_thread = Thread(target=self.run_cmd_loop, daemon=True, name="MeshtasticCmd")
            cmd_thread.start()
