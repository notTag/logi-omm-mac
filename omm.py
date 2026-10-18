# /// script
# requires-python = ">=3.10"
# dependencies = ["hidapi>=0.14"]
# ///
"""Minimal macOS stand-in for Logitech's Windows-only Onboard Memory Manager.

Protocol: HID++ 2.0 feature 0x8100 (Onboard Profiles), as implemented by
libratbag (src/hidpp20.c). Profile layout = "G402" memory model, 256-byte
sectors, CRC-CCITT (seed 0xFFFF, big-endian) in the last 2 bytes.

Usage:
  uv run omm.py info
  uv run omm.py dump
  uv run omm.py set-button SLOT BUTTON [--profile N] [--dry-run]
  uv run omm.py remap-g [--profile N] [--dry-run]
  uv run omm.py restore BACKUP_FILE [--dry-run]
"""

import argparse
import ctypes
import datetime
import pathlib
import sys

import hid

LOGITECH_VENDOR_ID = 0x046D
HIDPP_USAGE_PAGE = 0xFF00
REPORT_ID_LONG = 0x11
LONG_REPORT_LENGTH = 20
SOFTWARE_ID = 0x08
HIDPP20_ERROR_SUB_ID = 0xFF
HIDPP10_ERROR_SUB_ID = 0x8F
CANDIDATE_DEVICE_INDEXES = [0xFF, 1, 2, 3, 4, 5, 6]
READ_TIMEOUT_MS = 1000

FEATURE_ROOT_INDEX = 0x00
FEATURE_ONBOARD_PROFILES = 0x8100

ROOT_GET_FEATURE = 0x00
ONBOARD_GET_DESCRIPTION = 0x00
ONBOARD_SET_MODE = 0x10
ONBOARD_GET_MODE = 0x20
ONBOARD_SET_CURRENT_PROFILE = 0x30
ONBOARD_GET_CURRENT_PROFILE = 0x40
ONBOARD_MEMORY_READ = 0x50
ONBOARD_MEMORY_ADDRESS_WRITE = 0x60
ONBOARD_MEMORY_WRITE = 0x70
ONBOARD_MEMORY_WRITE_END = 0x80

ONBOARD_MODE = 0x01
SUPPORTED_MEMORY_MODEL = 0x01
SUPPORTED_PROFILE_FORMATS = {0x01, 0x02, 0x03, 0x04, 0x05}
DICTIONARY_SECTOR = 0x0000
ROM_PROFILES_BASE_SECTOR = 0x0100
DICTIONARY_END_MARKER = 0xFFFF
DICTIONARY_ENTRY_SIZE = 4
DICTIONARY_ENABLED_OFFSET = 2
CHUNK_SIZE = 16

PROFILE_BUTTONS_OFFSET = 32
BUTTON_BINDING_SIZE = 4
MAX_PROFILE_BUTTONS = 16

BINDING_TYPE_MACRO = 0x00
BINDING_TYPE_HID = 0x80
BINDING_TYPE_SPECIAL = 0x90
BINDING_TYPE_DISABLED = 0xFF
HID_SUBTYPE_MOUSE = 0x01
HID_SUBTYPE_KEYBOARD = 0x02
HID_SUBTYPE_CONSUMER = 0x03

SPECIAL_ACTION_NAMES = {
    0x01: "tilt left", 0x02: "tilt right", 0x03: "DPI up", 0x04: "DPI down",
    0x05: "DPI cycle", 0x06: "DPI default", 0x07: "DPI shift", 0x08: "profile next",
    0x09: "profile prev", 0x0A: "profile cycle", 0x0B: "G-Shift",
}

BACKUP_DIRECTORY = pathlib.Path(__file__).resolve().parent / "backups"

# Matches the layout set up by hand on 2026-10-16: G5/G6 stay back/forward,
# the rest get the spare buttons 6-9.
MOUSE_BUTTON_FOR_G_BUTTON = {4: 9, 5: 4, 6: 5, 7: 8, 8: 7, 9: 6}


def slot_for_g_button(g_number: int) -> int:
    # G604 slots are G-numbers shifted to 0-based (G1 = slot 0).
    return g_number - 1


