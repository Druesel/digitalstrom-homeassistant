"""The digitalSTROM integration."""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.yaml import loader as yaml_loader

from .api.apartment import DigitalstromApartment
from .api.client import DigitalstromClient
from .api.exceptions import CannotConnect, InvalidAuth, InvalidCertificate, ServerError
from .const import CONF_DSUID, CONF_SSL, DOMAIN, WEBSOCKET_WATCHDOG_INTERVAL

_LOGGER = logging.getLogger(__name__)

SERVICE_CALL_CUSTOM_ACTION = "call_custom_action"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_NAME = "name"
ATTR_PARAMETERS = "parameters"
ATTR_PATH = "path"

CUSTOM_ACTIONS_YAML = "digitalstrom_custom_actions.yaml"

CALL_CUSTOM_ACTION_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional(ATTR_NAME): cv.string,
        vol.Optional(ATTR_PATH): cv.string,
        vol.Optional(ATTR_PARAMETERS, default=dict): dict,
    }
)

PLATFORMS: list[Platform] = [
    Platform.UPDATE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.COVER,
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SCENE,
    Platform.CLIMATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """
    load configuration for digitalSTROM component
    """
    # not configured
    if DOMAIN not in config:
        return True

    # already imported
    if hass.config_entries.async_entries(DOMAIN):
        return True

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up digitalSTROM from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    client = DigitalstromClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        ssl=entry.data[CONF_SSL],
        loop=hass.loop,
    )
    client.set_app_token(entry.data[CONF_TOKEN])

    try:
        system_dsuid = await client.get_system_dsuid()
        if len(system_dsuid) < 8:  # 34
            raise ConfigEntryError("Invalid system DSUID received")
        if system_dsuid != entry.unique_id:
            _LOGGER.warning(
                f"Your system DSUID changed from {entry.unique_id} to {system_dsuid}"
            )

            if (
                hass.config_entries.async_entry_for_domain_unique_id(
                    DOMAIN, system_dsuid
                )
                is not None
            ):
                _LOGGER.error(f"Multiple config entries found for DSUID {system_dsuid}")
                raise ConfigEntryError(
                    translation_key="config_entry_error_multiple_entries_for_dsuid",
                    translation_placeholders={"dsuid": system_dsuid},
                )
            else:
                await migrate_system_dsuid(hass, entry, system_dsuid)
        apartment = DigitalstromApartment(client, system_dsuid)
        hass.data[DOMAIN].setdefault(entry.unique_id, dict())
        hass.data[DOMAIN][entry.unique_id]["client"] = client
        hass.data[DOMAIN][entry.unique_id]["entry_id"] = entry.entry_id
        hass.data[DOMAIN][entry.unique_id]["apartment"] = apartment
        await apartment.get_zones()
        await apartment.get_circuits()
        await apartment.get_devices()
    except (InvalidAuth, InvalidCertificate) as ex:
        raise ConfigEntryAuthFailed(ex) from ex
    except (CannotConnect, ServerError) as ex:
        raise ConfigEntryNotReady(ex) from ex

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)

    async def start_watchdog(event: Any = None) -> None:
        """Start websocket watchdog."""
        if "watchdog" not in hass.data[DOMAIN][entry.unique_id]:
            hass.data[DOMAIN][entry.unique_id]["watchdog"] = async_track_time_interval(
                hass,
                client.event_listener_watchdog,
                WEBSOCKET_WATCHDOG_INTERVAL,
                cancel_on_shutdown=True,
            )

    async def stop_watchdog(event: Any = None) -> None:
        await async_unload_entry(hass, entry)

    # If Home Assistant is already in a running state, start the watchdog
    # immediately, else trigger it after Home Assistant has finished starting.
    if hass.state == CoreState.running:
        await start_watchdog()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start_watchdog)
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, client.event_listener_watchdog
        )
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_watchdog)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry_data = hass.data[DOMAIN].get(entry.unique_id, {})
        if (remove_watchdog := entry_data.get("watchdog")) is not None:
            remove_watchdog()
        await entry_data["client"].stop_event_listener()
        hass.data[DOMAIN].pop(entry.unique_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_CALL_CUSTOM_ACTION)
    return unload_ok


def _async_register_services(hass: HomeAssistant) -> None:
    """Register services for the digitalSTROM integration."""
    if hass.services.has_service(DOMAIN, SERVICE_CALL_CUSTOM_ACTION):
        return

    async def async_call_custom_action(call: ServiceCall) -> None:
        """Call an authenticated dSS JSON API path for a custom action."""
        client = _get_service_client(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        action = await _async_resolve_custom_action(
            hass,
            client,
            call.data.get(ATTR_NAME),
            call.data.get(ATTR_PATH),
            call.data.get(ATTR_PARAMETERS, {}),
        )
        _LOGGER.debug("Calling custom digitalSTROM action path: %s", action)
        await client.request(action)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CALL_CUSTOM_ACTION,
        async_call_custom_action,
        schema=CALL_CUSTOM_ACTION_SCHEMA,
    )


def _get_service_client(
    hass: HomeAssistant, config_entry_id: str | None
) -> DigitalstromClient:
    """Return the configured client for a service call."""
    entries = hass.data.get(DOMAIN, {})
    if config_entry_id is not None:
        for entry_data in entries.values():
            if entry_data.get("entry_id") == config_entry_id:
                return entry_data["client"]
        raise HomeAssistantError(
            f"No digitalSTROM config entry found for config_entry_id {config_entry_id}"
        )

    if len(entries) == 1:
        return next(iter(entries.values()))["client"]

    raise HomeAssistantError(
        "Multiple digitalSTROM config entries found; provide config_entry_id"
    )


