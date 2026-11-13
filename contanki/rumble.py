"""Native DualSense rumble output backed by SDL3 HIDAPI.

The browser Gamepad API remains the sole input backend. This module only opens
the matching Sony DualSense device for rumble output.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SDL_INIT_GAMEPAD = 0x00002000
DUALSENSE_VENDOR = 0x054C
DUALSENSE_PRODUCT = 0x0CE6
MAX_DURATION_MS = 60_000
EASE_KEYS = {1: "again", 2: "hard", 3: "good", 4: "easy"}


def default_rumble_config() -> dict[str, Any]:
    """Return safe defaults for all four Anki answer grades."""
    return {
        "enabled": True,
        "again": {
            "enabled": True,
            "left_intensity": 100,
            "right_intensity": 100,
            "duration_ms": 1000,
        },
        "hard": {
            "enabled": True,
            "left_intensity": 50,
            "right_intensity": 50,
            "duration_ms": 200,
        },
        "good": {
            "enabled": True,
            "left_intensity": 80,
            "right_intensity": 80,
            "duration_ms": 700,
        },
        "easy": {
            "enabled": True,
            "left_intensity": 20,
            "right_intensity": 20,
            "duration_ms": 200,
        },
    }


def migrate_legacy_rumble_config(legacy: Any) -> dict[str, Any]:
    """Convert the old XInput add-on's seconds and 0..1 intensity values."""
    migrated = default_rumble_config()
    if not isinstance(legacy, dict):
        return migrated

    for key in EASE_KEYS.values():
        old = legacy.get(key)
        if not isinstance(old, dict):
            continue
        try:
            duration_ms = round(float(old.get("duration", 0.5)) * 1000)
            intensity = round(float(old.get("intensity", 0.5)) * 100)
        except (TypeError, ValueError):
            continue
        intensity = max(0, min(100, intensity))
        migrated[key] = {
            "enabled": True,
            "left_intensity": intensity,
            "right_intensity": intensity,
            "duration_ms": max(0, min(MAX_DURATION_MS, duration_ms)),
        }
    return migrated


def normalize_rumble_config(value: Any) -> dict[str, Any]:
    """Validate persisted settings and fill missing fields safely."""
    defaults = default_rumble_config()
    if not isinstance(value, dict):
        return defaults

    result: dict[str, Any] = {"enabled": bool(value.get("enabled", True))}
    for key in EASE_KEYS.values():
        raw = value.get(key)
        if not isinstance(raw, dict):
            raw = {}
        fallback = defaults[key]

        def bounded_int(name: str, fallback_value: int, maximum: int) -> int:
            try:
                return max(0, min(maximum, int(raw.get(name, fallback_value))))
            except (TypeError, ValueError):
                return fallback_value

        # Accept the legacy field names as a defensive migration path.
        legacy_intensity = raw.get("intensity")
        left = raw.get(
            "left_intensity",
            legacy_intensity if legacy_intensity is not None else fallback["left_intensity"],
        )
        right = raw.get(
            "right_intensity",
            legacy_intensity if legacy_intensity is not None else fallback["right_intensity"],
        )
        try:
            left_number = float(left)
            if legacy_intensity is not None and "left_intensity" not in raw and 0 <= left_number <= 1:
                left_number *= 100
            left_value = int(round(left_number))
            right_number = float(right)
            if legacy_intensity is not None and "right_intensity" not in raw and 0 <= right_number <= 1:
                right_number *= 100
            right_value = int(round(right_number))
        except (TypeError, ValueError, OverflowError):
            left_value = fallback["left_intensity"]
            right_value = fallback["right_intensity"]

        result[key] = {
            "enabled": bool(raw.get("enabled", True)),
            "left_intensity": max(0, min(100, left_value)),
            "right_intensity": max(0, min(100, right_value)),
            "duration_ms": bounded_int("duration_ms", fallback["duration_ms"], MAX_DURATION_MS),
        }
    return result


