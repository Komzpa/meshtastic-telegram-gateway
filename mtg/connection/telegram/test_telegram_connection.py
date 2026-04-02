import asyncio
import sys
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Mock external dependencies to avoid import conflicts
mock_telegram = MagicMock()
mock_update = MagicMock()
mock_update.ALL_TYPES = ['mocked_all_types']
mock_telegram.Update = mock_update
sys.modules['telegram'] = mock_telegram
sys.modules['telegram.ext'] = MagicMock()

from mtg.connection.telegram.telegram import TelegramConnection


@pytest.fixture
def mock_logger():
    """Mock logger fixture"""
    return MagicMock()


@pytest.fixture
def telegram_connection(mock_logger):
    """TelegramConnection instance fixture"""
    with patch('mtg.connection.telegram.telegram.Application.builder') as mock_builder:
        mock_app = MagicMock()
        mock_builder.return_value.token.return_value.build.return_value = mock_app
        return TelegramConnection("test_token", mock_logger)


class TestTelegramConnection:
    """Test TelegramConnection class"""

    def test_telegram_connection_init(self, mock_logger):
        """Test TelegramConnection initialization"""
        with patch('mtg.connection.telegram.telegram.Application.builder') as mock_builder:
            with patch('logging.getLogger') as mock_get_logger:
                mock_logger_httpx = MagicMock()
                mock_get_logger.return_value = mock_logger_httpx
                mock_builder_chain = MagicMock()
                mock_app = MagicMock()
                mock_builder.return_value = mock_builder_chain
                mock_builder_chain.token.return_value = mock_builder_chain
                mock_builder_chain.job_queue.return_value = mock_builder_chain
                mock_builder_chain.build.return_value = mock_app

                conn = TelegramConnection("test_token", mock_logger)

                assert conn.logger == mock_logger
                assert conn.application == mock_app
                mock_get_logger.assert_called_once_with("httpx")
                mock_logger_httpx.setLevel.assert_called_once_with(30)  # logging.WARNING = 30

    def test_send_message(self, telegram_connection):
        """Test send_message method"""
        mock_response = MagicMock()
        mock_response.json.return_value = {'ok': True, 'result': {'message_id': 55}}

        with patch('mtg.connection.telegram.telegram.requests.post') as mock_post:
            mock_post.return_value = mock_response
            result = telegram_connection.send_message(chat_id=12345, text="Test message")

        mock_post.assert_called_once_with(
            'https://api.telegram.org/bottest_token/sendMessage',
            json={'chat_id': 12345, 'text': 'Test message'},
            timeout=15,
        )
        assert result.message_id == 55

    def test_send_message_with_args_and_kwargs(self, telegram_connection):
        """Test send_message method with various args and kwargs"""
        mock_response = MagicMock()
        mock_response.json.return_value = {'ok': True, 'result': {'message_id': 89}}

        with patch('mtg.connection.telegram.telegram.requests.post') as mock_post:
            result = telegram_connection.send_message(
                12345, "Test message",
                parse_mode="HTML",
                reply_to_message_id=67890
            )

        mock_post.assert_called_once_with(
            'https://api.telegram.org/bottest_token/sendMessage',
            json={
                'chat_id': 12345,
                'text': 'Test message',
                'parse_mode': 'HTML',
                'reply_to_message_id': 67890,
            },
            timeout=15,
        )
        assert result.message_id == 89

    def test_poll(self, telegram_connection):
        """Test poll method"""
        telegram_connection.application.run_polling = MagicMock()

        # Mock the Update import in the actual module
        with patch('mtg.connection.telegram.telegram.Update') as mock_update:
            mock_update.ALL_TYPES = ['mocked_all_types']

            with patch('asyncio.new_event_loop') as mock_new_loop:
                with patch('asyncio.set_event_loop') as mock_set_loop:
                    mock_loop = MagicMock()
                    mock_new_loop.return_value = mock_loop
                    mock_loop.run_until_complete = MagicMock()

                    telegram_connection.poll()

                    # The actual call should pass the mocked Update.ALL_TYPES
                    telegram_connection.application.run_polling.assert_called_once_with(
                        allowed_updates=['mocked_all_types']
                    )
                    # Check that start_queue_processor was called
                    mock_loop.run_until_complete.assert_called_once()
                    mock_set_loop.assert_called_once_with(mock_loop)

    def test_shutdown(self, telegram_connection):
        """Test shutdown method"""
        telegram_connection.application.stop_running = MagicMock()

        telegram_connection.shutdown()

        telegram_connection.application.stop_running.assert_called_once()

    def test_token_passed_to_builder(self, mock_logger):
        """Test that token is correctly passed to the application builder"""
        with patch('mtg.connection.telegram.telegram.Application.builder') as mock_builder:
            mock_token_builder = MagicMock()
            mock_builder.return_value = mock_token_builder
            mock_token_builder.token.return_value = mock_token_builder
            mock_token_builder.job_queue.return_value = mock_token_builder
            mock_app = MagicMock()
            mock_token_builder.build.return_value = mock_app

            TelegramConnection("my_secret_token", mock_logger)

            mock_builder.assert_called_once()
            mock_token_builder.token.assert_called_once_with("my_secret_token")
            mock_token_builder.job_queue.assert_called_once_with(None)
            mock_token_builder.build.assert_called_once()

    def test_send_message_requires_chat_id_and_text(self, telegram_connection):
        """Test that send_message validates required payload fields."""
        assert telegram_connection.send_message(parse_mode="HTML") is None

    def test_logger_assignment(self, telegram_connection, mock_logger):
        """Test that logger is properly assigned"""
        assert telegram_connection.logger is mock_logger

    def test_application_assignment(self, telegram_connection):
        """Test that application is properly assigned"""
        assert telegram_connection.application is not None
        # The application should be the mock we set up in the fixture

    def test_httpx_logger_level_set(self, mock_logger):
        """Test that httpx logger level is set to WARNING"""
        with patch('mtg.connection.telegram.telegram.Application.builder'):
            with patch('logging.getLogger') as mock_get_logger:
                mock_httpx_logger = MagicMock()
                mock_get_logger.return_value = mock_httpx_logger

                TelegramConnection("test_token", mock_logger)

                mock_get_logger.assert_called_once_with("httpx")
                mock_httpx_logger.setLevel.assert_called_once_with(30)  # WARNING level
