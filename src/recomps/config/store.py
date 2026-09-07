"""Where named profiles live.

Profiles resolve in one order, most specific first:

  1. a profile saved by `recomps profiles new` in the user's config directory,
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

from recomps.config.profile import BUILTIN_PROFILES, CompProfile
from recomps.plugin.market import Market

APP_DIR_ENV = "RECOMPS_CONFIG_DIR"


def config_dir() -> Path:
    """The user's config directory, overridable for tests and CI."""
    override = os.environ.get(APP_DIR_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "recomps"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "recomps"


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


def rename_user_profile(market_name: str, old: str, new: str) -> bool:
    """Rename a saved search. Returns False if there was nothing to rename.

    Only a profile the *user* saved can be renamed. One the market plugin ships
    belongs to the market definition: renaming it here would leave the original
    still in `market.toml` and a copy under the new name, which is two searches
    where the user asked for one.
    """
    path = profiles_path(market_name)
    if not path.is_file():
        return False
    existing = json.loads(path.read_text(encoding="utf-8"))
    if old not in existing:
        return False
    if new in existing:
        raise ValueError(f"a saved search named {new!r} already exists")
    profile = existing.pop(old)
    profile["name"] = new
    existing[new] = profile
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return True


def delete_user_profile(market_name: str, name: str) -> bool:
    """Remove a saved profile. Returns False if there was nothing to remove.

    Only user profiles can be deleted; a profile the market plugin ships is
    part of the market definition and is edited there.
    """
    path = profiles_path(market_name)
    if not path.is_file():
        return False
    existing = json.loads(path.read_text(encoding="utf-8"))
    if name not in existing:
        return False
    del existing[name]
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return True


def profile_origin(market: Market, name: str) -> str:
    """Where a profile came from: 'saved by you' or 'shipped with the market'."""
    if name in load_user_profiles(market.name):
        return "yours"
    if name in market.profiles():
        return "market"
    return "built-in"


def describe_profile(profile: CompProfile) -> str:
    """A one-line summary for listings: what this profile actually searches for."""
    size = profile.subject_size()
    unit = "living sqft" if "living" in profile.metric.value else "sqft"
    parts = [profile.property_type.value.replace("_", " ")]
    if size:
        parts.append(f"subject {size:,.0f} {unit}")
    low, high = (None, None)
    if size:
        low, high = profile.similar_bracket.bounds(size)
    if low and high:
        parts.append(f"comps {low:,.0f}-{high:,.0f}")
    parts.append(f"{profile.window_days}d window")
    return ", ".join(parts)


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
        "Run `recomps profiles new` to create one."
    )


# ---------------------------------------------------------------------------
# The last market opened
# ---------------------------------------------------------------------------
#
# A private market is used from a folder, and typing its path at every launch
# is friction the interface should not impose. Only the *choice* is remembered
# -- a name or a path -- never anything about the market itself, so this file
# stays safe to sit in a config directory that is not private.


def recent_path() -> Path:
    return config_dir() / "recent-market.json"


def remember_market(choice: str | None, path: str | None) -> None:
    """Record which market was last opened, so the next launch can reopen it."""
    target = recent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"choice": choice, "path": path}, indent=2), encoding="utf-8"
    )


def last_market() -> tuple[str | None, str | None]:
    """The last market opened, as ``(choice, path)``. Both None if unknown.

    A corrupt or unreadable file is treated as "nothing remembered" rather than
    an error: this is a convenience, and it must never be the reason the
    interface will not start.
    """
    target = recent_path()
    if not target.is_file():
        return None, None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    choice = raw.get("choice")
    path = raw.get("path")
    return (
        choice if isinstance(choice, str) else None,
        path if isinstance(path, str) else None,
    )
