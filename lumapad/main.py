#!/usr/bin/env python3
"""
Gamepad LED Brightness Controller Service
Adjusts gamepad LED brightness based on room illuminance via MQTT
"""

import os
import sys
import json
import logging
import signal
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# Custom TRACE level, below DEBUG, for very high-frequency/noisy messages
# (e.g. periodic controller scans, individual sysfs reads/writes) that would
# otherwise drown out normal DEBUG output. Use logger.log(TRACE_LEVEL, ...)
# (or the `trace()` helper below) to emit at this level.
TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")
logging.TRACE = TRACE_LEVEL  # type: ignore[attr-defined]


def trace(message: str, *args, **kwargs) -> None:
    logger.log(TRACE_LEVEL, message, *args, **kwargs)

@dataclass
class Config:
    """Service configuration"""
    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    mqtt_topic: str = "home/sensors/light/illuminance"
    mqtt_username: Optional[str] = None
    mqtt_password: Optional[str] = None
    leds_base_path: str = "/sys/class/leds"
    controller_scan_interval: int = 5  # Seconds between scans for new controllers
    log_level: str = "INFO"
    xpad_dark_threshold_lux: float = 1.0  # Below this, xpad heartbeat blink is enabled
    max_illuminance_lux: float = 20.0  # Illuminance at/above which brightness is maxed out

    @classmethod
    def from_file(cls, config_path: str = "/etc/gamepad-led-service/config.json") -> 'Config':
        """Load configuration from JSON file"""
        if os.path.exists(config_path):
            logger.debug(f"Loading config from {config_path}")
            with open(config_path, 'r') as f:
                data = json.load(f)
                return cls(**data)
        logger.debug(f"No config file found at {config_path}, using defaults")
        return cls()


class LEDController(ABC):
    """Abstract base class for gamepad LED controllers"""

    def __init__(self, device_path: str, max_illuminance_lux: float = 20.0):
        self.device_path = device_path
        self.device_name = Path(device_path).name
        self.last_update = 0
        self.max_illuminance_lux = max_illuminance_lux

    @abstractmethod
    def set_brightness(self, illuminance: float) -> bool:
        """Set LED brightness based on illuminance value"""
        pass

    @abstractmethod
    def is_supported(self) -> bool:
        """Check if device is supported"""
        pass

    @abstractmethod
    def revert(self) -> None:
        """Revert the device to its original state"""
        pass

    def start(self) -> None:
        """
        Optional hook called once the controller has been confirmed
        supported and registered. Default no-op; controllers that need
        background management (e.g. a heartbeat blink thread) can
        override this.
        """
        pass

    def _calculate_brightness(self, illuminance: float, min_val: int, max_val: int) -> int:
        """
        Convert illuminance (0 to max_illuminance_lux, typically 0-65535 lux)
        to brightness value. Uses logarithmic scaling for better perceived
        brightness control.
        """
        # Normalize illuminance to 0-1 range
        normalized = min(max(illuminance / self.max_illuminance_lux, 0.0), 1.0)

        # Apply logarithmic scaling for better perception
        scaled = (2.718281828 ** (normalized * 2) - 1) / (2.718281828 ** 2 - 1)

        # Map to device range
        return int(min_val + scaled * (max_val - min_val))

    def _write_sysfs(self, path: str, value: str) -> bool:
        """Write value to sysfs file"""
        try:
            with open(path, 'w') as f:
                f.write(str(value))
            trace(f"Wrote '{value}' to {path}")
            return True
        except (IOError, OSError) as e:
            logger.error(f"Failed to write to {path}: {e}")
            return False

    def _read_sysfs(self, path: str) -> Optional[str]:
        """Read value from sysfs file"""
        try:
            with open(path, 'r') as f:
                value = f.read().strip()
            trace(f"Read '{value}' from {path}")
            return value
        except (IOError, OSError) as e:
            logger.error(f"Failed to read from {path}: {e}")
            return None


