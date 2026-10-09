"""The API's JSON body parser: DRF's, plus three refusals every /api/v1 view relies on.

* The body must be a JSON OBJECT. Every view reads ``request.data.get(...)``; a top-level list or
  string used to raise AttributeError -> 500.
* Nesting deeper than ``MAX_DEPTH`` is refused. A few KB of ``[[[[...`` blew Python's recursion
  limit inside ``json.loads`` (RecursionError -> 500), and a deep ``props`` dict did the same later
  in ``views._safe_props``.
* NUL characters are removed from every key and string. Postgres text and jsonb cannot store them,
  so one ``\\u0000`` in a chat message, feedback or event prop was a 500 at the INSERT.

Size stays capped by Django's DATA_UPLOAD_MAX_MEMORY_SIZE, which DRF enforces before parsing (400).
"""
from __future__ import annotations

from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser

MAX_DEPTH = 32


def _clean(value, depth: int = 0):
    if depth > MAX_DEPTH:
        raise ParseError("JSON nested too deeply")
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {(k.replace("\x00", "") if isinstance(k, str) else k): _clean(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v, depth + 1) for v in value]
    return value


class SafeJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        try:
            data = super().parse(stream, media_type, parser_context)
        except RecursionError:
            raise ParseError("JSON nested too deeply") from None
        if not isinstance(data, dict):
            raise ParseError("JSON body must be an object")
        return _clean(data)
