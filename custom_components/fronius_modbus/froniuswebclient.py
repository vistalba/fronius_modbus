"""The inverter's local web API: readings, settings and the Modbus setup.

The transport and the digest login live in `web_transport`; this module is
what the integration asks of the web interface, and how the answers parse.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import socket
from http import HTTPStatus
from typing import Any, cast

import aiohttp

from .component_readings import flag_value, json_object, take_readings
from .const import API_USERNAME, ModbusRestriction
from .export_limit import require_power_control, soft_limit_location
from .fronius_modbus_api.exceptions import ControlRefused
from .web_errors import (
    ClientIpResolutionError,
    FroniusWebResponseError,
    SunSpecModeChangeNeeded,
    FroniusWebAuthError,
)
from .web_transport import WebResponse, XHeaderDigestAuth, _http, login

_LOGGER = logging.getLogger(__name__)

# Only for a reply without the RS485 roles; a read one is written back as it is.
MASTER_RTUIF = {"master": {"rtuif": [{"if": "rtu0"}, {"if": "rtu1"}]}}
# Modes in which the Modbus TCP server answers; "both" also serves RTU clients.
TCP_MODES = ("tcp", "both")
# The slave mode in which no device is served over Modbus at all.
SLAVE_OFF = "off"
# The mode that adds Modbus TCP to what a slave serves: "tcp" alone would end
# the RS485 side an RTU client reads.
_MODE_WITH_TCP = {SLAVE_OFF: "tcp", "rtu": "both", "tcp": "tcp", "both": "both"}
RESTRICTION_LIST_SEPARATOR = ","
BATTERIES_PATH = "/api/config/batteries"
MODBUS_PATH = "/api/config/modbus"
SOLAR_API_PATH = "/api/config/solar_api"
# The inverter's name for the integer + scale factor models, the ones read here.
SUNSPEC_MODE_INT = "int"
# A component endpoint that answers 404: firmware without it, not a failed read.
ENDPOINT_MISSING = object()
# Lists in the answer to a config write that name the fields it did not take.
WRITE_REFUSALS = (
    "errors",
    "permissionFailure",
    "unknownNodes",
    "validationErrors",
    "writeFailure",
)


def _refusal(response: WebResponse) -> str:
    """The fields an answer names as not taken; only these, never the whole body."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    refused = {key: body[key] for key in WRITE_REFUSALS if body.get(key)}
    return f" refused {refused}" if refused else ""


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except TypeError, ValueError, OverflowError:
        return fallback


def is_enabled(value: Any) -> bool:
    """Whether a flag reads as on; a form the inverter does not use is not on.

    Read as on, `[1]` counted as Modbus control already switched on, and the
    write that switches it on was skipped.
    """
    return flag_value(value) is True


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = value.strip() if isinstance(value, str) else str(value).strip()
    return cleaned or None


def _is_power_meter_model(model: str | None) -> bool:
    if not model:
        return False
    model_l = model.lower()
    return "meter" in model_l or "wattnode" in model_l or "42,0411" in model_l


def _parse_storage_info(attributes: Any) -> dict[str, str | None]:
    if not isinstance(attributes, dict):
        return {"manufacturer": None, "model": "Battery Storage", "serial": None}

    nameplate = json_object(attributes.get("nameplate"))
    return {
        "manufacturer": _clean_text(nameplate.get("manufacturer"))
        or _clean_text(attributes.get("manufacturer")),
        "model": (
            _clean_text(attributes.get("model"))
            or _clean_text(nameplate.get("model"))
            or _clean_text(attributes.get("DisplayName"))
            or "Battery Storage"
        ),
        "serial": _clean_text(attributes.get("serial"))
        or _clean_text(nameplate.get("serial")),
    }


def _component_device(payload: Any) -> dict[str, Any] | None:
    nodes, _ = _body_data(payload, "Body", "Data")
    if not isinstance(nodes, dict):
        return None
    device = next(iter(nodes.values()), None)
    return device if isinstance(device, dict) else None


