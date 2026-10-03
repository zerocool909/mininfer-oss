"""Importing an adapter module is what registers it.

The `@adapter(kind)` decorator *is* the registration, so this list and
`catalogue.py` are the only two places a provider appears — and
`tests/test_ingest.py` asserts that every catalogue `kind` has an adapter, which
is what keeps the two agreeing.

The `ingest_*` names are re-exported because they are the module's public API: one
adapter per provider, each a pure function of a `Snapshot`.
"""
from __future__ import annotations

from . import chutes  # noqa: F401 - registering the adapter
from . import cloudflare  # noqa: F401 - registering the adapter
from . import cohere  # noqa: F401 - registering the adapter
from . import deepinfra  # noqa: F401 - registering the adapter
from . import github  # noqa: F401 - registering the adapter
from . import google  # noqa: F401 - registering the adapter
from . import hf_router  # noqa: F401 - registering the adapter
from . import novita  # noqa: F401 - registering the adapter
from . import nvidia  # noqa: F401 - registering the adapter
from . import openai_compat  # noqa: F401 - registering the adapter
from . import openrouter  # noqa: F401 - registering the adapter
from . import sambanova  # noqa: F401 - registering the adapter
from . import vercel  # noqa: F401 - registering the adapter

from .chutes import ingest_chutes
from .cloudflare import ingest_cloudflare
from .cohere import ingest_cohere
from .deepinfra import ingest_deepinfra
from .github import ingest_github
from .google import ingest_google
from .hf_router import ingest_hf_router
from .novita import ingest_novita
from .nvidia import ingest_nvidia
from .openai_compat import ingest_openai_compat
from .openrouter import ingest_openrouter
from .sambanova import ingest_sambanova
from .vercel import ingest_vercel

__all__ = [
    "ingest_chutes",
    "ingest_cloudflare",
    "ingest_cohere",
    "ingest_deepinfra",
    "ingest_github",
    "ingest_google",
    "ingest_hf_router",
    "ingest_novita",
    "ingest_nvidia",
    "ingest_openai_compat",
    "ingest_openrouter",
    "ingest_sambanova",
    "ingest_vercel",
]
