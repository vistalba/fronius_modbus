from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.components.repairs.models import RepairsFlowResult
from homeassistant.core import HomeAssistant
import voluptuous as vol
from homeassistant.helpers import issue_registry as ir

from .coordinator import FroniusRuntimeData
from .const import (
    DOMAIN,
    MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX,
    SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX,
)


class FroniusDisableSolarApiRepairFlow(RepairsFlow):
    """Repair flow that disables Solar API on the inverter."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id
        self._pending_flow_state = None

    def _fronius_issue_id(self) -> str:
        """The issue this flow repairs.

        Not `_issue_id`: Home Assistant 2026.10 keeps the issue id of a
        repairs flow in an attribute of that name.
        """
        return f"{SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX}{self._entry_id}"

    def _resolve_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._fronius_issue_id())

    def _description_placeholders(self) -> dict[str, str] | None:
        issue = ir.async_get(self.hass).async_get_issue(
            DOMAIN, self._fronius_issue_id()
        )
        return issue.translation_placeholders if issue else None

    async def _async_finish_repair(self) -> RepairsFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            self._resolve_issue()
            return self.async_create_entry(title="", data={})

        runtime: FroniusRuntimeData | None = getattr(entry, "runtime_data", None)
        web_control = None if runtime is None else runtime.web_control
        web = None if runtime is None else runtime.web
        if web_control is None or not web_control.configured or web is None:
            raise RuntimeError("Fronius Web API is not configured")

        await web_control.set_solar_api_enabled(False)
        await web.async_refresh()
        if web.data.solar_api_enabled is not False:
            raise RuntimeError("Solar API disable could not be confirmed")
        self._resolve_issue()
        return self.async_create_entry(title="", data={})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        del user_input
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            self._resolve_issue()
            return self.async_create_entry(title="", data={})

        return self.async_show_menu(
            step_id="init",
            menu_options=["fix", "ignore"],
            description_placeholders=self._description_placeholders(),
        )

    async def async_step_fix(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        del user_input
        return await self.async_step_confirm()

    async def async_step_ignore(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        del user_input
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            self._resolve_issue()
            return self.async_create_entry(title="", data={})

        ir.async_ignore_issue(self.hass, DOMAIN, self._fronius_issue_id(), True)
        return self.async_abort(reason="issue_ignored")

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        description_placeholders = self._description_placeholders()
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                # voluptuous, which the minimum Home Assistant 2026.9 types
                # this argument with; 2026.10 types it with probatio and
                # takes either.
                data_schema=vol.Schema({}),  # type: ignore[arg-type]
                description_placeholders=description_placeholders,
            )
        try:
            return await self._async_finish_repair()
        except Exception:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),  # type: ignore[arg-type]
                description_placeholders=description_placeholders,
                errors={"base": "cannot_connect"},
            )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create fix flow for a Fronius repairs issue."""
    if issue_id.startswith(SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX):
        entry_id = str(
            (data or {}).get("entry_id")
            or issue_id.removeprefix(SOLAR_API_LOW_FIRMWARE_ISSUE_ID_PREFIX)
        )
        return FroniusDisableSolarApiRepairFlow(entry_id)

    if issue_id.startswith(MIGRATION_RECONFIGURE_ISSUE_ID_PREFIX):
        # Left by an earlier version on an entry not set up since, such as a
        # disabled one: the reauthentication replaced it.
        return ConfirmRepairFlow()

    raise ValueError(f"Unknown issue: {issue_id}")
