"""The upgrade contract: what an entry of 1.4.1 owns must come through every later release.

`fixtures/upgrade_contract_1_4_1.json` was recorded once, by 1.4.1 itself, for
the entry shapes that release writes. Every later release sets the same entries
up and must hand back the same registry: the same unique ids, entity ids and
devices, the same disabled entities, the same states. A missing or changed row
is a broken installation somewhere - history cut off, an automation pointing at
nothing. New rows are allowed: adding is not breaking.

Never record the file again from a newer release; that would make the contract
say whatever the code does. `FM_RECORD_UPGRADE_CONTRACT=1` exists to record it
from the release it is named after, and a new contract gets a new file.
"""

import json
import os
import pathlib

import pytest

from custom_components import fronius_modbus
from custom_components.fronius_modbus.const import DOMAIN
from custom_components.fronius_modbus.froniuswebclient import FroniusWebClient
from custom_components.fronius_modbus.token_store import async_get_token_store
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import METER_UNIT_ID, AsyncFake
from .ha_test_plugin import MockConfigEntry

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(120)]

CONTRACT = pathlib.Path(__file__).with_name("fixtures") / "upgrade_contract_1_4_1.json"
RECORD = os.environ.get("FM_RECORD_UPGRADE_CONTRACT") == "1"

HOST = "192.0.2.10"

# The data a 1.4.1 setup writes, field for field (`_entry_payload` of a user
# flow with the defaults): the shape every entry of that release carries.
DATA_1_4_1 = {
    "name": "Fronius",
    "host": HOST,
    "port": 502,
    "inverter_modbus_unit_id": 1,
    "scan_interval": 10,
    "web_scan_interval": 60,
    "meter_power_interval": 0,
    "api_username": "customer",
    "auto_enable_modbus": True,
    "modbus_restriction": "keep",
    "reconfigure_required": False,
}
MODBUS_ONLY = DATA_1_4_1 | {"api_username": "none"}

# 1.4.1's options flow writes the whole settings payload into the options,
# and the options win over the data when the entry is read.
SCENARIOS = {
    "modbus_only": {"data": MODBUS_ONLY, "options": {}, "web": False},
    "modbus_only_after_options": {
        "data": MODBUS_ONLY,
        "options": MODBUS_ONLY | {"scan_interval": 15, "meter_power_interval": 2},
        "web": False,
    },
    "web_api_two_meters": {"data": DATA_1_4_1, "options": {}, "web": True},
}

# What 1.4.1 reads an entry with: the options first, then the data. A release
# that moves a value must leave it where this finds it, or going back to 1.4.1
# from a backup of the entry's new shape reads another setting.
SETTINGS_1_4_1 = (
    "host",
    "port",
    "inverter_modbus_unit_id",
    "scan_interval",
    "web_scan_interval",
    "meter_power_interval",
    "api_username",
    "modbus_restriction",
)


def read_like_1_4_1(entry, key: str):
    """`_entry_value` of 1.4.1, frozen."""
    return entry.options.get(key, entry.data.get(key))


@pytest.fixture(autouse=True)
def _custom_integration(enable_custom_integrations):
    return enable_custom_integrations


