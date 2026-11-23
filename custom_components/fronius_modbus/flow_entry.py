"""How a finished flow lands in its entry: the host it claims, the update, the reload."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_API_PASSWORD,
    DOMAIN,
    OPTION_KEYS,
    entry_title_after,
    entry_unique_id,
)
from .flow_settings import _entry_payload, split_entry_payload
from .flow_state import _AlreadyConfigured
from .token_store import async_forget_unused_tokens

_LOGGER = logging.getLogger(__name__)


def entry_with_host(
    hass: HomeAssistant, host_id: str
) -> config_entries.ConfigEntry | None:
    """The set-up entry whose configured host this is, whatever its unique id.

    The unique id is the inverter's serial number since 1.5 and the host
    before; 0.1.9 set none. The host decides for all three.
    """
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN, include_ignore=False)
            if entry_unique_id({**entry.data, **entry.options}) == host_id
        ),
        None,
    )


def host_holder(hass: HomeAssistant, host_id: str) -> config_entries.ConfigEntry | None:
    """The entry that holds this host: one set up for it, or a card ignored for it."""
    return entry_with_host(hass, host_id) or (
        hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, host_id)
    )


def entry_storage(
    entry: config_entries.ConfigEntry, payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The entry's data and options with ``payload`` written in.

    The connection goes to the data, the intervals to the options. Connection
    values an entry kept in its options before minor version 15 are dropped
    there: they win when the entry is read and would hide the new ones.
    """
    connection, intervals = split_entry_payload(payload)
    data = {**entry.data, **connection}
    for stale in (CONF_API_PASSWORD, "meter_modbus_unit_id", "meter_modbus_unit_ids"):
        data.pop(stale, None)
    for key in OPTION_KEYS:
        data.pop(key, None)
    options = {key: value for key, value in entry.options.items() if key in OPTION_KEYS}
    return data, options | intervals


def unique_id_after_move(
    entry: config_entries.ConfigEntry, settings: dict[str, Any]
) -> str | None:
    """The unique id an entry keeps when its host changes.

    A serial number stays with the inverter wherever it moves; a host id from
    before 1.5, or none at all, follows the host as it always did.
    """
    old_host_id = entry_unique_id({**entry.data, **entry.options})
    if entry.unique_id is None or entry.unique_id == old_host_id:
        return entry_unique_id(settings)
    return entry.unique_id


def _claim_host(
    hass: HomeAssistant, entry: config_entries.ConfigEntry, settings: dict[str, Any]
) -> str | None:
    """The unique id the entry keeps for ``settings``, unless another entry has the host.

    The duplicate check has to follow the host: a reconfigure or options
    change that moves it must not let a second entry for the new one through.
    """
    holder = host_holder(hass, entry_unique_id(settings))
    if holder is not None and holder.entry_id != entry.entry_id:
        raise _AlreadyConfigured
    return unique_id_after_move(entry, settings)


@callback
def async_adopt_serial_unique_id(
    hass: HomeAssistant, entry: config_entries.ConfigEntry, serial: str | None
) -> None:
    """Give the entry its inverter's serial number as unique id, once it is read.

    Entries before 1.5 carry the host, which changes. An inverter another
    entry already claims keeps the host id: two entries for one inverter are
    the owner's to sort out, and neither is touched.
    """
    serial = (serial or "").strip()
    if not serial or entry.unique_id == serial:
        return
    claimed = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, serial)
    if claimed is not None and claimed.entry_id != entry.entry_id:
        _LOGGER.warning(
            "Entry %s and entry %s serve the same inverter; the unique id of "
            "%s stays as it is",
            entry.entry_id,
            claimed.entry_id,
            entry.entry_id,
        )
        return
    hass.config_entries.async_update_entry(entry, unique_id=serial)


async def async_update_entry_from_input(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    validated_input: dict[str, Any],
    *,
    previous_host: str | None = None,
) -> None:
    """Write a reconfigure or reauth into the entry, and reload it once."""
    unique_id = _claim_host(hass, entry, validated_input)
    new_data, new_options = entry_storage(
        entry, _entry_payload(validated_input, reconfigure_required=False)
    )
    # Read before the update: its listener starts the reload right away.
    loaded = entry.state is config_entries.ConfigEntryState.LOADED
    changed = hass.config_entries.async_update_entry(
        entry,
        data=new_data,
        options=new_options,
        title=entry_title_after(
            entry.title, {**entry.data, **entry.options}, validated_input
        ),
        unique_id=unique_id,
    )
    if previous_host:
        await async_forget_unused_tokens(hass, previous_host)
    await _async_reload_unless_listened(hass, entry, changed=changed, loaded=loaded)


async def _async_reload_unless_listened(
    hass: HomeAssistant,
    entry: config_entries.ConfigEntry,
    *,
    changed: bool,
    loaded: bool,
) -> None:
    """Reload the entry unless its update listener already does.

    A change to a loaded entry reloads it through that listener; a second call
    here reloaded it twice. A new token alone changes nothing in
    the entry, and an entry that is not loaded has no listener: both need it.
    """
    if changed and loaded:
        return
    await hass.config_entries.async_reload(entry.entry_id)
