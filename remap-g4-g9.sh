#!/bin/sh
# Bind G604 buttons G4-G9 to generic mouse buttons. Safe to rerun: no write if already set.
# Extra args pass through, e.g. --dry-run or --profile 2.
exec uv run "$(dirname "$0")/omm.py" remap-g "$@"
