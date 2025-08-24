"""ZHA Entertainment mode support for Philips Hue bulbs."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from zigpy import types as t
from zigpy.exceptions import DeliveryError

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Entertainment mode constants
ENTERTAINMENT_CLUSTER_ID = 0xFC03  # Philips proprietary cluster
ENTERTAINMENT_ATTRIBUTE = 0x4000
MAX_ENTERTAINMENT_LIGHTS = 20
MIN_UPDATE_INTERVAL = 0.04  # 25 FPS max per light


class ZHAEntertainmentCluster:
    """Custom Zigbee cluster for Hue Entertainment streaming."""

    def __init__(self, zha_gateway):
        """Initialize entertainment cluster."""
        self._gateway = zha_gateway
        self._streaming_lights = {}
        self._is_streaming = False
        self._last_update = {}

    async def start_entertainment_stream(self, light_entities: list) -> bool:
        """Start entertainment streaming for specified lights."""
        _LOGGER.info("Starting ZHA entertainment stream for %d lights", len(light_entities))
        
        for light in light_entities:
            if hasattr(light, '_zha_device'):
                device_ieee = str(light._zha_device.ieee)
                self._streaming_lights[device_ieee] = {
                    "entity": light,
                    "last_update": 0,
                    "endpoint": light._zha_device.device.endpoints[1]
                }
        
        self._is_streaming = True
        return True

    async def stop_entertainment_stream(self) -> bool:
        """Stop entertainment streaming."""
        _LOGGER.info("Stopping ZHA entertainment stream")
        self._is_streaming = False
        self._streaming_lights.clear()
        self._last_update.clear()
        return True

    async def stream_colors_bulk(self, color_data: dict[str, dict]) -> bool:
        """Stream colors to multiple lights simultaneously."""
        if not self._is_streaming:
            return False
            
        current_time = time.time()
        tasks = []
        
        for device_ieee, light_info in self._streaming_lights.items():
            # Rate limit per light
            if current_time - light_info["last_update"] < MIN_UPDATE_INTERVAL:
                continue
                
            entity = light_info["entity"]
            entity_id = entity.entity_id
            
            if entity_id in color_data:
                colors = color_data[entity_id]
                light_info["last_update"] = current_time
                
                # Create direct Zigbee command
                task = self._send_direct_color_command(
                    light_info["endpoint"],
                    colors.get("x", 0.3),
                    colors.get("y", 0.3),
                    colors.get("bri", 254)
                )
                tasks.append(task)
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success_count = sum(1 for r in results if not isinstance(r, Exception))
            return success_count > 0
            
        return False

    async def _send_direct_color_command(self, endpoint, x: float, y: float, bri: int):
        """Send direct Zigbee color command bypassing HA entity."""
        try:
            # Get color cluster
            if hasattr(endpoint, 'light_color'):
                cluster = endpoint.light_color
            else:
                # Fallback to standard color cluster
                from zigpy.zcl.clusters.lighting import Color
                cluster = endpoint.in_clusters.get(Color.cluster_id)
                
            if not cluster:
                _LOGGER.error("No color cluster found for entertainment streaming")
                return False
            
            # Convert to Zigbee format
            zigbee_x = int(x * 65535)
            zigbee_y = int(y * 65535)
            zigbee_bri = max(1, min(254, bri))
            
            # Send commands with minimal transition time
            try:
                # Try bulk command if available
                if hasattr(cluster, 'move_to_color_and_brightness'):
                    await cluster.move_to_color_and_brightness(
                        zigbee_x, zigbee_y, zigbee_bri,
                        transition_time=1  # 100ms transition
                    )
                else:
                    # Fall back to separate commands
                    await asyncio.gather(
                        cluster.move_to_color(zigbee_x, zigbee_y, 1),
                        cluster.move_to_level(zigbee_bri, 1),
                        return_exceptions=True
                    )
                return True
                
            except DeliveryError as e:
                _LOGGER.debug("Entertainment command delivery error: %s", e)
                return False
                
        except Exception as e:
            _LOGGER.error("Entertainment color command failed: %s", e)
            return False


class ZHAEntertainmentGroup:
    """Manage entertainment groups for ZHA."""

    def __init__(self, hass: HomeAssistant, group_id: str):
        """Initialize entertainment group."""
        self.hass = hass
        self.group_id = group_id
        self.lights = []
        self.active = False
        self.created = time.time()
        self._entertainment_cluster = None

    def add_light(self, light_entity) -> bool:
        """Add a light to the entertainment group."""
        if self._is_entertainment_capable(light_entity):
            self.lights.append(light_entity)
            return True
        return False

    def _is_entertainment_capable(self, light_entity) -> bool:
        """Check if light supports entertainment mode."""
        if not hasattr(light_entity, '_zha_device'):
            return False
            
        device = light_entity._zha_device
        
        # Check manufacturer and model
        manufacturer = device.manufacturer
        model = device.model
        
        # Philips Hue models that support entertainment
        hue_entertainment_models = {
            "LCT001", "LCT002", "LCT003",  # Hue bulb A19
            "LCT010", "LCT011", "LCT012",  # Hue BR30
            "LCT014", "LCT015", "LCT016",  # Hue A19 (Gen 3)
            "LCT021", "LCT024",             # Hue Go and Play
            "LTW010", "LTW011", "LTW012",  # Hue White Ambiance
            "LTW015", "LTW016", "LTW017",  # White Ambiance (Gen 3)
            "LLC010", "LLC011", "LLC012",  # Living Colors
            "LLC020", "LST002", "LST003",  # Light Strip Plus
            "LCX001", "LCX002", "LCX003",  # Gradient Strip
        }
        
        if manufacturer and "Philips" in manufacturer:
            if model in hue_entertainment_models:
                _LOGGER.debug("Light %s (model %s) supports entertainment", 
                            light_entity.entity_id, model)
                return True
        
        return False

    async def start_streaming(self, entertainment_cluster: ZHAEntertainmentCluster) -> bool:
        """Start entertainment streaming for this group."""
        if not self.lights:
            return False
            
        self._entertainment_cluster = entertainment_cluster
        success = await entertainment_cluster.start_entertainment_stream(self.lights)
        
        if success:
            self.active = True
            _LOGGER.info("Entertainment streaming started for group %s", self.group_id)
            
        return success

    async def stop_streaming(self) -> bool:
        """Stop entertainment streaming for this group."""
        if self._entertainment_cluster:
            await self._entertainment_cluster.stop_entertainment_stream()
            
        self.active = False
        self._entertainment_cluster = None
        _LOGGER.info("Entertainment streaming stopped for group %s", self.group_id)
        return True

    async def update_colors(self, color_data: dict) -> bool:
        """Update colors for lights in this group."""
        if not self.active or not self._entertainment_cluster:
            return False
            
        # Filter color data to only include lights in this group
        group_light_ids = {light.entity_id for light in self.lights}
        filtered_data = {
            light_id: colors 
            for light_id, colors in color_data.items() 
            if light_id in group_light_ids
        }
        
        return await self._entertainment_cluster.stream_colors_bulk(filtered_data)


class ZHAEntertainmentManager:
    """Manager for ZHA entertainment mode."""

    def __init__(self, hass: HomeAssistant, zha_gateway):
        """Initialize entertainment manager."""
        self.hass = hass
        self._gateway = zha_gateway
        self._entertainment_cluster = ZHAEntertainmentCluster(zha_gateway)
        self._groups = {}

    @callback
    def async_create_group(self, group_id: str) -> ZHAEntertainmentGroup:
        """Create a new entertainment group."""
        if group_id in self._groups:
            _LOGGER.warning("Entertainment group %s already exists", group_id)
            return self._groups[group_id]
            
        group = ZHAEntertainmentGroup(self.hass, group_id)
        self._groups[group_id] = group
        _LOGGER.info("Created entertainment group %s", group_id)
        return group

    @callback
    def async_get_group(self, group_id: str) -> ZHAEntertainmentGroup | None:
        """Get an entertainment group by ID."""
        return self._groups.get(group_id)

    @callback
    def async_remove_group(self, group_id: str) -> bool:
        """Remove an entertainment group."""
        if group_id not in self._groups:
            return False
            
        group = self._groups[group_id]
        if group.active:
            asyncio.create_task(group.stop_streaming())
            
        del self._groups[group_id]
        _LOGGER.info("Removed entertainment group %s", group_id)
        return True

    async def async_start_streaming(self, group_id: str) -> bool:
        """Start streaming for a group."""
        group = self._groups.get(group_id)
        if not group:
            _LOGGER.error("Entertainment group %s not found", group_id)
            return False
            
        return await group.start_streaming(self._entertainment_cluster)

    async def async_stop_streaming(self, group_id: str) -> bool:
        """Stop streaming for a group."""
        group = self._groups.get(group_id)
        if not group:
            return False
            
        return await group.stop_streaming()

    async def async_update_colors(self, group_id: str, color_data: dict) -> bool:
        """Update colors for a streaming group."""
        group = self._groups.get(group_id)
        if not group:
            return False
            
        return await group.update_colors(color_data)

    @callback
    def async_get_entertainment_capable_lights(self) -> list:
        """Get all entertainment capable lights in the system."""
        capable_lights = []
        
        # Get all ZHA lights
        device_registry = dr.async_get(self.hass)
        
        for entry in device_registry.devices.values():
            if entry.config_entries and DOMAIN in entry.config_entries:
                # Check if this is a Philips Hue bulb
                if entry.manufacturer and "Philips" in entry.manufacturer:
                    # This is potentially an entertainment capable light
                    capable_lights.append(entry)
                    
        return capable_lights