class HidppError(Exception):
    pass


def crc_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        temp = (crc >> 8) ^ byte
        crc = (crc << 8) & 0xFFFF
        quick = temp ^ (temp >> 4)
        crc ^= quick
        quick = (quick << 5) & 0xFFFF
        crc ^= quick
        quick = (quick << 7) & 0xFFFF
        crc ^= quick
    return crc


def sector_has_valid_crc(sector_bytes: bytes) -> bool:
    stored_crc = int.from_bytes(sector_bytes[-2:], "big")
    computed_crc = crc_ccitt(sector_bytes[:-2])
    return stored_crc == computed_crc


def with_fresh_crc(sector_bytes: bytes) -> bytes:
    body = sector_bytes[:-2]
    crc = crc_ccitt(body)
    return body + crc.to_bytes(2, "big")


class HidppDevice:
    def __init__(self, hid_handle, device_index: int):
        self.hid_handle = hid_handle
        self.device_index = device_index
        self.onboard_feature_index = None

    def request(self, feature_index: int, function: int, parameters: bytes = b"") -> bytes:
        function_and_software_id = function | SOFTWARE_ID
        header = bytes([REPORT_ID_LONG, self.device_index, feature_index, function_and_software_id])
        padded_parameters = parameters.ljust(LONG_REPORT_LENGTH - len(header), b"\x00")
        self.hid_handle.write(header + padded_parameters)

        while True:
            reply = bytes(self.hid_handle.read(64, READ_TIMEOUT_MS))
            if not reply:
                raise HidppError(f"timeout: feature 0x{feature_index:02x} fn 0x{function:02x}")
            if len(reply) < 5 or reply[1] != self.device_index:
                continue
            reply_sub_id, reply_address = reply[2], reply[3]
            if reply_sub_id == feature_index and reply_address == function_and_software_id:
                return reply[4:]
            is_error_reply = reply_sub_id in (HIDPP20_ERROR_SUB_ID, HIDPP10_ERROR_SUB_ID)
            if is_error_reply and reply_address == feature_index and reply[4] == function_and_software_id:
                raise HidppError(f"device error 0x{reply[5]:02x} on fn 0x{function:02x}")

    def feature_index(self, feature_id: int) -> int:
        reply = self.request(FEATURE_ROOT_INDEX, ROOT_GET_FEATURE, feature_id.to_bytes(2, "big"))
        return reply[0]

    def onboard(self, function: int, parameters: bytes = b"") -> bytes:
        return self.request(self.onboard_feature_index, function, parameters)


def allow_shared_device_access() -> None:
    # hidapi on macOS opens devices exclusively by default, which fails while
    # BetterMouse (or macOS itself) already holds the mouse. The Python binding
    # doesn't expose the toggle, so call the bundled C symbol directly.
    hidapi_library = ctypes.CDLL(hid.__file__)
    hidapi_library.hid_darwin_set_open_exclusive(0)


def open_onboard_device() -> HidppDevice:
    allow_shared_device_access()
    seen_paths = set()
    for device_info in hid.enumerate(LOGITECH_VENDOR_ID, 0):
        is_vendor_usage_page = (device_info["usage_page"] & 0xFF00) == HIDPP_USAGE_PAGE
        if not is_vendor_usage_page or device_info["path"] in seen_paths:
            continue
        seen_paths.add(device_info["path"])
        hid_handle = hid.device()
        try:
            hid_handle.open_path(device_info["path"])
        except OSError:
            continue
        for device_index in CANDIDATE_DEVICE_INDEXES:
            device = HidppDevice(hid_handle, device_index)
            try:
                onboard_feature_index = device.feature_index(FEATURE_ONBOARD_PROFILES)
            except HidppError:
                continue
            if onboard_feature_index:
                device.onboard_feature_index = onboard_feature_index
                print(f"Using {device_info['product_string']!r} (PID 0x{device_info['product_id']:04x}), device index {device_index}")
                return device
        hid_handle.close()
    sys.exit(
        "No Logitech device with Onboard Profiles found.\n"
        "Check: mouse awake, receiver plugged in, and your terminal has Input Monitoring permission\n"
        "(System Settings > Privacy & Security > Input Monitoring)."
    )


