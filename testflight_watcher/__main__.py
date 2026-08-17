"""Punto d'ingresso: costruisce i pezzi, li collega e avvia.

    python -m testflight_watcher
"""

from __future__ import annotations

import logging
import sys
from threading import Thread

from .bot import build_application
from .client import TestFlightClient
from .config import ConfigError, load_config
from .monitor import Monitor
from .notifier import TelegramNotifier
from .store import Store

log = logging.getLogger("testflight_watcher")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for rumoroso in ("httpx", "telegram.ext.Application"):
        logging.getLogger(rumoroso).setLevel(logging.WARNING)


def main() -> int:
    setup_logging()

    try:
        cfg = load_config()
    except ConfigError as e:
        # Fallire subito e con chiarezza: un token mancante non deve
        # manifestarsi come silenzio mezz'ora dopo l'avvio.
        log.error("Configurazione non valida: %s", e)
        return 1

    store = Store(cfg.db_file)
    store.migrate()

    client = TestFlightClient(cfg)
    notifier = TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)
    monitor = Monitor(store, client, notifier, cfg.check_interval)

    Thread(target=monitor.run_forever, daemon=True, name="monitor").start()

    log.info("▶ Avvio bot Telegram (%d app in lista)", len(store.apps()))
    build_application(cfg, store, client).run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
