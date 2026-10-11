"""Support for Roborock image."""

from __future__ import annotations

from datetime import datetime
import io
import logging

from PIL import Image, UnidentifiedImageError
from roborock.devices.traits.v1.home import HomeTrait
from roborock.devices.traits.v1.map_content import MapContent
from vacuum_map_parser_base.config.drawable import Drawable

from homeassistant.components.image import ImageEntity
from homeassistant.components.roborock.const import (
    DEFAULT_DRAWABLES,
    DRAWABLES,
    MAP_SCALE,
)
from homeassistant.components.roborock.coordinator import RoborockDataUpdateCoordinator
from homeassistant.components.roborock.entity import RoborockCoordinatedEntityV1
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import map_render
from .const import (
    CONF_ANCHOR_VX,
    CONF_ANCHOR_VY,
    CONF_BG_OVERRIDES,
    CONF_FILE,
    CONF_FILE_HASH,
    DEFAULT_MAP_ROTATION,
    MAP_ROTATION_OPTIONS,
    get_map_rotation,
    map_key,
    map_unique_id,
    override_scales,
    signal_map_refresh,
)
from .preview import PreviewSession, async_get_session

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return PNG (width, height) from raw bytes, or None if not a PNG."""
    if len(data) < 24:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return (width, height)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Roborock image platform."""
    async_add_entities(
        RoborockMap(
            config_entry,
            map_unique_id(coord.duid_slug, map_info.map_flag, map_info.name),
            coord,
            coord.properties_api.home,
            map_info.map_flag,
            map_info.name,
        )
        for coord in config_entry.runtime_data
        if coord.properties_api.home is not None
        for map_info in (coord.properties_api.home.home_map_info or {}).values()
    )


