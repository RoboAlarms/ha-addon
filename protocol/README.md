# aioroboalarms

An asyncio protocol client for RoboAlarms panels: bounded framed JSON over mutual TLS,
numeric-comparison pairing, typed alarm state, and diagnostic telemetry. No Home Assistant
imports. Python 3.11 or newer.

`PanelClient`, `PanelState`, `generate_client_identity`, `client_ssl_context`, and
`cert_der_from_pem` are public entry points. A paired application must compare the SHA-256
of `PanelClient.panel_cert_der` to its stored fingerprint after connecting and before
sending commands. Never accept a changed fingerprint without another confirmed pairing.

The Home Assistant integration bundles the same client until a PyPI release is configured.
Run `python scripts/sync_protocol.py --check` to verify the distribution source matches it.
Run `python scripts/sync_protocol.py` after changing the bundled client, then `python -m build`
to build the independent wheel and source archive. The wheel contains no Home Assistant code.
