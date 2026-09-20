"""Keep the standalone distribution identical to the HACS-bundled protocol client."""

import argparse
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / "custom_components/roboalarms/aiopanel.py"
target = root / "protocol/aioroboalarms.py"
parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
if args.check:
    if source.read_bytes() != target.read_bytes():
        raise SystemExit("Run python scripts/sync_protocol.py before building the protocol package")
else:
    target.write_bytes(source.read_bytes())