class RoborockMap(RoborockCoordinatedEntityV1, ImageEntity):
    """A class to let you visualize the map."""

    _attr_has_entity_name = True
    image_last_updated: datetime
    _attr_name: str

    def __init__(
        self,
        config_entry: ConfigEntry,
        unique_id: str,
        coordinator: RoborockDataUpdateCoordinator,
        home_trait: HomeTrait,
        map_flag: int,
        map_name: str,
    ) -> None:
        """Initialize a Roborock map."""
        RoborockCoordinatedEntityV1.__init__(self, unique_id, coordinator)
        ImageEntity.__init__(self, coordinator.hass)

        self.config_entry = config_entry
        self.map_flag = map_flag
        self.rotation_key = map_key(coordinator.duid_slug, map_flag)
        self._home_trait = home_trait

        if not map_name:
            map_name = f"Map {map_flag}"
        self._attr_name = f"{map_name}_custom"

        self.cached_map = b""
        self._raw_image_size: tuple[int, int] | None = None
        self._overlay_cache: tuple[bytes, Image.Image] | None = None
        self._mask_cache: tuple[bytes, Image.Image] | None = None
        self._layers_generation = 0
        self._bg_cache: tuple[tuple[str, str], bytes] | None = None
        self._composite_cache: tuple[tuple, bytes] | None = None
        self._override_render_ok: bool | None = None

        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_selected(self) -> bool:
        """Return if this map is the currently selected map."""
        return self.map_flag == self.coordinator.properties_api.maps.current_map

    @property
    def _map_content(self) -> MapContent | None:
        if self._home_trait.home_map_content and (
            map_content := self._home_trait.home_map_content.get(self.map_flag)
        ):
            return map_content
        return None

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass load any previously cached maps from disk."""
        await super().async_added_to_hass()

        self._attr_image_last_updated = self.coordinator.last_home_update

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_map_refresh(self.config_entry.entry_id, self.rotation_key),
                self._async_handle_refresh,
            )
        )

        self.async_write_ha_state()

    @callback
    def _async_handle_refresh(self) -> None:
        """Refresh signal; bump last_updated to bust the image cache."""
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update."""
        if (map_content := self._map_content) is None:
            return

        if self.cached_map != map_content.image_content:
            self.cached_map = map_content.image_content
            self._raw_image_size = _png_dimensions(self.cached_map)
            self._attr_image_last_updated = self.coordinator.last_home_update

        super()._handle_coordinator_update()

    def _rotate_image(self, raw: bytes, rotation: int) -> bytes:
        """Rotate image in executor thread."""
        img = Image.open(io.BytesIO(raw))
        img = img.rotate(rotation, expand=True)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    def _get_rotation(self, preview: PreviewSession | None) -> int:
        """Return the preview session's rotation while active, else the stored one."""
        if preview is not None:
            rotation = preview.rotation
        else:
            rotation = get_map_rotation(
                self.hass, self.config_entry.entry_id, self.rotation_key
            )

        if rotation not in MAP_ROTATION_OPTIONS:
            _LOGGER.debug(
                "Unsupported map rotation %s, allowed values: %s, falling back to %s",
                rotation,
                MAP_ROTATION_OPTIONS,
                DEFAULT_MAP_ROTATION,
            )
            return DEFAULT_MAP_ROTATION

        return rotation

    def _override_topleft(
        self,
        map_content: MapContent,
        rotation: int,
        preview: PreviewSession | None,
    ) -> tuple[float, float] | None:
        """Rotated-frame top-left of the override, or None when none is served."""
        map_data = map_content.map_data
        dims = getattr(map_data, "image", None) and map_data.image.dimensions
        if dims is None or map_content.raw_api_response is None:
            return None

        if preview is not None:
            if preview.bg_bytes is None:
                return None
            return (preview.offset_x, preview.offset_y)

        override = self.config_entry.options.get(CONF_BG_OVERRIDES, {}).get(
            self.rotation_key
        )
        if override is None:
            return None
        ax = override.get(CONF_ANCHOR_VX)
        ay = override.get(CONF_ANCHOR_VY)
        if ax is None or ay is None:
            return None
        transform = map_render.MapTransform.from_dimensions(dims)
        return map_render.anchor_topleft(transform, ax, ay, rotation)

    def _core_drawables(self) -> list[Drawable]:
        """Drawables the core roborock entry has enabled (mirrors its setup)."""
        configured = self.coordinator.config_entry.options.get(DRAWABLES, {})
        return [
            drawable
            for drawable, default_value in DEFAULT_DRAWABLES.items()
            if configured.get(drawable, default_value)
        ]

    async def _async_get_overlay(self, raw: bytes) -> Image.Image:
        """Return the parsed drawables overlay, re-parsing when raw changed."""
        if self._overlay_cache is None or self._overlay_cache[0] != raw:
            overlay = await self.hass.async_add_executor_job(
                map_render.build_overlay, raw, self._core_drawables(), MAP_SCALE
            )
            self._overlay_cache = (raw, overlay)
            self._layers_generation += 1
        return self._overlay_cache[1]

    async def _async_get_mask(self, raw: bytes) -> Image.Image:
        """Return the wall/dock mask, built lazily (preview inversion only)."""
        if self._mask_cache is None or self._mask_cache[0] != raw:
            mask = await self.hass.async_add_executor_job(
                map_render.build_wall_mask, raw, MAP_SCALE
            )
            self._mask_cache = (raw, mask)
        return self._mask_cache[1]

    async def _async_get_override_bytes(
        self, filename: str, file_hash: str | None
    ) -> bytes | None:
        """Return the stored background image, cached per (filename, hash)."""
        if (
            file_hash is not None
            and self._bg_cache is not None
            and self._bg_cache[0] == (filename, file_hash)
        ):
            return self._bg_cache[1]
        try:
            data = await self.hass.async_add_executor_job(
                map_render.read_override_file, self.hass, filename
            )
        except OSError as err:
            _LOGGER.warning(
                "Background override file %s is unreadable: %s", filename, err
            )
            return None
        if file_hash is not None:
            self._bg_cache = ((filename, file_hash), data)
        return data

    @callback
    def _set_override_render_ok(self, ok: bool) -> None:
        """Record whether the last override render succeeded and republish state."""
        if self._override_render_ok is ok:
            return
        self._override_render_ok = ok
        self.async_write_ha_state()

    async def _async_render_override(
        self,
        map_content: MapContent,
        rotation: int,
        preview: PreviewSession | None,
    ) -> bytes | None:
        """Render the background-override composite, or None for the normal map."""
        topleft = self._override_topleft(map_content, rotation, preview)
        if topleft is None:
            return None

        if (raw := map_content.raw_api_response) is None:
            _LOGGER.debug(
                "Raw map data unavailable for %s; serving unmodified map",
                self.rotation_key,
            )
            self._set_override_render_ok(False)
            return None

        if preview is not None:
            bg = preview.bg_bytes
            scale_x = preview.scale_x
            scale_y = preview.scale_y
            invert_walls = preview.invert_walls
            variant: tuple = ("preview", preview.flow_id, preview.revision)
            filename = file_hash = None
        else:
            override = self.config_entry.options.get(CONF_BG_OVERRIDES, {}).get(
                self.rotation_key, {}
            )
            bg = None
            scale_x, scale_y = override_scales(override)
            invert_walls = False
            filename = override.get(CONF_FILE)
            file_hash = override.get(CONF_FILE_HASH)
            variant = ("saved", filename, file_hash)
            if filename is None:
                self._set_override_render_ok(False)
                return None

        try:
            overlay = await self._async_get_overlay(raw)
            mask = await self._async_get_mask(raw) if invert_walls else None

            params = (topleft[0], topleft[1], scale_x, scale_y, rotation)
            cache_key = (self._layers_generation, variant, params)
            if (
                self._composite_cache is not None
                and self._composite_cache[0] == cache_key
            ):
                self._set_override_render_ok(True)
                return self._composite_cache[1]

            if bg is None:
                bg = await self._async_get_override_bytes(filename, file_hash)
                if bg is None:
                    self._set_override_render_ok(False)
                    return None

            composite = await self.hass.async_add_executor_job(
                map_render.compose_final, overlay, mask, bg, *params
            )
            self._composite_cache = (cache_key, composite)
            self._set_override_render_ok(True)
            return composite
        except Exception as err:
            _LOGGER.warning(
                "Failed to render background override for %s: %s",
                self.rotation_key,
                err,
            )
            self._set_override_render_ok(False)
            return None

    async def async_image(self) -> bytes | None:
        """Get the image (with optional background override and rotation)."""
        if (map_content := self._map_content) is None:
            raise HomeAssistantError("Map flag not found in coordinator maps")

        preview = async_get_session(
            self.hass, self.config_entry.entry_id, self.rotation_key
        )
        rotation = self._get_rotation(preview)

        composite = await self._async_render_override(map_content, rotation, preview)
        if composite is not None:
            return composite

        base = map_content.image_content
        if rotation == DEFAULT_MAP_ROTATION:
            return base

        try:
            return await self.hass.async_add_executor_job(
                self._rotate_image, base, rotation
            )
        except (OSError, UnidentifiedImageError) as err:
            _LOGGER.debug(
                "Failed to rotate Roborock map image: %s, returning original image",
                err,
            )
            return base

    @property
    def extra_state_attributes(self):
        """Return extra attributes for map card usage (rotation-aware calibration)."""
        if (map_content := self._map_content) is None:
            raise HomeAssistantError("Map flag not found in coordinator maps")

        map_data = map_content.map_data
        if map_data is None:
            return {}

        if map_data.rooms is not None:
            for room in map_data.rooms.values():
                name = self._home_trait._rooms_trait.room_map.get(room.number)
                room.name = name.name if name else "Unknown"

        calibration = map_data.calibration()

        preview = async_get_session(
            self.hass, self.config_entry.entry_id, self.rotation_key
        )
        rotation = self._get_rotation(preview)
        topleft = self._override_topleft(map_content, rotation, preview)
        if topleft is not None and self._override_render_ok is not False:
            transform = map_render.MapTransform.from_dimensions(
                map_data.image.dimensions
            )
            w, h = transform.base_size
            ox0, oy0 = map_render.canvas_origin(*topleft)
            shift_x, shift_y = -ox0, -oy0
        elif rotation != DEFAULT_MAP_ROTATION and self._raw_image_size is not None:
            w, h = self._raw_image_size
            shift_x = shift_y = 0
        else:
            w = h = None

        if calibration is not None and w is not None:
            adjusted_calibration = []
            for pt in calibration:
                mp = pt.get("map") or {}
                x = mp.get("x")
                y = mp.get("y")

                if x is None or y is None:
                    adjusted_calibration.append(pt)
                    continue

                nx, ny = map_render.rotate_point(float(x), float(y), w, h, rotation)

                new_pt = dict(pt)
                new_map = dict(mp)
                new_map["x"] = nx + shift_x
                new_map["y"] = ny + shift_y
                new_pt["map"] = new_map
                adjusted_calibration.append(new_pt)

            calibration = adjusted_calibration

        return {
            "calibration_points": calibration,
            "rooms": map_data.rooms,
            "zones": map_data.zones,
        }
