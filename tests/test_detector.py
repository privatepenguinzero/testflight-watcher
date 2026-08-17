"""Test del detector sulle pagine TestFlight reali.

Le fixture sono pagine scaricate davvero da testflight.apple.com il
2026-08-17. Sono il motivo per cui questo modulo esiste separato dalla rete:
il bug che ha reso il bot inutile sarebbe stato preso dal primo di questi
test, ma la classificazione viveva dentro la chiamata HTTP e non era provabile
senza internet e un beta aperto sotto mano.
"""

from pathlib import Path

import pytest

from testflight_watcher.detector import (
    ST_ERROR,
    ST_FULL,
    ST_INVALID,
    ST_OPEN,
    ST_UNKNOWN,
    beta_status_text,
    button_labels,
    classify,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


# ── Le pagine reali ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("fixture", ["open-applist.html", "open-testerbuddy.html"])
def test_beta_aperto_e_rilevato_come_aperto(fixture):
    """Il caso che la versione precedente sbagliava sempre."""
    d = classify(200, load(fixture))
    assert d.state == ST_OPEN
    assert d.anomaly is None, f"segnali discordi su una pagina aperta: {d.anomaly}"
    assert d.structural == ST_OPEN
    assert d.textual == ST_OPEN


@pytest.mark.parametrize("fixture", ["closed-full.html", "closed-not-accepting.html"])
def test_beta_chiuso_e_rilevato_come_pieno(fixture):
    d = classify(200, load(fixture))
    assert d.state == ST_FULL
    assert d.anomaly is None
    assert d.structural == ST_FULL
    assert d.textual == ST_FULL


def test_pagina_404_e_link_non_valido():
    d = classify(404, load("notfound-404.html"))
    assert d.state == ST_INVALID


def test_i_due_segnali_concordano_su_tutte_le_fixture():
    """Il presupposto su cui poggia l'intero design.

    Se un giorno Apple cambia pagina e questo test fallisce, il modello a
    doppio segnale va rivisto: è esattamente il campanello che vogliamo.
    """
    attesi = {
        "open-applist.html": ST_OPEN,
        "open-testerbuddy.html": ST_OPEN,
        "closed-full.html": ST_FULL,
        "closed-not-accepting.html": ST_FULL,
    }
    for nome, atteso in attesi.items():
        d = classify(200, load(nome))
        assert d.structural == d.textual == atteso, f"{nome}: {d}"


# ── Estrazione dei singoli segnali ──────────────────────────────────────────

def test_beta_status_ignora_i_div_annidati():
    """L'icona dell'app è un <div> dentro beta-status.

    Fermarsi al primo </div> restituirebbe testo vuoto, che verrebbe scambiato
    per un beta aperto: falso positivo.
    """
    assert beta_status_text(load("closed-full.html")) == "This beta is full."


def test_i_pulsanti_di_un_beta_chiuso_sono_vuoti():
    labels = button_labels(load("closed-full.html"))
    assert labels, "nessun pulsante trovato: la pagina ha cambiato forma"
    assert not any(labels)


def test_i_pulsanti_di_un_beta_aperto_hanno_testo():
    labels = button_labels(load("open-applist.html"))
    assert any(labels)
    assert "View in TestFlight" in labels


# ── Casi costruiti: come si comporta quando la pagina cambia ────────────────

def test_segnali_discordi_danno_unknown_e_non_aprono():
    """Pulsanti attivi ma testo che dice "pieno".

    Non deve diventare `open`: sveglierebbe l'utente per niente. Non deve
    diventare `full` in silenzio: è la scelta che ha causato il bug originale.
    """
    body = """
      <div class="beta-status"><span>This beta is full.</span></div>
      <a class="button" href="#">View in TestFlight</a>
    """
    d = classify(200, body)
    assert d.state == ST_UNKNOWN
    assert d.anomaly is not None
    assert d.fingerprint is not None


def test_testo_sconosciuto_ma_pulsanti_attivi_resta_utilizzabile():
    """Apple riscrive l'invito: il segnale strutturale regge da solo."""
    body = """
      <div class="beta-status"><span>Qualcosa di mai visto prima.</span></div>
      <a class="button" href="#">Unisciti alla beta</a>
    """
    d = classify(200, body)
    assert d.state == ST_OPEN
    assert d.anomaly is not None, "il degrado deve essere segnalato"


def test_pulsanti_assenti_ma_testo_chiaro_resta_utilizzabile():
    """Apple cambia il markup: il segnale testuale regge da solo."""
    body = '<div class="beta-status"><span>This beta is full.</span></div>'
    d = classify(200, body)
    assert d.state == ST_FULL
    assert d.anomaly is not None


def test_pagina_irriconoscibile_da_unknown():
    d = classify(200, "<html><body>Manutenzione in corso</body></html>")
    assert d.state == ST_UNKNOWN
    assert d.anomaly is not None


def test_pagina_vuota_non_viene_scambiata_per_aperta():
    """Regressione: la vecchia logica trattava "nessun testo" come aperto."""
    assert classify(200, "").state == ST_UNKNOWN


def test_errore_http_non_e_uno_stato_del_beta():
    assert classify(500, "").state == ST_ERROR


# ── Impronta delle anomalie ─────────────────────────────────────────────────

def test_nessuna_impronta_quando_non_ci_sono_anomalie():
    assert classify(200, load("open-applist.html")).fingerprint is None


def test_stessa_anomalia_stessa_impronta():
    body = '<div class="beta-status"><span>Strano</span></div><a class="button">x</a>'
    assert classify(200, body).fingerprint == classify(200, body).fingerprint


def test_anomalie_diverse_impronte_diverse():
    a = '<div class="beta-status"><span>Strano</span></div><a class="button">x</a>'
    b = '<div class="beta-status"><span>Altro</span></div><a class="button">x</a>'
    assert classify(200, a).fingerprint != classify(200, b).fingerprint