class RumbleManager:
    """Owns one SDL3 gamepad handle and safely sends DualSense rumble."""

    def __init__(self, dll_path: str | Path | None = None) -> None:
        self.dll_path = (
            Path(dll_path) if dll_path else Path(__file__).with_name("SDL3.dll")
        )
        self.controller_id: str | None = None
        self._dll: Any = None
        self._dll_directory: Any = None
        self._initialized = False
        self._gamepad: int | None = None
        self._instance_id: int | None = None
        self._rumble_generation = 0

    @staticmethod
    def _decode(value: bytes | None) -> str:
        return value.decode("utf-8", errors="replace") if value else ""

    def _error(self) -> str:
        if self._dll is None:
            return "SDL3 is not loaded"
        return self._decode(self._dll.SDL_GetError()) or "unknown SDL3 error"

    def _bind(self) -> None:
        lib = self._dll
        lib.SDL_GetVersion.argtypes = []
        lib.SDL_GetVersion.restype = ctypes.c_int
        lib.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.SDL_SetHint.restype = ctypes.c_bool
        lib.SDL_InitSubSystem.argtypes = [ctypes.c_uint32]
        lib.SDL_InitSubSystem.restype = ctypes.c_bool
        lib.SDL_QuitSubSystem.argtypes = [ctypes.c_uint32]
        lib.SDL_QuitSubSystem.restype = None
        lib.SDL_GetError.argtypes = []
        lib.SDL_GetError.restype = ctypes.c_char_p
        lib.SDL_GetGamepads.argtypes = [ctypes.POINTER(ctypes.c_int)]
        lib.SDL_GetGamepads.restype = ctypes.POINTER(ctypes.c_uint32)
        lib.SDL_free.argtypes = [ctypes.c_void_p]
        lib.SDL_free.restype = None
        for name in ("SDL_GetGamepadNameForID", "SDL_GetGamepadPathForID"):
            fn = getattr(lib, name)
            fn.argtypes = [ctypes.c_uint32]
            fn.restype = ctypes.c_char_p
        for name in ("SDL_GetGamepadVendorForID", "SDL_GetGamepadProductForID"):
            fn = getattr(lib, name)
            fn.argtypes = [ctypes.c_uint32]
            fn.restype = ctypes.c_uint16
        lib.SDL_OpenGamepad.argtypes = [ctypes.c_uint32]
        lib.SDL_OpenGamepad.restype = ctypes.c_void_p
        lib.SDL_CloseGamepad.argtypes = [ctypes.c_void_p]
        lib.SDL_CloseGamepad.restype = None
        lib.SDL_RumbleGamepad.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32
        ]
        lib.SDL_RumbleGamepad.restype = ctypes.c_bool

    def _initialize(self) -> bool:
        if self._initialized:
            return True
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            logger.warning("DualSense rumble is supported only by the bundled Windows x64 backend.")
            return False
        if not self.dll_path.is_file():
            logger.warning("SDL3 rumble backend is missing: %s", self.dll_path)
            return False

        try:
            if hasattr(os, "add_dll_directory"):
                self._dll_directory = os.add_dll_directory(str(self.dll_path.parent))
            self._dll = ctypes.CDLL(str(self.dll_path))
            self._bind()
            version = int(self._dll.SDL_GetVersion())
            if version < 3_002_000:
                raise RuntimeError(f"SDL3 3.2.0 or newer is required; found {version}")
            for name, value in (
                (b"SDL_JOYSTICK_HIDAPI", b"1"),
                (b"SDL_JOYSTICK_HIDAPI_PS5", b"1"),
                (b"SDL_JOYSTICK_ENHANCED_REPORTS", b"auto"),
            ):
                self._dll.SDL_SetHint(name, value)
            if not self._dll.SDL_InitSubSystem(SDL_INIT_GAMEPAD):
                raise RuntimeError(f"SDL_InitSubSystem failed: {self._error()}")
            self._initialized = True
            logger.info("SDL3 rumble backend initialized (version=%d, enhanced_reports=auto).", version)
            return True
        except Exception:
            logger.exception("Unable to initialize SDL3 rumble backend.")
            self.close()
            return False

    def set_controller_id(self, controller_id: str | None) -> None:
        """Update the selected browser Gamepad API identity."""
        if controller_id == self.controller_id:
            return
        self.stop()
        self._close_gamepad()
        self.controller_id = controller_id

    def _browser_vendor_product(self) -> tuple[int, int] | None:
        if not self.controller_id:
            return None
        match = re.search(
            r"Vendor:\s*([0-9a-f]{1,4}).*?Product:\s*([0-9a-f]{1,4})",
            self.controller_id,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        return int(match.group(1), 16), int(match.group(2), 16)

    def _enumerate(self) -> list[dict[str, Any]]:
        count = ctypes.c_int(0)
        ids = self._dll.SDL_GetGamepads(ctypes.byref(count))
        if not ids:
            return []
        devices: list[dict[str, Any]] = []
        try:
            for index in range(count.value):
                instance_id = int(ids[index])
                devices.append({
                    "instance_id": instance_id,
                    "name": self._decode(self._dll.SDL_GetGamepadNameForID(instance_id)),
                    "path": self._decode(self._dll.SDL_GetGamepadPathForID(instance_id)),
                    "vendor": int(self._dll.SDL_GetGamepadVendorForID(instance_id)),
                    "product": int(self._dll.SDL_GetGamepadProductForID(instance_id)),
                })
        finally:
            self._dll.SDL_free(ctypes.cast(ids, ctypes.c_void_p))
        return devices

    def _close_gamepad(self) -> None:
        if self._gamepad and self._dll is not None:
            try:
                self._dll.SDL_CloseGamepad(self._gamepad)
            except Exception:
                logger.exception("Failed to close SDL3 gamepad.")
        self._gamepad = None
        self._instance_id = None

    def _ensure_gamepad(self) -> bool:
        if self._gamepad:
            return True
        if not self.controller_id:
            logger.warning("No active Contanki controller identity; refusing to rumble an arbitrary device.")
            return False
        if not self._initialize():
            return False

        browser_identity = self._browser_vendor_product()
        if browser_identity is not None and browser_identity != (DUALSENSE_VENDOR, DUALSENSE_PRODUCT):
            logger.info("Selected controller is not a DualSense; rumble is not applicable.")
            return False
        if browser_identity is None and not re.search(
            r"dualsense|wireless controller", self.controller_id, re.IGNORECASE
        ):
            logger.info("Selected browser controller identity does not identify a DualSense.")
            return False

        candidates = [
            device for device in self._enumerate()
            if device["vendor"] == DUALSENSE_VENDOR
            and device["product"] == DUALSENSE_PRODUCT
        ]
        if len(candidates) != 1:
            logger.warning(
                "Expected exactly one SDL DualSense matching the active Contanki device; found %d.",
                len(candidates),
            )
            return False

        device = candidates[0]
        handle = self._dll.SDL_OpenGamepad(device["instance_id"])
        if not handle:
            logger.warning("SDL_OpenGamepad failed for %s: %s", device["name"], self._error())
            return False
        self._gamepad = handle
        self._instance_id = device["instance_id"]
        logger.info(
            "SDL3 rumble device opened: %s (%04X:%04X).",
            device["name"], device["vendor"], device["product"],
        )
        return True

    def rumble(self, left_intensity: int, right_intensity: int, duration_ms: int) -> bool:
        """Send one non-blocking rumble command."""
        left = round(max(0, min(100, int(left_intensity))) * 65535 / 100)
        right = round(max(0, min(100, int(right_intensity))) * 65535 / 100)
        duration = max(0, min(MAX_DURATION_MS, int(duration_ms)))
        if duration == 0 or (left == 0 and right == 0):
            return self.stop()
        if not self._ensure_gamepad():
            return False
        self._rumble_generation += 1
        generation = self._rumble_generation
        if not self._dll.SDL_RumbleGamepad(self._gamepad, left, right, duration):
            logger.warning("SDL_RumbleGamepad failed: %s", self._error())
            self._close_gamepad()
            return False
        # SDL's duration is a driver-side hint. Also schedule an explicit stop
        # on Anki's Qt event loop so hardware/driver timing cannot leave a long
        # pulse running beyond the user's configured duration.
        self._schedule_stop(duration, generation)
        return True

    def _schedule_stop(self, duration_ms: int, generation: int) -> None:
        """Schedule a generation-safe stop without creating a worker thread."""
        try:
            from aqt.qt import QTimer

            QTimer.singleShot(
                duration_ms,
                lambda: self._stop_if_generation(generation),
            )
        except Exception:
            # SDL still receives its native duration; this is a defensive
            # fallback for standalone tests or unusual Anki Qt environments.
            logger.exception("Unable to schedule the rumble safety stop.")

    def _stop_if_generation(self, generation: int) -> None:
        if generation == self._rumble_generation:
            self.stop()

    def trigger(self, ease: int, config: Any) -> bool:
        """Play the configured feedback for Again/Hard/Good/Easy."""
        settings = normalize_rumble_config(config)
        try:
            key = EASE_KEYS.get(int(ease))
        except (TypeError, ValueError):
            return False
        if not self.controller_id:
            return False
        if not settings["enabled"] or key is None:
            return False
        grade = settings[key]
        if not grade["enabled"]:
            return False
        return self.rumble(
            grade["left_intensity"],
            grade["right_intensity"],
            grade["duration_ms"],
        )

    def test(self) -> bool:
        """Send a short, moderate test pulse to both motors."""
        return self.rumble(50, 50, 300)

    def stop(self) -> bool:
        """Stop both motors immediately when a device is open."""
        self._rumble_generation += 1
        if not self._gamepad or not self._dll:
            return True
        ok = bool(self._dll.SDL_RumbleGamepad(self._gamepad, 0, 0, 0))
        if not ok:
            logger.warning("SDL3 rumble stop failed: %s", self._error())
        return ok

    def close(self) -> None:
        """Stop output and release SDL resources."""
        self.stop()
        self._close_gamepad()
        if self._initialized and self._dll is not None:
            try:
                self._dll.SDL_QuitSubSystem(SDL_INIT_GAMEPAD)
            except Exception:
                logger.exception("Failed to shut down SDL3 gamepad subsystem.")
        self._initialized = False
        self._dll = None
        if self._dll_directory is not None:
            try:
                self._dll_directory.close()
            except Exception:
                pass
            self._dll_directory = None
