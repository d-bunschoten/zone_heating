# Voorbeeld-dashboard voor Zone Heating

`zone_heating.yaml` is een **voorbeeld**-Lovelace-dashboard. Het volgt de keuze
uit de spec (`verwarming-specificaties.md` §4 en §10, besluit 6): standaard
`thermostat`-kaarten plus een config-sectie met de losse helper-entiteiten, en
nadrukkelijk **geen custom-kaart**.

## Wat het toont

- **Globale sectie** (vaste entity-id's, één keer voor het hele huis):
  - `select.heating_mode` — master-stand (`auto` / `fallback` / `off`).
  - `number.heating_boost_duration`, `number.heating_override_timeout` — de
    globale knoppen (de boost-*temperatuur* is per zone, zie hieronder).
  - `binary_sensor.heating_home_present` en `binary_sensor.heating_in_fallback`
    — alleen-lezen status.
- **Per zone** een `thermostat`-kaart op `climate.<zone>` naast een
  `entities`-kaart met:
  - de dagelijkse knoppen `number.<zone>_away_temp` / `_standby_temp` /
    `_active_temp` / `_boost_temp`, en de optionele
    `schedule.<zone>_*_schedule`-helpers;
  - een alleen-lezen rij met `sensor.<zone>_control_source`,
    `binary_sensor.<zone>_present` en `binary_sensor.<zone>_window_open`.

De thermostaat en de config staan per zone naast elkaar via `horizontal_stack`.

## Installeren

Twee manieren (zie ook het kop-commentaar in `zone_heating.yaml`):

1. **Snel proberen:** Instellingen → Dashboards → "+ Dashboard toevoegen" →
   "Nieuw dashboard vanaf nul" → open het dashboard → menu (3 puntjes) →
   "Onbewerkte configuratie-editor" → plak de inhoud van `zone_heating.yaml`
   (vanaf `title:`).
2. **Als YAML-dashboard** in `configuration.yaml`:

   ```yaml
   lovelace:
     mode: storage
     dashboards:
       zone-heating:
         mode: yaml
         title: Verwarming
         icon: mdi:radiator
         filename: dashboards/zone_heating.yaml
         show_in_sidebar: true
   ```

## Zones zijn dynamisch — per zone kopiëren

De integratie heeft geen vaste zones. Elke zone voeg je los toe via de
config-/options-flow; HA leidt de per-zone entity-id's af van de **zonenaam**
(het `object_id` van het zone-device). "Woonkamer" wordt dus `climate.woonkamer`,
`number.woonkamer_away_temp`, enzovoort.

Een extra zone op het dashboard zetten:

1. Kopieer één heel `horizontal_stack`-blok in `zone_heating.yaml`.
2. Vervang overal het zone-deel van de entity-id (bv. `woonkamer` →
   `slaapkamer`). Spaties en hoofdletters worden in een entity-id kleine letters
   met underscores ("Zolder voor" → `zolder_voor`). De exacte id per zone staat
   onder Instellingen → Entiteiten.
3. Heeft de zone geen `schedule`-helpers (die zijn optioneel en maak je zelf
   aan), laat dan die `schedule.*`-rijen weg — zoals in het voorbeeld bij de
   zone "keuken".