def read_description(device: HidppDevice) -> dict:
    reply = device.onboard(ONBOARD_GET_DESCRIPTION)
    description = {
        "memory_model": reply[0],
        "profile_format": reply[1],
        "macro_format": reply[2],
        "profile_count": reply[3],
        "rom_profile_count": reply[4],
        "button_count": min(reply[5], MAX_PROFILE_BUTTONS),
        "sector_count": reply[6],
        "sector_size": int.from_bytes(reply[7:9], "big"),
    }
    if description["memory_model"] != SUPPORTED_MEMORY_MODEL or description["profile_format"] not in SUPPORTED_PROFILE_FORMATS:
        sys.exit(f"Unsupported onboard memory layout: {description}")
    return description


def read_sector(device: HidppDevice, sector: int, sector_size: int) -> bytes:
    sector_bytes = bytearray(sector_size)
    for offset in range(0, sector_size, CHUNK_SIZE):
        # Firmware rejects reads starting past sector_size - 16 (libratbag note).
        read_offset = min(offset, sector_size - CHUNK_SIZE)
        parameters = sector.to_bytes(2, "big") + read_offset.to_bytes(2, "big")
        chunk = device.onboard(ONBOARD_MEMORY_READ, parameters)[:CHUNK_SIZE]
        sector_bytes[read_offset:read_offset + CHUNK_SIZE] = chunk
    return bytes(sector_bytes)


def write_sector(device: HidppDevice, sector: int, sector_bytes: bytes) -> None:
    sector_size = len(sector_bytes)
    start_parameters = sector.to_bytes(2, "big") + (0).to_bytes(2, "big") + sector_size.to_bytes(2, "big")
    device.onboard(ONBOARD_MEMORY_ADDRESS_WRITE, start_parameters)
    for offset in range(0, sector_size, CHUNK_SIZE):
        device.onboard(ONBOARD_MEMORY_WRITE, sector_bytes[offset:offset + CHUNK_SIZE])
    device.onboard(ONBOARD_MEMORY_WRITE_END)

    read_back = read_sector(device, sector, sector_size)
    if read_back != sector_bytes:
        raise HidppError(f"verify failed for sector 0x{sector:04x}: read-back differs from written bytes")


def active_profile_index(device: HidppDevice) -> int:
    reported_profile = device.onboard(ONBOARD_GET_CURRENT_PROFILE)[1]
    # G604 reports 1-based profile numbers (libratbag quirk INDEX_OFFSET).
    return max(reported_profile - 1, 0)


def rom_sector_for(profile_index: int, rom_profile_count: int) -> int:
    rom_slot = profile_index + 1 if profile_index < rom_profile_count else 1
    return ROM_PROFILES_BASE_SECTOR + rom_slot


def user_sector_for(profile_index: int) -> int:
    return profile_index + 1


def dictionary_enables(dictionary_bytes: bytes, profile_index: int) -> bool:
    entry_offset = profile_index * DICTIONARY_ENTRY_SIZE
    entry = dictionary_bytes[entry_offset:entry_offset + DICTIONARY_ENTRY_SIZE]
    entry_sector = int.from_bytes(entry[0:2], "big")
    return entry_sector == user_sector_for(profile_index) and entry[DICTIONARY_ENABLED_OFFSET] != 0


def load_profile_state(device: HidppDevice, description: dict) -> dict:
    sector_size = description["sector_size"]
    dictionary_bytes = read_sector(device, DICTIONARY_SECTOR, sector_size)
    dictionary_valid = sector_has_valid_crc(dictionary_bytes)

    profiles = []
    for profile_index in range(description["profile_count"]):
        user_sector = user_sector_for(profile_index)
        user_bytes = read_sector(device, user_sector, sector_size)
        user_profile_usable = (
            dictionary_valid
            and dictionary_enables(dictionary_bytes, profile_index)
            and sector_has_valid_crc(user_bytes)
        )
        if user_profile_usable:
            source_sector, source_bytes = user_sector, user_bytes
        else:
            source_sector = rom_sector_for(profile_index, description["rom_profile_count"])
            source_bytes = read_sector(device, source_sector, sector_size)
        profiles.append({
            "index": profile_index,
            "user_sector": user_sector,
            "user_bytes": user_bytes,
            "source_sector": source_sector,
            "source_bytes": source_bytes,
            "from_rom": not user_profile_usable,
        })
    return {"dictionary_bytes": dictionary_bytes, "dictionary_valid": dictionary_valid, "profiles": profiles}


