"""Per-zone boost temperature (F5.4, spec §1.1, §3).

The former global ``number.heating_boost_temp`` is gone; each zone now has its
own ``number.<zone>_boost_temp`` (default 23). A forced ``boost`` preset must use
*that zone's* value, so two zones can boost to different comfort setpoints while
the boost *duration* stays global.
"""

from __future__ import annotations

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
)
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.zone_heating.backend_tado import (
    ATTR_HVAC_MODE,
    TADO_DOMAIN,
    TADO_SERVICE_SET_MODE,
    TADO_SERVICE_SET_SCHEDULE,
)
from custom_components.zone_heating.const import (
    CONF_NAME,
    CONF_OFF_DELAY,
    CONF_ON_DELAY,
    CONF_TARGETS,
    DEFAULT_BOOST_TEMP,
    DOMAIN,
    SUBENTRY_TYPE_ZONE,
)

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

LIVING_TARGET = "climate.living"
STUDY_TARGET = "climate.study"
LIVING_CLIMATE = "climate.woonkamer"
STUDY_CLIMATE = "climate.studeerkamer"
LIVING_BOOST = "number.woonkamer_boost_temp"
STUDY_BOOST = "number.studeerkamer_boost_temp"


def _zone(name: str, target: str) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={
            CONF_NAME: name,
            CONF_TARGETS: [target],
            CONF_ON_DELAY: 0,
            CONF_OFF_DELAY: 0,
        },
        subentry_type=SUBENTRY_TYPE_ZONE,
        title=name,
        unique_id=None,
    )


async def _setup(hass: HomeAssistant) -> list[ServiceCall]:
    hass.states.async_set(LIVING_TARGET, "heat", {ATTR_TEMPERATURE: 18.0})
    hass.states.async_set(STUDY_TARGET, "heat", {ATTR_TEMPERATURE: 18.0})
    calls = async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_MODE)
    async_mock_service(hass, TADO_DOMAIN, TADO_SERVICE_SET_SCHEDULE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Zone Heating",
        unique_id="zone_heating_hub",
        subentries_data=[
            _zone("Woonkamer", LIVING_TARGET),
            _zone("Studeerkamer", STUDY_TARGET),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return calls


async def _set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: entity_id, "value": value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _boost(hass: HomeAssistant, climate: str) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: climate, ATTR_PRESET_MODE: "boost"},
        blocking=True,
    )
    await hass.async_block_till_done()


def _last_temp_for(calls: list[ServiceCall], target: str) -> float:
    """The temperature of the most recent set_mode(heat) that hit ``target``."""
    for call in reversed(calls):
        if call.data.get(ATTR_HVAC_MODE) != "heat":
            continue
        entities = call.data[ATTR_ENTITY_ID]
        if target in (entities if isinstance(entities, list) else [entities]):
            return call.data[ATTR_TEMPERATURE]
    raise AssertionError(f"no heat set_mode for {target}")


async def test_each_zone_has_boost_temp_number(hass: HomeAssistant) -> None:
    """Every zone gets its own boost-temp number defaulting to 23."""
    await _setup(hass)
    assert float(hass.states.get(LIVING_BOOST).state) == DEFAULT_BOOST_TEMP
    assert float(hass.states.get(STUDY_BOOST).state) == DEFAULT_BOOST_TEMP


async def test_boost_uses_per_zone_temp(hass: HomeAssistant) -> None:
    """A boost uses that zone's own boost-temp; another zone keeps its default."""
    calls = await _setup(hass)
    # Only the living room gets a warmer boost; the study keeps the 23 default.
    await _set_number(hass, LIVING_BOOST, 25.0)

    await _boost(hass, LIVING_CLIMATE)
    await _boost(hass, STUDY_CLIMATE)

    assert _last_temp_for(calls, LIVING_TARGET) == 25.0
    assert _last_temp_for(calls, STUDY_TARGET) == DEFAULT_BOOST_TEMP
