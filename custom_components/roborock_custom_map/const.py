"""Constants and shared key helpers for Roborock Custom Map integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

DOMAIN = "roborock_custom_map"

CONF_MAP_ROTATION = "map_rotation"
DEFAULT_MAP_ROTATION = 0
MAP_ROTATION_OPTIONS = (0, 90, 180, 270)

SIGNAL_MAP_REFRESH = "roborock_custom_map_refresh"

CONF_BG_OVERRIDES = "bg_overrides"
CONF_FILE = "file"
CONF_FILE_HASH = "file_hash"
CONF_OFFSET_X = "offset_x"
CONF_OFFSET_Y = "offset_y"
CONF_SCALE = "scale"
CONF_SCALE_X = "scale_x"
CONF_SCALE_Y = "scale_y"
CONF_CONFIRM = "confirm"
CONF_ANCHOR_VX = "anchor_vx"
CONF_ANCHOR_VY = "anchor_vy"

DATA_PREVIEW_SESSIONS = "preview_sessions"
DATA_LAST_BG_OVERRIDES = "last_bg_overrides"

PREVIEW_WS_TYPE = f"{DOMAIN}/start_preview"

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_BG_DIMENSION = 8192
MAX_SCALED_DIMENSION = 8192
MAX_SCALED_PIXELS = 16_000_000
MAX_COMPOSITE_DIMENSION = 16384
MAX_COMPOSITE_PIXELS = 32_000_000
PREVIEW_SESSION_TTL = 1800

MIN_SCALE = 0.001
MAX_SCALE = 1000.0


def override_scales(record: dict) -> tuple[float, float]:
    """Per-axis scales of an override record (legacy single-scale fallback)."""
    legacy = record.get(CONF_SCALE, 1.0)
    return (
        record.get(CONF_SCALE_X, legacy),
        record.get(CONF_SCALE_Y, legacy),
    )


def map_key(duid_slug: str, map_flag: int) -> str:
    """Per-map settings key, shared by rotation and background overrides."""
    return f"{duid_slug}_{map_flag}"


def map_unique_id(duid_slug: str, map_flag: int, map_name: str | None) -> str:
    """Unique id of a map's image entity (must stay stable across releases)."""
    return f"{duid_slug}_custom_map_{map_name or f'Map {map_flag}'}"


def signal_map_refresh(entry_id: str, key: str) -> str:
    """Dispatcher signal telling one map's image entity to re-render."""
    return f"{SIGNAL_MAP_REFRESH}_{entry_id}_{key}"


def get_map_rotation(hass: HomeAssistant, entry_id: str, key: str) -> int:
    """Return the currently applied rotation of one map."""
    return (
        hass.data.get(DOMAIN, {})
        .get(entry_id, {})
        .get(CONF_MAP_ROTATION, {})
        .get(key, DEFAULT_MAP_ROTATION)
    )


def set_map_rotation(hass: HomeAssistant, entry_id: str, key: str, rotation: int) -> None:
    """Store one map's rotation in the integration's runtime data."""
    hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {}).setdefault(
        CONF_MAP_ROTATION, {}
    )[key] = rotation


SIGNAL_SET_ROTATION = "roborock_custom_map_set_rotation"


def signal_set_rotation(entry_id: str, key: str) -> str:
    """Dispatcher signal asking the select entity to change a map's rotation."""
    return f"{SIGNAL_SET_ROTATION}_{entry_id}_{key}"
