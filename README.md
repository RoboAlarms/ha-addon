<p align="center">
  <img src="custom_components/roboalarms/brand/icon@2x.png" width="120" height="120" alt="RoboAlarms logo">
</p>

<h1 align="center">RoboAlarms for Home Assistant</h1>

<p align="center">
  <a href="https://github.com/RoboAlarms/ha-addon/actions/workflows/validate.yml"><img src="https://github.com/RoboAlarms/ha-addon/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
  <a href="https://github.com/RoboAlarms/ha-addon/actions/workflows/tests.yml"><img src="https://github.com/RoboAlarms/ha-addon/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/hacs/integration"><img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.3%2B-41BDF5" alt="Home Assistant 2026.3+">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/RoboAlarms/ha-addon" alt="License"></a>
</p>

<p align="center">
  <a href="https://docs.roboalarms.com/">Documentation</a> ·
  <a href="https://docs.roboalarms.com/installer/home-assistant/">Setup guide</a> ·
  <a href="https://github.com/RoboAlarms/ha-addon/releases">Releases</a> ·
  <a href="https://github.com/RoboAlarms/ha-addon/issues">Report an issue</a>
</p>

Bring your **RoboAlarms Panel** into Home Assistant. See alarm status, monitor zones,
use panel events in automations, and arm or disarm with an authorized user code—all
through a direct connection on your local network.

RoboAlarms is an open-source touchscreen alarm project with local alarm logic and
optional Home Assistant integration. We develop software; we don't sell panels,
sensors or monitoring services. This repository contains the **HACS custom
integration**, not a Home Assistant Supervisor add-on.

> [!IMPORTANT]
> **Testing release.** This integration is available for development-device testing
> with a matching panel firmware build. RoboAlarms' first public firmware release is
> still ahead. The project is not a listed or certified alarm system.

## A local connection to your alarm

- **Pair at the panel.** Compare the six-digit code on both screens before allowing
  the connection. No Home Assistant account password or MQTT broker is needed.
- **See changes as they happen.** The panel pushes status and events over an
  encrypted, mutually authenticated connection with pinned certificates.
- **Keep the panel in charge.** Alarm commands follow the same permissions and code
  checks as the touchscreen. This integration sends a code only for the requested
  action and does not store it.

The alarm engine runs on the panel; Home Assistant is optional. Sensors shared from
Home Assistant still depend on that server and the network connection.

## What appears in Home Assistant

| Feature | What it provides |
| --- | --- |
| Alarm controls | One alarm entity per partition, with Home (Stay), Away and Night arming |
| Zones | Named zone sensors, plus tamper, low-battery and supervision diagnostics |
| Panel health | Trouble, AC power and installer-mode sensors; uptime and available Wi-Fi signal |
| Everyday controls | Chime per partition and a button to restart the exit delay |
| Events | Arming, disarming, alarms and troubles for dashboards and automations |
| Firmware updates | Release availability and installation progress; authorize and start installation on the panel |

Each zone appears as a device under the panel, so you can assign it to a room.
Added, renamed and deleted zones are reconciled without restarting Home Assistant.
Zone battery percentages and radio signal readings depend on future sensor telemetry;
current firmware supplies low-battery and supervision flags.

## Install the integration

You need Home Assistant **2026.3 or newer**, compatible RoboAlarms development
firmware, and a network connection between the two. Automatic discovery uses mDNS;
manual setup by address is available when discovery cannot cross your network.

### With HACS

