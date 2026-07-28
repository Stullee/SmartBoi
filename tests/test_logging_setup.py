import logging

from smartboi.logging_setup import setup_logging


def test_setup_logging_quiets_known_noisy_loggers(tmp_path):
    setup_logging(level="INFO", log_dir=str(tmp_path))
    for name in ("httpx", "httpcore", "aiohttp.access", "ib_async"):
        assert logging.getLogger(name).level == logging.WARNING
