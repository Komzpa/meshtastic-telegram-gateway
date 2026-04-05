"""Tests for logging helpers."""

import logging
from pathlib import Path

from mtg.log.log import setup_logger


def test_setup_logger_recreates_missing_log_dir(tmp_path):
    log_dir = tmp_path / "logs"
    logger = setup_logger("test_logger_recreate_dir", log_dir=log_dir)
    try:
        log_file = log_dir / "test_logger_recreate_dir.log"
        assert log_file.exists()

        for handler in logger.handlers:
            if hasattr(handler, "baseFilename"):
                handler.acquire()
                try:
                    if handler.stream is not None:
                        handler.stream.close()
                    handler.stream = None
                    log_file.unlink()
                    log_dir.rmdir()
                finally:
                    handler.release()

        logger.info("recreated")

        assert log_file.exists()
        assert "recreated" in log_file.read_text(encoding="utf-8")
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
        logging.Logger.manager.loggerDict.pop("test_logger_recreate_dir", None)
