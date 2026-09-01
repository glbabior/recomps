"""Where named profiles live.

Profiles resolve in one order, most specific first:

  1. a profile saved by `lotcomps configure` in the user's config directory,
  2. a profile the market plugin ships,
  3. the built-in vacant-land default.

A user profile shadowing a plugin profile of the same name is intentional: the
plugin describes the market, the user describes their own property, and the
second should win without editing the first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from lotcomps.config.profile import BUILTIN_PROFILES, CompProfile
from lotcomps.plugin.market import Market

APP_DIR_ENV = "LOTCOMPS_CONFIG_DIR"


def config_dir() -> Path:
    """The user's config directory, overridable for tests and CI."""
    override = os.environ.get(APP_DIR_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "lotcomps"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lotcomps"


def profiles_path(market_name: str) -> Path:
    return config_dir() / f"{market_name}.profiles.json"


def load_user_profiles(market_name: str) -> dict[str, CompProfile]:
    path = profiles_path(market_name)
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {name: CompProfile.from_dict(data) for name, data in raw.items()}


def save_user_profile(market_name: str, profile: CompProfile) -> Path:
    path = profiles_path(market_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
    existing[profile.name] = profile.to_dict()
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return path


def available_profiles(market: Market) -> dict[str, CompProfile]:
    merged: dict[str, CompProfile] = dict(market.profiles())
    merged.update(load_user_profiles(market.name))
    return merged


def resolve_profile(market: Market, name: str | None) -> CompProfile:
    profiles = available_profiles(market)
    if name is None:
        name = market.default_profile() or next(iter(profiles), "")
    if name in profiles:
        return profiles[name]
    if name in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name](name)
    known = ", ".join(sorted(profiles)) or "none"
    raise KeyError(
        f"unknown profile {name!r} for market {market.name!r}; available: {known}. "
        "Run `lotcomps configure` to create one."
    )
