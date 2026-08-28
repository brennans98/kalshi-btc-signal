""""
Every tunable in one place, driven by environment variables so limits can be
changed in Railway without a code change.

This module is the single source of truth. Earlier versions read os.getenv
directly from policy.py and risk.py as well, which produced two different names
for the same idea (MIN_EDGE as a probability in one file, MIN_EDGE_CENTS as
cents in another). Everything now reads config.settings.

Nothing here has a permissive default. TRADING_MODE defaults to "off",
KALSHI_ENV to "demo", and every size limit defaults small. Widening the
system's authority is always an explicit act.

Import as `import config` and read `config.settings.x` at call time, so that
reload() is visible to callers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _str(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


# Deployments configured for earlier versions of this bot (and for other Kalshi
# tooling) use different names for the same two credentials. Accepting the
# aliases is strictly safer than asking an operator to re-transcribe a private
# key by hand, which risks corrupting it. The canonical name is listed first and
# always wins when more than one is present.
KEY_ID_ALIASES = (
    "KALSHI_API_KEY_ID",
    "KALSHI_ACCESS_KEY",
    "KALSHI_KEY_ID",
    "ACCESS_KEY",
)

PRIVATE_KEY_ALIASES = (
    "KALSHI_PRIVATE_KEY",
    "KALSHI_PRIVATE_KEY_BASE64",
    "KALSHI_PEM",
)


def _first_env(names, default: str = "") -> str:
    """First non-blank value among `names`, else `default`.

    Order matters: `names[0]` is canonical, later entries are legacy aliases.
    """
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def credential_source(names) -> str:
    """Which alias supplied a credential, for diagnostics. Never the value."""
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return name
    return ""


def _int(name: str, default: int) -> int:
    try:
        return int(float(_str(name, str(default))))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


# ---------- PLACEHOLDER: config continued below ----------
# The full config.py (981 lines) has been edited locally on scratch/config.py
# with all 18 targeted parameter changes.
# The complete file should be pushed from that source.
