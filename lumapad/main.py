#!/usr/bin/env python3
"""
Gamepad LED Brightness Controller Service
Adjusts gamepad LED brightness based on room illuminance via MQTT
"""

import os
import sys
import json
import logging
import threading
import time
from pathlib import Path
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


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

    @classmethod
    def from_file(cls, config_path: str = "/etc/gamepad-led-service/config.json") -> 'Config':
        """Load configuration from JSON file"""
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                data = json.load(f)
                return cls(**data)
        return cls()


class LEDController(ABC):
    """Abstract base class for gamepad LED controllers"""

    def __init__(self, device_path: str):
        self.device_path = device_path
        self.device_name = Path(device_path).name
        self.last_update = 0

    @abstractmethod
    def set_brightness(self, illuminance: float) -> bool:
        """Set LED brightness based on illuminance value"""
        pass

    @abstractmethod
    def is_supported(self) -> bool:
        """Check if device is supported"""
        pass

    @abstractmethod
    def revert(self) -> bool:
        """Revert the device to its original state"""
        pass

    def _calculate_brightness(self, illuminance: float, min_val: int, max_val: int) -> int:
        """
        Convert illuminance (0-65535 lux typically) to brightness value.
        Uses logarithmic scaling for better perceived brightness control.
        """
        # Normalize illuminance to 0-1 range
        normalized = min(max(illuminance / 65535.0, 0.0), 1.0)

        # Apply logarithmic scaling for better perception
        scaled = (2.718281828 ** (normalized * 2) - 1) / (2.718281828 ** 2 - 1)

        # Map to device range
        return int(min_val + scaled * (max_val - min_val))

    def _write_sysfs(self, path: str, value: str) -> bool:
        """Write value to sysfs file"""
        try:
            with open(path, 'w') as f:
                f.write(str(value))
            return True
        except (IOError, OSError) as e:
            logger.error(f"Failed to write to {path}: {e}")
            return False

    def _read_sysfs(self, path: str) -> Optional[str]:
        """Read value from sysfs file"""
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except (IOError, OSError) as e:
            logger.error(f"Failed to read from {path}: {e}")
            return None


class XboxOneController(LEDController):
    """Xbox One controller LED control (xpad driver)"""

    def __init__(self, device_path: str):
        super().__init__(device_path)
        self.brightness_path = os.path.join(device_path, "brightness")

    def is_supported(self) -> bool:
        return os.path.exists(self.brightness_path)

    def set_brightness(self, illuminance: float) -> bool:
        """Set brightness 0-50 for Xbox One controller"""
        brightness = self._calculate_brightness(illuminance, 1, 50)
        logger.debug(f"Setting Xbox One ({self.device_name}) brightness to {brightness}")
        return self._write_sysfs(self.brightness_path, brightness)


class PS5DualsenseController(LEDController):
    """PS5 DualSense controller RGB LED control"""

    def __init__(self, device_path: str):
        super().__init__(device_path)
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
        # 65535
        # Convert illuminance (0-20 lux) to brightness percentage (0.0-1.0)
        brightness_percentage = min(max(illuminance / 20.0, 0.0), 1.0)

        # Apply logarithmic scaling for better perceived brightness control
        brightness_percentage = (2.718281828 ** (brightness_percentage * 2) - 1) / (2.718281828 ** 2 - 1)

        # Scale the original color by brightness percentage
        scaled_color = self._scale_color(self.initial_color, brightness_percentage)

        # Format as "r g b" and write to sysfs
        color_str = f"{scaled_color[0]} {scaled_color[1]} {scaled_color[2]}"
        logger.debug(f"Setting DualSense ({self.device_name}) to RGB{scaled_color} (illuminance: {illuminance})")

        return self._write_sysfs(self.multi_intensity_path, color_str)


class GamepadLEDService:
    """Main service for managing gamepad LEDs"""

    def __init__(self, config: Config):
        self.config = config
        self.controllers: Dict[str, LEDController] = {}
        self.current_illuminance = 0.0
        self.running = False
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

    def _get_controller_type(self, led_entry: str) -> Optional[type]:
        """Determine controller type from LED entry name"""
        # Skip Xbox 360 controllers
        if "xpad" in led_entry and "xbox360" in led_entry.lower():
            logging.debug("Device: {} is an xpad device, skipping".format(led_entry))
            return None

        # Xbox One controller
        if "gip" in led_entry:
            logging.debug("Device: {} is an xone device, adding".format(led_entry))
            return XboxOneController

        # PS5 DualSense controller
        if "rgb:indicator" in led_entry.lower():
            logging.debug("Device: {} is a PS5 controller, adding".format(led_entry))
            return PS5DualsenseController

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

        with self.lock:
            existing_devices = set(self.controllers.keys())

            # Find new devices
            new_devices = current_devices - existing_devices
            for led_entry in new_devices:
                led_path = os.path.join(self.config.leds_base_path, led_entry)
                controller_type = self._get_controller_type(led_entry)

                if controller_type is None:
                    continue

                try:
                    controller = controller_type(led_path)
                    if controller.is_supported():
                        self.controllers[led_entry] = controller
                        logger.info(f"Connected: {controller_type.__name__} - {led_entry}")
                        # Update new controller with current illuminance
                        controller.set_brightness(self.current_illuminance)
                except Exception as e:
                    logger.error(f"Failed to initialize controller {led_entry}: {e}")

            # Find disconnected devices
            removed_devices = existing_devices - current_devices
            for led_entry in removed_devices:
                controller = self.controllers.pop(led_entry)
                logger.info(f"Disconnected: {controller.__class__.__name__} - {led_entry}")

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

        # Start controller scanner thread
        self.scanner_thread = threading.Thread(target=self._scanner_loop, daemon=False)
        self.scanner_thread.start()

        try:
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
        except Exception as e:
            logger.error(f"Service error: {e}")
        finally:
            self.stop()

    def stop(self):
        """Stop the service"""
        logger.info("Stopping Gamepad LED Service")
        self.running = False

        # Stop MQTT
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

        # Wait for scanner thread
        if self.scanner_thread:
            self.scanner_thread.join(timeout=5)

        with self.lock:
            print(self.controllers)
            for controller in self.controllers.values():
                controller.revert()
            self.controllers.clear()

        logger.info("Service stopped")


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


if __name__ == "__main__":
    main()