def key_name(hid_usage: int) -> str:
    if 0x04 <= hid_usage <= 0x1D:
        return chr(ord("a") + hid_usage - 0x04)
    if 0x1E <= hid_usage <= 0x26:
        return str(hid_usage - 0x1D)
    if hid_usage == 0x27:
        return "0"
    return f"usage 0x{hid_usage:02x}"


def describe_binding(binding: bytes) -> str:
    binding_type, subtype = binding[0], binding[1]
    if binding_type == BINDING_TYPE_DISABLED:
        return "disabled"
    if binding_type == BINDING_TYPE_MACRO:
        return f"macro (page {binding[1]}, offset {binding[3]})"
    if binding_type == BINDING_TYPE_SPECIAL:
        return SPECIAL_ACTION_NAMES.get(subtype, f"special 0x{subtype:02x}")
    if binding_type == BINDING_TYPE_HID and subtype == HID_SUBTYPE_MOUSE:
        button_mask = int.from_bytes(binding[2:4], "big")
        return f"mouse button {button_mask.bit_length()}" if button_mask else "mouse (none)"
    if binding_type == BINDING_TYPE_HID and subtype == HID_SUBTYPE_KEYBOARD:
        modifier_flags = binding[2]
        modifier_prefix = ("Ctrl+" if modifier_flags & 0x01 else "") + ("Shift+" if modifier_flags & 0x02 else "")
        return f"keyboard {modifier_prefix}{key_name(binding[3])}"
    if binding_type == BINDING_TYPE_HID and subtype == HID_SUBTYPE_CONSUMER:
        return f"consumer control 0x{int.from_bytes(binding[2:4], 'big'):04x}"
    return f"unknown {binding.hex()}"


def binding_at(profile_bytes: bytes, slot: int) -> bytes:
    offset = PROFILE_BUTTONS_OFFSET + slot * BUTTON_BINDING_SIZE
    return profile_bytes[offset:offset + BUTTON_BINDING_SIZE]


def mouse_button_binding(button_number: int) -> bytes:
    button_mask = 1 << (button_number - 1)
    return bytes([BINDING_TYPE_HID, HID_SUBTYPE_MOUSE]) + button_mask.to_bytes(2, "big")


