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
| `scripts/placeholder_icon.py` | regenerates the placeholder brand icon (stdlib only, no Pillow) |
| `.github/workflows/` | `validate.yml` (hassfest + HACS action), `tests.yml` (pytest + ruff) |
| `hacs.json` | HACS metadata; `homeassistant` is the minimum HA version (2026.3.0, needed for the in-repo brand icon) |

## Where things stand (2026-09-21)

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
- brand icon is a generated placeholder (shield + check); `docs/panel-home.png` is the real
  panel's Home screen from the firmware repo's UI preview (regenerate there, shot 01).
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
