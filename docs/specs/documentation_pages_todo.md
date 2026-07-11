# TODO - Documentation / Integrations pages

Build a small public docs section for recall.select, modeled on
`https://memclaw.net/docs/integrations/claude-code/`: a left sidebar, one page
per integration, each showing how to wire recall.select in as an MCP server.

This is the **public, generic** twin of what `app/api/connect.py` already renders
**per key** (`/m/{key}.md`). The docs pages teach the setup with a placeholder
link; the per-key `.md` is the personalized, ready-to-paste version. Keep the two
in sync - a change to the connection story belongs in both.

## Goal & scope (v1)

- A docs landing page + one page per integration, server-rendered with the
  existing Jinja + Bootstrap 5 dark theme (match `templates/account.html` /
  `plans.html`: same `--rs-bg / --rs-text / --rs-accent`, `.btn-rs`, cards).
- Static content only - no auth, no DB. These pages must render even when Mongo
  is cold (like the landing page's cold-tolerant path).
- Public and crawlable (SEO matters for onboarding): real `<title>`,
  meta description, Open Graph, and a sitemap entry per page.

Non-goals for v1: search (`⌘K`), versioned docs, API reference autogen,
per-agent-key UI. Leave hooks for them but do not build them.

## Reference structure (memclaw, for parity)

- Left sidebar sections: Getting Started, Concepts, **Integrations**, For Agents,
  API Reference.
- Integrations listed there: Claude Code, Claude Desktop, Cursor, Generic MCP,
  REST, LangChain, LlamaIndex, CrewAI, AutoGen, OpenAI Agents SDK, Per-agent keys.
- Each integration page: title + one-line description, then **Install**,
  **Using it**, **Tips**; a JSON config snippet with a copy button; callout
  boxes; breadcrumbs; links to related pages.
- URL pattern: `/docs/<section>/<subsection>/`.

## Proposed URL structure (recall.select)

- `/docs` - docs landing / overview (what recall.select is, the one-link idea).
- `/docs/quickstart` - generate a link, paste it, store & recall once.
- `/docs/integrations` - index of all integration pages.
- `/docs/integrations/<tool>` - one per tool.
- `/docs/concepts/memory-link` - what the link is, why the whole URL is the secret.

Decide: trailing slash or not. Pick one and 301 the other (be consistent with the
existing routes, which are slash-less: `/plans`, `/account`).

### Integrations to ship first (in priority order)

1. `claude-code` - Anthropic Claude Code CLI (`.mcp.json` at repo root).
2. `claude-desktop` - config file MCP entry.
3. `cursor` - Cursor MCP settings.
4. `generic-mcp` - any Streamable HTTP MCP client (mirror `connect.py`).
5. `chatgpt` / `openai` - if/when a supported path exists.

Backlog (stub pages or omit): LangChain, LlamaIndex, CrewAI, AutoGen, REST.

## Architecture decision (resolve before coding)

Where does page content live? Options:

- **A. Markdown files** under `docs/content/` (or `app/docs_content/`), rendered
  at request time with a Markdown lib + front-matter for title/description/order.
  Pro: content edits stay out of code, easy to add a page. Con: adds a Markdown
  dependency + sanitization concern.
- **B. Jinja templates** per page under `templates/docs/`. Pro: no new deps, full
  control of the copy-button/callout markup, consistent with current templates.
  Con: content and layout mixed; more boilerplate per page.
- **C. Python data + one template** - a registry (list of dicts: slug, title,
  description, sections, config JSON) driving a single `docs_page.html`, like
  `billing.plans_table()` drives `plans.html`. Pro: one template, add an
  integration by adding a dict; testable; no new deps. Con: long-form prose in
  Python is awkward.

**Recommendation: C** for the integration pages (they are structurally
identical - title, blurb, JSON snippet, steps, tips), with the shared JSON config
generated from one helper so every page stays consistent with `connect.py`.
Use B/A only if prose pages (concepts) grow long.

## Status

**Done (first slice):** shared MCP config helper (`app/services/docs.py`,
`mcp_config`/`mcp_config_json`) now the single source for both the docs pages and
`connect.py`'s per-key `.md`; integrations registry (`INTEGRATIONS`) with
`claude-code` + `generic-mcp`; routes `/docs` (redirect), `/docs/integrations`
(index), `/docs/integrations/{slug}` (404 on unknown, no DB); templates
`docs/_base.html` (sidebar + breadcrumb + copy button + callout), `integration.html`,
`integrations_index.html`; `docs:` i18n namespace (English source); FastAPI's
built-in API docs relocated to `/api/docs`; tests in `tests/test_docs.py` incl. the
docs↔`connect.py` drift guard; README updated.

**Still open:** `/docs` landing + non-integration sections (Concepts, Getting
Started); more integrations (Claude Desktop, Cursor - only ship once verified);
right-hand TOC; mobile offcanvas sidebar; `sitemap.xml` + `robots.txt`; per-guide
prose translation; `⌘K` search.

## Implementation checklist

### Content / data layer
- [ ] Create an integrations registry (slug, name, tagline, `config_snippet`,
      `steps`, `tips`, `related`). One source of truth, like `billing.PLANS`.
- [ ] Factor the MCP config JSON out of `app/api/connect.py` into a shared helper
      (e.g. `app/services/docs.py` or reuse a connect helper) so the docs snippet
      and the per-key `.md` cannot drift. Use a **placeholder link**
      (`https://recall.select/m/YOUR-LINK`) in docs, real key in `connect.py`.
- [ ] Per-tool config specifics: Claude Code = `.mcp.json` at repo root; Claude
      Desktop / Cursor = their config paths + restart step; Generic = raw
      Streamable HTTP URL + the header-auth alternative (already in `connect.py`).

### Routes (in `app/main.py`, alongside `/plans`, `/account`)
- [ ] `GET /docs` -> `docs_index.html`.
- [ ] `GET /docs/integrations` -> integrations index.
- [ ] `GET /docs/integrations/{slug}` -> `docs_integration.html`; 404 on unknown
      slug (do not 500). Cold-Mongo tolerant (no DB access needed).
- [ ] Keep route registration order safe re: the `/m/{key}` and `/m/{key}.md`
      Starlette routes - docs routes are unrelated but re-check precedence.

### Templates (`app/templates/docs/`)
- [ ] `_docs_base.html` - shared shell: header (logo, links back to home /
      pricing / account), left **sidebar** nav (sections + integration list,
      active item highlighted), main content column, optional right-hand TOC.
      Reuse the dark-theme CSS vars and `.btn-rs`; extract shared CSS to a
      `static/css/rs.css` if duplication across templates gets old (currently
      inlined per page).
- [ ] `docs_index.html`, `docs_integration.html`, `docs_integrations_index.html`.
- [ ] **Breadcrumbs** (Docs / Integrations / Claude Code).
- [ ] **Copy button** on every code block - lift the `copyMemoryLink()` pattern
      from `index.html` into a small reusable snippet; add the analytics
      `data-*` attribute like the existing copy button (see recent commit
      "Add data attribute for analytics tracking on copy button").
- [ ] **Callout** component (info / warning box) for the "treat the whole URL as
      a secret" note.
- [ ] Responsive: sidebar collapses to an offcanvas/toggler on mobile (Bootstrap
      5 offcanvas). Target audience is non-technical - keep copy plain, no jargon.

### i18n
- [ ] Add a `docs:` namespace to `app/translations/rs.en.yml` (English is the
      source; other locales fall back, matching how `plans`/`account` are handled).
- [ ] Structural UI strings (nav labels, breadcrumbs, "Copy", section headings)
      via `t(...)`. Long per-integration prose can stay English-only in the
      registry for v1; note it for later translation.

### SEO / meta
- [ ] Per-page `<title>`, meta description, canonical URL, Open Graph tags
      (reuse the pattern in `index.html`).
- [ ] Add a `/sitemap.xml` (or extend one if it exists) with the docs URLs, and
      ensure `robots.txt` allows `/docs`.
- [ ] Internal links: landing page + `/account` "Upgrade" area should link to
      `/docs`; footer gets a "Docs" link.

### Tests (`tests/`, mongomock TestClient like `test_api.py`)
- [ ] `GET /docs` and `/docs/integrations` return 200 and list every registered
      integration.
- [ ] `GET /docs/integrations/claude-code` returns 200 and contains the MCP URL
      placeholder + the `.mcp.json` snippet.
- [ ] Unknown slug -> 404 (not 500).
- [ ] Registry <-> `connect.py` consistency: a test asserting the shared config
      helper produces the same JSON shape both pages use (guard against drift).
- [ ] Renders with a cold/absent DB (no Mongo dependency on these routes).

## Docs to keep in sync when this lands
- `README.md` (architecture + route list) - add the `/docs/*` routes.
- `docs/specs/initial_specification.md` if the docs section becomes part of the
  product surface.
- `app/api/connect.py` - if the connection instructions change, update the
  shared helper so docs pages follow.

## Open questions
- [ ] Trailing slash convention for `/docs/...`?
- [ ] Content in Python registry (C) vs Markdown files (A) - confirm before build.
- [ ] Do we need the `⌘K` search in v1, or defer? (Deferring.)
- [ ] Which integrations can we honestly document as *working today* vs. "coming
      soon" stubs? Only ship pages we can verify end to end.
