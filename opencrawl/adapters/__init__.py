"""Adapters for structured systems (APIs behind a portal), registered by name.

An adapter is a module with
  NAME                                      the portal name: a source may carry it as a key (`legistar: client`),
                                            and `targets.PORTAL_SOURCES[NAME]` builds a source from a portal link
  detect(src, home_html, hints) -> str|None the client / endpoint id when `src` is on that platform
  probe(fetcher, src, client) -> int        how many items the API answers with (0 = not working); may raise
  crawl(fetcher, src, client, since, modified_since, ctx, limits) -> Iterator[Record | Checkpoint]
  shard_key(src) -> str | None              the host all of its traffic goes to (so --shard keeps it on one runner)

Adapters are tried only when `detect` says the source is on that platform, so probing a source costs a few
requests, not one per adapter. Add your own with `register(module)` (from a project's own code or a plugin).

The built-in adapters cover US local-government meeting systems (Legistar, CivicClerk, BoardDocs); YouTube
channels are handled by opencrawl.transcripts through the `youtube:` endpoint.
"""
from __future__ import annotations

from types import ModuleType

ADAPTERS: dict[str, ModuleType] = {}


def register(mod: ModuleType) -> ModuleType:
    ADAPTERS[mod.NAME] = mod
    return mod


def _load_builtin():
    from . import boarddocs, civicclerk, legistar  # noqa: F401 — each registers itself on import


_load_builtin()
