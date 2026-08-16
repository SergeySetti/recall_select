"""Documentation / integration pages (``app.services.docs`` + the /docs routes).

The service is pure data + formatting (no DB), so most of this needs no backend.
The route tests reuse the mongomock TestClient from ``test_api`` only to prove the
pages render with a cold/absent DB dependency (they must not touch Mongo).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import connect
from app.main import app
from app.services import docs


@pytest.fixture
def api():
    """A TestClient with no lifespan/DB - the /docs routes are static content."""
    return TestClient(app)


# --- Shared config helper --------------------------------------------------

def test_mcp_config_path_form():
    cfg = docs.mcp_config("https://recall.select/m/abc")
    assert cfg == {
        "mcpServers": {
            "recall-select": {"type": "http", "url": "https://recall.select/m/abc"}
        }
    }
    # Round-trips as valid JSON.
    assert json.loads(docs.mcp_config_json("https://recall.select/m/abc")) == cfg


def test_mcp_config_header_form_carries_bearer_key():
    cfg = docs.mcp_config("https://recall.select/mcp", header_key="secret")
    entry = cfg["mcpServers"]["recall-select"]
    assert entry["url"] == "https://recall.select/mcp"
    assert entry["headers"] == {"Authorization": "Bearer secret"}


def test_connect_md_uses_the_shared_helper():
    """The per-key .md and the public docs must not drift: the exact JSON the
    docs page shows (with a real key) is what the instructions embed."""
    key = "rs_testkey"
    md = connect._instructions_md("https://recall.select", key)
    assert docs.mcp_config_json(f"https://recall.select/m/{key}") in md
    assert (
        docs.mcp_config_json("https://recall.select/mcp", header_key=key) in md
    )


# --- Registry --------------------------------------------------------------

def test_registry_has_claude_code_with_derived_config():
    intg = docs.get_integration("claude-code")
    assert intg is not None
    assert intg.config_filename == ".mcp.json"
    # config_json is derived from the shared helper + placeholder link.
    assert docs.PLACEHOLDER_KEY in intg.config_json
    assert "mcpServers" in intg.config_json


def test_unknown_slug_is_none():
    assert docs.get_integration("does-not-exist") is None


# --- Routes ----------------------------------------------------------------

def test_integration_page_renders(api):
    resp = api.get("/docs/integrations/claude-code")
    assert resp.status_code == 200
    body = resp.text
    assert "Claude Code" in body
    assert ".mcp.json" in body
    assert docs.PLACEHOLDER_KEY in body  # placeholder link, never a real key


def test_integrations_index_lists_every_integration(api):
    resp = api.get("/docs/integrations")
    assert resp.status_code == 200
    for intg in docs.list_integrations():
        assert intg.name in resp.text


def test_unknown_integration_is_404_not_500(api):
    assert api.get("/docs/integrations/nope").status_code == 404


def test_docs_root_redirects_to_integrations(api):
    resp = api.get("/docs", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/docs/integrations"


# --- Codex (TOML, not JSON) ------------------------------------------------
# Codex reads ~/.codex/config.toml. Handing it the JSON every other client uses
# is what left it unable to connect, so both surfaces must emit TOML for it.


def test_mcp_config_toml_matches_the_json_entry():
    url = "https://recall.select/m/abc"

    toml = docs.mcp_config_toml(url)

    assert toml == '[mcp_servers.recall-select]\nurl = "https://recall.select/m/abc"'
    # Same server name and URL as the JSON form - one source of truth.
    assert docs.SERVER_NAME in toml
    assert docs.mcp_config(url)["mcpServers"][docs.SERVER_NAME]["url"] in toml


def test_codex_guide_is_registered_and_renders_toml(api):
    codex = docs.get_integration("codex")

    assert codex is not None
    assert codex.config_format == "toml"
    assert codex.config_json.startswith("[mcp_servers.")
    assert "mcpServers" not in codex.config_json  # never the JSON shape

    page = api.get("/docs/integrations/codex")
    assert page.status_code == 200
    assert "config.toml" in page.text
    assert "[mcp_servers.recall-select]" in page.text


def test_codex_appears_in_the_guide_index(api):
    index = api.get("/docs/integrations")

    assert index.status_code == 200
    assert "Codex" in index.text
    assert "/docs/integrations/codex" in index.text


def test_agent_instructions_cover_codex(mongo_db=None):
    """The .md an agent fetches must name Codex's file and TOML syntax."""
    md = connect._instructions_md("https://recall.select", "KEY123")

    assert "~/.codex/config.toml" in md
    assert "TOML, not JSON" in md
    assert '[mcp_servers.recall-select]\nurl = "https://recall.select/m/KEY123"' in md
    # The JSON form is still there for the clients that want it.
    assert '"mcpServers"' in md
    assert "https://recall.select/docs/integrations" in md
