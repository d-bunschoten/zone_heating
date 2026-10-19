# Zone Heating

A Home Assistant custom integration that orchestrates heating **per zone** on top
of your existing thermostats (built with [Tado][tado] via `tado_hijack` in mind,
but it drives any `climate` entity).

A *zone* is a logical room this integration invents: it groups one or more
underlying `climate` entities (e.g. a living room = room thermostat + three TRVs)
and drives them as one. Each zone becomes its own Home Assistant device with its
own `climate.<zone>` entity, temperature knobs and status sensors, while a single
global *hub* carries the house-wide controls.

> **Status.** All phases of the implementation plan are built (hub + subentries,
> manual control, master modes, fallback, dead-man's-switch, derived baselines,
> window handling, example dashboard). It is covered by a mock/engine test suite
> and a load smoke-test, but it has **not** yet been validated end-to-end against
> a live Tado. That happens in a later migration phase. Treat it as feature-complete
> but pre-production.

## Why

Tado's own schedules are rigid and its "away" handling is coarse. Zone Heating
keeps Tado as the executor but moves the *decisions* into Home Assistant: real
presence per room with debounce, a window-open modifier, a derived frost/standby
baseline, temporary boosts and overrides, and a safe fallback to Tado's own
schedule if Home Assistant ever goes down. All logic lives in a pure-Python,
HA-agnostic `engine/` package so it can be unit-tested in isolation.

## Features

- **Master modes** (`select.heating_mode`): `auto` (HA drives everything),
  `fallback` (hand control back to the thermostat's own schedule), `off`
  (write a flat frost-protection baseline per zone).
- **Per-zone state machine**: Away / Standby / Active, driven by debounced
  presence, optional per-block schedule helpers, and the master mode.
- **Custom detection**: a turn of the physical knob (or any external setpoint
  change) is detected via a shadow-state algorithm and respected as *Custom*
  for a configurable window, instead of being fought by HA.
- **Dead-man's-switch**: when on, HA re-asserts its overlays on a heartbeat that
  sits well inside the overlay-termination window; if HA crashes, the overlay
  lapses and the thermostat falls back to its own schedule instead of freezing
  on a stale setpoint.
- **Derived baseline**: the normal baseline written to Tado is derived from
  `standby_schedule` + `standby_temp` (not configured separately), deduplicated
  so a reload only rewrites a baseline that actually changed — no needless cloud
  calls.
- **Window modifier**: an open window cuts heating (immediately by default for
  safety); closing waits out a close-delay so a briefly cracked window does not
  whipsaw the setpoint.
- **Boost / override**: global boost temperature + duration, and a respect-window
  timeout for manual overrides.

## Installation

### Via HACS (custom repository)

1. In HACS, add this repository as a **custom repository** with category
   *Integration*.
2. Install **Zone Heating**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for
   *Zone Heating*.

> The repository is currently local and not yet published on GitHub, so the
> `documentation`/`issue_tracker` links in `manifest.json` are placeholders.
> Replace them (and point HACS at the real repo) once it is published.

### Manual

Copy `custom_components/zone_heating/` into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

## Requirements

- Home Assistant 2026.10.0 or newer.

## Configuration

### 1. The hub

Adding the integration creates a single **hub** entry (only one is allowed). Open
its configuration (**Configure**) to set the global options:

- **Home presence entity / template** — drives house-wide *Away*.
- **Home presence on/off-delay** — debounce for the house signal.
- **Dead-man's-switch** — on by default.
- **Overlay termination** — `Next schedule block` or `Fixed timer` (+ duration).

> **Away only works with a configured home source.** Without a home presence
> entity or template, `binary_sensor.heating_home_present` stays `on`, so *Away*
> never triggers and zones only move between Standby and Active (spec §1.2). This
> is a deliberate, safe default — set a home source if you want Away to engage.

### 2. Zones

Each zone is added as a **subentry** of the hub (**Add heating zone**), and each
becomes its own device. Per zone you configure:

- **Name** and **target climate entities** (for a Tado room, pick the room
  thermostat; it cascades to its TRVs).
- **Presence entity / template** + **on/off-delay** — per-room presence.
- **Window sensor** + **open/close-delay** — the window modifier.
- **`schedule_switch`** — the `tado_hijack` schedule switch for the zone. Leave it
  empty to derive `switch.<target>_schedule` from the first target; set it
  explicitly if the derivation does not match.
- **Standby / Active schedule helpers** — optional `schedule` helpers that vary
  the temperature per time block. **You create these helpers yourself** (Settings
  → Devices & Services → Helpers → Schedule) and each on-block may carry a
  `temperature` in its `data`. The Active schedule additionally *gates* when
  Active is allowed.
- **Off / frost-protection temperature** — the flat baseline written in master
  mode Off.

The remaining per-zone knobs (`away_temp`, `standby_temp`, `active_temp`) and
status sensors appear as entities on the zone's device after setup.

## Dashboard

An example Lovelace dashboard lives in [`dashboards/`](dashboards/) — standard
`thermostat` cards plus a config section with the loose helper entities, and
deliberately no custom card. See [`dashboards/README.md`](dashboards/README.md)
for how to install it.

## Design & specification

The full design and specification (in Dutch) live alongside the local Home
Assistant workspace under `ha-local/docs/`:

- `verwarming-specificaties.md` — the leading specification (single source of
  truth): entities, config, state machine, algorithms, defaults.
- `verwarming-ontwerp.md` — the design it is derived from.
- `verwarming-implementatieplan.md` — the phased implementation plan (F0–F7).
- `verwarming-inventarisatie.md` — the inventory of the existing setup.

## License

MIT — see [LICENSE](LICENSE).

[tado]: https://www.tado.com/
