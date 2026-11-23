"""The quality scale file lists every rule, and what it claims the code can back.

Hassfest validates `quality_scale.yaml` only for integrations in core, so a
custom integration's file is checked by nobody unless this test does it. The
rule list is the one hassfest knows (`script/hassfest/quality_scale.py` of
core); a rule added there and missing here turns this test red once copied in.
"""

import json
import pathlib
import tomllib

import pytest
import yaml

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "fronius_modbus"
)
QUALITY_SCALE = PACKAGE / "quality_scale.yaml"

RULES = {
    "bronze": (
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow",
        "config-flow-test-coverage",
        "dependency-transparency",
        "docs-actions",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "docs-triggers",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    ),
    "silver": (
        "action-exceptions",
        "config-entry-unloading",
        "docs-configuration-parameters",
        "docs-installation-parameters",
        "entity-unavailable",
        "integration-owner",
        "log-when-unavailable",
        "parallel-updates",
        "reauthentication-flow",
        "test-coverage",
    ),
    "gold": (
        "devices",
        "diagnostics",
        "discovery",
        "discovery-update-info",
        "docs-data-update",
        "docs-examples",
        "docs-known-limitations",
        "docs-supported-devices",
        "docs-supported-functions",
        "docs-troubleshooting",
        "docs-use-cases",
        "dynamic-devices",
        "entity-category",
        "entity-device-class",
        "entity-disabled-by-default",
        "entity-translations",
        "exception-translations",
        "icon-translations",
        "reconfiguration-flow",
        "repair-issues",
        "stale-devices",
    ),
    "platinum": ("async-dependency", "inject-websession", "strict-typing"),
}
ALL_RULES = {rule for rules in RULES.values() for rule in rules}


def _rules() -> dict[str, dict]:
    raw = yaml.safe_load(QUALITY_SCALE.read_text(encoding="utf-8"))["rules"]
    return {
        name: value if isinstance(value, dict) else {"status": value}
        for name, value in raw.items()
    }


def _done(rule: str) -> bool:
    return _rules()[rule]["status"] == "done"


def _source(name: str) -> str:
    return (PACKAGE / name).read_text(encoding="utf-8")


def test_the_file_lists_every_rule_of_the_scale_and_no_other():
    assert set(_rules()) == ALL_RULES
    assert len(ALL_RULES) == 54


def test_every_rule_has_a_valid_status_and_every_exemption_a_reason():
    for name, rule in _rules().items():
        assert rule["status"] in {"done", "todo", "exempt"}, name
        if rule["status"] == "exempt":
            assert rule.get("comment", "").strip(), f"{name} is exempt without a reason"


def test_a_rule_still_to_do_says_what_is_missing():
    """A bare todo is a promise nobody can check against the code."""
    for name, rule in _rules().items():
        if rule["status"] == "todo":
            assert rule.get("comment", "").strip(), name


# What hassfest verifies in core when a rule is marked done, done here.


def test_a_done_config_flow_is_declared_and_present():
    if not _done("config-flow"):
        pytest.skip("not claimed")
    manifest = json.loads(_source("manifest.json"))
    assert manifest["config_flow"] is True
    assert (PACKAGE / "config_flow.py").exists()


def test_a_done_discovery_is_declared():
    if not _done("discovery"):
        pytest.skip("not claimed")
    manifest = json.loads(_source("manifest.json"))
    assert manifest.get("zeroconf") or "async_step_zeroconf" in _source(
        "config_flow.py"
    )


def test_a_done_reconfiguration_flow_has_its_step():
    if not _done("reconfiguration-flow"):
        pytest.skip("not claimed")
    assert "async def async_step_reconfigure(" in _source("config_flow.py")


def test_done_runtime_data_is_assigned_at_setup():
    if not _done("runtime-data"):
        pytest.skip("not claimed")
    assert "entry.runtime_data = " in _source("__init__.py")


def test_a_done_integration_owner_is_a_codeowner():
    if not _done("integration-owner"):
        pytest.skip("not claimed")
    assert json.loads(_source("manifest.json"))["codeowners"]


def test_a_done_unique_config_entry_aborts_a_known_device():
    if not _done("unique-config-entry"):
        pytest.skip("not claimed")
    flow = _source("config_flow.py")
    assert "async_set_unique_id" in flow
    assert "_abort_if_unique_id_configured" in flow


def test_a_done_reauthentication_flow_has_its_step():
    if not _done("reauthentication-flow"):
        pytest.skip("not claimed")
    assert "async def async_step_reauth(" in _source("config_flow.py")


def test_done_diagnostics_have_their_module():
    if not _done("diagnostics"):
        pytest.skip("not claimed")
    assert "async_get_config_entry_diagnostics" in _source("diagnostics.py")


def test_done_stale_devices_let_the_owner_remove_one():
    if not _done("stale-devices"):
        pytest.skip("not claimed")
    assert "async def async_remove_config_entry_device(" in _source("__init__.py")


def test_a_done_async_dependency_leaves_nothing_to_the_executor():
    """The hint the rule names: the library called through async_add_executor_job."""
    if not _done("async-dependency"):
        pytest.skip("not claimed")
    client = _source("froniuswebclient.py")
    assert "import requests" not in client
    assert "import aiohttp" in client
    for name in ("__init__.py", "config_flow.py", "web_control.py"):
        source = _source(name)
        assert "async_add_executor_job" not in source, name


def test_a_done_inject_websession_passes_a_session_to_every_client():
    if not _done("inject-websession"):
        pytest.skip("not claimed")
    for name in ("__init__.py", "config_flow.py"):
        source = _source(name)
        constructions = source.count("FroniusWebClient(")
        assert constructions
        assert source.count("session=async_web_session(hass)") >= constructions, name


def test_a_done_strict_typing_runs_mypy_strictly_over_every_module():
    if not _done("strict-typing"):
        pytest.skip("not claimed")
    configuration = tomllib.loads(
        (PACKAGE.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    mypy = configuration["tool"]["mypy"]
    checked = {pathlib.Path(name).name for name in mypy["files"]}
    assert mypy.get("strict") is True
    assert {source.name for source in PACKAGE.glob("*.py")} <= checked
