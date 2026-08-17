"""Accesso HTTP alle pagine di join TestFlight.

L'unico modulo che parla con Apple. Tenerlo separato è ciò che rende il
detector provabile senza rete.

Note sul mezzo, misurate il 2026-08-17:

- La risposta non contiene né ETag né Last-Modified: niente richieste
  condizionali, ogni controllo riscarica la pagina intera. Neanche le
  richieste `Range` sono supportate: l'header viene ignorato e il server
  risponde 200 con il corpo completo, quindi non si può chiedere solo il
  frammento che ci interessa. Sul filo sono però ~9,5 KB, non ~41 KB:
  l'impersonation di curl_cffi include l'Accept-Encoding del browser e la
  risposta arriva compressa con gzip.
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


# Apple assegna questo identificativo a ogni risposta *generata*. Se si ripete
# fra due controlli, stiamo rileggendo la stessa copia in cache: è il modo per
# accorgersi se un giorno il cache-buster smettesse di funzionare.
CORRELATION_HEADER = "X-Apple-Jingle-Correlation-Key"

# Apple oggi non manda Retry-After sulle pagine di join, ma se iniziasse a
# limitarci sarebbe il modo corretto per dirci quanto aspettare.
RETRY_AFTER_HEADER = "Retry-After"

# Codici con cui un server dice "ti sto rifiutando", da distinguere da un
# guasto di rete: meritano una pausa lunga, non un ritentativo immediato.
BLOCKED_STATUSES = (401, 403, 429)


def _parse_retry_after(value: str | None) -> int | None:
    """Secondi di attesa richiesti dal server.

    La specifica ammette anche una data HTTP: in quel caso rinunciamo e
    lasciamo decidere al backoff, piuttosto che sbagliare il calcolo.
    """
    if not value:
        return None
    try:
        secondi = int(value.strip())
    except ValueError:
        return None
    return secondi if secondi > 0 else None


@dataclass(frozen=True)
class Fetched:
    status_code: int
    body: str
    correlation_key: str | None = None
    retry_after: int | None = None

    @property
    def blocked(self) -> bool:
        return self.status_code in BLOCKED_STATUSES


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
        return Fetched(
            status_code=r.status_code,
            body=r.text,
            correlation_key=r.headers.get(CORRELATION_HEADER),
            retry_after=_parse_retry_after(r.headers.get(RETRY_AFTER_HEADER)),
        )
