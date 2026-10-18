# Zone Heating

A Home Assistant custom integration that orchestrates heating zones.

> **Status: scaffold / work in progress (phase F0).**
> This repository currently contains only a clean, loadable integration
> skeleton. No heating logic, entities, or platforms are implemented yet.

## What it is

Zone Heating is a hub-style integration (domain `zone_heating`) intended to
coordinate heating across multiple zones in a home: baseline schedules,
temporary overrides, and a per-zone state machine. The orchestration logic lives
in a pure-Python, Home Assistant-agnostic `engine/` package so it can be tested
independently.

## Install

### Via HACS (custom repository)

1. In HACS, add `https://github.com/dariob/zone_heating` as a custom repository
   (category: *Integration*).
2. Install **Zone Heating**.
3. Restart Home Assistant.
4. Go to **Settings -> Devices & Services -> Add Integration** and search for
   *Zone Heating*.

### Manual

Copy `custom_components/zone_heating/` into your Home Assistant `config/custom_components/`
directory and restart Home Assistant.

## Requirements

- Home Assistant 2026.10.0 or newer.

## License

MIT - see [LICENSE](LICENSE).
