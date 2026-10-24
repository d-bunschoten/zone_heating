# Zone Heating

A Home Assistant custom integration that orchestrates heating **per zone** on top
of your existing thermostats. It drives [Tado][tado] zones through the
`tado_hijack` integration; the backend is abstracted (a generic `climate` stub is
in place as the extension point for other systems), but the Tado backend is the
one wired today — so `tado_hijack` is currently required.

A *zone* is a logical room this integration invents: it groups one or more
underlying `climate` entities (e.g. a living room = room thermostat + three TRVs)
and drives them as one. Each zone becomes its own Home Assistant device with its
own `climate.<zone>` entity, temperature knobs and status sensors, while a single
global *hub* carries the house-wide controls.

> **Status — pre-production.** The integration is feature-complete and covered by
> a pure-engine + mocked-service test suite (CI: tests + hassfest), and it loads
> cleanly in an isolated Home Assistant. It has **not** yet been validated
> end-to-end against a live Tado account. Use it with that in mind.

## Why

Tado's own schedules are rigid and its "away" handling is coarse. Zone Heating
keeps the thermostat as the executor but moves the *decisions* into Home
Assistant: real presence per room with debounce, a window-open modifier, a
derived frost/standby baseline, temporary boosts and manual overrides, and a safe
fallback to the thermostat's own schedule if Home Assistant ever goes down.

All decision logic lives in a pure-Python, Home-Assistant-agnostic `engine/`
package so it can be unit-tested in isolation, with a thin HA layer around it and
a backend abstraction (a `tado_hijack` backend plus a generic `climate` stub).

## How it works

**The thermostat's own schedule stays underneath as a safety net.** Home Assistant
drives a zone with cheap, temporary *overlays*; the derived baseline it writes to
the thermostat is the floor it falls back to. Day-to-day steering is all overlays —
the baseline is (re)written only rarely.

### Master mode (`select.heating_mode`)

A single house-wide switch with three values:

- **Managed** — Home Assistant runs the per-zone state machine and asserts
  overlays (with the dead-man's-switch). You can still steer any individual zone
  by hand; that is why it is called *Managed* rather than *Auto*.
- **Fallback** — Home Assistant lets go and the thermostat runs its own baseline
  schedule; HA only observes. Switching to Fallback resumes each zone onto its
  schedule immediately.
- **Off** — each zone is set to a flat frost-protection baseline and the state
  machine is paused.

In Fallback and Off the per-zone machine is paused, so per-zone controls are
ignored until you return to Managed.

### Per zone

Each zone is a standard `climate` entity, so it works with the normal thermostat
card:

- **`hvac_mode`**: `auto` (the integration chooses the stand), `heat` (a manual
  stand) or `off`.
- **`preset_mode`**: standard Home Assistant presets — **away**, **eco**
  (standby), **comfort** (active) and **boost** — so the card shows proper icons.
  Dialing a temperature by hand (or turning the physical knob) becomes **Custom**.
- **`zone_state`** attribute surfaces the real stand: Away / Standby / Active /
  Boost / Custom / Window-open / Off / Fallback.

In `auto`, the stand is chosen from debounced presence and the master mode: **Away**
(house away), **Active** (room in use) or **Standby** (otherwise). Optional per-zone
`schedule` helpers vary the temperature per time block.

## Features

- **Custom detection** — a turn of the physical knob (or any external setpoint
  change) is detected via a shadow-state algorithm and respected as *Custom* for a
  configurable respect-window, instead of being fought by Home Assistant.
- **Dead-man's-switch** — when on, HA re-asserts its overlays on a heartbeat that
  sits well inside the overlay-termination window; if HA crashes, the overlay
  lapses and the thermostat falls back to its own schedule instead of freezing on a
  stale setpoint. Termination is configurable (next schedule block, or a fixed timer).
- **Derived baseline** — the normal baseline is derived from the zone's
  `standby_schedule` + `standby_temp` (not configured separately) and written in
  the schedule's natural shape (one write for a uniform week; grouped per-day
  otherwise), deduplicated so a reload only rewrites a baseline that actually
  changed.
