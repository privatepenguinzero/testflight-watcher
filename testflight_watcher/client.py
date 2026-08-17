"""Accesso HTTP alle pagine di join TestFlight.

L'unico modulo che parla con Apple. Tenerlo separato è ciò che rende il
detector provabile senza rete.

Note sul mezzo, misurate il 2026-08-17:

- La risposta non contiene né ETag né Last-Modified: niente richieste
  condizionali, ogni controllo scarica la pagina intera (~40 KB).
- Apple serve una copia in cache per 600s. Gli header `Cache-Control: no-cache`
  e `Pragma: no-cache` vengono ignorati, ma un parametro variabile in query
  string ottiene una risposta generata al momento. Si riconosce da
  `X-Apple-Jingle-Correlation-Key` diversa a ogni richiesta e da `max-age`
  pieno a 600 invece che decrescente.

Senza cache-buster, `CHECK_INTERVAL` non significa nulla: la freschezza reale
oscilla fra l'intervallo e l'intervallo più 600s, a seconda di dove si cade
nella finestra di cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from curl_cffi import requests

from .config import Config

TESTFLIGHT_URL = "https://testflight.apple.com/join/{}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    # Forzato: il detector riconosce le frasi inglesi, e senza questo Apple
    # potrebbe rispondere nella lingua dedotta dall'IP.
    "Accept-Language": "en-US,en;q=0.9",
}


def join_url(tf_id: str) -> str:
    """URL pulito, quello che finisce nelle notifiche e che apre l'utente."""
    return TESTFLIGHT_URL.format(tf_id)


def check_url(tf_id: str) -> str:
    """URL per i controlli, con cache-buster.

    Il parametro non cambia la pagina servita (verificato: un beta aperto
    resta aperto e uno pieno resta pieno), ma evita di rileggere una copia
    vecchia fino a 10 minuti.
    """
    return f"{join_url(tf_id)}?_={time.time_ns()}"


@dataclass(frozen=True)
class Fetched:
    status_code: int
    body: str


class TestFlightClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg

    def fetch(self, tf_id: str) -> Fetched:
        """Scarica la pagina. Solleva se la rete non risponde."""
        r = requests.get(
            check_url(tf_id),
            headers=HEADERS,
            timeout=self._cfg.request_timeout,
            # Fingerprint TLS di un browser reale: senza, Apple può bloccare.
            impersonate=self._cfg.impersonate,
        )
        return Fetched(status_code=r.status_code, body=r.text)