async def _async_resolve_custom_action(
    hass: HomeAssistant,
    client: DigitalstromClient,
    name: str | None,
    path: str | None,
    parameters: dict[str, Any],
) -> str:
    """Resolve a named or direct custom action to a dSS JSON API path."""
    if name is None and path is None:
        raise HomeAssistantError("Provide either name or path")

    if name is not None:
        event_path = await _async_find_user_defined_action_path(client, name)
        if event_path is not None:
            return _build_json_path(
                "event/raise",
                {
                    "name": "action_execute",
                    "parameter": f"path={event_path}",
                },
            )

        custom_actions = await hass.async_add_executor_job(
            _load_custom_actions, hass.config.path(CUSTOM_ACTIONS_YAML)
        )
        if name not in custom_actions:
            raise HomeAssistantError(
                f"Custom digitalSTROM action '{name}' was not found in {CUSTOM_ACTIONS_YAML}"
            )
        action = custom_actions[name]
        if isinstance(action, str):
            path = action
            action_parameters = {}
        elif isinstance(action, dict):
            path = action.get(ATTR_PATH)
            action_parameters = action.get(ATTR_PARAMETERS, {})
            if not isinstance(action_parameters, dict):
                raise HomeAssistantError(
                    f"Custom digitalSTROM action '{name}' parameters must be a mapping"
                )
        else:
            raise HomeAssistantError(
                f"Custom digitalSTROM action '{name}' must be a path or mapping"
            )
        parameters = {**action_parameters, **parameters}

    return _build_json_path(path, parameters)


async def _async_find_user_defined_action_path(
    client: DigitalstromClient, name: str
) -> str | None:
    """Find a User Defined Action path by its configured name."""
    result = await client.request("property/getChildren?path=/usr/events")
    children = result.get("result", result)
    for child in children:
        child_name = child.get("name")
        if child_name is None:
            continue
        event_path = f"/usr/events/{child_name}"
        try:
            name_result = await client.request(
                _build_json_path("property/getString", {"path": f"{event_path}/name"})
            )
        except ServerError:
            continue
        if name_result.get("value") == name:
            return event_path
    return None


def _load_custom_actions(path: str) -> dict[str, Any]:
    """Load named custom action definitions from YAML."""
    try:
        custom_actions = yaml_loader.load_yaml(path)
    except FileNotFoundError as ex:
        raise HomeAssistantError(
            f"Create {CUSTOM_ACTIONS_YAML} in the Home Assistant config directory to use named custom actions"
        ) from ex

    if custom_actions is None:
        return {}
    if not isinstance(custom_actions, dict):
        raise HomeAssistantError(f"{CUSTOM_ACTIONS_YAML} must contain a mapping")
    return custom_actions


def _build_json_path(path: str | None, parameters: dict[str, Any]) -> str:
    """Build a relative dSS JSON API path for the authenticated client."""
    if path is None:
        raise HomeAssistantError("A dSS JSON API path is required")
    if "://" in path:
        raise HomeAssistantError("Use a relative dSS JSON API path, not a full URL")

    path = path.strip().removeprefix("/").removeprefix("json/")
    if not path:
        raise HomeAssistantError("A dSS JSON API path is required")

    if parameters:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}{urllib.parse.urlencode(parameters, doseq=True)}"
    return path


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove config entry from a device if it's no longer present."""
    return True


async def migrate_system_dsuid(
    hass: HomeAssistant, config_entry: ConfigEntry, new_dsuid: str
) -> None:
    old_dsuid = config_entry.unique_id
    if old_dsuid is None or old_dsuid == new_dsuid or len(new_dsuid) < 8:
        return

    new_data = dict(config_entry.data)
    new_data[CONF_DSUID] = new_dsuid
    hass.config_entries.async_update_entry(
        config_entry, data=new_data, unique_id=new_dsuid
    )

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device_entries = dr.async_entries_for_config_entry(
        device_registry, config_entry_id=config_entry.entry_id
    )
    entity_entries = er.async_entries_for_config_entry(
        entity_registry, config_entry_id=config_entry.entry_id
    )
    for dev in device_entries:
        new_unique_id = None
        for identifier in dev.identifiers:
            domain, unique_id = identifier
            if domain == DOMAIN and old_dsuid in unique_id:
                new_unique_id = unique_id.replace(old_dsuid, new_dsuid)
                _LOGGER.debug(
                    f'Migrating identifier for device "{dev.name}": {unique_id} to {new_unique_id}'
                )
        if new_unique_id is not None:
            device_registry.async_update_device(
                dev.id, new_identifiers={(DOMAIN, new_unique_id)}
            )
    for ent in entity_entries:
        if old_dsuid in ent.unique_id:
            new_unique_id = ent.unique_id.replace(old_dsuid, new_dsuid)
            name = ent.original_name if ent.name is None else ent.name
            _LOGGER.debug(
                f'Migrating unique_id for entity "{name}": {ent.unique_id} to {new_unique_id}'
            )
            entity_registry.async_update_entity(
                ent.entity_id,
                new_unique_id=ent.unique_id.replace(old_dsuid, new_dsuid),
            )
