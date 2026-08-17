"""Configurazione da variabili d'ambiente.

Validata all'avvio: se manca il token si deve fallire subito e con un messaggio
chiaro, non al primo tentativo di invio mezz'ora dopo.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Apple dichiara `Cache-Control: max-age=600` sulle pagine di join: sotto i
# 600s si rischia di rileggere la stessa copia in cache. 300s è un compromesso
# fra reattività e richieste sprecate.
DEFAULT_CHECK_INTERVAL = 300
APPLE_CACHE_SECONDS = 600
MIN_CHECK_INTERVAL = 30


class ConfigError(RuntimeError):
    """Configurazione mancante o non valida."""


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    check_interval: int = DEFAULT_CHECK_INTERVAL
    db_file: str = "/app/data/data.json"
    # curl_cffi impersona il fingerprint TLS di un browser reale: senza questo
    # Apple può bloccare le richieste automatiche.
    impersonate: str = "safari17_0"
    request_timeout: int = 20


def _required(env: dict, name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(
            f"{name} non impostata. Serve nel file .env o nell'ambiente del container."
        )
    return value


def _interval(env: dict) -> int:
    raw = (env.get("CHECK_INTERVAL") or "").strip()
    if not raw:
        return DEFAULT_CHECK_INTERVAL
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"CHECK_INTERVAL deve essere un numero di secondi, non {raw!r}"
        ) from None
    if value < MIN_CHECK_INTERVAL:
        raise ConfigError(
            f"CHECK_INTERVAL={value} è troppo basso: minimo {MIN_CHECK_INTERVAL}s. "
            "Bussare più spesso non anticipa la risposta di Apple, che tiene la "
            f"pagina in cache per {APPLE_CACHE_SECONDS}s."
        )
    return value


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env

    cfg = Config(
        telegram_token=_required(env, "TELEGRAM_TOKEN"),
        telegram_chat_id=_required(env, "TELEGRAM_CHAT_ID"),
        check_interval=_interval(env),
        db_file=(env.get("DB_FILE") or "/app/data/data.json").strip(),
        impersonate=(env.get("IMPERSONATE") or "safari17_0").strip(),
    )

    if cfg.check_interval < APPLE_CACHE_SECONDS:
        log.info(
            "CHECK_INTERVAL=%ds è sotto i %ds di cache dichiarati da Apple: "
            "parte dei controlli leggerà con ogni probabilità una copia già "
            "vista, senza guadagno di reattività.",
            cfg.check_interval,
            APPLE_CACHE_SECONDS,
        )

    return cfg
