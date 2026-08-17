"""Test delle decisioni di notifica, con client e notifier finti.

Nessuna rete e nessun Telegram: qui si verifica *quando* il bot parla, che è
la parte che l'utente percepisce come "funziona" o "non funziona".
"""

from pathlib import Path

import pytest

from testflight_watcher.client import Fetched
from testflight_watcher.monitor import Monitor
from testflight_watcher.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
APERTO = (FIXTURES / "open-applist.html").read_text(encoding="utf-8", errors="replace")
PIENO = (FIXTURES / "closed-full.html").read_text(encoding="utf-8", errors="replace")


class ClientFinto:
    """Restituisce risposte predefinite, o solleva se l'elemento è un'eccezione."""

    def __init__(self, *risposte):
        self.risposte = list(risposte)
        self.chiamate = 0

    def fetch(self, tf_id):
        self.chiamate += 1
        r = self.risposte[min(self.chiamate - 1, len(self.risposte) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


class NotifierFinto:
    def __init__(self):
        self.messaggi = []

    def send(self, testo):
        self.messaggi.append(testo)
        return True

    @property
    def disponibilita(self):
        return [m for m in self.messaggi if "Posto disponibile" in m]

    @property
    def anomalie(self):
        return [m for m in self.messaggi if "Rilevamento incerto" in m]

    @property
    def cache(self):
        return [m for m in self.messaggi if "Risposte dalla cache" in m]


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "data.json"))
    s.add("aBcD1234", "App di prova")
    return s


def monitor(store, client, notifier):
    return Monitor(store, client, notifier, interval=300)


def ok(body, key=None):
    return Fetched(status_code=200, body=body, correlation_key=key)


# ── Il caso che prima non funzionava ────────────────────────────────────────

def test_beta_che_apre_genera_una_notifica(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(PIENO), ok(APERTO)), n)

    m.check_once()
    assert n.disponibilita == [], "un beta pieno non deve svegliare nessuno"
    assert store.apps()["aBcD1234"]["state"] == "full"

    m.check_once()
    assert len(n.disponibilita) == 1
    assert "App di prova" in n.disponibilita[0]
    assert store.apps()["aBcD1234"]["state"] == "open"


def test_beta_che_resta_aperto_non_rinotifica(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(APERTO)), n)

    for _ in range(4):
        m.check_once()

    assert len(n.disponibilita) == 1, "niente spam finché resta aperto"


def test_beta_che_riapre_notifica_di_nuovo(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(APERTO), ok(PIENO), ok(APERTO)), n)

    m.check_once()
    m.check_once()
    m.check_once()

    assert len(n.disponibilita) == 2


# ── Errori di rete ──────────────────────────────────────────────────────────

def test_errore_di_rete_non_cancella_lo_stato_e_non_notifica(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(APERTO), ConnectionError("rete giù")), n)

    m.check_once()
    assert store.apps()["aBcD1234"]["state"] == "open"

    m.check_once()
    assert store.apps()["aBcD1234"]["state"] == "open", "lo stato noto va conservato"
    assert len(n.disponibilita) == 1, "il ripristino non deve rinotificare"


def test_http_500_e_trattato_come_errore_non_come_beta_pieno(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(APERTO), Fetched(500, "")), n)

    m.check_once()
    m.check_once()
    assert store.apps()["aBcD1234"]["state"] == "open"


def test_dopo_un_errore_l_app_viene_saltata_per_un_giro(store):
    """Backoff: la prima ripetizione salta un giro, non ritenta subito."""
    c = ClientFinto(ConnectionError("giù"))
    m = monitor(store, c, NotifierFinto())

    m.check_once()
    assert c.chiamate == 1
    m.check_once()
    assert c.chiamate == 1, "giro saltato dal backoff"
    m.check_once()
    assert c.chiamate == 2


# ── Anomalie ────────────────────────────────────────────────────────────────

DISCORDE = """
  <div class="beta-status"><span>This beta is full.</span></div>
  <a class="button" href="#">View in TestFlight</a>
"""


def test_segnali_discordi_avvisano_ma_non_annunciano_posti(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(DISCORDE)), n)

    m.check_once()

    assert len(n.anomalie) == 1
    assert n.disponibilita == [], "un'incertezza non è un posto libero"
    assert store.apps()["aBcD1234"]["state"] == "unknown"
    assert "This beta is full." in n.anomalie[0], "il testo grezzo serve a diagnosticare"


def test_la_stessa_anomalia_avvisa_una_volta_sola(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(DISCORDE)), n)

    for _ in range(5):
        m.check_once()

    assert len(n.anomalie) == 1


def test_anomalia_che_rientra_e_si_ripresenta_avvisa_di_nuovo(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(DISCORDE), ok(PIENO), ok(DISCORDE)), n)

    m.check_once()
    m.check_once()
    assert store.apps()["aBcD1234"]["anomaly_fingerprint"] is None
    m.check_once()

    assert len(n.anomalie) == 2


# ── Freschezza delle risposte ───────────────────────────────────────────────

def test_correlation_key_ripetuta_segnala_risposte_dalla_cache(store):
    """Se il cache-buster smettesse di funzionare, deve emergere.

    È lo stesso schema di guasto del bug originale: un pezzo che si rompe
    senza far rumore. Qui fa rumore.
    """
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(PIENO, key="STESSA")), n)

    m.check_once()
    assert n.cache == [], "la prima risposta non ha un termine di paragone"

    m.check_once()
    assert len(n.cache) == 1
    assert "aBcD1234" in n.cache[0]


def test_l_avviso_di_cache_non_si_ripete_a_ogni_giro(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(PIENO, key="STESSA")), n)

    for _ in range(6):
        m.check_once()

    assert len(n.cache) == 1


def test_chiavi_diverse_non_generano_avvisi(store):
    """Il funzionamento normale: Apple rigenera a ogni richiesta."""
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(PIENO, "K1"), ok(PIENO, "K2"), ok(PIENO, "K3")), n)

    for _ in range(3):
        m.check_once()

    assert n.cache == []


def test_dopo_una_chiave_fresca_un_nuovo_blocco_riavvisa(store):
    n = NotifierFinto()
    m = monitor(
        store,
        ClientFinto(ok(PIENO, "K1"), ok(PIENO, "K1"), ok(PIENO, "K2"), ok(PIENO, "K2")),
        n,
    )
    for _ in range(4):
        m.check_once()

    assert len(n.cache) == 2, "un nuovo blocco è un evento nuovo"


def test_il_rilevamento_cache_non_altera_lo_stato_del_beta(store):
    """È una diagnosi sul trasporto, non sul beta."""
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(APERTO, key="STESSA")), n)

    m.check_once()
    m.check_once()

    assert store.apps()["aBcD1234"]["state"] == "open"
    assert len(n.disponibilita) == 1


def test_senza_correlation_key_non_si_segnala_nulla(store):
    """Apple potrebbe togliere l'header: non deve diventare un falso allarme."""
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(PIENO)), n)

    for _ in range(3):
        m.check_once()

    assert n.cache == []


def test_app_rimossa_durante_il_giro_non_viene_reinserita(store):
    n = NotifierFinto()
    m = monitor(store, ClientFinto(ok(APERTO)), n)
    store.remove("aBcD1234")

    m.check_once()
    assert store.apps() == {}
