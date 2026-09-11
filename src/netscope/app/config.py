"""
netscope.app.config

Centralized configuration (TASK-035), per
docs/architecture/architecture-decisions.md's "Configuration strategy"
decision: a single configuration object, loaded once in app's
composition root and passed down explicitly (never a global/singleton),
backed by TOML, with built-in defaults so NetScope runs with zero
required configuration.

WHY THIS LIVES IN app, NOT core
------------------------------------
Loading a TOML file is file I/O -- an infrastructure concern `core`
must stay free of (the same rule that keeps `sqlite3` out of `core`,
architecture-overview.md SS3's dependency table). The `NetScopeConfig`
*data* is a plain, otherwise-inert dataclass that could live in `core`,
but splitting a small, single-purpose config module across two packages
(a bare dataclass in `core`, a loader in `app`) would be over-
engineering for what future-roadmap.md's TASK-035 row itself offers as
one location ("`core/config.py`/`app/config.py` (new)"); this keeps
both together in `app`, next to the composition root that actually
uses it.

PYTHON VERSION NOTE
------------------------
architecture-decisions.md's "Configuration strategy" entry flags a
then-unresolved tension: TOML parsing via the stdlib `tomllib` needs
Python 3.11+, but `pyproject.toml` declared `>=3.10` (chosen before any
task needed TOML), and explicitly says resolving this is "a decision to
make at implementation time" -- i.e. now, by this task. This task
raises the floor to `>=3.11` (see `pyproject.toml`) rather than adding
the `tomli` backport as a new dependency: the project's own primary
development environment is Windows/Python 3.13 (repository handover
notes), so the floor change costs nothing in practice, and it satisfies
"no new dependency unless explicitly required" more directly than
adding one would.

WHAT THIS MODULE DOES NOT DO
---------------------------------
Does not centralize *every* hardcoded value in the codebase -- only the
ones architecture-decisions.md's Configuration strategy names
explicitly ("target hosts, timeouts, sample counts... scattered ...
PUBLIC_DNS, PUBLIC_CDN_HTTP [in ui/cli.py]"). This task's own scope is
those named CLI-level target constants; probe-internal defaults
(timeouts, ping counts inside adapters/probes/*) are unchanged --
centralizing those is a larger, separately-scoped concern this task
does not expand into.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATH = Path.home() / ".netscope" / "config.toml"

# The only NetScopeConfig fields load_config() will accept from a TOML
# file -- an unrecognized key is ignored (see load_config's docstring)
# rather than silently becoming an unintended dataclass field.
_KNOWN_FIELDS = frozenset({"public_dns_target", "dns_lookup_domain", "public_cdn_url", "gateway"})


@dataclass
class NetScopeConfig:
    """NetScope's single configuration object. Every field has a
    built-in default -- NetScope runs with zero required configuration
    (architecture-decisions.md's explicit requirement). Passed down
    explicitly to whichever `app` use case needs it; never read from a
    global/singleton (same decision document).

    Fields map 1:1 onto the constants architecture-decisions.md's audit
    names as scattered: `public_dns_target`/`public_cdn_url` replace
    `ui/cli.py`'s old module-level `PUBLIC_DNS`/`PUBLIC_CDN_HTTP`;
    `dns_lookup_domain` replaces the domain name that was inline string
    literal in the same file; `gateway` gives the `--gateway` CLI flag
    a configurable default so it doesn't have to be passed on every
    invocation (the flag, when given, still overrides this).
    """

    public_dns_target: str = "1.1.1.1"
    dns_lookup_domain: str = "example.com"
    public_cdn_url: str = "https://www.cloudflare.com/"
    gateway: Optional[str] = None


def load_config(path: Optional[Path] = None) -> NetScopeConfig:
    """Load NetScopeConfig from a TOML file at `path` (default
    `~/.netscope/config.toml`), or return built-in defaults unchanged
    if the file doesn't exist -- a missing config file is not an error,
    per NetScope's "zero required configuration" requirement.

    Keys in the TOML file that aren't a known NetScopeConfig field are
    ignored rather than raising -- this keeps the loader intentionally
    simple (no validation framework) for what is currently a four-field
    config object; a typo'd or future/unknown key doesn't crash
    NetScope.
    """
    path = path if path is not None else DEFAULT_CONFIG_PATH
    if not path.exists():
        return NetScopeConfig()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    kwargs = {key: value for key, value in data.items() if key in _KNOWN_FIELDS}
    return NetScopeConfig(**kwargs)
