"""Test della persistenza e della migrazione di schema.

Tutto gira su file temporanei: nessun test tocca il data.json reale.
"""

import json

import pytest

from testflight_watcher.store import SCHEMA_VERSION, Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data.json"))


def scrivi(store: Store, contenuto: dict) -> None:
    with open(store.path, "w", encoding="utf-8") as f:
        json.dump(contenuto, f)


# ── Migrazione ──────────────────────────────────────────────────────────────

def test_migrazione_v0_preserva_gli_id_e_i_nomi(store):
    """Riproduce la forma esatta del file in uso prima del refactor.

    È la garanzia che aggiornare il container non perde gli ID inseriti:
    se questo test si rompe, qualcuno sta buttando via i dati dell'utente.
    """
    scrivi(store, {
        "apps": {
            "aBcD1234": {"name": "WA", "available": False},
            "eFgH5678": {"name": "WA2", "available": False},
            "iJkL9012": {"name": "Cloudflare", "available": True},
        }
    })

    assert store.migrate() is True

    apps = store.apps()
    assert set(apps) == {"aBcD1234", "eFgH5678", "iJkL9012"}
    assert apps["aBcD1234"]["name"] == "WA"
    assert apps["eFgH5678"]["name"] == "WA2"
    assert apps["iJkL9012"]["name"] == "Cloudflare"

    # available -> state, con il campo vecchio rimosso
    assert apps["aBcD1234"]["state"] == "full"
    assert apps["iJkL9012"]["state"] == "open"
    assert all("available" not in a for a in apps.values())

    # campi nuovi presenti
    for a in apps.values():
        assert a["detail"] == ""
        assert a["last_checked"] is None
        assert a["anomaly_fingerprint"] is None


def test_migrazione_salva_una_copia_dello_schema_precedente(store):
    scrivi(store, {"apps": {"aBcD1234": {"name": "WA", "available": False}}})
    store.migrate()

    with open(store.path + ".bak-v0", encoding="utf-8") as f:
        originale = json.load(f)
    assert originale["apps"]["aBcD1234"]["available"] is False


def test_migrazione_v1_aggiunge_i_campi_nuovi(store):
    scrivi(store, {"apps": {"aBcD1234": {"name": "WA", "state": "full"}}})
    assert store.migrate() is True

    app = store.apps()["aBcD1234"]
    assert app["state"] == "full", "lo stato già noto non va perso"
    assert app["anomaly_fingerprint"] is None


def test_migrazione_e_idempotente(store):
    scrivi(store, {"apps": {"aBcD1234": {"name": "WA", "available": True}}})
    assert store.migrate() is True
    assert store.migrate() is False, "la seconda migrazione non deve riscrivere"


def test_migrazione_su_db_vuoto_non_esplode(store):
    assert store.load()["apps"] == {}
    store.migrate()
    assert store.load()["version"] == SCHEMA_VERSION


# ── Robustezza del file ─────────────────────────────────────────────────────

def test_file_corrotto_viene_messo_da_parte_non_cancellato(store):
    with open(store.path, "w", encoding="utf-8") as f:
        f.write("{ questo non e' json")

    assert store.load()["apps"] == {}

    with open(store.path + ".corrupt", encoding="utf-8") as f:
        assert "questo non e' json" in f.read()


def test_la_scrittura_non_lascia_file_temporanei(store, tmp_path):
    store.add("aBcD1234", "WA")
    residui = [p.name for p in tmp_path.iterdir() if p.name.startswith(".data-")]
    assert residui == []


# ── Operazioni ──────────────────────────────────────────────────────────────

def test_add_rifiuta_i_duplicati(store):
    assert store.add("aBcD1234", "WA") is True
    assert store.add("aBcD1234", "Altro") is False
    assert store.apps()["aBcD1234"]["name"] == "WA"


def test_remove_restituisce_la_app_rimossa(store):
    store.add("aBcD1234", "WA")
    assert store.remove("aBcD1234")["name"] == "WA"
    assert store.remove("aBcD1234") is None


def test_rename_restituisce_il_nome_precedente(store):
    store.add("aBcD1234", "WA")
    assert store.rename("aBcD1234", "WhatsApp") == "WA"
    assert store.rename("mai-vista", "X") is None
    assert store.apps()["aBcD1234"]["name"] == "WhatsApp"


def test_record_check_aggiorna_stato_e_data(store):
    store.add("aBcD1234", "WA")
    store.record_check("aBcD1234", state="open", detail="To join…")

    app = store.apps()["aBcD1234"]
    assert app["state"] == "open"
    assert app["detail"] == "To join…"
    assert app["last_checked"].endswith("Z"), "data in ISO-8601 UTC"


def test_record_check_su_app_rimossa_non_la_resuscita(store):
    """L'utente può rimuovere un'app mentre il suo controllo è in corso."""
    store.add("aBcD1234", "WA")
    store.remove("aBcD1234")
    store.record_check("aBcD1234", state="open")
    assert store.apps() == {}


def test_record_check_non_perde_le_app_aggiunte_nel_frattempo(store):
    """Il monitor rilegge il DB prima di scrivere.

    Senza questo, il giro di controlli sovrascriveva con una copia vecchia e
    le app aggiunte dall'utente sparivano.
    """
    store.add("aBcD1234", "WA")
    # simula l'utente che aggiunge un'app durante il giro di controlli
    store.add("nuovA999", "Appena aggiunta")
    store.record_check("aBcD1234", state="full", detail="This beta is full.")

    assert set(store.apps()) == {"aBcD1234", "nuovA999"}
