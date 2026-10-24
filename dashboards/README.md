# Example dashboard for Zone Heating

`zone_heating.yaml` is an **example** Lovelace dashboard: standard `thermostat`
cards plus a per-zone settings section with the loose helper entities, and
deliberately no custom card.

## What it shows

- **Global section** (fixed entity ids, once for the whole house):
  - `select.heating_mode` — master mode (shown as *Managed* / *Fallback* / *Off*).
  - `number.heating_boost_duration`, `number.heating_override_timeout` — the global
    knobs (the boost *temperature* is per zone, see below).
  - `binary_sensor.heating_home_present` and `binary_sensor.heating_in_fallback` —
    read-only status.
- **Per zone** a `thermostat` card on `climate.<zone>` next to an `entities` card,
  grouped per stand (Away / Standby / Active / Boost): the daily knobs
  `number.<zone>_away_temp` / `_standby_temp` / `_active_temp` / `_boost_temp`, the
  optional `schedule.<zone>_*` helpers, and a read-only row with
  `sensor.<zone>_override_ends`, `sensor.<zone>_control_source`,
  `binary_sensor.<zone>_present` and `binary_sensor.<zone>_window_open`.

The thermostat and settings sit side by side per zone via `horizontal_stack`.

## Install

Two ways (see also the header comment in `zone_heating.yaml`):

1. **Quick try:** Settings → Dashboards → "+ Add dashboard" → "New dashboard from
   scratch" → open it → menu (⋮) → "Raw configuration editor" → paste the contents
   of `zone_heating.yaml` (from `title:` onward).
2. **As a YAML dashboard** in `configuration.yaml`:

   ```yaml
   lovelace:
     mode: storage
     dashboards:
       zone-heating:
         mode: yaml
         title: Heating
         icon: mdi:radiator
         filename: dashboards/zone_heating.yaml
         show_in_sidebar: true
   ```

## Zones are dynamic — copy per zone

The integration has no fixed zones. You add each zone through the config/options
flow; Home Assistant derives the per-zone entity ids from the **zone name** (the
zone device's `object_id`). So "Living room" becomes `climate.living_room`,
`number.living_room_away_temp`, and so on.

To add another zone to the dashboard:

1. Copy one whole `horizontal_stack` block in `zone_heating.yaml`.
2. Replace the zone part of every entity id (e.g. `woonkamer` → `slaapkamer`).
   Spaces and capitals become lowercase-with-underscores in an entity id. The
   exact id per zone is under Settings → Entities.
3. If a zone has no `schedule` helpers (they are optional and you create them
   yourself), drop those `schedule.*` rows — as in the "keuken" example zone.
