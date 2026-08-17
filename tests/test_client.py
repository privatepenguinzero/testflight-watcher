"""Test della costruzione degli URL. Nessuna rete."""

from testflight_watcher.client import check_url, join_url


def test_url_di_notifica_e_pulito():
    """È il link che apre l'utente: niente parametri interni."""
    assert join_url("aBcD1234") == "https://testflight.apple.com/join/aBcD1234"
    assert "?" not in join_url("aBcD1234")


def test_url_di_controllo_ha_il_cache_buster():
    """Senza, Apple serve una copia vecchia fino a 10 minuti."""
    url = check_url("aBcD1234")
    assert url.startswith("https://testflight.apple.com/join/aBcD1234?")


def test_il_cache_buster_cambia_a_ogni_chiamata():
    assert check_url("aBcD1234") != check_url("aBcD1234")
