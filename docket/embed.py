# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Embedding client — talks to a self-hosted Ollama HTTP API.

Kept deliberately thin. Everything is best-effort: if embeddings are disabled or
Ollama is unreachable, callers fall back to keyword-only search. The heavy model
runtime lives in Ollama (separate service), not in this image.
"""
import logging

import httpx

from . import config

log = logging.getLogger(__name__)


def enabled() -> bool:
    return config.EMBED_ENABLED


def _url(path: str) -> str:
    return config.EMBED_OLLAMA_URL.rstrip("/") + path


def embed(texts):
    """Embed a list of texts via Ollama /api/embed. Returns a list of float vectors.
    Raises on any failure (caller decides whether to fall back)."""
    if isinstance(texts, str):
        texts = [texts]
    with httpx.Client(timeout=config.EMBED_TIMEOUT_SECS) as c:
        r = c.post(_url("/api/embed"), json={"model": config.EMBED_MODEL, "input": texts})
        r.raise_for_status()
        data = r.json()
    vectors = data.get("embeddings")
    if not vectors or len(vectors) != len(texts):
        raise ValueError(f"unexpected embed response: {len(vectors or [])} vectors for {len(texts)} inputs")
    return vectors


def embed_query(text: str):
    """Embed a single query string → one vector."""
    return embed([text])[0]