class XboxOneController(LEDController):
    """Xbox One controller LED control (xone driver)"""

    def __init__(self, device_path: str, max_illuminance_lux: float = 20.0):
        super().__init__(device_path, max_illuminance_lux)
        self.brightness_path = os.path.join(device_path, "brightness")
        self.initial_brightness = None  # Cache the current brightness

    def is_supported(self) -> bool:
        return os.path.exists(self.brightness_path)

    def revert(self) -> None:
        if self.initial_brightness is None:
            logger.warning(f"No initial brightness recorded for {self.device_name}, skipping revert")
            return
        logger.info(f"Setting Xbox One ({self.device_name}) brightness back to {self.initial_brightness}")
        self._write_sysfs(self.brightness_path, self.initial_brightness)

    def set_brightness(self, illuminance: float) -> bool:
        """Set brightness 0-50 for Xbox One controller"""
        # Read current brightness if we haven't cached it yet
        if self.initial_brightness is None:
            current = self._read_sysfs(self.brightness_path)
            if current is None:
                logger.warning(f"Could not read initial brightness for {self.device_name}")
                return False
            self.initial_brightness = current
            logger.info(f"Xbox One ({self.device_name}) initial brightness: {self.initial_brightness}")

        brightness = self._calculate_brightness(illuminance, 1, 50)
        logger.debug(f"Setting Xbox One ({self.device_name}) brightness to {brightness}")
        return self._write_sysfs(self.brightness_path, brightness)


class PS5DualsenseController(LEDController):
    """PS5 DualSense controller RGB LED control"""

    def __init__(self, device_path: str, max_illuminance_lux: float = 20.0):
        super().__init__(device_path, max_illuminance_lux)
        self.multi_intensity_path = os.path.join(device_path, "multi_intensity")
        self.initial_color = None  # Cache the current color

    def is_supported(self) -> bool:
        return os.path.exists(self.multi_intensity_path)

    def revert(self) -> None:
        logger.info("Setting {} back to RGB{}".format(self.device_name, self.initial_color))
        color_str = f"{self.initial_color[0]} {self.initial_color[1]} {self.initial_color[2]}"
        self._write_sysfs(self.multi_intensity_path, color_str)

    def _read_current_color(self) -> Optional[tuple]:
        """Read the current RGB values from the device"""
        color_str = self._read_sysfs(self.multi_intensity_path)
        if not color_str:
            return None

        try:
            values = [int(v.strip()) for v in color_str.split()]
            if len(values) == 3:
                if sum(values) == 0:
                    # TODO: Allow configuring custom default color
                    return (0, 0, 255)
                return tuple(values)
        except ValueError:
            logger.error(f"Failed to parse color values: {color_str}")

        return None

    def _get_max_component(self, color: tuple) -> int:
        """Get the maximum RGB component value"""
        return max(color)

    def _scale_color(self, color: tuple, brightness_percentage: float) -> tuple:
        """
        Scale RGB values by brightness percentage while preserving color.

        brightness_percentage: 0.0 to 1.0
        Returns: (r, g, b) tuple with scaled values
        """
        max_component = self._get_max_component(color)

        if max_component == 0:
            # If color is black, return black
            return (0, 0, 0)

        # Calculate scaling factor to maintain color ratio
        scale_factor = brightness_percentage

        # Scale each component proportionally
        scaled = tuple(int(c * scale_factor) for c in color)

        return scaled

    def set_brightness(self, illuminance: float) -> bool:
        """
        Set brightness for DualSense controller while preserving color.

        Reads current color on first call, then scales it based on illuminance.
        """
        # Read current color if we haven't cached it yet
        if self.initial_color is None:
            self.initial_color = self._read_current_color()

            if self.initial_color is None:
                logger.warning(f"Could not read initial color for {self.device_name}")
                return False

            logger.info(f"DualSense ({self.device_name}) initial color: RGB{self.initial_color}")

        # Convert illuminance (0 to max_illuminance_lux) to brightness percentage (0.0-1.0)
        brightness_percentage = min(max(illuminance / self.max_illuminance_lux, 0.0), 1.0)

        # Apply logarithmic scaling for better perceived brightness control
        brightness_percentage = (2.718281828 ** (brightness_percentage * 2) - 1) / (2.718281828 ** 2 - 1)

        # Scale the original color by brightness percentage
        scaled_color = self._scale_color(self.initial_color, brightness_percentage)

        # Format as "r g b" and write to sysfs
        color_str = f"{scaled_color[0]} {scaled_color[1]} {scaled_color[2]}"
        logger.debug(f"Setting DualSense ({self.device_name}) to RGB{scaled_color} (illuminance: {illuminance})")

        return self._write_sysfs(self.multi_intensity_path, color_str)


