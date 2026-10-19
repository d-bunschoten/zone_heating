"""Lightweight checks on the example Lovelace dashboard (F6, spec §4/§10).

Pure YAML: no Home Assistant runtime is needed. We only assert that the shipped
example parses and that its structural promises hold, so a typo in the example
dashboard is caught by the suite rather than in the user's browser.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DASHBOARD = (
    Path(__file__).resolve().parent.parent / "dashboards" / "zone_heating.yaml"
)


def _iter_cards(node: object):
    """Yield every card dict in the dashboard, recursing into nested stacks."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_cards(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_cards(item)


def _load() -> dict:
    return yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))


def test_dashboard_is_valid_yaml() -> None:
    """(a) The example dashboard is valid YAML with a views list."""
    config = _load()
    assert isinstance(config, dict)
    assert isinstance(config.get("views"), list) and config["views"]


def test_thermostat_cards_target_climate_entities() -> None:
    """(b) Every `type: thermostat` card points at a `climate.` entity."""
    config = _load()
    thermostats = [
        card
        for card in _iter_cards(config)
        if isinstance(card, dict) and card.get("type") == "thermostat"
    ]
    # Two example zones ship in the dashboard.
    assert len(thermostats) >= 2
    for card in thermostats:
        entity = card.get("entity")
        assert isinstance(entity, str)
        assert entity.startswith("climate."), entity


def test_global_master_mode_present() -> None:
    """(c) The global select.heating_mode knob is on the dashboard."""
    entities = {
        card.get("entity")
        for card in _iter_cards(_load())
        if isinstance(card, dict)
    }
    assert "select.heating_mode" in entities