[![Open RoboAlarms in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=RoboAlarms&repository=ha-addon&category=integration)

Or add the [custom repository](https://www.hacs.xyz/docs/faq/custom_repositories/) yourself:

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/RoboAlarms/ha-addon` with type **Integration**.
3. Find **RoboAlarms**, download the integration, and restart Home Assistant.

### Manually

Copy `custom_components/roboalarms` into the `custom_components` folder of your
Home Assistant configuration directory, then restart Home Assistant.

## Pair your panel

[![Set up the RoboAlarms integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=roboalarms)

1. On the panel, open **Settings > Integrations**, unlock with your master or installer
   code if prompted, then open **Home Assistant** and enable the integration.
2. In Home Assistant, open **Settings > Devices & services** and add the discovered
   **RoboAlarms Panel**. If it is not discovered, choose **Add integration**, search
   for **RoboAlarms**, and enter the address shown under **Settings > Network** on the panel.
3. When Home Assistant prompts you, tap **Pair with Home Assistant** on the panel.
   Pairing stays open for two minutes.
4. Compare the six-digit code on both screens. If they match, tap
   **The codes match - allow** on the panel. Reject the request if they differ.
5. The panel and its zones appear in Home Assistant, ready to assign to areas.

The [illustrated setup guide](https://docs.roboalarms.com/installer/home-assistant/)
walks through installation, pairing and connection checks.

## Share Home Assistant entities with the panel

The integration's options let you choose which binary sensors the panel may use as
alarm zones and which entities it may control. Both lists start empty; only the
entities you select are offered to the panel.

This two-way work is still in development:

- **Home Assistant sensors as zones:** implemented in software; panel hardware
  acceptance testing is still pending.
- **Device control:** the integration's allowed-entity checks and action handling
  are implemented. The panel's Devices screen for lights, locks, climate and other
  controls is not yet available.

See the [sensor-sharing guide](https://docs.roboalarms.com/installer/home-assistant-zones/)
for the current setup flow and limitations.

## Alarm actions and connection recovery

Arming and disarming require an authorized panel user code. The panel checks it and
enforces its permissions and keypad lockout rules. Duress remains silent in the
visible alarm state.

**Developer Tools > Actions** also exposes `roboalarms.bypass_zone`,
`roboalarms.unbypass_zone`, `roboalarms.arm` (including silent-exit and
no-entry-delay options), and `roboalarms.panic`. The panel decides whether each
request is allowed. Remote panic is currently disabled by firmware policy.

Use **Reconfigure** on the integration entry to change its address or port while
preserving pairing, entities and options. A changed panel identity or certificate
requires pairing again. Incompatible protocol versions create a repair issue;
update to a matching integration and firmware combination.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Panel not discovered | mDNS normally needs the same subnet, or an mDNS reflector between subnets. Try manual setup by address; the network must still allow the connection. |
| Could not reach the panel | Enable Home Assistant integration on the panel and check that your network allows TCP port **6054** between the devices. |
| Pairing timed out | Open pairing on the panel again, then restart the pairing step in Home Assistant. |
| Pairing codes differ | Reject the request. Check that you are connecting to the intended panel on a trusted network, then try again. |
| Pairing required again | A factory reset or unpairing removes the previous trust. Follow the reauthentication prompt and compare the new codes. |

For more help, read the [troubleshooting guide](https://docs.roboalarms.com/reference/troubleshooting/)
or [open an issue](https://github.com/RoboAlarms/ha-addon/issues). Include versions and
relevant error messages; leave out alarm codes, private keys and personal network details.

## Development and contributions

The [v0.1.0 testing release](https://github.com/RoboAlarms/ha-addon/releases/tag/v0.1.0)
includes discovery, pairing, entities, alarm actions, diagnostics, reconfiguration
and repairs. Pairing, reconnect and status have been verified with development
hardware and Home Assistant. Broader hardware acceptance testing is ongoing.

Issues, testing feedback and pull requests are welcome. For development, use
Python **3.14.2 or newer** with Home Assistant's matching test dependencies:

```bash
pip install -r requirements_test.txt ruff
python scripts/sync_protocol.py --check
pytest
ruff check .
ruff format --check .
```

Run Home Assistant tests in Linux or Docker; Home Assistant's dependencies do not
install directly on native Windows. The [standalone protocol client](protocol/README.md)
has its own packaging notes. PyPI publication and HACS default-store inclusion
remain separate from this custom-repository testing release.

## License

[Apache 2.0](LICENSE). No RoboAlarms subscription is required. Hardware and optional
third-party services are provided separately.