def _parse_storage_readable(payload: Any) -> dict[str, Any]:
    info: dict[str, Any] = dict(_parse_storage_info(None))
    info["missing"] = payload is ENDPOINT_MISSING
    device = _component_device(payload)
    if device is None:
        info["readings"] = None
        return info
    attributes = device.get("attributes")
    info.update(
        _parse_storage_info(attributes if isinstance(attributes, dict) else None)
    )
    info["readings"] = take_readings(device, "storage")
    return info


def _parse_inverter_readable(payload: Any) -> dict[str, Any]:
    device = _component_device(payload)
    return {
        "readings": None if device is None else take_readings(device, "inverter"),
        "missing": payload is ENDPOINT_MISSING,
    }


def _without_descriptions(node: Any) -> Any:
    """A config as it is written: the inverter reads every field with a _<field>_meta."""
    if isinstance(node, dict):
        return {
            key: _without_descriptions(value)
            for key, value in node.items()
            if not key.startswith("_")
        }
    if isinstance(node, list):
        return [_without_descriptions(value) for value in node]
    return node


def _config_object(payload: Any, path: str) -> dict[str, Any]:
    """A config as it is written back: without descriptions, and an object.

    Anything else is a failed read, not a crash further down.
    """
    config = _without_descriptions(payload)
    if not isinstance(config, dict):
        raise FroniusWebResponseError(
            f"HTTP 200 on {path} answered no object", HTTPStatus.OK
        )
    return config


def _nested_object(config: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    """A nested part of a config, checked like the whole.

    Only an absent part defaults to empty; a present `[]`, `0` or `""` would
    otherwise be written back with defaults.
    """
    return _config_object(config.get(key, {}), path)


def _mode_with_tcp(mode: Any) -> str:
    """The slave mode that adds Modbus TCP to ``mode``; an unknown one becomes tcp."""
    return _MODE_WITH_TCP.get(mode, "tcp") if isinstance(mode, str) else "tcp"


def _is_number(value: Any) -> bool:
    """A finite number: NaN compares false with anything."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _same(current: Any, wanted: Any) -> bool:
    """Whether the inverter already holds what a write would set.

    It answers flags and words in its own spelling ("true", 1, "Manual").
    """
    if current is None:
        return False
    if isinstance(wanted, bool):
        # An unknown form is never what is wanted: it is written.
        return flag_value(current) == wanted
    if isinstance(wanted, str) and isinstance(current, str):
        return current.casefold() == wanted.casefold()
    return bool(current == wanted)


def _network(entry: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(entry.strip(), strict=False)
    except ValueError:
        return None


def _restriction_with(restriction: dict[str, Any], address: str) -> dict[str, Any]:
    """The restriction with ``address`` allowed, keeping every host already on it.

    The inverter takes a list of hosts and networks; replacing it locked out
    the others. A list that is switched off is not revived with it.
    """
    listed = str(restriction.get("ip") or "")
    entries = [entry for entry in listed.split(RESTRICTION_LIST_SEPARATOR) if entry]
    if not is_enabled(restriction.get("on")):
        entries = []
    client = ipaddress.ip_address(address)
    networks = [_network(entry) for entry in entries]
    if not any(network is not None and client in network for network in networks):
        entries.append(address)
    return {**restriction, "on": True, "ip": RESTRICTION_LIST_SEPARATOR.join(entries)}


def _body_data(payload: Any, *path: str) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    node: Any = payload
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return (node, ".".join(path)) if isinstance(node, dict) else (None, None)


def _rtu_address(attributes: dict[str, Any]) -> int | None:
    """A meter's RS485 address, or None when the answer gives no usable one."""
    try:
        rtu_addr = int(str(attributes.get("addr", "")).strip())
    except TypeError, ValueError:
        return None
    return rtu_addr if rtu_addr > 0 else None


def _parse_power_meter_info(
    payload: Any,
    meter_address_offset: int,
) -> dict[str, Any] | None:
    nodes, payload_shape = _body_data(payload, "Body", "Data")
    if nodes is None:
        nodes, payload_shape = _body_data(payload, "meter", "Body", "Data")
    if nodes is None:
        return None

    result: dict[str, Any] = {
        "unit_ids": [],
        "primary_unit_id": None,
        "locations_by_unit_id": {},
        "payload_shape": payload_shape,
        "incomplete": False,
    }

    meters: list[dict[str, Any]] = []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        attributes = node.get("attributes")
        if not isinstance(attributes, dict):
            continue

        model = _clean_text(attributes.get("model"))
        if not _is_power_meter_model(model):
            continue
        rtu_addr = _rtu_address(attributes)
        if rtu_addr is None:
            # A meter the answer names but cannot place; leaving it out
            # silently would read as that meter being gone.
            result["incomplete"] = True
            continue

        label = (_clean_text(attributes.get("label")) or "").lower()
        location = _clean_text(attributes.get("meter-location"))
        meters.append(
            {
                "unit_id": int(meter_address_offset) + rtu_addr - 1,
                "rtu_addr": rtu_addr,
                "is_primary": label == "<primary>" or location == "0",
                "location": _as_int(location, -1),
            }
        )

    if not meters:
        return result

    meters.sort(
        key=lambda meter: (not meter["is_primary"], meter["rtu_addr"], meter["unit_id"])
    )
    seen: set[int] = set()
    unit_ids: list[int] = []
    for meter in meters:
        unit_id = meter["unit_id"]
        if unit_id in seen:
            continue
        seen.add(unit_id)
        unit_ids.append(unit_id)
        if meter["location"] >= 0:
            result["locations_by_unit_id"][unit_id] = meter["location"]
        if result["primary_unit_id"] is None and meter["is_primary"]:
            result["primary_unit_id"] = unit_id

    result["unit_ids"] = unit_ids
    if result["primary_unit_id"] is None and unit_ids:
        result["primary_unit_id"] = unit_ids[0]
    return result


async def _ipv4_address(host: str) -> str:
    """The IPv4 address of ``host``; a literal is taken as it is."""
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, 80, family=socket.AF_INET, type=socket.SOCK_DGRAM
    )
    return str(infos[0][4][0])


