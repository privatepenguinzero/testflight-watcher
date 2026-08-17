"""Configurazione da variabili d'ambiente.

Validata all'avvio: se manca il token si deve fallire subito e con un messaggio
chiaro, non al primo tentativo di invio mezz'ora dopo.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Il cache-buster in client.py fa sì che ogni controllo ottenga una risposta
# generata al momento: l'intervallo è quindi la latenza di rilevamento reale,
# non un valore diluito dalla cache di Apple. Il rovescio è che ogni controllo
# colpisce l'origine, quindi intervalli brevi si traducono in traffico vero.
# 300s è un compromesso prudente; scendere è legittimo se serve reattività.
DEFAULT_CHECK_INTERVAL = 300
MIN_CHECK_INTERVAL = 30
# Sotto questa soglia il volume di richieste inizia a essere degno di nota.
CHATTY_CHECK_INTERVAL = 120


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
            "Ogni controllo è una richiesta all'origine di Apple."
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

    if cfg.check_interval < CHATTY_CHECK_INTERVAL:
        # Non è un errore: è una scelta legittima per beta molto contesi. Ma
        # sotto i 120s il volume verso Apple cresce in fretta e vale la pena
        # che sia una decisione consapevole, non un default dimenticato.
        log.info(
            "CHECK_INTERVAL=%ds: circa %d richieste al giorno per app. "
            "Le risposte sono fresche (cache-buster attivo), ma intervalli "
            "brevi aumentano il rischio di rate-limiting da parte di Apple.",
            cfg.check_interval,
            round(86400 / cfg.check_interval),
        )

    return cfg
