"""Test dell'interfaccia Telegram, senza rete e senza bot reale."""

import asyncio

import pytest
from telegram.error import BadRequest

from testflight_watcher.bot import render_list, safe_edit, status_label


class QueryFinta:
    def __init__(self, errore=None):
        self.errore = errore
        self.chiamate = 0

    async def edit_message_text(self, text, **kwargs):
        self.chiamate += 1
        if self.errore:
            raise self.errore


def test_ripremere_lo_stesso_pulsante_non_e_un_errore():
    """Telegram rifiuta un edit che non cambia nulla.

    Succede a ogni doppio clic sullo stesso pulsante ed è innocuo: prima
    riempiva i log di traceback.
    """
    q = QueryFinta(BadRequest("Message is not modified: specified new message content"))
    asyncio.run(safe_edit(q, "stesso testo"))
    assert q.chiamate == 1


def test_gli_altri_BadRequest_non_vengono_nascosti():
    """Tollerare "not modified" non deve diventare un ingoia-tutto."""
    q = QueryFinta(BadRequest("Chat not found"))
    with pytest.raises(BadRequest, match="Chat not found"):
        asyncio.run(safe_edit(q, "testo"))


def test_una_modifica_riuscita_passa():
    q = QueryFinta()
    asyncio.run(safe_edit(q, "nuovo testo"))
    assert q.chiamate == 1


# ── Presentazione ───────────────────────────────────────────────────────────

def test_ogni_stato_ha_una_sua_etichetta():
    visti = {
        status_label({"state": s})
        for s in ("open", "full", "invalid", "unknown", None)
    }
    assert len(visti) == 5, "stati diversi non devono apparire uguali all'utente"


def test_lo_stato_incerto_e_visibile_nella_lista():
    testo = render_list({"aBcD1234": {"name": "App", "state": "unknown"}})
    assert "incerto" in testo


def test_i_nomi_con_caratteri_html_non_rompono_il_messaggio():
    """Il nome lo scrive l'utente: senza escape l'invio fallirebbe."""
    testo = render_list({"aBcD1234": {"name": "App <b>& Co", "state": "full"}})
    assert "&lt;b&gt;" in testo and "&amp;" in testo