- **Window modifier** — an open window turns the zone off after a short open-delay
  (1 min by default); closing waits out a close-delay (5 min by default) so a
  briefly cracked window does not whipsaw the setpoint.
- **Boost & override** — a **per-zone** boost temperature with a global boost
  duration, and a respect-window timeout for manual overrides (shown as a
  timestamp sensor per zone).
- **Dynamic entities** — a zone only exposes the sensors that apply: the presence
  and window binary sensors appear only when a source is configured, and linked
  `schedule` helpers are grouped onto the zone's device.

## Installation

### Via HACS

1. In HACS, add `https://github.com/d-bunschoten/zone_heating` as a **custom
   repository** with category *Integration*.
2. Install **Zone Heating** and restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for
   *Zone Heating*.

### Manual

Copy `custom_components/zone_heating/` into your Home Assistant
`config/custom_components/` directory and restart Home Assistant.

## Requirements

- Home Assistant 2026.10.0 or newer.
- The **`tado_hijack`** HACS integration, set up for your Tado account. Zone
  Heating drives Tado through it — its `set_mode` / `set_schedule` services and the
  per-zone schedule switch. It is declared only as a soft `after_dependencies`, so
  Home Assistant sets `tado_hijack` up first when it is present (its services are
  ready before Zone Heating writes), while Zone Heating still loads if `tado_hijack`
  is absent or briefly unavailable — set it up first for things to actually work.

## Configuration

### 1. The hub

Adding the integration creates a single **hub** entry (only one is allowed). Open
its configuration (**Configure**) to set the global options:

- **Home presence entity / template** — drives house-wide *Away*.
- **Home presence on/off-delay** — debounce for the house signal.
- **Dead-man's-switch** — on by default.
- **Overlay termination** — *Next schedule block* or *Fixed timer* (+ duration).

> **Away only engages with a configured home source.** Without a home presence
> entity or template, `binary_sensor.heating_home_present` stays `on`, so *Away*
> never triggers and zones only move between Standby and Active. This is a
> deliberate, safe default — set a home source if you want Away to engage.

### 2. Zones

Each zone is added as a **subentry** of the hub (**Add heating zone**) and becomes
its own device. Per zone you configure:

- **Name** and **target climate entities** (for a Tado room, pick the room
  thermostat; it cascades to its TRVs).
- **Presence entity / template** + **on/off-delay** — per-room presence.
- **Window sensor** + **open/close-delay** — the window modifier.
- **Schedule switch** — the `tado_hijack` schedule switch for the zone, used to
  mirror whether the zone runs its schedule or an HA overlay.
- **Standby / Active schedule helpers** — optional `schedule` helpers that vary the
  temperature per time block. **You create these helpers yourself** (Settings →
  Devices & Services → Helpers → Schedule); each on-block may carry a `temperature`
  in its `data`. For the Active schedule you can choose whether it also *gates* when
  Active is allowed, or just supplies the temperature.
- **Off / frost-protection temperature** — the flat baseline written in master
  mode Off (5 °C by default).

The daily per-zone knobs (`away_temp`, `standby_temp`, `active_temp`,
`boost_temp`) and the status sensors appear as entities on the zone's device after
setup.

## Dashboard

An example Lovelace dashboard lives in [`dashboards/`](dashboards/) — standard
`thermostat` cards plus a per-zone settings section, grouped by stand. See
[`dashboards/README.md`](dashboards/README.md) for how to install it.

## Development

- Tests: `uv run --group test pytest` (pure-engine unit tests + mocked-service
  integration tests).
- The `engine/` package holds the import-pure decision logic; everything that
  touches Home Assistant lives in the thin glue around it.

## License

MIT — see [LICENSE](LICENSE).

[tado]: https://www.tado.com/