class PS5DualsensePlayerLEDController(LEDController):
    """
    PS5 DualSense player-indicator LED brightness control.

    The DualSense exposes 5 separate LED class devices (one per player
    slot), each with its own `brightness`/`max_brightness` sysfs pair.
    Only the LEDs that are part of the controller's current player-number
    pattern are lit (non-zero); the rest are already off (0) and should
    stay that way. Each entry is otherwise controlled completely
    independently of the others.
    """

    def __init__(self, device_path: str, max_illuminance_lux: float = 20.0):
        super().__init__(device_path, max_illuminance_lux)
        self.brightness_path = os.path.join(device_path, "brightness")
        self.max_brightness_path = os.path.join(device_path, "max_brightness")
        self.initial_brightness = None  # Cache the current brightness
        self.max_brightness = None  # Cache the device's max_brightness (typically 3)

    def is_supported(self) -> bool:
        return os.path.exists(self.brightness_path) and os.path.exists(self.max_brightness_path)

    def revert(self) -> None:
        if self.initial_brightness is None:
            logger.warning(f"No initial brightness recorded for {self.device_name}, skipping revert")
            return
        logger.info(f"Setting DualSense player LED ({self.device_name}) brightness back to {self.initial_brightness}")
        self._write_sysfs(self.brightness_path, self.initial_brightness)

    def set_brightness(self, illuminance: float) -> bool:
        """
        Set brightness for a single player-indicator LED segment.

        Only LEDs that were initially lit (part of the active
        player-number pattern) are adjusted; LEDs that started off are
        left alone so we don't spuriously light up the wrong pattern.
        """
        if self.initial_brightness is None:
            current = self._read_sysfs(self.brightness_path)
            if current is None:
                logger.warning(f"Could not read initial brightness for {self.device_name}")
                return False
            self.initial_brightness = int(current)

            max_brightness_str = self._read_sysfs(self.max_brightness_path)
            self.max_brightness = int(max_brightness_str) if max_brightness_str else 3

            logger.info(
                f"DualSense player LED ({self.device_name}) initial brightness: "
                f"{self.initial_brightness}/{self.max_brightness}"
            )

        if self.initial_brightness == 0:
            # Not part of the active player-number pattern, leave it off.
            return True

        # Only a handful of discrete levels are available (typically
        # 0-3), so never dim an active segment all the way to off - keep
        # it within 1..max_brightness.
        brightness = self._calculate_brightness(illuminance, 1, self.max_brightness)
        logger.debug(f"Setting DualSense player LED ({self.device_name}) brightness to {brightness}")
        return self._write_sysfs(self.brightness_path, brightness)


