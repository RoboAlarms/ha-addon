"""Constants for the RoboAlarms integration."""

from __future__ import annotations

DOMAIN = "roboalarms"

# The panel's local API (TLS over TCP, u32-length-prefixed JSON frames).
DEFAULT_PORT = 6054

# mDNS service the panel advertises while the integration is enabled on it
# (HAI-001). Must match the zeroconf matcher in manifest.json.
ZEROCONF_TYPE = "_roboalarms._tcp.local."

# TXT record keys the panel advertises. Draft: the protocol spec lives with
# the panel firmware (alarm_proto) and these are finalized together with it.
# Config entry data (an external contract once released: add, never rename).
CONF_PANEL_FP = "panel_fp"  # hex SHA-256 of the panel's certificate, pinned at pairing
CONF_CLIENT_KEY = "client_key"  # this entry's private key (PEM)
CONF_CLIENT_CERT = "client_cert"  # this entry's certificate (PEM); the panel pins its hash

# Config entry options.
CONF_SHARE_ENTITIES = "share_entities"  # the entities the panel may use as zones (HAI-009)

ZC_PROP_ID = "id"  # panel id (stable, lowercase hex)
ZC_PROP_NAME = "name"  # the panel's friendly name
ZC_PROP_MODEL = "model"
ZC_PROP_FIRMWARE = "fw"
ZC_PROP_API = "api"  # API major version
ZC_PROP_PAIRED = "paired"  # "1" once an HA instance is paired