class _WebClientAnsweringNothing(AsyncFake):
    """An inverter whose web interface gives no answer: no test reaches a socket."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get_power_meter_info(self, *_args, **_kwargs):
        return None

    def __getattr__(self, name: str):
        if not hasattr(FroniusWebClient, name):
            raise AttributeError(name)

        def _quiet(*_args, **_kwargs):
            return None

        return _quiet


class _WebClientWithTwoMeters(_WebClientAnsweringNothing):
    """A logged-in web API with a second meter; every other read unanswered."""

    def get_power_meter_info(self, *_args, **_kwargs):
        return {
            "unit_ids": [METER_UNIT_ID, METER_UNIT_ID + 1],
            "primary_unit_id": METER_UNIT_ID,
            "locations_by_unit_id": {METER_UNIT_ID: 0, METER_UNIT_ID + 1: 1},
        }

    def __getattr__(self, name: str):
        if not hasattr(FroniusWebClient, name):
            raise AttributeError(name)

        def _quiet(*_args, **_kwargs):
            return None

        return _quiet


def _registry_rows(hass, entry) -> dict[str, dict]:
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    rows = {}
    for row in er.async_entries_for_config_entry(entities, entry.entry_id):
        device = devices.async_get(row.device_id) if row.device_id else None
        state = hass.states.get(row.entity_id)
        rows[f"{row.domain}:{row.unique_id}"] = {
            "entity_id": row.entity_id,
            "device": sorted(map(list, device.identifiers)) if device else None,
            "disabled_by": row.disabled_by,
            "entity_category": row.entity_category,
            "translation_key": row.translation_key,
            "device_class": row.original_device_class,
            "unit": row.unit_of_measurement,
            "state_class": (row.capabilities or {}).get("state_class"),
            "state": None if state is None else state.state,
        }
    return rows


def _device_rows(hass, entry) -> dict[str, dict]:
    devices = dr.async_get(hass)
    return {
        "|".join(sorted(f"{d}:{i}" for d, i in device.identifiers)): {
            "manufacturer": device.manufacturer,
            "model": device.model,
            "name": device.name,
            "via_device": device.via_device_id is not None,
        }
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
    }


async def _set_up(hass, mock_modbus, monkeypatch, scenario: dict) -> MockConfigEntry:
    monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientAnsweringNothing)
    if scenario["web"]:
        mock_modbus.add_unit(METER_UNIT_ID + 1, like=METER_UNIT_ID)
        monkeypatch.setattr(fronius_modbus, "FroniusWebClient", _WebClientWithTwoMeters)
        await async_get_token_store(hass).async_save_token(HOST, realm="r", token="t")
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="01JUPGRADECONTRACT00000000",
        title=f"Fronius {HOST}",
        data=dict(scenario["data"]),
        options=dict(scenario["options"]),
        unique_id=HOST,
        version=1,
        minor_version=14,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # A restart: what the first start wrote, the second must keep.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _record(name: str, snapshot: dict) -> None:
    recorded = _contract() if CONTRACT.exists() else {}
    recorded[name] = snapshot
    CONTRACT.write_text(
        json.dumps(recorded, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_an_entry_of_1_4_1_keeps_every_entity_and_device(
    hass, mock_modbus, monkeypatch, name
):
    entry = await _set_up(hass, mock_modbus, monkeypatch, SCENARIOS[name])
    snapshot = {
        "entities": _registry_rows(hass, entry),
        "devices": _device_rows(hass, entry),
    }
    if RECORD:
        _record(name, snapshot)
    expected = _contract()[name]

    missing = sorted(set(expected["entities"]) - set(snapshot["entities"]))
    assert not missing, f"entities gone after the upgrade: {missing}"
    changed = {
        key: (row, snapshot["entities"][key])
        for key, row in expected["entities"].items()
        if snapshot["entities"][key] != row
    }
    assert not changed, f"entities changed by the upgrade: {changed}"
    gone = sorted(set(expected["devices"]) - set(snapshot["devices"]))
    assert not gone, f"devices gone after the upgrade: {gone}"
    for key, row in expected["devices"].items():
        assert snapshot["devices"][key] == row, key


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_an_upgraded_entry_reads_the_same_settings_in_1_4_1(
    hass, mock_modbus, monkeypatch, name
):
    """Going back to 1.4.1 must find every setting where it left it.

    Home Assistant loads an entry of a higher minor version with older code
    and hands it to that code's migration, which 1.4.1 answers with "nothing
    to do". Only a major version would refuse it.
    """
    scenario = SCENARIOS[name]
    before = MockConfigEntry(
        domain=DOMAIN, data=scenario["data"], options=scenario["options"]
    )
    entry = await _set_up(hass, mock_modbus, monkeypatch, scenario)

    assert entry.version == 1
    assert entry.minor_version >= 14
    for key in SETTINGS_1_4_1:
        assert read_like_1_4_1(entry, key) == read_like_1_4_1(before, key), key


def test_the_contract_holds_every_scenario():
    """A scenario added without a recording would compare against nothing."""
    assert sorted(_contract()) == sorted(SCENARIOS)
    for snapshot in _contract().values():
        assert snapshot["entities"]
        assert any(
            row["state"] != STATE_UNAVAILABLE for row in snapshot["entities"].values()
        )