class FroniusWebClient:
    """Authenticated client for the Fronius web API."""

    def __init__(
        self,
        host: str,
        username: str | None = None,
        password: str = "",
        token: dict[str, str] | None = None,
        timeout: float = 4.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._host = host
        self._session = session
        self._username = username if username is not None else API_USERNAME
        self._password = password
        self._timeout = timeout
        self._failing_optional_reads: set[str] = set()
        self._auth = XHeaderDigestAuth(
            self._username,
            password=password,
            token=token,
            timeout=self._timeout,
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> WebResponse:
        url = f"http://{self._host}{path}"
        if authenticated:
            response = await self._auth.send(self._session, method, url, payload)
        else:
            response = await _http(
                self._session, method, url, payload=payload, timeout=self._timeout
            )
        if response.status_code in (401, 403):
            raise FroniusWebAuthError(
                f"Fronius Web API auth failed with status {response.status_code}"
            )
        if response.status_code >= 400:
            # A bare status left owners without the inverter's reason.
            raise FroniusWebResponseError(
                f"HTTP {response.status_code} on {path}{_refusal(response)}",
                response.status_code,
            )
        return response

    async def _get_json(self, path: str) -> dict[str, Any]:
        payload: dict[str, Any] = (await self._request("get", path)).json()
        return payload

    async def _get_public_json(self, path: str) -> dict[str, Any]:
        payload: dict[str, Any] = (
            await self._request("get", path, authenticated=False)
        ).json()
        return payload

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> None:
        """Write, and fail on a field the inverter names as refused.

        It answers a config write field by field in the body (seen on a GEN24),
        so HTTP 200 alone does not say every field was taken.
        """
        response = await self._request("post", path, payload=payload)
        if refusal := _refusal(response):
            raise FroniusWebResponseError(
                f"HTTP {response.status_code} on {path}{refusal}",
                response.status_code,
            )

    async def _read_or_nothing(self, path: str) -> dict[str, Any]:
        """A config to compare against; one that cannot be read compares as empty."""
        try:
            return _config_object(await self._get_json(path), path)
        except FroniusWebResponseError, ValueError:
            return {}

    async def _post_changes(
        self,
        path: str,
        wanted: dict[str, Any],
        current: dict[str, Any] | None = None,
    ) -> bool:
        """Write only the fields of ``wanted`` the inverter does not hold; whether any were.

        Compared against a fresh read, not the last poll: a value another
        controller changed in between would otherwise be skipped as "already
        set". A config that cannot be read is written in full.
        """
        if current is None:
            current = await self._read_or_nothing(path)
        changes = {
            key: value
            for key, value in wanted.items()
            if not _same(current.get(key), value)
        }
        if not changes:
            return False
        await self._post(path, changes)
        return True

    async def resolve_client_ip(self) -> str:
        """Home Assistant's own address towards the inverter.

        Connecting a UDP socket sends nothing; only a host name needs a lookup,
        and that one runs off the event loop.
        """
        try:
            address = await _ipv4_address(self._host)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((address, 80))
                client_ip: str = sock.getsockname()[0]
        except OSError as err:
            raise ClientIpResolutionError(
                f"Failed resolving local IP for {self._host}"
            ) from err

        if not client_ip or client_ip.startswith("127."):
            raise ClientIpResolutionError(
                f"Invalid local IP resolved for {self._host}: {client_ip!r}"
            )
        return client_ip

    async def login(self) -> bool:
        return await login(
            self._host,
            self._username,
            password=self._password,
            token=self._auth.token,
            timeout=self._timeout,
            session=self._session,
        )

    async def get_modbus_config(self) -> dict[str, Any]:
        return await self._get_json("/api/config/modbus")

    async def get_solar_api_config(self) -> dict[str, Any]:
        return await self._get_json("/api/config/solar_api")

    async def get_optional_json(self, path: str) -> Any:
        """A display-only read: when it fails its values are unknown, not the refresh.

        The component endpoints differ between firmware versions, and one of
        them failing must not take the controls down with it. A failure is
        still logged once, and its end: a swallowed HTTP 500 read as "no
        value". A switched-off device and a rejected login are
        not this read's to handle.
        """
        try:
            payload = await self._get_json(path)
        except (FroniusWebResponseError, ValueError) as err:
            missing = getattr(err, "status_code", None) == HTTPStatus.NOT_FOUND
            if path not in self._failing_optional_reads:
                self._failing_optional_reads.add(path)
                log = _LOGGER.debug if missing else _LOGGER.warning
                log("%s did not answer, its values stay unknown: %s", path, err)
            return ENDPOINT_MISSING if missing else None
        if path in self._failing_optional_reads:
            self._failing_optional_reads.discard(path)
            _LOGGER.info("%s answers again", path)
        return payload

    async def get_storage_info(self) -> dict[str, Any]:
        return _parse_storage_readable(
            await self.get_optional_json(
                "/api/components/BatteryManagementSystem/readable"
            )
        )

    async def get_inverter_info(self) -> dict[str, Any]:
        return _parse_inverter_readable(
            await self.get_optional_json("/api/components/inverter/readable")
        )

    async def get_power_meter_info(
        self, meter_address_offset: int = 200
    ) -> dict[str, Any] | None:
        try:
            data = await self._get_public_json("/api/components/PowerMeter/readable")
            meter_info = _parse_power_meter_info(data, meter_address_offset)
            if meter_info is None:
                top_level_keys = list(data.keys()) if isinstance(data, dict) else []
                _LOGGER.debug(
                    "Unrecognized PowerMeter payload shape: top-level keys=%s",
                    top_level_keys,
                )
                return None

            _LOGGER.debug(
                "Parsed the PowerMeter payload via %s: unit_ids=%s primary_unit_id=%s",
                meter_info.get("payload_shape"),
                meter_info.get("unit_ids"),
                meter_info.get("primary_unit_id"),
            )
            return meter_info
        except Exception as err:
            _LOGGER.debug(
                "Failed reading the power meter config via the web API: %s",
                err,
            )
        return None

    async def restriction_allows(self, address: str) -> bool:
        """Whether the Modbus restriction is on and already allows ``address``."""
        config = _config_object(await self.get_modbus_config(), MODBUS_PATH)
        slave = _nested_object(config, "slave", MODBUS_PATH)
        ctr = _nested_object(slave, "ctr", MODBUS_PATH)
        restriction = _nested_object(ctr, "restriction", MODBUS_PATH)
        return _restriction_with(restriction, address) == restriction

    async def ensure_modbus_enabled(
        self,
        port: int,
        meter_address: int,
        inverter_unit_id: int,
        restriction: ModbusRestriction = ModbusRestriction.KEEP,
        switch_sunspec_mode: bool = False,
        allowed_host: str | None = None,
    ) -> bool:
        config = _config_object(await self.get_modbus_config(), MODBUS_PATH)
        # Checked like the slave: it is written back as read.
        master = _config_object(
            config.get("master", MASTER_RTUIF["master"]), MODBUS_PATH
        )
        slave = _nested_object(config, "slave", MODBUS_PATH)
        ctr = _nested_object(slave, "ctr", MODBUS_PATH)
        current_restriction = _nested_object(ctr, "restriction", MODBUS_PATH)
        wanted = current_restriction
        if restriction == ModbusRestriction.HOME_ASSISTANT:
            wanted = _restriction_with(
                current_restriction, allowed_host or await self.resolve_client_ip()
            )
        if restriction == ModbusRestriction.OFF:
            wanted = {**current_restriction, "on": False}
        serves_tcp = slave.get("mode") in TCP_MODES

        if (
            serves_tcp
            # The integration reads the integer+SF models only; a float map
            # would pass every other check and then fail the probe.
            and slave.get("sunspecMode") == SUNSPEC_MODE_INT
            and is_enabled(ctr.get("on"))
            and _as_int(slave.get("port"), port) == int(port)
            and _as_int(slave.get("meterAddress"), meter_address) == int(meter_address)
            and _as_int(slave.get("rtu_inverter_slave_id"), inverter_unit_id)
            == int(inverter_unit_id)
            and is_enabled(current_restriction.get("on"))
            == is_enabled(wanted.get("on"))
            and current_restriction.get("ip") == wanted.get("ip")
        ):
            return False

        # A firmware that names no model type has nothing to switch away from,
        # and with the slave off no device reads the other models.
        current_mode = slave.get("sunspecMode")
        other_mode = current_mode is not None and current_mode != SUNSPEC_MODE_INT
        served = slave.get("mode") != SLAVE_OFF
        if other_mode and served and not switch_sunspec_mode:
            raise SunSpecModeChangeNeeded(str(current_mode))

        # Everything read is written back: the RS485 roles, serial settings and
        # a "both" mode belong to other devices on the inverter.
        payload = {
            "master": master,
            "slave": {
                **slave,
                "rtuif": slave.get("rtuif", []),
                "mode": _mode_with_tcp(slave.get("mode")),
                "port": port,
                "sunspecMode": SUNSPEC_MODE_INT,
                "meterAddress": meter_address,
                "rtu_inverter_slave_id": inverter_unit_id,
                "ctr": {**ctr, "on": True, "restriction": wanted},
            },
        }
        await self._post(MODBUS_PATH, payload)
        _LOGGER.info(
            "Enabled Modbus TCP via the web API (port=%s inverter_id=%s meter_id=%s restriction=%s)",
            port,
            inverter_unit_id,
            meter_address,
            is_enabled(wanted.get("on")),
        )
        return True

    async def get_battery_config(self) -> dict[str, Any]:
        return await self._get_json("/api/config/batteries")

    async def set_solar_api_enabled(self, enabled: bool) -> bool:
        """Switch the Solar API, leaving the owner's discovery switch as it is.

        Switching off clears it too: a discovered device would switch the API
        straight back on.
        """
        current = _config_object(await self.get_solar_api_config(), SOLAR_API_PATH)
        wanted: dict[str, Any] = {"SolarAPIv1Enabled": bool(enabled)}
        if not enabled:
            wanted["activeOnExternalDevicesDiscovered"] = False
        if all(_same(current.get(key), value) for key, value in wanted.items()):
            return False
        # Written whole: only the battery endpoint is known to take single fields.
        await self._post(SOLAR_API_PATH, {**current, **wanted})
        return True

    async def reset_modbus_control(self) -> bool:
        await self._post("/api/commands/ModbusReset")
        return True

    async def set_battery_config(self, mode: int) -> bool:
        """Self-consumption optimisation only; the SoC window has its own switch."""
        return await self._post_changes(BATTERIES_PATH, {"HYB_EM_MODE": mode})

    async def set_battery_power(self, power: int) -> bool:
        """The target in manual self-consumption optimisation, without the mode."""
        return await self._post_changes(BATTERIES_PATH, {"HYB_EM_POWER": power})

    async def set_soc_limits(
        self, soc_min: int | None = None, soc_max: int | None = None
    ) -> bool:
        """The SoC limit asked for, checked against the window the inverter holds now.

        The other limit is not sent: taken from the last poll it undid a change
        made in the inverter UI since.
        """
        current = await self.check_soc_window(soc_min, soc_max)
        limits = {"BAT_M0_SOC_MIN": soc_min, "BAT_M0_SOC_MAX": soc_max}
        wanted = {key: int(value) for key, value in limits.items() if value is not None}
        return await self._post_changes(BATTERIES_PATH, wanted, current)

    async def check_soc_window(
        self, soc_min: int | None = None, soc_max: int | None = None
    ) -> dict[str, Any]:
        """Refuse a limit that would invert the window the inverter holds now.

        A config that cannot be read is an error, not an empty window: read as
        empty it passed, and Modbus took a minimum the web API then refused.
        Returns the config read.
        """
        current = _config_object(await self._get_json(BATTERIES_PATH), BATTERIES_PATH)
        lower = soc_min if soc_min is not None else current.get("BAT_M0_SOC_MIN")
        upper = soc_max if soc_max is not None else current.get("BAT_M0_SOC_MAX")
        if not (_is_number(lower) and _is_number(upper)):
            # Unknown is not "fits": Modbus would take a minimum the web API
            # may refuse.
            raise FroniusWebResponseError(
                f"HTTP 200 on {BATTERIES_PATH} answered no SoC window", HTTPStatus.OK
            )
        if cast(float, lower) > cast(float, upper):
            raise ControlRefused(
                "soc_minimum_above_maximum", "SoC Minimum must not exceed SoC Maximum"
            )
        return current

    async def set_soc_mode(self, mode: str) -> bool:
        return await self._post_changes(BATTERIES_PATH, {"BAT_M0_SOC_MODE": mode})

    async def set_backup_reserve(self, percent: int) -> bool:
        return await self._post_changes(
            BATTERIES_PATH, {"HYB_BACKUP_RESERVED": int(percent)}
        )

    async def set_battery_charge_sources(
        self,
        charge_from_grid: bool | None = None,
        charge_from_ac: bool | None = None,
    ) -> bool:
        """Only the flags given; a flag not given stays as the inverter holds it."""
        flags = {
            "HYB_EVU_CHARGEFROMGRID": charge_from_grid,
            "HYB_BM_CHARGEFROMAC": charge_from_ac,
        }
        wanted = {key: bool(value) for key, value in flags.items() if value is not None}
        return await self._post_changes(BATTERIES_PATH, wanted)

    async def get_export_limit_config(self) -> dict[str, Any]:
        """Read current Export Limit Control configuration from the inverter."""
        try:
            return await self._get_json("/api/config/limit_settings/powerLimits")
        except FroniusWebResponseError as err:
            # Firmware without the endpoint answers 404; any other status is a
            # failure the refresh has to report.
            if err.status_code != HTTPStatus.NOT_FOUND:
                raise
            return {}

    async def set_export_soft_limit(self, power_w: int) -> bool:
        """Set the export soft limit in watts; whether it was written.

        Writes back the config just read, with only the switch and the watts patched.
        """
        path = "/api/config/limit_settings/powerLimits"
        config = await self._get_json(path)
        try:
            active_power = config["exportLimits"]["activePower"]
            soft = active_power["softLimit"]
            enabled = soft.get("enabled")
        except (KeyError, TypeError, AttributeError) as err:
            raise FroniusWebResponseError(
                f"HTTP 200 on {path} answered no export soft limit", HTTPStatus.OK
            ) from err
        require_power_control(active_power)
        holder, key = soft_limit_location(active_power)
        if is_enabled(enabled) and _same(holder.get(key), int(power_w)):
            return False
        soft["enabled"] = True
        holder[key] = int(power_w)
        await self._post(path, config)
        return True
