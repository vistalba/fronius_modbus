"""Sensor platform: every FroniusSensorDescription becomes a FroniusSensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import UNKNOWN_STATE
from .coordinator import (
    FroniusConfigEntry,
    FroniusMeterPowerCoordinator,
    FroniusRuntimeData,
)
from .descriptions import FroniusSensorDescription
from .entities import sensor_descriptions
from .entity import FroniusEntity, FroniusTotalSensor
from .mppt_entities import left_out_mppt_keys

# The coordinator polls; entities never fetch on their own.
PARALLEL_UPDATES = 0

_TOTAL_STATE_CLASSES = (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING)


class FroniusSensor(FroniusEntity, SensorEntity):
    """A sensor whose state is its description's value_fn."""

    entity_description: FroniusSensorDescription

    @classmethod
    def create(
        cls,
        runtime: FroniusRuntimeData,
        entry: FroniusConfigEntry,
        description: FroniusSensorDescription,
    ) -> FroniusEntity:
        """Build a FroniusTotalSensor for total(-increasing) sensors, else a plain one."""
        if description.state_class in _TOTAL_STATE_CLASSES:
            return FroniusTotalSensor(runtime, entry, description)
        return cls(runtime, entry, description)

    async def async_added_to_hass(self) -> None:
        """Follow the web poll and the meter power poll where the description reads them."""
        await super().async_added_to_hass()
        web = self._runtime.web
        if self.entity_description.also_web and web is not None:
            self.async_on_remove(
                web.async_add_listener(self._handle_coordinator_update)
            )
        meter_power = self._meter_power
        if meter_power is not None:
            self.async_on_remove(
                meter_power.async_add_listener(self._handle_coordinator_update)
            )

    @property
    def _meter_power(self) -> FroniusMeterPowerCoordinator | None:
        """The fast meter power poll, where it polls the meter this sensor shows."""
        meter_power = self._runtime.meter_power
        description = self.entity_description
        serves = (
            description.meter_power
            and meter_power is not None
            and meter_power.meter_unit_id == description.meter_unit_id
        )
        return meter_power if serves else None

    @property
    def _fast_reading(self) -> float | None:
        """The fast poll's last power, or None where it does not serve or failed."""
        meter_power = self._meter_power
        if meter_power is None or not meter_power.last_update_success:
            return None
        return meter_power.data

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The description's attributes, where it has any."""
        attributes_fn = self.entity_description.attributes_fn
        return None if attributes_fn is None else attributes_fn(self._runtime)

    @property
    def native_value(self) -> Any:
        """The description's value; for an enum, one of its options or unknown.

        A state outside the options fails every state write. A flag or code
        the device sends that no option names reads as unknown, as an
        unmapped code does.
        """
        fast_reading = self._fast_reading
        # A failed fast read leaves the full poll's reading, as without the option.
        if fast_reading is not None:
            return fast_reading
        value = self.entity_description.value_fn(self._runtime)
        options = self.entity_description.options
        if options is None or value is None or value in options:
            return value
        return UNKNOWN_STATE if UNKNOWN_STATE in options else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FroniusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add every sensor the current runtime produces, and a string's once it is lit.

    An MPPT string dark at setup sends no value, so its sensors wait for the
    first one and are added then, without a reload (quality scale
    dynamic-devices). The waiting keys are taken together with the first
    descriptions, with nothing awaited in between: a poll there would light a
    string neither of them saw.
    """
    runtime = entry.runtime_data
    waiting = set(left_out_mppt_keys(runtime))

    @callback
    def _add(keys: set[str] | None = None) -> None:
        async_add_entities(
            FroniusSensor.create(runtime, entry, description)
            for description in sensor_descriptions(runtime)
            if keys is None or description.key in keys
        )

    @callback
    def _add_lit_strings() -> None:
        lit = waiting - left_out_mppt_keys(runtime)
        if not lit:
            return
        waiting.difference_update(lit)
        _add(lit)

    _add()
    if waiting:
        entry.async_on_unload(runtime.modbus.async_add_listener(_add_lit_strings))