class XpadController(LEDController):
    """
    Xbox 360-style controller LED control (xpad driver).

    The xpad "brightness" attribute is not a real brightness dial - it's an
    LED effect/mode selector (off, player-N solid, blink patterns, rotate,
    etc). There's no way to dim these LEDs, so instead of mapping illuminance
    to brightness we blink the LED briefly once in a while as a "still alive"
    heartbeat when the room is dark, and otherwise leave it alone.

    The driver/hardware can also drive this attribute on its own (there's no
    reliable way to distinguish this from another process writing to it, and
    sysfs doesn't notify us about it either way) - most notably, we don't
    want to fight the pad's player-slot indicator. We poll the attribute to
    detect when its value no longer matches what we last wrote, and back off
    until it settles back to the value we captured when we started managing
    the pad.
    """

    POLL_INTERVAL = 1.0        # Seconds between polls for external changes
    BLINK_INTERVAL = 10.0      # Seconds between heartbeat blinks
    BLINK_DURATION = 1.0       # Seconds the blink stays "on"
    REVERT_TIMEOUT = 15.0      # Max seconds to wait for external control to clear on shutdown

    def __init__(self, device_path: str, dark_threshold_lux: float = 1.0):
        super().__init__(device_path)
        self.brightness_path = os.path.join(device_path, "brightness")
        self.dark_threshold_lux = dark_threshold_lux

        self.initial_brightness: Optional[str] = None
        self._last_written: Optional[str] = None
        self._external_override = False
        self._current_illuminance = 0.0
        # We don't know the real illuminance until the first MQTT reading
        # comes in - until then, assume it's NOT dark so we don't blink
        # based on the placeholder 0.0 default.
        self._have_illuminance_reading = False

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state_lock = threading.RLock()

    def is_supported(self) -> bool:
        return os.path.exists(self.brightness_path)

    def start(self) -> None:
        """Capture the pad's current LED state and start the heartbeat thread"""
        # xpad briefly animates through a "N flashes, then on" code while
        # assigning a player slot right after the pad connects. Give it a
        # moment to settle so we don't capture one of those transient codes
        # as our baseline (which would replay the whole flash animation
        # every time we use it as the heartbeat "on" pulse).
        time.sleep(5)

        current = self._read_sysfs(self.brightness_path)
        if current is None:
            logger.warning(f"Could not read initial LED state for {self.device_name}")
            return

        self.initial_brightness = current
        self._last_written = current
        logger.info(f"Xpad ({self.device_name}) initial LED state: {self.initial_brightness}")

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_brightness(self, illuminance: float) -> bool:
        """
        Xpad doesn't support real brightness control - just record the
        illuminance so the heartbeat thread can decide whether it's dark
        enough to blink.
        """
        with self._state_lock:
            self._current_illuminance = illuminance
            self._have_illuminance_reading = True
        return True

    def _write_value(self, value: str) -> bool:
        ok = self._write_sysfs(self.brightness_path, value)
        if ok:
            self._last_written = value
        return ok

    def _check_external_override(self) -> None:
        """Detect whether something outside of us changed the LED state"""
        current = self._read_sysfs(self.brightness_path)
        if current is None:
            return

        if self._external_override:
            if current == self.initial_brightness:
                logger.info(f"Xpad ({self.device_name}) LED returned to baseline, resuming control")
                self._external_override = False
                self._last_written = current
        elif self._last_written is not None and current != self._last_written:
            logger.info(
                f"Xpad ({self.device_name}) LED changed externally "
                f"({self._last_written} -> {current}), yielding control"
            )
            self._external_override = True
            self._last_written = current

    def _run(self) -> None:
        """
        Background heartbeat loop: while it's dark, keep the LED off except
        for a brief pulse every BLINK_INTERVAL seconds; once it's bright
        again, restore the pad's normal baseline state and leave it alone.
        Yields control whenever something else drives the LED.
        """
        logger.debug(f"Starting Xpad heartbeat thread for {self.device_name}")
        if self.initial_brightness is None:
            return
        last_blink = 0.0
        dark_mode_active = False  # whether we've dimmed the LED for "dark"

        while not self._stop_event.is_set():
            self._check_external_override()

            if not self._external_override:
                with self._state_lock:
                    illuminance = self._current_illuminance
                    have_reading = self._have_illuminance_reading

                # Don't treat the room as dark until we've received a real
                # illuminance reading - otherwise we'd treat the 0.0
                # placeholder value as "pitch dark" and blink regardless of
                # actual room light.
                is_dark = have_reading and illuminance < self.dark_threshold_lux
                now = time.monotonic()

                if not is_dark:
                    # Bright enough - just show the pad's normal LED state
                    if dark_mode_active or self._last_written != self.initial_brightness:
                        self._write_value(self.initial_brightness)
                    dark_mode_active = False
                elif not dark_mode_active:
                    # Just went dark - turn the LED off and start the
                    # heartbeat timer
                    self._write_value("0")
                    dark_mode_active = True
                    last_blink = now
                elif (now - last_blink) >= self.BLINK_INTERVAL:
                    logger.debug(f"Blinking Xpad ({self.device_name})")
                    self._write_value(self.initial_brightness)
                    self._stop_event.wait(self.BLINK_DURATION)

                    # Recheck: don't stomp on an external change that may
                    # have happened while the blink was on
                    self._check_external_override()
                    if not self._external_override:
                        self._write_value("0")

                    last_blink = now

            self._stop_event.wait(self.POLL_INTERVAL)

        logger.debug(f"Stopping Xpad heartbeat thread for {self.device_name}")

    def revert(self) -> None:
        """
        Restore the pad's original LED state, waiting (up to a timeout) for
        any externally-driven state to settle back to baseline first.
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.POLL_INTERVAL * 2)

        if self.initial_brightness is None:
            return

        if self._external_override:
            logger.info(
                f"Xpad ({self.device_name}) LED is externally controlled, "
                f"waiting up to {self.REVERT_TIMEOUT:.0f}s for it to settle"
            )
            deadline = time.monotonic() + self.REVERT_TIMEOUT
            while time.monotonic() < deadline:
                current = self._read_sysfs(self.brightness_path)
                if current == self.initial_brightness:
                    self._external_override = False
                    break
                time.sleep(self.POLL_INTERVAL)
            else:
                logger.warning(
                    f"Timed out waiting for Xpad ({self.device_name}) LED to settle; reverting anyway"
                )

        logger.info(f"Setting Xpad ({self.device_name}) brightness back to {self.initial_brightness}")
        self._write_sysfs(self.brightness_path, self.initial_brightness)


class GamepadLEDService:
    """Main service for managing gamepad LEDs"""

    def __init__(self, config: Config):
        self.config = config
        self.controllers: Dict[str, LEDController] = {}
        self.current_illuminance = 0.0
        self.running = False
        self.exit_code = 0
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._setup_mqtt()

        # Threads
        self.scanner_thread = None
        self.lock = threading.RLock()

    def _setup_mqtt(self):
        """Configure MQTT client"""
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

        if self.config.mqtt_username and self.config.mqtt_password:
            self.mqtt_client.username_pw_set(
                self.config.mqtt_username,
                self.config.mqtt_password
            )

    def _on_mqtt_connect(self, client, userdata, connect_flags, reason_code, properties):
        """MQTT connection callback"""
        if reason_code == 0:
            logger.info("Connected to MQTT broker")
            client.subscribe(self.config.mqtt_topic)
        else:
            logger.error(f"MQTT connection failed with code {reason_code}")

    def _on_mqtt_message(self, client, userdata, msg):
        """MQTT message callback"""
        try:
            message_json = json.loads(msg.payload.decode())
            self.current_illuminance = message_json["illuminance"]
            logger.debug(f"Received illuminance: {self.current_illuminance} lux")
            self._update_all_controllers()
        # TODO: More exception handling here
        except ValueError:
            logger.error(f"Invalid illuminance value: {msg.payload}")

    def _on_mqtt_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        """MQTT disconnect callback"""
        if reason_code != 0:
            logger.warning(f"Unexpected MQTT disconnection with code {reason_code}")

    def _get_driver_module(self, led_path: str) -> Optional[str]:
        """
        Resolve the kernel driver module backing a LED sysfs entry, by
        following device -> driver -> module symlinks, e.g.:

          /sys/class/leds/<entry>/device/driver/module -> ../../../module/hid_playstation

        Returns the module name (e.g. "hid_playstation") or None if it
        can't be determined (missing symlinks, permissions, etc).
        """
        module_path = os.path.join(led_path, "device", "driver", "module")
        try:
            resolved = os.path.realpath(module_path)
        except OSError as e:
            trace(f"Failed to resolve driver module for {led_path}: {e}")
            return None

        if not os.path.exists(resolved):
            return None

        return os.path.basename(resolved)

    def _get_controller_type(self, led_entry: str, led_path: str) -> Optional[type]:
        """Determine controller type from LED entry name and backing driver module"""
        # Xbox 360-style controller (xpad driver) - no real brightness
        # control, so it's managed separately as a heartbeat-blink pad
        if "xpad" in led_entry:
            trace(f"Device: {led_entry} is an xpad (Xbox 360-style) device, adding")
            return XpadController

        # Xbox One controller
        if "gip" in led_entry:
            trace(f"Device: {led_entry} is an xone device, adding")
            return XboxOneController

        # PS5 DualSense controller - verify the driver module to avoid
        # false-positive matches on unrelated LED devices that happen to
        # share the same naming convention
        if "rgb:indicator" in led_entry.lower() or "white:player-" in led_entry.lower():
            driver_module = self._get_driver_module(led_path)
            if driver_module != "hid_playstation":
                trace(
                    f"Device: {led_entry} looks like a DualSense LED but is backed by "
                    f"driver module '{driver_module}', not 'hid_playstation', skipping"
                )
                return None

            if "rgb:indicator" in led_entry.lower():
                trace(f"Device: {led_entry} is a PS5 controller, adding")
                # Wait a while before messing with the LEDs
                # the DS5 can freak out if the LEDs are touched by multiple programs
                logger.debug("Sleeping 15 seconds to prevent breaking the DS5 LEDs")
                time.sleep(15)
                return PS5DualsenseController

            trace(f"Device: {led_entry} is a PS5 controller player LED, adding")
            return PS5DualsensePlayerLEDController

        trace(f"Device: {led_entry} did not match any known controller type, skipping")
        return None

    def _scan_controllers(self):
        """Scan for supported gamepad controllers"""
        if not os.path.exists(self.config.leds_base_path):
            logger.error(f"LEDs base path not found: {self.config.leds_base_path}")
            return

        try:
            current_devices = set(os.listdir(self.config.leds_base_path))
        except OSError as e:
            logger.error(f"Failed to scan LED devices: {e}")
            return

        trace(
            f"Scanning {len(current_devices)} LED device(s), "
            f"{len(self.controllers)} currently managed"
        )

        with self.lock:
            existing_devices = set(self.controllers.keys())

            # Find new devices
            new_devices = current_devices - existing_devices
            for led_entry in new_devices:
                led_path = os.path.join(self.config.leds_base_path, led_entry)
                trace(f"Found new LED device: {led_entry}")
                controller_type = self._get_controller_type(led_entry, led_path)

                if controller_type is None:
                    continue

                try:
                    if controller_type is XpadController:
                        controller = controller_type(led_path, self.config.xpad_dark_threshold_lux)
                    else:
                        controller = controller_type(led_path, self.config.max_illuminance_lux)

                    if controller.is_supported():
                        controller.start()
                        self.controllers[led_entry] = controller
                        logger.info(f"Connected: {controller_type.__name__} - {led_entry}")
                    else:
                        trace(
                            f"Device: {led_entry} matched {controller_type.__name__} "
                            f"but is not supported (missing expected sysfs attributes), skipping"
                        )
                except Exception as e:
                    logger.error(f"Failed to initialize controller {led_entry}: {e}")

            # Find disconnected devices
            removed_devices = existing_devices - current_devices
            for led_entry in removed_devices:
                controller = self.controllers.pop(led_entry)
                logger.info(f"Disconnected: {controller.__class__.__name__} - {led_entry}")
                logger.debug(f"Reverting and tearing down {led_entry} after disconnect")
                try:
                    controller.revert()
                except Exception as e:
                    logger.error(f"Error tearing down disconnected controller {led_entry}: {e}")


    def _update_all_controllers(self):
        """Update LED brightness for all controllers"""
        with self.lock:
            for device_id, controller in self.controllers.items():
                try:
                    if not controller.set_brightness(self.current_illuminance):
                        logger.warning(f"Failed to update {device_id}")
                except Exception as e:
                    logger.error(f"Error updating {device_id}: {e}")

    def _scanner_loop(self):
        """Background thread that periodically scans for new/disconnected controllers"""
        logger.info("Starting controller scanner thread")

        while self.running:
            try:
                self._scan_controllers()
                time.sleep(self.config.controller_scan_interval)
            except Exception as e:
                logger.error(f"Error in scanner loop: {e}")
                time.sleep(self.config.controller_scan_interval)

        logger.info("Stopping controller scanner thread")

    def run(self):
        """Start the service"""
        self.running = True
        logger.info("Starting Gamepad LED Service")

        # SIGTERM (sent by e.g. `systemctl stop`/`kill`) terminates the
        # process immediately by default, bypassing our cleanup entirely -
        # register a handler so controllers still get reverted on shutdown.
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_signal)

        # Start controller scanner thread
        self.scanner_thread = threading.Thread(target=self._scanner_loop, daemon=False)
        self.scanner_thread.start()

        try:
            logger.debug(
                f"Connecting to MQTT broker {self.config.mqtt_broker}:{self.config.mqtt_port} "
                f"(topic: {self.config.mqtt_topic})"
            )
            self.mqtt_client.connect(
                self.config.mqtt_broker,
                self.config.mqtt_port,
                keepalive=60
            )
            self.mqtt_client.loop_start()

            # Keep service running
            while self.running:
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Service interrupted by user")
            self.stop()
        except Exception as e:
            logger.error(f"Service error: {e}")
            self.stop(1)
        else:
            self.stop()

    def _handle_signal(self, signum, frame) -> None:
        """Handle SIGTERM/SIGHUP by requesting a graceful shutdown"""
        logger.info(f"Received signal {signal.Signals(signum).name}, shutting down")
        self.running = False

    def stop(self, code: int = 0):
        """Stop the service"""
        logger.info("Stopping Gamepad LED Service")
        self.exit_code = code
        self.running = False

        # Stop MQTT
        logger.debug("Disconnecting from MQTT broker")
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

        # Wait for scanner thread
        if self.scanner_thread:
            logger.debug("Waiting for controller scanner thread to stop")
            self.scanner_thread.join(timeout=5)

        with self.lock:
            # Revert all controllers in parallel - some (e.g. XpadController)
            # may block for a while waiting for externally-driven LED state
            # to settle before restoring the original value, and we don't
            # want to wait on each of those sequentially.
            logger.debug(f"Reverting {len(self.controllers)} controller(s)")
            revert_threads = []
            for controller in self.controllers.values():
                t = threading.Thread(target=self._safe_revert, args=(controller,), daemon=True)
                t.start()
                revert_threads.append(t)

            for t in revert_threads:
                t.join(timeout=XpadController.REVERT_TIMEOUT + 5)
                if t.is_alive():
                    logger.warning("A controller took too long to revert; continuing shutdown anyway")

            self.controllers.clear()

        logger.info("Service stopped")

    def _safe_revert(self, controller: LEDController) -> None:
        """Revert a single controller, logging (rather than raising) on failure"""
        logger.debug(f"Reverting {controller.__class__.__name__} - {controller.device_name}")
        try:
            controller.revert()
        except Exception as e:
            logger.error(f"Error reverting {controller.device_name}: {e}")


def main():
    """Entry point"""
    config = Config.from_file("config.json")
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )
    service = GamepadLEDService(config)
    service.run()
    exit(service.exit_code)


if __name__ == "__main__":
    main()
