"""Live preview: session store and websocket command.

While an options flow step is open, a PreviewSession holds the unsaved floor
plan bytes and transform parameters; the websocket handler updates it and bumps
the revision so the flow's preview image re-fetches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import math
import time
from typing import Any, Literal

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.file_upload import DOMAIN as FILE_UPLOAD_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import map_render
from .const import (
    CONF_CONFIRM,
    CONF_FILE,
    CONF_MAP_ROTATION,
    CONF_OFFSET_X,
    CONF_OFFSET_Y,
    CONF_SCALE_X,
    CONF_SCALE_Y,
    DATA_PREVIEW_SESSIONS,
    DOMAIN,
    MAP_ROTATION_OPTIONS,
    MAX_SCALE,
    MAX_UPLOAD_BYTES,
    MIN_SCALE,
    PREVIEW_SESSION_TTL,
    PREVIEW_WS_TYPE,
    signal_map_refresh,
)

_preview_revision = itertools.count(1)


@dataclass
class PreviewSession:
    """State of one active preview session (one per options flow).

    offset_x/offset_y are the override's top-left in the rotated frame.
    """

    flow_id: str
    rotation_key: str
    entity_id: str
    bg_bytes: bytes | None
    offset_x: float = 0.0
    offset_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation: int = 0
    mode: Literal["upload", "tune", "remove"] = "tune"
    invert_walls: bool = False
    revision: int = field(default_factory=lambda: next(_preview_revision))
    file_id: str | None = None
    fallback_bg: bytes | None = None
    last_active: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        """Return True when the session saw no activity within the TTL."""
        return time.monotonic() - self.last_active > PREVIEW_SESSION_TTL

    def touch(self) -> None:
        """Record activity, extending the session's lifetime."""
        self.last_active = time.monotonic()


def _session_store(
    hass: HomeAssistant, entry_id: str
) -> dict[str, PreviewSession] | None:
    """Return the flow_id-keyed session store (guarded: gone during reloads)."""
    return hass.data.get(DOMAIN, {}).get(entry_id, {}).get(DATA_PREVIEW_SESSIONS)


@callback
def async_set_session(
    hass: HomeAssistant, entry_id: str, session: PreviewSession
) -> None:
    """Register or replace the preview session of an options flow.

    Any other flow's session for the same map is dropped.
    """
    if (store := _session_store(hass, entry_id)) is None:
        return
    for flow_id, existing in list(store.items()):
        if (
            flow_id != session.flow_id
            and existing.rotation_key == session.rotation_key
        ):
            del store[flow_id]
    store[session.flow_id] = session


@callback
def async_get_session(
    hass: HomeAssistant, entry_id: str, rotation_key: str
) -> PreviewSession | None:
    """Return the newest active preview session for a map, dropping expired ones."""
    if (store := _session_store(hass, entry_id)) is None:
        return None
    newest: PreviewSession | None = None
    for flow_id, session in list(store.items()):
        if session.expired:
            del store[flow_id]
            continue
        if session.rotation_key == rotation_key and (
            newest is None or session.last_active > newest.last_active
        ):
            newest = session
    return newest


@callback
def async_remove_sessions_for_flow(
    hass: HomeAssistant, entry_id: str, flow_id: str
) -> None:
    """Drop a finished/abandoned flow's session and refresh its map."""
    if not (store := _session_store(hass, entry_id)):
        return
    if (session := store.pop(flow_id, None)) is not None:
        map_render.clear_scaled_backgrounds()
        async_dispatcher_send(
            hass, signal_map_refresh(entry_id, session.rotation_key)
        )


@callback
def async_setup_preview(hass: HomeAssistant) -> None:
    """Register the preview websocket command (called by the flow manager)."""
    websocket_api.async_register_command(hass, ws_start_preview)


def _as_float(value: Any, fallback: float) -> float:
    """Coerce a form value to a finite float, falling back on invalid input."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def _as_rotation(value: Any, fallback: int) -> int:
    """Coerce a form value to a supported rotation, else keep the fallback."""
    try:
        rotation = int(value)
    except (TypeError, ValueError):
        return fallback
    return rotation if rotation in MAP_ROTATION_OPTIONS else fallback


def _clamp_scale(scale: float) -> float:
    """Clamp a scale multiplier into the supported range."""
    return min(max(scale, MIN_SCALE), MAX_SCALE)


def _read_pending_upload(hass: HomeAssistant, file_id: str) -> bytes | None:
    """Read and validate an uploaded temp file by id without consuming it.

    Runs in the executor. Returns None when the file is missing or invalid.
    """
    store = hass.data.get(FILE_UPLOAD_DOMAIN)
    if store is None or not store.has_file(file_id):
        return None
    try:
        path = store.file_path(file_id)
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    try:
        map_render.validate_upload(data)
    except map_render.InvalidBackgroundImage:
        return None
    return data


@websocket_api.websocket_command(
    {
        vol.Required("type"): PREVIEW_WS_TYPE,
        vol.Required("flow_id"): str,
        vol.Required("flow_type"): "options_flow",
        vol.Required("user_input"): dict,
    }
)
@websocket_api.async_response
async def ws_start_preview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Apply form values to the preview session and describe the preview image."""
    try:
        flow_status = hass.config_entries.options.async_get(msg["flow_id"])
    except UnknownFlow:
        connection.send_error(msg["id"], "unknown_flow", "Options flow not found")
        return

    entry_id = flow_status["handler"]
    store = _session_store(hass, entry_id) or {}
    session = store.get(msg["flow_id"])
    if session is None or session.expired:
        connection.send_error(
            msg["id"], "unknown_preview_session", "No active preview session"
        )
        return

    user_input = msg["user_input"]

    if session.mode == "upload":
        new_file_id = user_input.get(CONF_FILE) or None
        if new_file_id != session.file_id:
            session.file_id = new_file_id
            if new_file_id is None:
                session.bg_bytes = session.fallback_bg
            else:
                data = await hass.async_add_executor_job(
                    _read_pending_upload, hass, new_file_id
                )
                session.bg_bytes = data if data is not None else session.fallback_bg
    elif session.mode == "remove":
        session.bg_bytes = (
            None if user_input.get(CONF_CONFIRM) else session.fallback_bg
        )
    else:
        session.offset_x = _as_float(user_input.get(CONF_OFFSET_X), session.offset_x)
        session.offset_y = _as_float(user_input.get(CONF_OFFSET_Y), session.offset_y)
        session.scale_x = _clamp_scale(
            _as_float(user_input.get(CONF_SCALE_X), session.scale_x * 100) / 100
        )
        session.scale_y = _clamp_scale(
            _as_float(user_input.get(CONF_SCALE_Y), session.scale_y * 100) / 100
        )
        session.rotation = _as_rotation(
            user_input.get(CONF_MAP_ROTATION), session.rotation
        )

    session.revision = next(_preview_revision)
    session.touch()

    async_dispatcher_send(
        hass, signal_map_refresh(entry_id, session.rotation_key)
    )

    connection.send_result(msg["id"])
    connection.subscriptions[msg["id"]] = lambda: None

    state = hass.states.get(session.entity_id)
    access_token = state and state.attributes.get("access_token")
    if not access_token:
        connection.send_message(
            websocket_api.event_message(
                msg["id"], {"error": "Map image entity is unavailable"}
            )
        )
        return

    connection.send_message(
        websocket_api.event_message(
            msg["id"],
            {
                "entity_id": session.entity_id,
                "state": str(session.revision),
                "domain": "image",
                "attributes": {
                    "access_token": access_token,
                    "friendly_name": "",
                },
            },
        )
    )