def write_backup(label: str, sector: int, sector_bytes: bytes) -> pathlib.Path:
    BACKUP_DIRECTORY.mkdir(exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIRECTORY / f"{timestamp}-{label}-sector-0x{sector:04x}.bin"
    backup_path.write_bytes(sector_bytes)
    return backup_path


def ensure_onboard_mode(device: HidppDevice) -> None:
    current_mode = device.onboard(ONBOARD_GET_MODE)[0]
    if current_mode != ONBOARD_MODE:
        print(f"Switching mouse to onboard mode (was {current_mode}).")
        device.onboard(ONBOARD_SET_MODE, bytes([ONBOARD_MODE]))


def command_info(device: HidppDevice, arguments) -> None:
    description = read_description(device)
    state = load_profile_state(device, description)
    current_index = active_profile_index(device)
    onboard_mode = device.onboard(ONBOARD_GET_MODE)[0]
    print(f"Description: {description}")
    print(f"Onboard mode: {onboard_mode} (1 = onboard), active profile: {current_index + 1}")
    print(f"Profile dictionary CRC valid: {state['dictionary_valid']}")
    for profile in state["profiles"]:
        origin = "ROM default" if profile["from_rom"] else "user"
        marker = "  <- active" if profile["index"] == current_index else ""
        print(f"\nProfile {profile['index'] + 1} ({origin}, sector 0x{profile['source_sector']:04x}){marker}")
        for slot in range(description["button_count"]):
            binding = binding_at(profile["source_bytes"], slot)
            print(f"  slot {slot:2d}: {describe_binding(binding):28s} [{binding.hex()}]")


def command_dump(device: HidppDevice, arguments) -> None:
    description = read_description(device)
    state = load_profile_state(device, description)
    print(write_backup("dictionary", DICTIONARY_SECTOR, state["dictionary_bytes"]))
    for profile in state["profiles"]:
        print(write_backup(f"profile{profile['index'] + 1}-user", profile["user_sector"], profile["user_bytes"]))
        if profile["from_rom"]:
            print(write_backup(f"profile{profile['index'] + 1}-rom", profile["source_sector"], profile["source_bytes"]))


def build_dictionary(enabled_profile_indexes: set, profile_count: int, sector_size: int) -> bytes:
    # Mirrors the G604's own on-flash format: [sector be16][enabled][0xff] per entry.
    dictionary = bytearray()
    for profile_index in range(profile_count):
        enabled_flag = 1 if profile_index in enabled_profile_indexes else 0
        dictionary += user_sector_for(profile_index).to_bytes(2, "big") + bytes([enabled_flag, 0xFF])
    dictionary += DICTIONARY_END_MARKER.to_bytes(2, "big")
    dictionary = dictionary.ljust(sector_size, b"\xff")
    return with_fresh_crc(bytes(dictionary))


def dictionary_with_profile_enabled(dictionary_bytes: bytes, profile_index: int) -> bytes:
    entry_offset = profile_index * DICTIONARY_ENTRY_SIZE
    entry_sector = int.from_bytes(dictionary_bytes[entry_offset:entry_offset + 2], "big")
    if entry_sector != user_sector_for(profile_index):
        sys.exit(f"Unexpected dictionary entry for profile {profile_index + 1}: sector 0x{entry_sector:04x}. Aborting.")
    edited_dictionary = bytearray(dictionary_bytes)
    edited_dictionary[entry_offset + DICTIONARY_ENABLED_OFFSET] = 1
    return with_fresh_crc(bytes(edited_dictionary))


def apply_mouse_buttons(device: HidppDevice, button_for_slot: dict, profile_number, dry_run: bool) -> None:
    description = read_description(device)
    for slot, button in button_for_slot.items():
        if not 0 <= slot < description["button_count"]:
            sys.exit(f"Slot must be 0..{description['button_count'] - 1}; run `info` to see slots.")
        if not 1 <= button <= 16:
            sys.exit("Mouse button number must be 1..16.")

    state = load_profile_state(device, description)
    target_index = profile_number - 1 if profile_number else active_profile_index(device)
    target_profile = state["profiles"][target_index]

    edited_profile = bytearray(target_profile["source_bytes"])
    changed_slot_count = 0
    for slot, button in button_for_slot.items():
        old_binding = binding_at(target_profile["source_bytes"], slot)
        new_binding = mouse_button_binding(button)
        if old_binding == new_binding and not target_profile["from_rom"]:
            print(f"Profile {target_index + 1}, slot {slot}: already {describe_binding(new_binding)}")
            continue
        print(f"Profile {target_index + 1}, slot {slot}: {describe_binding(old_binding)} -> {describe_binding(new_binding)}")
        binding_offset = PROFILE_BUTTONS_OFFSET + slot * BUTTON_BINDING_SIZE
        edited_profile[binding_offset:binding_offset + BUTTON_BINDING_SIZE] = new_binding
        changed_slot_count += 1
    if changed_slot_count == 0:
        print("Nothing to write.")
        return
    new_profile_bytes = with_fresh_crc(bytes(edited_profile))

    planned_writes = [(target_profile["user_sector"], new_profile_bytes, f"profile {target_index + 1}")]
    if state["dictionary_valid"]:
        if target_profile["from_rom"]:
            new_dictionary = dictionary_with_profile_enabled(state["dictionary_bytes"], target_index)
            planned_writes.append((DICTIONARY_SECTOR, new_dictionary, "profile dictionary (enable target)"))
    else:
        # User area never initialized: enable only the target profile so the
        # dictionary never points at an unwritten sector.
        new_dictionary = build_dictionary({target_index}, description["profile_count"], description["sector_size"])
        planned_writes.append((DICTIONARY_SECTOR, new_dictionary, "profile dictionary (new)"))

    for sector, _, label in planned_writes:
        print(f"  will write sector 0x{sector:04x} ({label})")
    if dry_run:
        print("Dry run: nothing written.")
        return

    write_backup("dictionary", DICTIONARY_SECTOR, state["dictionary_bytes"])
    for profile in state["profiles"]:
        write_backup(f"profile{profile['index'] + 1}-user", profile["user_sector"], profile["user_bytes"])
    print(f"Backups saved in {BACKUP_DIRECTORY}")

    ensure_onboard_mode(device)
    for sector, sector_bytes, label in planned_writes:
        write_sector(device, sector, sector_bytes)
        print(f"  wrote + verified sector 0x{sector:04x} ({label})")

    reload_parameters = bytes([0, target_index + 1])
    device.onboard(ONBOARD_SET_CURRENT_PROFILE, reload_parameters)
    print("Done. Profile reloaded; press the button to test.")


def command_set_button(device: HidppDevice, arguments) -> None:
    apply_mouse_buttons(device, {arguments.slot: arguments.button}, arguments.profile, arguments.dry_run)


def command_remap_g(device: HidppDevice, arguments) -> None:
    button_for_slot = {
        slot_for_g_button(g_number): mouse_button
        for g_number, mouse_button in MOUSE_BUTTON_FOR_G_BUTTON.items()
    }
    apply_mouse_buttons(device, button_for_slot, arguments.profile, arguments.dry_run)


def command_restore(device: HidppDevice, arguments) -> None:
    backup_path = pathlib.Path(arguments.backup_file)
    sector_hex = backup_path.stem.rsplit("sector-", 1)[-1]
    sector = int(sector_hex, 16)
    backup_bytes = backup_path.read_bytes()
    description = read_description(device)
    if len(backup_bytes) != description["sector_size"]:
        sys.exit(f"Backup is {len(backup_bytes)} bytes; device sector size is {description['sector_size']}.")
    if sector >= ROM_PROFILES_BASE_SECTOR:
        sys.exit("Refusing to write a ROM sector.")
    print(f"Restore {backup_path.name} -> sector 0x{sector:04x} (CRC valid in backup: {sector_has_valid_crc(backup_bytes)})")
    if arguments.dry_run:
        print("Dry run: nothing written.")
        return
    write_sector(device, sector, backup_bytes)
    reload_parameters = bytes([0, active_profile_index(device) + 1])
    device.onboard(ONBOARD_SET_CURRENT_PROFILE, reload_parameters)
    print("Restored + verified. Active profile reloaded.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Logitech onboard-profile editor for macOS (HID++ 0x8100).")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("info", help="show profiles and button bindings")
    subcommands.add_parser("dump", help="back up dictionary + profile sectors to ./backups")
    set_button = subcommands.add_parser("set-button", help="bind a button slot to a generic mouse button")
    set_button.add_argument("slot", type=int, help="button slot from `info`")
    set_button.add_argument("button", type=int, help="mouse button number, e.g. 6")
    set_button.add_argument("--profile", type=int, help="1-based profile (default: active)")
    set_button.add_argument("--dry-run", action="store_true")
    remap_g = subcommands.add_parser("remap-g", help="bind G4-G9 to generic mouse buttons (no-op if already set)")
    remap_g.add_argument("--profile", type=int, help="1-based profile (default: active)")
    remap_g.add_argument("--dry-run", action="store_true")
    restore = subcommands.add_parser("restore", help="write a backup file back to its sector")
    restore.add_argument("backup_file")
    restore.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    commands = {
        "info": command_info,
        "dump": command_dump,
        "set-button": command_set_button,
        "remap-g": command_remap_g,
        "restore": command_restore,
    }
    device = open_onboard_device()
    try:
        commands[arguments.command](device, arguments)
    except HidppError as error:
        sys.exit(f"HID++ error: {error}")


if __name__ == "__main__":
    main()
