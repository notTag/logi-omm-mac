#!/bin/sh
# Put profile 1 back to its pre-remap bindings (the 2026-10-16 11:39 backup).
# Extra args pass through, e.g. --dry-run.
script_directory="$(dirname "$0")"
original_profile_1="$script_directory/backups/20261016-113914-profile1-user-sector-0x0001.bin"
exec uv run "$script_directory/omm.py" restore "$original_profile_1" "$@"
