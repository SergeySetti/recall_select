"""Documentation content (services layer): the public integration guides.

The integration pages under ``/docs`` teach the very same MCP setup that
``app.api.connect`` hands out **per key** (``/m/{key}.md``) - only here the link
is a **placeholder** instead of a real credential. So the two surfaces cannot
drift, the MCP client config is built in exactly one place - :func:`mcp_config` /
:func:`mcp_config_json` - and consumed by both the docs pages and ``connect``.

Pure data + formatting: no I/O, no DB. The pages render with a cold backend.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

# The MCP server key used across every client config we emit.
SERVER_NAME = "recall-select"

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Stand-in shown wherever a real memory key would go in public docs.
PLACEHOLDER_KEY = "YOUR-MEMORY-KEY"


def mcp_config(url: str, *, header_key: str | None = None) -> dict:
    """The MCP client config object for our server at ``url``.

    With ``header_key`` set, emits the key-less endpoint form that carries the
    credential in an ``Authorization: Bearer`` header instead of the URL.
    """
    entry: dict = {"type": "http", "url": url}
    if header_key is not None:
        entry["headers"] = {"Authorization": f"Bearer {header_key}"}
    return {"mcpServers": {SERVER_NAME: entry}}


def mcp_config_json(url: str, *, header_key: str | None = None) -> str:
    """:func:`mcp_config` rendered as the indented JSON shown in a code block."""
    return json.dumps(mcp_config(url, header_key=header_key), indent=2)


def mcp_config_toml(url: str) -> str:
    """The same server entry as TOML, for clients configured that way (Codex).

    Derived from the same ``SERVER_NAME`` and URL as :func:`mcp_config_json` so
    the two formats cannot drift apart.
    """
    return f'[mcp_servers.{SERVER_NAME}]\nurl = "{url}"'


def mcp_config_yaml(url: str, *, header_key: str | None = None) -> str:
    """The same server entry as YAML, for clients configured that way (Hermes).

    Hermes keeps its servers in ``~/.hermes/config.yaml`` under a snake_case
    ``mcp_servers`` map (like Codex's TOML) and infers the HTTP transport from
    the presence of ``url``, so there is no ``type`` field. Derived from
    :func:`mcp_config` so the server name, URL and header cannot drift from the
    JSON form.
    """
    entry = mcp_config(url, header_key=header_key)["mcpServers"][SERVER_NAME]
    lines = ["mcp_servers:", f"  {SERVER_NAME}:", f'    url: "{entry["url"]}"']
    if entry.get("headers"):
        lines.append("    headers:")
        lines += [f'      {name}: "{value}"' for name, value in entry["headers"].items()]
    return "\n".join(lines)


def hermes_env_var(server_name: str = SERVER_NAME) -> str:
    """The env-var name Hermes itself picks when it stores a bearer token.

    ``hermes mcp add ... --auth header`` writes the secret to the active
    profile's ``.env`` under this name and leaves only a ``${...}`` reference in
    ``config.yaml``. Mirrors Hermes's own ``_env_key_for_server``.
    """
    return f"MCP_{re.sub(r'[^A-Za-z0-9_]', '_', server_name.upper()).strip('_')}_API_KEY"


def placeholder_mcp_url(key: str = PLACEHOLDER_KEY) -> str:
    """The MCP server URL with a placeholder key, for public docs.

    Note it has no ``.md`` suffix: the ``.md`` link returns setup instructions;
    dropping it yields the MCP Streamable HTTP endpoint the client connects to.
    """
    return f"{PUBLIC_BASE_URL}/m/{key}"


@dataclass(frozen=True)
class Integration:
    """One integration guide: everything the page template renders.

    Prose (``tagline`` / ``steps`` / ``usage`` / ``tips``) is English-only for
    now - the page chrome is translated, the long-form copy is tracked for later.
    The config JSON is *derived* (never stored) so it always matches
    :func:`mcp_config_json`.
    """

    slug: str
    name: str
    tagline: str
    config_filename: str
    config_note: str
    steps: tuple[str, ...]
    usage: str
    tips: tuple[str, ...] = ()
    related: tuple[tuple[str, str], ...] = ()  # (href, label)
    # Also show the "key in a header, not the URL" alternative on this page.
    show_header_alt: bool = False
    # Config syntax this client expects: most take JSON, Codex TOML, Hermes YAML.
    config_format: str = "json"

    def _render(self, url: str, *, header_key: str | None = None) -> str:
        """One config block in this client's own syntax.

        Every format comes from the same builders, so a client that wants TOML
        or YAML still gets the same server name, URL and header as everyone else.
        """
        if self.config_format == "toml":
            if header_key is not None:  # no verified TOML spelling for headers yet
                raise NotImplementedError("header auth has no TOML form")
            return mcp_config_toml(url)
        if self.config_format == "yaml":
            return mcp_config_yaml(url, header_key=header_key)
        return mcp_config_json(url, header_key=header_key)

    @property
    def config_json(self) -> str:
        """The primary client config, with the placeholder link."""
        return self._render(placeholder_mcp_url())

    @property
    def header_config_json(self) -> str:
        """The header-auth alternative config (key-less ``/mcp`` endpoint)."""
        return self._render(f"{PUBLIC_BASE_URL}/mcp", header_key=PLACEHOLDER_KEY)


# The catalogue. Insertion order is the sidebar order. Add an integration by
# adding an entry here - the routes and template need no change.
INTEGRATIONS: dict[str, Integration] = {
    "claude-code": Integration(
        slug="claude-code",
        name="Claude Code",
        tagline="Wire recall.select into Anthropic's Claude Code CLI.",
        config_filename=".mcp.json",
        config_note="Save this as .mcp.json in the root folder of your project.",
        steps=(
            "Create a file named .mcp.json at the root of your project.",
            "Paste the configuration below, replacing the placeholder with your "
            "own memory link.",
            "Restart Claude Code - the recall.select memory tools are picked up "
            "automatically.",
        ),
        usage=(
            "Once connected, Claude Code can save what matters as you work and "
            "recall it by meaning in later sessions, so you never have to explain "
            "the same thing twice."
        ),
        tips=(
            "Prefer the terminal? Running "
            "\"claude mcp add --transport http recall-select <your-link>\" does the "
            "same thing without editing a file.",
            "Your link is the whole password: anyone who has it can read and write "
            "your memory. Add .mcp.json to your .gitignore so it never lands in "
            "version control.",
        ),
        related=(
            ("/docs/integrations/codex", "Codex"),
            ("/docs/integrations/hermes", "Hermes"),
            ("/docs/integrations/generic-mcp", "Generic MCP client"),
            ("/plans", "Plans & limits"),
        ),
    ),
    "codex": Integration(
        slug="codex",
        name="Codex",
        tagline="Give OpenAI's Codex a memory it keeps between sessions.",
        config_filename="~/.codex/config.toml",
        config_note=(
            "Codex is configured in TOML, not JSON. Add this block to "
            "~/.codex/config.toml (or .codex/config.toml inside a project, to "
            "scope the memory to that project)."
        ),
        steps=(
            "Open ~/.codex/config.toml - create the file if it isn't there yet.",
            "Add the block below, replacing the placeholder with your own memory "
            "link. Keep the /m/ link exactly as it is: no .md on the end - that "
            "suffix returns these instructions instead of the memory server.",
            "Restart Codex. Ask it \"what MCP tools do you have?\" - it should "
            "list store_memory and recall_memory.",
        ),
        usage=(
            "Codex can now remember across sessions: it saves what you tell it to "
            "keep and finds it later by meaning, so a new session starts already "
            "knowing your preferences and decisions instead of asking again."
        ),
        tips=(
            "No API key or token is needed. Your memory link already carries the "
            "credential, which is why there is no bearer_token_env_var line here.",
            "If your Codex build has the MCP subcommand, "
            "\"codex mcp add recall-select --url <your-link>\" writes the same "
            "block for you.",
            "The link is the whole password. A project-scoped .codex/config.toml "
            "belongs in .gitignore.",
        ),
        related=(
            ("/docs/integrations/claude-code", "Claude Code"),
            ("/docs/integrations/hermes", "Hermes"),
            ("/docs/integrations/generic-mcp", "Generic MCP client"),
        ),
        config_format="toml",
    ),
    "hermes": Integration(
        slug="hermes",
        name="Hermes",
        tagline="Give Nous Research's Hermes agent a memory that outlives the chat.",
        config_filename="~/.hermes/config.yaml",
        config_note=(
            "Hermes is configured in YAML, not JSON. Add this block to "
            "~/.hermes/config.yaml - if the file already has an mcp_servers "
            "section, add the recall-select entry under it rather than a second "
            "mcp_servers line."
        ),
        steps=(
            "Open ~/.hermes/config.yaml - create the file if it isn't there yet.",
            "Add the block below, replacing the placeholder with your own memory "
            "link. Keep the /m/ link exactly as it is: no .md on the end - that "
            "suffix returns these instructions instead of the memory server.",
            "Start Hermes (\"hermes chat\") and ask it what tools it has - it "
            "should list mcp__recall_select__store_memory, "
            "mcp__recall_select__recall_memory and "
            "mcp__recall_select__delete_memory.",
        ),
        usage=(
            "Hermes can now keep what matters between sessions: it saves what you "
            "ask it to remember and finds it later by meaning, so tomorrow's chat "
            "starts already knowing what today's decided. Hermes prefixes the "
            "tools it borrows from a server, so recall.select's store_memory "
            "shows up as mcp__recall_select__store_memory - same tool, longer name "
            "(the hyphen in the server name becomes an underscore in the prefix)."
        ),
        tips=(
            "No API key or token is needed on top of the link - the memory link "
            "already carries the credential.",
            "Prefer the terminal? \"hermes mcp add recall-select --url "
            "<your-link>\" writes the same entry for you.",
            "To keep the secret out of config.yaml entirely, run \"hermes mcp add "
            "recall-select --url https://recall.select/mcp --auth header\" and "
            f"paste your link when prompted: Hermes stores it in the profile's "
            f".env as {hermes_env_var()} and writes only "
            f"\"Authorization: Bearer ${{{hermes_env_var()}}}\" into config.yaml. "
            "Any ${VARIABLE} in a header is expanded the same way if you'd rather "
            "write it by hand.",
            "config.yaml holds the whole password. Keep the file to yourself "
            "(chmod 600) and out of any repo you push.",
        ),
        related=(
            ("/docs/integrations/claude-code", "Claude Code"),
            ("/docs/integrations/generic-mcp", "Generic MCP client"),
            ("/plans", "Plans & limits"),
        ),
        show_header_alt=True,
        config_format="yaml",
    ),
    "generic-mcp": Integration(
        slug="generic-mcp",
        name="Generic MCP client",
        tagline="Connect any client that speaks the MCP Streamable HTTP transport.",
        config_filename="MCP client config",
        config_note="Add the server to your client's MCP configuration.",
        steps=(
            "Open your MCP client's configuration (usually a JSON file).",
            "Add the recall.select server entry below, using your own memory link.",
            "Reload the client so it connects to the new server.",
        ),
        usage=(
            "Any MCP client that supports the Streamable HTTP transport can use "
            "recall.select the same way: store memories now, recall them by "
            "meaning later. No extra API key or auth step - the link is the "
            "credential."
        ),
        tips=(
            "The link is a secret. Don't paste it where it might be logged or "
            "shared.",
        ),
        related=(
            ("/docs/integrations/claude-code", "Claude Code"),
            ("/docs/integrations/codex", "Codex"),
            ("/docs/integrations/hermes", "Hermes"),
            ("/plans", "Plans & limits"),
        ),
        show_header_alt=True,
    ),
}


def get_integration(slug: str) -> Integration | None:
    return INTEGRATIONS.get(slug)


def list_integrations() -> list[Integration]:
    """Sidebar/index order (registry insertion order)."""
    return list(INTEGRATIONS.values())
