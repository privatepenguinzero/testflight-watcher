"""Punto d'ingresso: costruisce i pezzi, li collega e avvia.

    python -m testflight_watcher
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys

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
    app = build_application(cfg, store, client)

    # Il monitoraggio gira nel JobQueue invece che in un thread staccato: così
    # nasce e muore con l'applicazione, gli errori passano dal suo error
    # handler e non resta un daemon thread che nessuno sorveglia.
    # Il lavoro è bloccante (curl_cffi), quindi va comunque fuori dal loop
    # asyncio: il lock nello Store resta necessario.
    async def giro(_context) -> None:
        try:
            # run_repeating ha cadenza fissa: il jitter va aggiunto qui, per
            # non bussare ad Apple a intervalli perfettamente regolari.
            await asyncio.sleep(monitor.jitter_seconds())
            await asyncio.to_thread(monitor.check_once)
        except Exception as e:
            # Un giro fallito non deve fermare quelli successivi.
            log.exception("Errore nel ciclo di sorveglianza: %s", e)

    if app.job_queue is None:
        log.error(
            "JobQueue non disponibile: manca APScheduler. "
            "Installa python-telegram-bot[job-queue]."
        )
        return 1

    app.job_queue.run_repeating(
        giro,
        interval=cfg.check_interval,
        # Jitter sul primo avvio: due container riavviati insieme non partono
        # in perfetta sincronia verso Apple.
        first=random.uniform(0, min(10, cfg.check_interval)),
        name="sorveglianza",
    )

    log.info(
        "▶ Avvio (%d app in lista, controllo ogni %ds)",
        len(store.apps()),
        cfg.check_interval,
    )
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
