"""Invio di messaggi Telegram dal thread di monitoraggio.

Sincrono di proposito: il monitor è un thread normale, non un loop asyncio, e
far dialogare i due mondi qui non porterebbe alcun vantaggio.
"""

from __future__ import annotations

import html
import logging

from curl_cffi import requests

log = logging.getLogger(__name__)


def esc(text) -> str:
    """Escape per parse_mode HTML.

    I nomi delle app li scrive l'utente: senza escape un nome con < o &
    fa fallire l'invio dell'intero messaggio.
    """
    return html.escape(str(text), quote=False)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    def send(self, message: str) -> bool:
        """Ritorna True se Telegram ha accettato il messaggio.

        Non solleva: un errore di invio non deve far cadere il giro di
        controlli né, peggio, uccidere il thread di monitoraggio.
        """
        try:
            r = requests.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except Exception as e:
            log.error("Invio notifica fallito: %s", e)
            return False

        if r.status_code != 200:
            log.error("Invio notifica fallito: HTTP %s %s", r.status_code, r.text[:200])
            return False
        return True
