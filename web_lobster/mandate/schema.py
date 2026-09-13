"""Mandate — the user-approved scope a web task must stay inside.

A mandate is policy, not prompt text. It is enforced by the browser layer
(see enforcer.py) on every real request the page makes, so a prompt-injected
executor cannot talk its way past it.

A mandate declares:
- origins:    where the browser may navigate and send writes (POST/PUT/...)
- data:       user values the agent may enter, and the origins each may reach.
              The executor refers to them as {{name}} placeholders, so the
              model never sees the raw value.
- expires_at: after this time nothing is allowed.

Origin patterns look like "https://www.united.com", "united.com" (https is
assumed), "http://127.0.0.1:8000", or "https://*.united.com". A leading "*."
matches any subdomain but not the apex domain itself — list both if needed.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

# Grant values shorter than this can't be tracked reliably in network traffic
# (a 3-character value matches half the query strings on the web).
MIN_GRANT_VALUE_LENGTH = 4

_DEFAULT_PORTS = {"http": 80, "https": 443}
_WS_SCHEMES = {"ws": "http", "wss": "https"}
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

Origin = tuple[str, str, int]  # (scheme, host, port)


def url_origin(url: str) -> Optional[Origin]:
    """Return the (scheme, host, port) origin of an http(s)/ws(s) URL, else None."""
    try:
        parts = urlsplit(url)
        scheme = _WS_SCHEMES.get(parts.scheme.lower(), parts.scheme.lower())
        if scheme not in _DEFAULT_PORTS or not parts.hostname:
            return None
        return scheme, parts.hostname.lower(), parts.port or _DEFAULT_PORTS[scheme]
    except ValueError:  # malformed port
        return None


def parse_origin_pattern(pattern: str) -> Origin:
    """Parse an origin pattern into (scheme, host, port). Raises ValueError."""
    raw = pattern.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"origin pattern must not include a path: {pattern!r}")
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError(f"origin pattern must be http or https: {pattern!r}")
    host = (parts.hostname or "").lower()
    bare = host[2:] if host.startswith("*.") else host
    if not bare or "*" in bare or "." not in bare and bare != "localhost":
        raise ValueError(f"invalid host in origin pattern: {pattern!r}")
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as e:
        raise ValueError(f"invalid port in origin pattern: {pattern!r}") from e
    return scheme, host, port


def origin_matches(origin: Origin, pattern: Origin) -> bool:
    scheme, host, port = origin
    p_scheme, p_host, p_port = pattern
    if scheme != p_scheme or port != p_port:
        return False
    if p_host.startswith("*."):
        return host.endswith(p_host[1:])  # ".united.com" keeps the label boundary
    return host == p_host


def _parse_patterns(patterns: list[str]) -> list[Origin]:
    return [parse_origin_pattern(p) for p in patterns]


class DataGrant(BaseModel):
    """A user value the agent may enter, and the origins allowed to receive it."""
    name: str
    # Excluded from dumps and repr so the raw value never lands in logs or the UI.
    value: str = Field(repr=False, exclude=True)
    origins: list[str] = Field(min_length=1)

    _patterns: list[Origin] = PrivateAttr(default_factory=list)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"grant name must be an identifier: {v!r}")
        return v

    @field_validator("value")
    @classmethod
    def _check_value(cls, v: str) -> str:
        if len(v) < MIN_GRANT_VALUE_LENGTH:
            raise ValueError(
                f"grant value must be at least {MIN_GRANT_VALUE_LENGTH} characters"
            )
        return v

    @field_validator("origins")
    @classmethod
    def _check_origins(cls, v: list[str]) -> list[str]:
        _parse_patterns(v)
        return v

    def model_post_init(self, __context) -> None:
        self._patterns = _parse_patterns(self.origins)

    def allows(self, url: str) -> bool:
        origin = url_origin(url)
        return origin is not None and any(origin_matches(origin, p) for p in self._patterns)


class Mandate(BaseModel):
    """The scope a single web task is allowed to act within."""
    task: str
    origins: list[str] = Field(min_length=1)
    data: list[DataGrant] = Field(default_factory=list)
    expires_at: Optional[float] = None  # unix seconds; None = no expiry

    _patterns: list[Origin] = PrivateAttr(default_factory=list)

    @field_validator("origins")
    @classmethod
    def _check_origins(cls, v: list[str]) -> list[str]:
        _parse_patterns(v)
        return v

    @model_validator(mode="after")
    def _check_unique_grants(self) -> Mandate:
        names = [g.name for g in self.data]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate data grant names: {sorted(dupes)}")
        return self

    def model_post_init(self, __context) -> None:
        self._patterns = _parse_patterns(self.origins)

    def allows_origin(self, url: str) -> bool:
        origin = url_origin(url)
        return origin is not None and any(origin_matches(origin, p) for p in self._patterns)

    def grant(self, name: str) -> Optional[DataGrant]:
        return next((g for g in self.data if g.name == name), None)

    def is_expired(self, now: Optional[float] = None) -> bool:
        return self.expires_at is not None and (now or time.time()) >= self.expires_at

    def default_start_url(self) -> Optional[str]:
        """Root URL of the first non-wildcard origin, for starting the browser."""
        for scheme, host, port in self._patterns:
            if not host.startswith("*."):
                suffix = "" if port == _DEFAULT_PORTS[scheme] else f":{port}"
                return f"{scheme}://{host}{suffix}/"
        return None

    @classmethod
    def from_yaml(cls, path: str | Path) -> Mandate:
        """Load a mandate file. Accepts `expires_in_minutes` as a relative expiry."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Mandate file not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        minutes = data.pop("expires_in_minutes", None)
        if minutes is not None:
            data["expires_at"] = time.time() + float(minutes) * 60
        return cls(**data)


def ensure_scheme(url: str) -> str:
    """Treat a bare host as https, as the controller does, so checks see the real target."""
    return url if url.startswith(("http://", "https://")) else f"https://{url}"
