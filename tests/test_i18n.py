"""Locale negotiation, the language-switcher route, and localized rendering.

The negotiation function is pure and needs no backends. The HTTP checks drive
the landing page through the TestClient without a `with` block, so the lifespan
never touches real Mongo/Qdrant (same pattern as test_api.py).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, negotiate_locale, t
from app.main import app


# --- pure negotiation -------------------------------------------------------

def test_cookie_wins_over_accept_language():
    assert negotiate_locale("ru", "uk-UA,uk;q=0.9,en;q=0.5") == "ru"


def test_accept_language_used_when_no_cookie():
    assert negotiate_locale(None, "uk-UA,uk;q=0.9,en;q=0.5") == "uk"


def test_accept_language_respects_quality_order():
    # en has higher q than ru despite appearing later in the header.
    assert negotiate_locale(None, "ru;q=0.3, en;q=0.9") == "en"


def test_unsupported_everywhere_falls_back_to_english():
    assert negotiate_locale("xx", "fr-FR,fr;q=0.9,ja;q=0.5") == DEFAULT_LOCALE
    assert negotiate_locale(None, None) == DEFAULT_LOCALE


def test_translations_exist_for_every_shipped_locale():
    # A representative key must differ per locale (i.e. actually translated).
    titles = {loc: t("rs.meta.title", locale=loc) for loc in SUPPORTED_LOCALES}
    assert len({*titles.values()}) == len(SUPPORTED_LOCALES)


def test_semantic_clarification_in_every_locale():
    # The "recall by meaning" hook (the tiramisu example) answers "why not just
    # a folder?" and must be present and translated in every shipped locale.
    texts = {loc: t("rs.body.semantic", locale=loc) for loc in SUPPORTED_LOCALES}
    for text in texts.values():
        assert "tiramis" in text.lower() or "тирамису" in text or "тірамісу" in text
    assert len({*texts.values()}) == len(SUPPORTED_LOCALES)


def test_german_is_supported():
    assert "de" in SUPPORTED_LOCALES
    assert negotiate_locale(None, "de-DE,de;q=0.9,en;q=0.5") == "de"
    assert "Langzeitgedächtnis" in t("rs.body.lead", locale="de")


def test_polish_spanish_italian_are_supported():
    # (locale, Accept-Language header, a word from that locale's lead copy)
    cases = [
        ("pl", "pl-PL,pl;q=0.9,en;q=0.5", "Pamięć"),
        ("es", "es-ES,es;q=0.9,en;q=0.5", "configuración"),
        ("it", "it-IT,it;q=0.9,en;q=0.5", "configurazione"),
    ]
    for locale, header, word in cases:
        assert locale in SUPPORTED_LOCALES
        assert negotiate_locale(None, header) == locale
        assert word in t("rs.body.lead", locale=locale)


# --- HTTP: switcher route + localized rendering -----------------------------

def test_set_language_sets_cookie_and_redirects():
    client = TestClient(app)
    resp = client.get("/lang/uk", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert resp.cookies.get("lang") == "uk"


def test_set_language_rejects_unknown_code():
    client = TestClient(app)
    resp = client.get("/lang/zz", follow_redirects=False)
    assert resp.cookies.get("lang") == DEFAULT_LOCALE


def test_cookie_localizes_the_page():
    client = TestClient(app)
    client.cookies.set("lang", "ru")
    html = client.get("/").text
    assert '<html lang="ru">' in html
    assert "долговременная память" in html  # ru meta title copy


def test_accept_language_localizes_without_cookie():
    client = TestClient(app)
    html = client.get("/", headers={"Accept-Language": "uk"}).text
    assert '<html lang="uk">' in html


def test_default_is_english():
    client = TestClient(app)
    html = client.get("/").text
    assert '<html lang="en">' in html
