# CLAUDE.md

Guidance for AI assistants working in this repository.

## Start here (new session)
1. Read "Where things stand" below: what exists, what is stubbed, what comes next.
2. Run `git status`: work since the last commit is often uncommitted (the owner commits when
   they ask for it).
3. The plan of record is **Features/23 (Home Assistant integration)** in the panel repository
   `RoboAlarms/alarm-panel` (private; the owner's checkout usually lives next to this one as
   `../AlarmSystem`). Requirement IDs `HAI-NNN` come from there; Features/22 and /24 cover the
   two-way work. When behaviour changes here, update that doc and this file's status together.
4. **This repository is public.** Nothing owner-specific goes in it: no addresses, serial
   ports, codes, panel ids, network details. Those belong in the panel repo's git-ignored
   `PANEL.local.md`, never here.

## Shared continuity: Claude and Codex

The owner switches between assistants. `AGENTS.md` directs Codex to this same guide; keep
project rules and progress shared instead of maintaining a separate Codex status file.

At the start, read the current status here, inspect the working tree and recent commits, and
read the corresponding panel feature documents when available. Dated status is historical
unless checked against the current code and evidence. Preserve unfinished work from either
assistant and do not ask the owner to repeat context already recorded.

For substantial work, record the objective, scope and decisions before implementing, update
at meaningful checkpoints, and refresh "Where things stand" before ending or handing off.
Include changed files, completed and pending work, tests actually run and their results,
unrun checks or blockers, and the next concrete step. Distinguish edited, committed, released
and hardware-verified states. Keep Features/23 (and /22 or /24 when applicable) in the panel
repository in agreement; preserve old evidence under clearly historical headings.

This repository is public: keep credentials and owner-specific details out of these notes.
Do not use private assistant memory as the only record of essential context. No new
authorization to commit, publish, flash or push is implied by a handoff.

## What this is
The **HACS custom integration** (domain `roboalarms`) for the **RoboAlarms Panel** — an
open-source touchscreen alarm panel on the Elecrow CrowPanel Advanced 7" (ESP32-P4). The
project is RoboAlarms; the device is the RoboAlarms Panel (owner's decision, 2026-09-20).
Despite
the repository's name, this is **not** a Supervisor add-on: the add-on route was considered
and rejected (Features/23, "Why a HACS integration"). Milestones: **M12** (discovery, pairing,
entities, commands) and **M13** (two-way: HA entities as panel zones, panel device control).

| Path | What |
|---|---|
| `custom_components/roboalarms/` | the integration: manifest, config flow, strings; platforms arrive as they are built |
| `custom_components/roboalarms/brand/` | icon shown by HA/HACS (HA 2026.3+ serves it from here; home-assistant/brands no longer takes custom-integration PRs) |
| `tests/` | pytest with `pytest-homeassistant-custom-component` |
| `docs/BRAND.md`, `docs/brand/` | shared R/house logo source and the provenance of the 256/512px integration exports |
| `.github/workflows/` | `validate.yml` (hassfest + HACS action), `tests.yml` (pytest + ruff) |
| `hacs.json` | HACS metadata; `homeassistant` is the minimum HA version (2026.3.0, needed for the in-repo brand icon) |

## Where things stand (2026-10-06)

**Two fixes from a bench run on a development panel (Claude, 2026-10-06): checks passed;
committed 2026-10-07, not released.** Panel repo docs/features/23 validation.md part 2, B029, BL-173:
- **Keepalive (panel repo docs/bugs/B029):** a quiet house's link dropped every 95 s. The panel
  measures silence by what it receives and drops at 90 s; `_async_listen` pinged only after 30 s
  of *hearing* nothing, which never happened since the panel sends an update every 10 s. It now
  also pings after 30 s in which it *sent* nothing (`PanelClient.last_sent`, kept by `send`;
  `protocol/aioroboalarms.py` synced). Tests: `test_pings_while_the_panel_talks` (1 ping instead
  of >= 3 before the fix) and `test_pings_a_quiet_panel`.
- **A scene never run was unavailable:** its state is the time it last ran, "unknown" until it
  has; `_says_something()` keeps a scene's unknown available in the catalog and the state
  messages, so the panel no longer greys it and refuses a tap. Test:
  `test_a_scene_never_run_is_there` (failed before the fix).
- Checks then: 165 tests, Ruff clean, protocol sync check OK. Live: Home Assistant 2026.9.4 in
  Docker with both fixes, paired with the panel - no drop since, the scene runnable.

**The rest of BL-173 and the integration's side of BL-171 (Claude, 2026-10-06, later): checks
passed; committed 2026-10-07, not released; tried live with a panel that reads the values (panel repo
validation.md part 3).** Tests in
`tests/test_bench_findings.py`:
- **The panel's `ping` gets a `pong`** (`_async_handle` -> `_async_pong`; it was ignored).
  `test_answers_the_panels_ping`.
- **`changed_by`**: the alarm entity listens to this entry's `roboalarms_event`s; `armed`,
  `disarmed` and `auto_stay` for its partition set it to the event's `user_name`, else its
  `user` number, else None; silent events never (HAI-008).
  `test_changed_by_follows_arm_and_disarm_events`, `test_changed_by_ignores_another_panels_events`.
- **A code the panel won't take** (`invalid_code`, `not_authorized`, `code_required`,
  `locked_out`) raises `ServiceValidationError` with `translation_key` = the result name and
  text in `strings.json` "exceptions" (en, es; Home Assistant drops a trailing period, so the
  texts have none); other refusals stay `HomeAssistantError` with the panel's words.
  `test_a_refused_code_is_a_validation_error` (x8), `test_other_refusals_stay_as_they_were`;
  `test_init.py: test_refused_command_raises` now matches the new words.
