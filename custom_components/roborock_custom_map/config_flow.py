"""Config flow for Roborock Custom Map integration."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import map_render
from .const import (
    CONF_ANCHOR_VX,
    CONF_ANCHOR_VY,
    CONF_BG_OVERRIDES,
    CONF_CONFIRM,
    CONF_FILE,
    CONF_FILE_HASH,
    CONF_MAP_ROTATION,
    CONF_OFFSET_X,
    CONF_OFFSET_Y,
    CONF_SCALE_X,
    CONF_SCALE_Y,
    DOMAIN,
    MAP_ROTATION_OPTIONS,
    MAX_SCALE,
    MIN_SCALE,
    get_map_rotation,
    map_key,
    map_unique_id,
    override_scales,
    set_map_rotation,
    signal_map_refresh,
    signal_set_rotation,
)
from .preview import (
    PreviewSession,
    async_remove_sessions_for_flow,
    async_set_session,
)
from .preview import async_setup_preview as async_setup_preview_ws

_LOGGER = logging.getLogger(__name__)

CONF_MAP = "map"

REMOVE_SCHEMA = vol.Schema({vol.Required(CONF_CONFIRM, default=False): bool})

UPLOAD_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_FILE): FileSelector(
            FileSelectorConfig(
                accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"
            )
        )
    }
)

OFFSET_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        mode=NumberSelectorMode.BOX,
        step="any",
        min=-20000,
        max=20000,
        unit_of_measurement="px",
    )
)
SCALE_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        mode=NumberSelectorMode.BOX,
        step="any",
        min=0,
        max=round(MAX_SCALE * 100),
        unit_of_measurement="%",
    )
)
ROTATION_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        mode=SelectSelectorMode.DROPDOWN,
        options=[
            SelectOptionDict(value=str(deg), label=f"{deg}°")
            for deg in MAP_ROTATION_OPTIONS
        ],
    )
)


@dataclass
class MapChoice:
    """One configurable map of one vacuum."""

    rotation_key: str
    map_flag: int
    label: str
    entity_id: str | None
    coordinator: Any


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Roborock Custom Map."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Roborock Custom Map", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Return the options flow (background overrides)."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Manage per-map background overrides."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._maps: dict[str, MapChoice] = {}
        self._selected: MapChoice | None = None
        self._new_bg: bytes | None = None
        self._new_ext: str | None = None

    @property
    def _choice(self) -> MapChoice:
        """The selected map (only valid after async_step_init)."""
        assert self._selected is not None
        return self._selected

    @staticmethod
    async def async_setup_preview(hass: HomeAssistant) -> None:
        """Set up the preview websocket API (invoked by the flow manager)."""
        async_setup_preview_ws(hass)

    @callback
    def async_remove(self) -> None:
        """Clean up the tuning session when the flow finishes or is abandoned."""
        async_remove_sessions_for_flow(
            self.hass, self.config_entry.entry_id, self.flow_id
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select which map to configure (skipped when there is only one)."""
        coordinators = getattr(self.config_entry, "runtime_data", None)
        if not coordinators:
            return self.async_abort(reason="not_loaded")

        entity_registry = er.async_get(self.hass)
        self._maps = {}
        for coordinator in coordinators:
            home = coordinator.properties_api.home
            if home is None:
                continue
            for map_info in (home.home_map_info or {}).values():
                rotation_key = map_key(coordinator.duid_slug, map_info.map_flag)
                unique_id = map_unique_id(
                    coordinator.duid_slug, map_info.map_flag, map_info.name
                )
                entity_id = entity_registry.async_get_entity_id(
                    "image", DOMAIN, unique_id
                )
                label = rotation_key
                if entity_id and (state := self.hass.states.get(entity_id)):
                    label = state.name or rotation_key
                self._maps[rotation_key] = MapChoice(
                    rotation_key=rotation_key,
                    map_flag=map_info.map_flag,
                    label=label,
                    entity_id=entity_id,
                    coordinator=coordinator,
                )

        if not self._maps:
            return self.async_abort(reason="no_maps")

        if len(self._maps) == 1:
            self._selected = next(iter(self._maps.values()))
            return await self.async_step_map()

        if user_input is not None:
            if (choice := self._maps.get(user_input[CONF_MAP])) is None:
                return self.async_abort(reason="unknown_map")
            self._selected = choice
            return await self.async_step_map()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAP): SelectSelector(
                        SelectSelectorConfig(
                            mode=SelectSelectorMode.LIST,
                            options=[
                                SelectOptionDict(value=key, label=choice.label)
                                for key, choice in sorted(self._maps.items())
                            ],
                        )
                    )
                }
            ),
        )

    async def async_step_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Route to upload or the manage menu depending on stored state."""
        if self._current_override() is not None:
            return await self.async_step_map_menu()
        return await self.async_step_upload()

    async def async_step_map_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage an existing override."""
        return self.async_show_menu(
            step_id="map_menu",
            menu_options=["tune", "upload", "remove_confirm"],
        )

    async def async_step_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Upload or replace the custom floor plan, with a live preview."""
        choice = self._choice
        errors: dict[str, str] = {}
        override = self._current_override()
        rotation = self._current_rotation()

        if user_input is not None:
            if not (file_id := user_input.get(CONF_FILE)):
                return self._async_close()
            try:
                self._new_bg, self._new_ext = await self.hass.async_add_executor_job(
                    self._read_uploaded_file, file_id
                )
            except map_render.InvalidBackgroundImage as err:
                errors["base"] = err.error_key
            else:
                off_x, off_y, scale_x, scale_y = self._stored_placement(
                    override, rotation
                )
                if (
                    result := await self._async_store(
                        off_x, off_y, scale_x, scale_y, rotation, finish=False
                    )
                ) is not None:
                    return result
                return await self.async_step_tune()

        if self._raw_map_bytes() is None:
            return self.async_abort(reason="no_raw_map")
        if choice.entity_id is None:
            return self.async_abort(reason="no_entity")

        fallback_bg = await self._async_current_bg_bytes()
        off_x, off_y, scale_x, scale_y = self._stored_placement(override, rotation)
        self._async_seed_preview(
            bg_bytes=fallback_bg,
            offset_x=off_x,
            offset_y=off_y,
            scale_x=scale_x,
            scale_y=scale_y,
            rotation=rotation,
            mode="upload",
            fallback_bg=fallback_bg,
        )

        return self.async_show_form(
            step_id="upload",
            data_schema=UPLOAD_SCHEMA,
            errors=errors,
            preview=DOMAIN,
        )

    async def async_step_tune(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Adjust the floor plan: offsets/scale with a live preview."""
        choice = self._choice
        override = self._current_override()

        if user_input is not None:
            rotation = int(user_input[CONF_MAP_ROTATION])
            set_map_rotation(
                self.hass, self.config_entry.entry_id, choice.rotation_key, rotation
            )
            async_dispatcher_send(
                self.hass,
                signal_set_rotation(self.config_entry.entry_id, choice.rotation_key),
                rotation,
            )
            async_dispatcher_send(
                self.hass,
                signal_map_refresh(self.config_entry.entry_id, choice.rotation_key),
            )
            return await self._async_store(
                user_input[CONF_OFFSET_X],
                user_input[CONF_OFFSET_Y],
                float(user_input[CONF_SCALE_X]) / 100,
                float(user_input[CONF_SCALE_Y]) / 100,
                rotation,
                finish=True,
            )

        if self._raw_map_bytes() is None:
            return self.async_abort(reason="no_raw_map")
        if choice.entity_id is None:
            return self.async_abort(reason="no_entity")

        bg = self._new_bg
        if bg is None and override is not None:
            try:
                bg = await self.hass.async_add_executor_job(
                    map_render.read_override_file, self.hass, override[CONF_FILE]
                )
            except OSError:
                bg = None
        if bg is None:
            return self.async_abort(reason="missing_override_file")

        rotation = self._current_rotation()
        if self._map_transform() is None:
            return self.async_abort(reason="no_raw_map")
        off_x, off_y, scale_x, scale_y = self._stored_placement(override, rotation)
        pct_x = round(scale_x * 100, 2)
        pct_y = round(scale_y * 100, 2)
        defaults = {
            CONF_OFFSET_X: round(off_x, 2),
            CONF_OFFSET_Y: round(off_y, 2),
        }

        self._async_seed_preview(
            bg_bytes=bg,
            offset_x=defaults[CONF_OFFSET_X],
            offset_y=defaults[CONF_OFFSET_Y],
            scale_x=scale_x,
            scale_y=scale_y,
            rotation=rotation,
            invert_walls=True,
        )

        return self.async_show_form(
            step_id="tune",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OFFSET_X, default=defaults[CONF_OFFSET_X]
                    ): OFFSET_SELECTOR,
                    vol.Required(
                        CONF_OFFSET_Y, default=defaults[CONF_OFFSET_Y]
                    ): OFFSET_SELECTOR,
                    vol.Required(CONF_SCALE_X, default=pct_x): SCALE_SELECTOR,
                    vol.Required(CONF_SCALE_Y, default=pct_y): SCALE_SELECTOR,
                    vol.Required(
                        CONF_MAP_ROTATION, default=str(rotation)
                    ): ROTATION_SELECTOR,
                }
            ),
            preview=DOMAIN,
        )

    async def async_step_remove_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm removal of the floor plan via an explicit checkbox."""
        choice = self._choice

        if user_input is not None:
            if not user_input.get(CONF_CONFIRM):
                return self._async_close()
            await self.hass.async_add_executor_job(
                map_render.delete_override_files_for_key,
                self.hass,
                choice.rotation_key,
            )
            return self._async_save_override_record(None)

        if choice.entity_id is not None:
            override = self._current_override()
            current_bg = await self._async_current_bg_bytes()
            rotation = self._current_rotation()
            off_x, off_y, scale_x, scale_y = self._stored_placement(
                override, rotation
            )
            self._async_seed_preview(
                bg_bytes=current_bg,
                offset_x=off_x,
                offset_y=off_y,
                scale_x=scale_x,
                scale_y=scale_y,
                rotation=rotation,
                mode="remove",
                fallback_bg=current_bg,
            )

        return self.async_show_form(
            step_id="remove_confirm",
            data_schema=REMOVE_SCHEMA,
            preview=DOMAIN if choice.entity_id is not None else None,
        )

    @callback
    def _async_seed_preview(self, **fields: Any) -> None:
        """Register a preview session for the selected map and refresh it."""
        choice = self._choice
        async_set_session(
            self.hass,
            self.config_entry.entry_id,
            PreviewSession(
                flow_id=self.flow_id,
                rotation_key=choice.rotation_key,
                entity_id=choice.entity_id,
                **fields,
            ),
        )
        async_dispatcher_send(
            self.hass,
            signal_map_refresh(self.config_entry.entry_id, choice.rotation_key),
        )

    def _stored_placement(
        self, override: dict[str, Any] | None, rotation: int
    ) -> tuple[float, float, float, float]:
        """Rotated-frame offsets and scales of the stored override, or defaults."""
        if (
            override
            and override.get(CONF_ANCHOR_VX) is not None
            and (transform := self._map_transform()) is not None
        ):
            off_x, off_y = map_render.anchor_topleft(
                transform,
                override[CONF_ANCHOR_VX],
                override[CONF_ANCHOR_VY],
                rotation,
            )
            scale_x, scale_y = override_scales(override)
            return off_x, off_y, scale_x, scale_y
        return 0.0, 0.0, 1.0, 1.0

    async def _async_store(
        self,
        offset_x: float,
        offset_y: float,
        scale_x: float,
        scale_y: float,
        rotation: int,
        *,
        finish: bool,
    ) -> ConfigFlowResult | None:
        """Persist the floor plan file (if newly uploaded) and its placement.

        With finish=False the entry is updated in place and None is returned so
        the flow can continue.
        """
        choice = self._choice
        override = self._current_override()

        transform = self._map_transform()
        if transform is None:
            return self.async_abort(reason="no_raw_map")
        w_px, h_px = transform.base_size
        bx, by = map_render.unrotate_point(
            float(offset_x), float(offset_y), w_px, h_px, rotation
        )
        anchor_vx, anchor_vy = transform.base_to_vacuum(bx, by)

        if self._new_bg is not None:
            filename = f"{choice.rotation_key}.{self._new_ext}"
            new_bg = self._new_bg

            def _persist() -> str:
                map_render.delete_override_files_for_key(
                    self.hass, choice.rotation_key
                )
                map_render.save_override_file(self.hass, filename, new_bg)
                return hashlib.sha1(new_bg).hexdigest()[:16]

            try:
                file_hash = await self.hass.async_add_executor_job(_persist)
            except OSError as err:
                _LOGGER.error(
                    "Could not store floor plan for %s: %s",
                    choice.rotation_key,
                    err,
                )
                return self.async_abort(reason="write_failed")
            self._new_bg = None
        elif override is not None:
            filename = override[CONF_FILE]
            file_hash = override.get(CONF_FILE_HASH)
        else:
            return self.async_abort(reason="missing_override_file")

        options = self._merged_options(
            {
                CONF_FILE: filename,
                CONF_FILE_HASH: file_hash,
                CONF_ANCHOR_VX: anchor_vx,
                CONF_ANCHOR_VY: anchor_vy,
                CONF_SCALE_X: min(max(float(scale_x), MIN_SCALE), MAX_SCALE),
                CONF_SCALE_Y: min(max(float(scale_y), MIN_SCALE), MAX_SCALE),
            }
        )
        if finish:
            return self.async_create_entry(data=options)
        self.hass.config_entries.async_update_entry(
            self.config_entry, options=options
        )
        return None

    @callback
    def _merged_options(self, record: dict[str, Any] | None) -> dict[str, Any]:
        """Entry options with the selected map's override record set or removed."""
        options = dict(self.config_entry.options)
        overrides = dict(options.get(CONF_BG_OVERRIDES, {}))
        if record is None:
            overrides.pop(self._choice.rotation_key, None)
        else:
            overrides[self._choice.rotation_key] = record
        options[CONF_BG_OVERRIDES] = overrides
        return options

    @callback
    def _async_save_override_record(
        self, record: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Write (or delete) the selected map's override record into options."""
        return self.async_create_entry(data=self._merged_options(record))

    @callback
    def _async_close(self) -> ConfigFlowResult:
        """Finish the flow without changing anything (a no-op action)."""
        return self.async_create_entry(data=dict(self.config_entry.options))

    def _read_uploaded_file(self, file_id: str) -> tuple[bytes, str]:
        """Read and validate the uploaded file (runs in the executor)."""
        try:
            with process_uploaded_file(self.hass, file_id) as path:
                data = path.read_bytes()
        except (ValueError, OSError) as err:
            raise map_render.InvalidBackgroundImage("upload_failed") from err
        return data, map_render.validate_upload(data)

    def _raw_map_bytes(self) -> bytes | None:
        """Return the selected map's raw payload, if available."""
        choice = self._choice
        home = choice.coordinator.properties_api.home
        if home is None or not home.home_map_content:
            return None
        map_content = home.home_map_content.get(choice.map_flag)
        return map_content.raw_api_response if map_content else None

    def _map_transform(self) -> map_render.MapTransform | None:
        """Vacuum<->pixel transform for the selected map's current crop, or None."""
        choice = self._choice
        home = choice.coordinator.properties_api.home
        if home is None or not home.home_map_content:
            return None
        map_content = home.home_map_content.get(choice.map_flag)
        map_data = map_content.map_data if map_content else None
        dims = getattr(map_data, "image", None) and map_data.image.dimensions
        return map_render.MapTransform.from_dimensions(dims) if dims else None

    def _current_override(self) -> dict[str, Any] | None:
        """Return the stored override record for the selected map."""
        return self.config_entry.options.get(CONF_BG_OVERRIDES, {}).get(
            self._choice.rotation_key
        )

    def _current_rotation(self) -> int:
        """Selected map's currently applied rotation."""
        return get_map_rotation(
            self.hass, self.config_entry.entry_id, self._choice.rotation_key
        )

    async def _async_current_bg_bytes(self) -> bytes | None:
        """Bytes of the map's stored floor plan, or None when there is none."""
        override = self._current_override()
        if override is None or CONF_FILE not in override:
            return None
        try:
            return await self.hass.async_add_executor_job(
                map_render.read_override_file, self.hass, override[CONF_FILE]
            )
        except OSError:
            return None
