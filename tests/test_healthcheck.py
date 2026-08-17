"""Test dell'healthcheck: il monitor fermo deve diventare visibile."""

from datetime import datetime, timedelta, timezone

import pytest

from testflight_watcher.healthcheck import check, staleness, tolerance_seconds
from testflight_watcher.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data.json"))


def test_un_monitor_appena_girato_e_sano(store):
    store.record_cycle()
    sano, motivo = check(store, interval=300)
    assert sano, motivo


def test_un_monitor_fermo_da_troppo_e_malato(store):
    store.record_cycle()
    fra_un_ora = datetime.now(timezone.utc) + timedelta(hours=1)

    sano, motivo = check(store, interval=300, now=fra_un_ora)
    assert not sano
    assert "monitor fermo" in motivo


def test_un_ritardo_breve_non_fa_rimbalzare_il_container(store):
    """Un ciclo lento o una rete incerta non sono un guasto."""
    store.record_cycle()
    poco_dopo = datetime.now(timezone.utc) + timedelta(seconds=400)

    sano, _ = check(store, interval=300, now=poco_dopo)
    assert sano, "300s di interval tollera fino a 900s"


def test_all_avvio_senza_cicli_non_si_dichiara_guasto(store):
    """Del primo giro si occupa start_period nel compose."""
    sano, motivo = check(store, interval=300)
    assert sano
    assert "avvio" in motivo


def test_la_tolleranza_ha_un_minimo():
    """Con intervalli molto brevi la soglia non deve diventare risibile."""
    assert tolerance_seconds(300) == 900
    assert tolerance_seconds(10) == 120


def test_una_data_illeggibile_non_fa_esplodere_l_healthcheck():
    assert staleness("non-una-data") is None
    assert staleness(None) is None


def test_il_battito_e_del_ciclo_non_delle_singole_app(store):
    """Un'app in backoff ha last_checked vecchio, ma il loop è sano.

    Se l'healthcheck guardasse le singole app, un beta bloccato da Apple
    farebbe riavviare il container per niente.
    """
    store.add("aBcD1234", "In backoff")
    store.record_cycle()

    assert store.apps()["aBcD1234"]["last_checked"] is None
    sano, _ = check(store, interval=300)
    assert sano