- **Codes 4-10 digits** in `services.py` (the only 4-8 limit; services.yaml, the config flow and
  the entity had none). `test_services_take_codes_of_4_to_10_digits`.
- **`EVENT_TYPES`** gains the panel's 25 newer types (Z-Wave 9, Zigbee 9, backup 2, walk test 2,
  `clock_set`, `automation_failed`, `arming_put_off`): now exactly `alarm_event_name()`'s list.
  The entity doesn't translate event types, so no strings. `test_event_types_cover_the_panels_list`,
  `test_a_newer_event_reaches_the_event_entity`.
- **A device's values (BL-171)**: a `control` entity's catalog row and `state` message carry
  `level` (light brightness %, fan percentage, cover/valve position), `setpoint` and `temp`
  (climate, hundredths) and `modes` (climate `hvac_modes`), each only when known
  (`coordinator._control_values`); a light that is off but can dim (`brightness_supported`)
  says level 0, so the panel keeps its Brightness slider when the link comes up while it is
  off (`test_an_off_light_that_dims_says_0`). A state change in Home Assistant sends a `state` only when
  the message differs from the last one this connection sent for that entity (an attribute
  the panel doesn't get sends nothing); a connection, the watch list and an options change
  always send. `test_catalog_carries_a_devices_values`, `test_values_absent_when_not_known`,
  `test_a_brightness_change_alone_sends_a_state`, `test_a_thermostat_setpoint_change_sends_a_state`,
  `test_an_options_change_sends_every_state_again`.
- **What a device can do, what a thermostat is doing, since when (BL-171, BL-174; Claude,
  2026-10-06, after the panel's UAT; committed 2026-10-07, not released; on the bench
  Home Assistant since 2026-10-06)**: a `control` row and state carry `supports` (light `brightness` from
  `supported_color_modes`, fan `set_percentage`, cover/valve `stop`, `set_position` from
  `supported_features`; also while unavailable; absent when Home Assistant doesn't say or for
  other domains), `hvac_action` (climate, while available) and `since` (Unix s: unavailable
  since `last_changed` on a state; a scene's run time; never on an unavailable entity's catalog
  row) - `coordinator._supports`, `_since`. A change of `supports` or `hvac_action` alone sends
  a state (the `_sent` dedup compares whole messages); zone-only entities carry none of them.
  `test_supports_from_home_assistants_flags`, `test_supports_left_out_when_not_known`,
  `test_a_features_change_alone_sends_a_state`, `test_supports_and_since_while_unavailable`,
  `test_hvac_action_crosses`, `test_a_scene_says_when_it_ran`;
  `test_coordinator.py: test_a_controllable_entity_that_goes_away_says_so` now expects `since`.
  Checks: 192 tests in the `roboalarms-test` image, Ruff lint and format clean, protocol sync
  check OK (`aiopanel.py` unchanged). Needs the panel's side (its protocol.md: these keys) to
  have an effect; an older panel skips them.
- Checks before that part: 186 tests in the `roboalarms-test` image, Ruff lint and format clean, protocol sync
  check OK (`aiopanel.py` unchanged by this part).
- Live with the panel (Home Assistant 2026.9.4): the values on its Smart Home, a mode both
  ways, the chime switch (the panel no longer asks a remote client for a code, its
  CORE-CHIME-007), a wrong code as `service_validation_error` over the websocket, `changed_by`
  on arm and disarm.
- Left: commit and release all of the above (every installed v0.1.0 drops a quiet house's link
  every 95 s). Home Assistant `sensor` entities as zones are not offered (the options list
  `binary_sensor` only); the panel repo's docs now say so.

## Earlier status (2026-09-29)

**Z-Wave devices, a CO fix and Spanish (Claude, 2026-09-29): checks passed; committed and pushed
with this note.** Panel repo BL-043, BL-027 and BL-005:
- **The panel's own Z-Wave outputs** over the native link (protocol.md, API 1 addition):
  `aiopanel.py` `ZwaveDevice` + `PanelState.apply_zwave` (the whole list each time, validated
  whole, unknown kinds left out; `protocol/aioroboalarms.py` synced), the coordinator handles
  `zwave_devices`/`zwave_result` and sends `zwave_set` (`async_zwave_set`, shared
  `_async_request`), new platforms `light` (dimmer, brightness <-> level, back to the last level)
  and `cover` (garage, CLOSE only - opening takes a code at the panel), `switch` gains the
  panel's switches/relays. Each is its own device under the panel; missing or offline =
  unavailable, never removed by itself (the panel lists nothing while its radio starts);
  `async_remove_config_entry_device` lets a person delete one the panel no longer lists.
  `tests/test_zwave.py` (incl. the firmware's golden `zwave_devices` string). Because the light
  platform now always loads, `tests/test_init.py` has an autouse `light_integration_first`
  fixture (also imported by `test_coordinator.py`) so service mocks aren't replaced.
- **CO zones**: the panel sends device class `carbon_monoxide`; it now maps to
  `BinarySensorDeviceClass.CO` (was only `co`). Found on the bench panel during BL-027 part 1
  (panel repo docs/features/23 validation.md), verified live.
- **Spanish** `translations/es.json` (a machine draft for native review; the flag is this note
  and panel Features/37) and `tests/test_translations.py` (keys/placeholders parity, en.json ==
  strings.json).
- Checks: 160 tests in the `roboalarms-test` Docker image, Ruff lint and format clean, protocol
  sync check OK. Not run: hassfest/HACS action (CI runs them on push). Not released (version
  still 0.1.0); not on a panel with a Z-Wave radio.
- **`via_device_id` (panel repo BL-111, 2026-09-29, later the same day):** Home Assistant
  2026.8 replaced DeviceInfo `via_device` with `via_device_id` (the old form warns on every
  start and stops in 2027.8). `entity.parent_of()` gives `via_device_id` when DeviceInfo has
  that key and `via_device` before, so the minimum stays 2026.3; `__init__.py` registers the
  panel's device (`entity.panel_device_info`) before the platforms and keeps its id in
  `coordinator.panel_device_id`. Tests: `test_entities.py` both forms, with Home Assistant's
  `report_usage` patched (no report on the new form). 162 tests, Ruff clean.

## Earlier status (2026-09-24)

**Brand and README refresh (Codex): complete, checks passed; included in this source revision.** Owner requested
the existing R/house logo from the docs/main website, removal of the README panel screenshot
and caption, and clearer, consistent project copy. Updated both integration icon sizes with
transparent exports of that exact source; retained the master in `docs/brand/`, documented
provenance in `docs/BRAND.md`, and removed the obsolete placeholder generator and screenshot.
README now explains the HACS integration, setup, controls and recovery, links the public guides,
and separates the v0.1.0 integration testing release from the unreleased firmware. It also
corrects pairing-button names/address location and marks panel Devices UI and sensor-sharing
hardware acceptance as unfinished. Integration behavior and version are unchanged. The owner
approved committing and pushing to `origin/main` on 2026-09-24; check Git history and remote
tracking for delivery state. No release, live Home Assistant update or hardware test is part
of that request. Existing unrelated edits preserved.

**Home Assistant display name (Codex, 2026-09-24): complete, checks passed.** The user requested the
integration picker name change from "RoboAlarms Panel" to "RoboAlarms", matching the website's
Home Assistant screenshot. Updated `manifest.json`, the setup README, and generic config-entry/
device-name fallbacks; the domain, entity ids, service names and protocol identifiers are
unchanged. The disposable screenshot lab loaded this edited integration. CUA verified the
picker and configured integration page display "RoboAlarms"; the lab-only pairing was recovered
against its fictional panel. Replaced the website asset with a genuine Home Assistant frontend
capture showing the four fictional devices and 22 entities; no text was repainted. The capture
is 976×600 PNG, cropped from the 1280×720 CUA capture. All 145 tests, Ruff lint/format checks,
translation parity and `git diff --check` passed. No release, commit or push was requested.

Validation: existing offline Docker image (`roboalarms-test`, Python 3.14.7) passed protocol
source synchronization, all 145 pytest tests, Ruff lint and formatting (29 files). README local
links pass; `git diff --check` passes. Visually inspected the exported icon; PNG checks confirm
256/512px RGBA and the master source SHA-256 matches the documentation asset. Hassfest/HACS CI
and an installed Home Assistant branding refresh were not run. Next: review the push-triggered
CI results. A release bump/install remains separate if the icon should be
delivered to existing HACS installations.

## Earlier continuity update (2026-09-21)

Latest change: shared Claude/Codex continuity instructions and the `AGENTS.md` entry point.
No integration implementation changed or tests ran for this documentation-only change; nothing
was committed or published. The working tree was clean before these two instruction edits.

For implementation/release status, read the panel repository's
`Features/23-Home-Assistant-Integration/README.md` and `validation.md`: they record completed
M12 work and the v0.1.0 testing release, superseding "Nothing released yet" in the earlier
snapshot below. M13 Devices UI remains tracked in Features/24. Verify Git and those records
before resuming; no fresh hardware or release validation was performed in this tracking task.

## Earlier implementation snapshot (2026-09-20; partly superseded above)
Nothing released yet:
- **Protocol v1 exists** (spec: `Features/23/protocol.md` in the panel repo): 4-byte
  big-endian length + JSON frames on TCP 6054, `hello` both ways, numeric-comparison pairing
  with a commitment. `aiopanel.py` is the client (framing, hello, `pair_code`/`pair_commit`)
  — the seed of the planned `aioroboalarms` package, no HA imports allowed in it. Its tests
  run against a fake asyncio panel and share golden pairing vectors verbatim with the panel's
  `host/tests/test_link.c`: change the derivation and one of the two suites fails.
- config flow: manual (host/port) and zeroconf steps, unique id from the panel id, discovery
  updates a known entry's address (HAI-004), connection tested before anything is created,
  then **pairing** (HAI-003): the flow instructs to open pairing on the panel, starts the
  commitment exchange, shows the 6-digit code in a progress step until Allow is tapped on the
  panel, and the entry stores the client identity (generated per flow with `cryptography`)
  plus the panel's pinned certificate fingerprint (`const.py` CONF_* keys). The panel side
  (`ha_link` in the firmware repo) has now been paired and tested on development hardware.
- **entities work against a fake panel**: `coordinator.py` (one connection, the snapshot at
  connect, deltas folded in, events onto the bus as `roboalarms_event`, commands matched to
  results by id, pings after 30 quiet seconds, reconnect with backoff, the pinned fingerprint
  checked on every connect — a mismatch starts reauth), `alarm_control_panel.py` (one per
  partition, `ha_state` straight from the panel), `binary_sensor.py` (every zone its own
  device under the panel, new zones appear without a restart), `diagnostics.py` (keys
  redacted), and the reauth flow (pair again after a factory reset). `tests/test_init.py`
  drives all of it over a real socket. `strings.json` = `translations/en.json`.
- **both halves of M13 work against the fake panel** (HAI-009): the options flow picks the
  binary sensors the panel may use as zones (`entry.options["share_entities"]`) and the
  entities it may control (`entry.options["control_entities"]`), both empty by default. The
  coordinator sends their union as a paged `catalog` after every connect and whenever the
  options change - each entry carrying `zones` and `control` - answers the panel's `watch`
  with a `state` each and pushes changes from then on. A watch for anything outside
  `share_entities` is ignored without a word, and the subscription goes away with the link.
  A `call` is executed only for an entity in `control_entities` and only with an action
  Features/24's table lists for its domain; anything else is answered `not_allowed`, a
  missing or unavailable entity `unavailable`, a service that raises `failed`, and every
  call with an id gets exactly one `call_result`. The panel's own side of both is still to
  come.
- At this earlier snapshot the icon was a shield/check placeholder and the README showed
  a panel Home screenshot. Both were replaced/removed in the 2026-09-24 brand refresh above.
- CI: hassfest + HACS validation, pytest, ruff.

**M12 completion work (2026-09-20):**
- Reconfigure preserves identity and pins, releases the single connection while testing,
  and restores the old entry on failure. Protocol mismatch creates a per-entry repair;
  unpairing or a changed identity/certificate starts reauthentication.
- Zone diagnostics (tamper, low battery, supervision), panel trouble/AC/installer-mode,
  uptime/Wi-Fi signal and firmware update status are implemented. Removed zones and their
  entities/devices are reconciled without restart. Installation stays authorized on-panel.
- Custom bypass/unbypass, arm flags and panic actions carry a code and report engine refusals.
  Panic remains disabled by firmware policy. Own integration entities cannot be shared back.
- Real panel numeric-comparison pairing, reconnect, snapshot/status and a Home Assistant
  Docker setup/reconfigure/unload test pass. The firmware needed identity, retained peer
  certificate and nested stack-buffer fixes before it worked on hardware.
- Standalone `aioroboalarms` wheel is buildable from `protocol/`; sync check ensures identical
  client code in HACS. PyPI account/publisher configuration is still needed for publication.
- GitHub testing release authorized by owner. HACS default-store inclusion remains separate.

**Open items:**
- ~~Product name and domain~~ decided (owner, 2026-09-20): the project is **RoboAlarms**,
  the device the **RoboAlarms Panel**, the domain `roboalarms`, the mDNS type
  `_roboalarms._tcp`. The domain and entity unique ids still freeze for good at the
  **first release**; until then a rename is possible but needs the owner's say.
- No Honeywell mentions in this public repository (owner, 2026-09-20): Honeywell behaviour
  was reference material while designing the panel, not something this repo advertises.
- GitHub repository description and topics (e.g. `home-assistant`, `hacs-integration`,
  `alarm-panel`) must be set on GitHub, or the HACS validation action fails.
- The README's mention of the panel firmware gets a link once `RoboAlarms/alarm-panel` is
  public.
- TXT keys in `const.py` (`id`, `name`, `model`, `fw`, `api`, `paired`) are a draft; finalize
  together with the protocol spec and keep panel and integration in step.

## Rules
- **External contracts once released:** the domain, entity unique ids, config entry data keys
  and the zeroconf service type. Never rename, only add. Until the first release they may
  still change.
- `manifest.json`: hassfest key order (`domain`, `name`, then alphabetical); `version` bumps
  with every release (tag = release = HACS version); runtime deps go in `requirements`
  pinned `==`, HA-style.
- `strings.json` and `translations/en.json` stay identical; user-facing text lives there,
  not in Python.
- Quality target (Features/23): Bronze and Silver of HA's Integration Quality Scale from the
  start — full config-flow test coverage, test-before-configure, unique ids,
  `has_entity_name`, entities unavailable on link loss, reauth, clean unload — plus the Gold
  rules users notice (discovery updates, devices, stale devices, diagnostics, reconfigure,
  repairs, translations, entity categories).
- The protocol's source of truth is `alarm_proto` in the panel repo. Message shapes, TXT keys
  and the pairing-code derivation change there first; golden vectors are shared with the
  tests here so the two cannot drift.
- Duress and silent alarms never change visible alarm state here (HAI-008) — same rule as
  the panel's MQTT path.
- Checks before handing work back: `pytest` and `ruff check .` / `ruff format --check .`
  green locally (install with `pip install -r requirements_test.txt ruff`); CI runs the same
  plus hassfest and the HACS action.
- **Python 3.14**: HA 2026.3+ (our minimum) requires Python >= 3.14.2; on an older Python,
  pip silently resolves an old homeassistant and the tests test the wrong thing. HA core
  does not install on native Windows (`lru-dict` needs MSVC): run the suite in Docker
  instead — `docker run --rm -v ${PWD}:/app -w /app python:3.14 bash -c
  "pip install -r requirements_test.txt && pytest"`. Ruff runs fine natively.
- Commit messages: imperative mood. Don't commit or push unless asked.
