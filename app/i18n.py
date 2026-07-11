"""Translation setup (python-i18n, https://github.com/danhper/python-i18n).

Strings live in ``app/translations/{namespace}.{locale}.yml`` (namespace ``rs``)
and are looked up with :func:`t`, e.g. ``t("rs.meta.description")``. The same
helper is exposed to Jinja templates (see ``app/main.py``) so a single string is
authored once and reused across the page body and every social/meta tag.

Per request the active locale is chosen by :func:`negotiate_locale` (cookie →
``Accept-Language`` → English) and bound into a :func:`translator` passed to the
template, so the whole page - copy, meta tags and JS strings - renders in it.
"""
from pathlib import Path
from typing import Callable

import i18n

TRANSLATIONS_DIR = Path(__file__).resolve().parent / "translations"

# Locales we ship a `rs.<locale>.yml` for. English is the default and fallback.
# Order is the footer switcher's display order.
SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ru", "uk", "de", "pl", "es", "it")
DEFAULT_LOCALE = "en"
# Cookie the language switcher sets to remember an explicit choice.
LOCALE_COOKIE = "lang"

# File layout: `rs.en.yml`, keys nested under the locale root.
i18n.set("file_format", "yml")
i18n.set("filename_format", "{namespace}.{locale}.{format}")
i18n.set("locale", DEFAULT_LOCALE)
i18n.set("fallback", DEFAULT_LOCALE)
i18n.load_path.append(str(TRANSLATIONS_DIR))

# Re-export so callers/templates use `t(...)` without importing i18n directly.
t = i18n.t

__all__ = [
    "t",
    "translator",
    "negotiate_locale",
    "SUPPORTED_LOCALES",
    "DEFAULT_LOCALE",
    "LOCALE_COOKIE",
]


def translator(locale: str) -> Callable[..., str]:
    """Return a ``t``-like callable bound to *locale* (for a single request)."""

    def _t(key: str, **kwargs: object) -> str:
        return i18n.t(key, locale=locale, **kwargs)

    return _t


def _parse_accept_language(header: str | None) -> list[str]:
    """Primary language subtags from an ``Accept-Language`` header, best first."""
    if not header:
        return []
    weighted: list[tuple[float, str]] = []
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        tag, _, params = part.partition(";")
        weight = 1.0
        if params.startswith("q="):
            try:
                weight = float(params[2:])
            except ValueError:
                weight = 0.0
        primary = tag.strip().split("-")[0].lower()
        if primary:
            weighted.append((weight, primary))
    # Stable sort by descending quality; preserves header order within a tier.
    weighted.sort(key=lambda item: item[0], reverse=True)
    return [primary for _, primary in weighted]


def negotiate_locale(
    cookie_value: str | None, accept_language: str | None
) -> str:
    """Resolve the active locale: explicit cookie, then browser preference, then
    English. Only returns a locale we actually ship."""
    if cookie_value in SUPPORTED_LOCALES:
        return cookie_value  # type: ignore[return-value]
    for primary in _parse_accept_language(accept_language):
        if primary in SUPPORTED_LOCALES:
            return primary
    return DEFAULT_LOCALE
