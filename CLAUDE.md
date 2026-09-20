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

## What this is
The **HACS custom integration** (domain `alarmsystem`) for the AlarmSystem panel — an
open-source touchscreen alarm panel on the Elecrow CrowPanel Advanced 7" (ESP32-P4). Despite
the repository's name, this is **not** a Supervisor add-on: the add-on route was considered
and rejected (Features/23, "Why a HACS integration"). Milestones: **M12** (discovery, pairing,
entities, commands) and **M13** (two-way: HA entities as panel zones, panel device control).

| Path | What |
|---|---|
| `custom_components/alarmsystem/` | the integration: manifest, config flow, strings; platforms arrive as they are built |
| `custom_components/alarmsystem/brand/` | icon shown by HA/HACS (HA 2026.3+ serves it from here; home-assistant/brands no longer takes custom-integration PRs) |
| `tests/` | pytest with `pytest-homeassistant-custom-component` |
| `scripts/placeholder_icon.py` | regenerates the placeholder brand icon (stdlib only, no Pillow) |
| `.github/workflows/` | `validate.yml` (hassfest + HACS action), `tests.yml` (pytest + ruff) |
| `hacs.json` | HACS metadata; `homeassistant` is the minimum HA version (2026.3.0, needed for the in-repo brand icon) |

## Where things stand (2026-09-19)
Scaffolded, nothing released yet:
- config flow: manual (host/port) and zeroconf steps, unique id from the panel id TXT key,
  discovery updates a known entry's address (HAI-004). The connection test is a **stub that
  always raises CannotConnect** — the panel's TLS API and the `aioalarmsystem` client don't
  exist yet, so no entry can be created. Honest by design; tests pin this behaviour.
- `__init__.py` forwards to an empty platform list; `strings.json` = `translations/en.json`.
- brand icon is a generated placeholder (shield + check); replace when the project has real
  branding, or rerun the script after tweaking it.
- CI: hassfest + HACS validation, pytest, ruff.

**Next steps (M12 order, from Features/23 tasks):**
1. Protocol spec and `alarm_proto` codecs + pairing-code derivation in the panel repo
   (host-tested; golden vectors shared with the Python tests here).
2. `aioalarmsystem` on PyPI (asyncio client, fake panel for tests) — planned as its own
   repository so this integration can move toward HA core later; decide when it starts.
3. Wire `_async_validate_connection` to the client; pairing step in the config flow
   (HAI-003), then reauth and reconfigure (HAI-004).
4. Platforms: `alarm_control_panel` first, then binary_sensor, sensor, switch, button,
   event, update (HAI-005..008); push coordinator; diagnostics and repairs (HAI-011).
5. First GitHub release when a panel can actually pair; HACS default-store inclusion later.

**Open items:**
- Product name and domain: "AlarmSystem" / `alarmsystem` are the working names. The domain
  and entity unique ids become an external contract at the **first release** — decide the
  final name before then (Features/23 open question). Renaming later breaks users.
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
