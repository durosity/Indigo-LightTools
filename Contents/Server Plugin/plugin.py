#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Copyright (c) 2024, Perceptive Automation, LLC. All rights reserved.
# https://www.indigodomo.com
try:
    # This is primarily for IDEs - the indigo package is always included when a plugin is started.
    import indigo
except ImportError:
    pass
import os
import sys
import time
import threading
import json

# Note the "indigo" module is automatically imported and made available inside
# our global name space by the host process.
################################################################################
# Log level constants — higher number = more verbose
LOG_NONE = 0          # Errors only
LOG_INFORMATIONAL = 1 # High-level events (plugin start, scene saves, flash starts)
LOG_DETAILED = 2      # Per-device action results (default)
LOG_DEBUG = 3         # Monitor decision reasoning, per-tick change detection
LOG_EXTENDED = 4      # Full state dumps every monitor cycle (firehose)

LOG_LEVEL_MAP = {
    "none":          LOG_NONE,
    "informational": LOG_INFORMATIONAL,
    "detailed":      LOG_DETAILED,
    "debug":         LOG_DEBUG,
    "extended":      LOG_EXTENDED,
}

LOG_LEVEL_NAMES = {
    LOG_NONE:          "None",
    LOG_INFORMATIONAL: "Informational",
    LOG_DETAILED:      "Detailed",
    LOG_DEBUG:         "Debug",
    LOG_EXTENDED:      "Extended Debug",
}


class Plugin(indigo.PluginBase):
    
    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)
        self.debug: bool = False
        # Logging level — default Detailed, overridden by PluginConfig pref
        self.log_level: int = self._parse_log_level(plugin_prefs.get("logLevel", "detailed"))
        self.last_variable_values = {}  # Track variable values for change detection
        self.last_device_brightness = {}  # Track device brightness for change detection
        self.flash_threads = {}  # Track active flash threads by a unique key
        self.flash_stop_events = {}  # Threading events to signal stop
        self.flash_lock = threading.Lock()  # Lock for thread-safe operations
        self.flashing_devices = set()  # Track which devices are currently flashing
        self.scene_off_timers = {}  # Track 10-second timers after manual OFF for scenes
        self.scene_lock = threading.Lock()  # Lock for scene operations
        self.scene_activation_timers = {}  # Track 15-second delay after scene activation
        self.relay2_pending_changes = {}  # Track pending relay changes for Relay2Dimmer/Fan devices
        self.relay2_lock = threading.Lock()  # Lock for relay2 operations
        self.relay2_last_states = {}  # Track last known relay states for change detection
        self.multi_device_last_brightness = {}  # Track last brightness for multi-device virtual dimmers
        self.multi_device_controlled_states = {}  # Track last known states of controlled devices
        self.multi_device_cooldown = {}  # Track cooldown timers to prevent feedback loops
        self.scene_monitor_bounce_timers = {}  # Track bounce delay timers for Scene Monitor devices
        self.scene_monitor_last_state = {}  # Track last known state of Scene Monitor devices
        self.sunrise_threads = {}  # Track active sunrise/sunset threads by device ID
        self.sunrise_stop_events = {}  # Threading events to signal stop for sunrise/sunset
        self.sunrise_pause_events = {}  # Threading events to signal pause for sunrise/sunset
        self.sunrise_lock = threading.Lock()  # Lock for sunrise operations
    
    ########################################
    # Logging helpers — gated by self.log_level
    ########################################
    def _parse_log_level(self, value):
        """Convert a logLevel pref string to its int constant. Defaults to Detailed."""
        if isinstance(value, int):
            return value if value in LOG_LEVEL_NAMES else LOG_DETAILED
        return LOG_LEVEL_MAP.get(str(value).strip().lower(), LOG_DETAILED)
    
    def log_info(self, msg, min_level=LOG_INFORMATIONAL):
        """Log at info level if configured log_level is at least min_level."""
        if self.log_level >= min_level:
            self.logger.info(msg)
    
    def log_detailed(self, msg):
        """Log per-device action results. Shown at Detailed and above."""
        if self.log_level >= LOG_DETAILED:
            self.logger.info(msg)
    
    def log_debug(self, msg):
        """Log monitor decisions / per-tick reasoning. Shown at Debug and above."""
        if self.log_level >= LOG_DEBUG:
            self.logger.info(msg)
    
    def log_extended(self, msg):
        """Log full state dumps. Shown only at Extended Debug."""
        if self.log_level >= LOG_EXTENDED:
            self.logger.info(msg)
    
    def log_warning(self, msg):
        """Warnings — always shown except at None level."""
        if self.log_level >= LOG_INFORMATIONAL:
            self.logger.warning(msg)
    
    def log_error(self, msg):
        """Errors — always shown regardless of log_level."""
        self.logger.error(msg)
    
    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        """Called when the plugin's preferences dialog is closed."""
        if userCancelled:
            return
        new_level = self._parse_log_level(valuesDict.get("logLevel", "detailed"))
        if new_level != self.log_level:
            old_name = LOG_LEVEL_NAMES.get(self.log_level, "?")
            new_name = LOG_LEVEL_NAMES.get(new_level, "?")
            self.logger.info(f"Logging level changed: {old_name} → {new_name}")
            self.log_level = new_level
        else:
            self.logger.info(f"Logging level unchanged: {LOG_LEVEL_NAMES.get(self.log_level, '?')}")
    
    def getVariableList(self, filter="", valuesDict=None, typeId="", targetId=0):
        items = []
        for var in indigo.variables.iter():
            items.append((str(var.id), var.name))
        if not items:
            items.append(("", "-- No variables found --"))
        return items
    
    def getDeviceList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of devices that are relays or dimmers"""
        items = []
        for dev in indigo.devices:
            # Check if device is a relay or dimmer by looking at its base class
            if hasattr(dev, '__class__'):
                class_name = dev.__class__.__name__
                # Include Relay and Dimmer devices
                if class_name in ['RelayDevice', 'DimmerDevice']:
                    items.append((str(dev.id), dev.name))
        
        if not items:
            items.append(("", "-- No compatible devices found --"))
        return sorted(items, key=lambda x: x[1])
    
    def _extract_var_id(self, var_id_str):
        """Helper to extract variable ID from Indigo.List or string"""
        if not var_id_str:
            return None
        
        # Handle Indigo.List objects
        if hasattr(var_id_str, '__iter__') and not isinstance(var_id_str, str):
            try:
                var_id_str = list(var_id_str)[0] if var_id_str else ""
            except (IndexError, TypeError):
                return None
        
        if not var_id_str:
            return None
        
        try:
            return int(var_id_str)
        except (ValueError, TypeError):
            return None
    
    def _get_brightness(self, dev):
        """Get current brightness from device"""
        # For dimmer devices, use the brightness property
        if hasattr(dev, 'brightness'):
            return dev.brightness
        # Fallback to state
        return dev.states.get("brightnessLevel", 0)
    
    def _get_scale_params(self, dev):
        """Get min and max scale values from device props"""
        try:
            scale_min_str = dev.pluginProps.get("scaleMin", "0")
            scale_max_str = dev.pluginProps.get("scaleMax", "100")
            
            scale_min = float(scale_min_str)
            scale_max = float(scale_max_str)
            
            # Ensure min < max
            if scale_min >= scale_max:
                self.log_warning(f"Invalid scale range for {dev.name}, using defaults 0-100")
                return 0.0, 100.0, False
            
            # Determine if this is a float scale
            # Check if either value has decimals in the string OR if the range is small (<=10)
            has_decimal_in_string = ('.' in scale_min_str) or ('.' in scale_max_str)
            scale_range = scale_max - scale_min
            is_small_range = scale_range <= 10
            
            # If the range is small (like 0-1, 0-10) or has decimals, treat as float
            is_float_scale = has_decimal_in_string or is_small_range
            
            return scale_min, scale_max, is_float_scale
        except (ValueError, TypeError):
            return 0.0, 100.0, False
    
    def _variable_to_brightness(self, var_value, scale_min, scale_max):
        """Convert variable value (on custom scale) to brightness (0-100)
        Returns: (brightness, was_clamped, clamped_value)
        """
        try:
            value = float(var_value)
            original_value = value
            
            # Clamp to scale range and track if we clamped
            was_clamped = False
            if value < scale_min:
                value = scale_min
                was_clamped = True
            elif value > scale_max:
                value = scale_max
                was_clamped = True
            
            # Convert to 0-100 range
            scale_range = scale_max - scale_min
            brightness = ((value - scale_min) / scale_range) * 100
            
            return int(round(brightness)), was_clamped, value
        except (ValueError, TypeError):
            return None, False, None
    
    def _brightness_to_variable(self, brightness, scale_min, scale_max, is_float_scale):
        """Convert brightness (0-100) to variable value (on custom scale)"""
        # Clamp brightness to 0-100
        brightness = max(0, min(100, brightness))
        
        # Convert to custom scale
        scale_range = scale_max - scale_min
        value = (brightness / 100.0) * scale_range + scale_min
        
        # Return as integer or float based on scale type
        if is_float_scale:
            # For float scales, use appropriate precision based on range
            if scale_range <= 1:
                # For 0-1 range, use 2 decimal places (0.70 = 0.7)
                result = str(round(value, 2))
            elif scale_range <= 10:
                result = str(round(value, 2))
            else:
                result = str(round(value, 1))
        else:
            result = str(int(round(value)))
        
        return result
    
    def _flash_device_thread(self, thread_id, device_ids, flash_count, flash_duration, gap_duration, 
                             flash_to_brightness, flash_to_minimum):
        """Thread function to handle the flashing sequence"""
        try:
            # Store original states FIRST before marking as flashing
            original_states = {}
            for dev_id in device_ids:
                try:
                    dev = indigo.devices[int(dev_id)]
                    if hasattr(dev, 'brightness'):
                        # It's a dimmer
                        original_states[dev_id] = {
                            'type': 'dimmer',
                            'brightness': dev.brightness,
                            'on': dev.onState
                        }
                    else:
                        # It's a relay
                        original_states[dev_id] = {
                            'type': 'relay',
                            'on': dev.onState
                        }
                except Exception as e:
                    self.logger.error(f"Error getting original state for device {dev_id}: {e}")
                    continue
            
            # NOW mark these devices as currently flashing
            with self.flash_lock:
                for dev_id in device_ids:
                    self.flashing_devices.add(int(dev_id))
            
            # Set defaults for brightness levels
            max_brightness = flash_to_brightness if flash_to_brightness is not None else 100
            min_brightness = flash_to_minimum if flash_to_minimum is not None else 0
            
            # Perform the flashes
            for flash_num in range(flash_count):
                # Check if we should stop
                if self.flash_stop_events.get(thread_id, threading.Event()).is_set():
                    self.log_info(f"Flash sequence {thread_id} cancelled")
                    break
                
                # Flash to MAX brightness first
                for dev_id, original_state in original_states.items():
                    try:
                        dev = indigo.devices[int(dev_id)]
                        
                        if original_state['type'] == 'dimmer':
                            indigo.dimmer.setBrightness(dev.id, value=max_brightness)
                        else:
                            # Relay - turn on
                            indigo.device.turnOn(dev.id)
                    
                    except Exception as e:
                        self.logger.error(f"Error flashing device {dev_id} to max: {e}")
                
                # Wait for flash duration
                if self.flash_stop_events.get(thread_id, threading.Event()).wait(flash_duration):
                    self.log_info(f"Flash sequence {thread_id} cancelled during flash")
                    break
                
                # Flash to MIN brightness
                for dev_id, original_state in original_states.items():
                    try:
                        dev = indigo.devices[int(dev_id)]
                        
                        if original_state['type'] == 'dimmer':
                            indigo.dimmer.setBrightness(dev.id, value=min_brightness)
                        else:
                            # Relay - turn off
                            indigo.device.turnOff(dev.id)
                    
                    except Exception as e:
                        self.logger.error(f"Error flashing device {dev_id} to min: {e}")
                
                # Wait for gap (unless this was the last flash)
                if flash_num < flash_count - 1:
                    if self.flash_stop_events.get(thread_id, threading.Event()).wait(gap_duration):
                        self.log_info(f"Flash sequence {thread_id} cancelled during gap")
                        break
            
            # Ensure all devices are back to original state
            for dev_id, original_state in original_states.items():
                try:
                    dev = indigo.devices[int(dev_id)]
                    
                    if original_state['type'] == 'dimmer':
                        indigo.dimmer.setBrightness(dev.id, value=original_state['brightness'])
                    else:
                        if original_state['on']:
                            indigo.device.turnOn(dev.id)
                        else:
                            indigo.device.turnOff(dev.id)
                
                except Exception as e:
                    self.logger.error(f"Error in final restore for device {dev_id}: {e}")
            
        finally:
            # Remove devices from flashing set and clean up this thread from tracking
            with self.flash_lock:
                for dev_id in device_ids:
                    self.flashing_devices.discard(int(dev_id))
                if thread_id in self.flash_threads:
                    del self.flash_threads[thread_id]
                if thread_id in self.flash_stop_events:
                    del self.flash_stop_events[thread_id]
    
    def flashLamps(self, pluginAction):
        """Action handler for flashing lamps"""
        try:
            # Extract parameters
            device_ids = pluginAction.props.get("deviceList", [])
            if not device_ids:
                self.logger.error("No devices selected for flashing")
                return
            
            # Handle Indigo.List objects - convert to regular list
            if hasattr(device_ids, '__iter__') and not isinstance(device_ids, str):
                device_ids = list(device_ids)
            
            # Ensure device_ids is a list
            if not isinstance(device_ids, list):
                device_ids = [device_ids]
            
            # Convert any nested lists or ensure all items are strings
            cleaned_device_ids = []
            for dev_id in device_ids:
                if isinstance(dev_id, list):
                    # If it's a list, take the first item
                    cleaned_device_ids.append(str(dev_id[0]) if dev_id else "")
                else:
                    cleaned_device_ids.append(str(dev_id))
            
            device_ids = [d for d in cleaned_device_ids if d]  # Remove empty strings
            
            if not device_ids:
                self.logger.error("No valid devices selected for flashing")
                return
            
            flash_count = int(pluginAction.props.get("flashCount", 3))
            flash_duration = float(pluginAction.props.get("flashDuration", 0.5))
            gap_duration = float(pluginAction.props.get("gapDuration", 0.5))
            
            # Optional brightness parameters (for dimmers)
            flash_to_brightness_str = pluginAction.props.get("flashToBrightness", "").strip()
            flash_to_minimum_str = pluginAction.props.get("flashToMinimum", "").strip()
            
            # Only convert to int if the string is not empty
            flash_to_brightness = int(flash_to_brightness_str) if flash_to_brightness_str else None
            flash_to_minimum = int(flash_to_minimum_str) if flash_to_minimum_str else None
            
            # Validate values
            if flash_to_brightness is not None:
                flash_to_brightness = max(0, min(100, flash_to_brightness))
            if flash_to_minimum is not None:
                flash_to_minimum = max(0, min(100, flash_to_minimum))
            
            if flash_count <= 0:
                self.logger.error("Flash count must be greater than 0")
                return
            
            if flash_duration <= 0 or gap_duration < 0:
                self.logger.error("Flash and gap duration must be positive")
                return
            
            # Create unique thread ID
            thread_id = f"flash_{time.time()}"
            
            # Create stop event for this thread
            stop_event = threading.Event()
            
            # Start flash thread
            with self.flash_lock:
                self.flash_stop_events[thread_id] = stop_event
                flash_thread = threading.Thread(
                    target=self._flash_device_thread,
                    args=(thread_id, device_ids, flash_count, flash_duration, gap_duration,
                          flash_to_brightness, flash_to_minimum)
                )
                flash_thread.daemon = True
                self.flash_threads[thread_id] = flash_thread
                flash_thread.start()
            
            device_names = [indigo.devices[int(dev_id)].name for dev_id in device_ids]
            self.log_info(f"Started flashing {len(device_ids)} device(s): {', '.join(device_names)} "
                            f"({flash_count} flashes, {flash_duration}s duration, {gap_duration}s gap)")
        
        except Exception as e:
            self.logger.error(f"Error in flashLamps: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    def cancelAllFlashes(self, pluginAction):
        """Action handler to cancel all running flash sequences"""
        try:
            with self.flash_lock:
                if not self.flash_stop_events:
                    self.log_info("No flash sequences currently running")
                    return
                
                count = len(self.flash_stop_events)
                # Signal all threads to stop
                for stop_event in self.flash_stop_events.values():
                    stop_event.set()
            
            self.log_info(f"Cancelled {count} flash sequence(s)")
        
        except Exception as e:
            self.logger.error(f"Error in cancelAllFlashes: {e}")
    
    ########################################
    # Sunrise/Sunset Simulator Action Methods
    ########################################
    
    def sunriseStart(self, pluginAction):
        """Action handler to start sunrise/sunset simulation"""
        try:
            dev_id = int(pluginAction.props.get("targetDeviceId"))
            dev = indigo.devices[dev_id]
            
            if dev.deviceTypeId != "SunriseSimulator":
                self.logger.error(f"Device {dev.name} is not a Sunrise/Sunset Simulator")
                return
            
            # Turn on the device (which will start the simulation)
            indigo.device.turnOn(dev_id)
            
        except Exception as e:
            self.logger.error(f"Error in sunriseStart: {e}")
    
    def sunriseStop(self, pluginAction):
        """Action handler to stop sunrise/sunset simulation"""
        try:
            dev_id = int(pluginAction.props.get("targetDeviceId"))
            dev = indigo.devices[dev_id]
            
            if dev.deviceTypeId != "SunriseSimulator":
                self.logger.error(f"Device {dev.name} is not a Sunrise/Sunset Simulator")
                return
            
            # Turn off the device (which will stop the simulation)
            indigo.device.turnOff(dev_id)
            
        except Exception as e:
            self.logger.error(f"Error in sunriseStop: {e}")
    
    def sunrisePause(self, pluginAction):
        """Action handler to pause sunrise/sunset simulation"""
        try:
            dev_id = int(pluginAction.props.get("targetDeviceId"))
            dev = indigo.devices[dev_id]
            
            if dev.deviceTypeId != "SunriseSimulator":
                self.logger.error(f"Device {dev.name} is not a Sunrise/Sunset Simulator")
                return
            
            current_status = dev.states.get('status', 'Idle')
            
            if current_status == "Running":
                # Pause the simulation
                self.log_info(f"Pausing Sunrise/Sunset Simulator '{dev.name}'")
                with self.sunrise_lock:
                    pause_event = self.sunrise_pause_events.get(dev_id)
                    if pause_event:
                        pause_event.set()
                dev.updateStateOnServer("status", "Paused")
            elif current_status == "Paused":
                # Resume the simulation
                self.log_info(f"Resuming Sunrise/Sunset Simulator '{dev.name}'")
                with self.sunrise_lock:
                    pause_event = self.sunrise_pause_events.get(dev_id)
                    if pause_event:
                        pause_event.clear()
                dev.updateStateOnServer("status", "Running")
            else:
                self.log_info(f"Sunrise/Sunset Simulator '{dev.name}' is not running, cannot pause")
            
        except Exception as e:
            self.logger.error(f"Error in sunrisePause: {e}")
    
    ########################################
    # Scene Device Methods
    ########################################
    
    def getSceneDeviceList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of all controllable devices for scene control"""
        try:
            items = []
            
            for dev in indigo.devices:
                include = False
                
                if hasattr(dev, '__class__'):
                    class_name = dev.__class__.__name__
                    # Include native Indigo device types
                    if class_name in ['DimmerDevice', 'RelayDevice', 'ThermostatDevice', 'SpeedControlDevice']:
                        include = True
                
                # Also check for plugin-based speed control devices (like fans)
                if not include:
                    if hasattr(dev, 'speedIndex') or hasattr(dev, 'speedLevel'):
                        include = True
                
                if include:
                    items.append((str(dev.id), dev.name))
            
            if not items:
                items.append(("", "-- No controllable devices found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getSceneDeviceList: {e}")
            return [("", "-- Error loading devices --")]
        """Get list of dimmer devices for scene control"""
        try:
            items = []
            for dev in indigo.devices:
                if hasattr(dev, '__class__') and dev.__class__.__name__ == 'DimmerDevice':
                    items.append((str(dev.id), dev.name))
            if not items:
                items.append(("", "-- No dimmers found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getSceneDimmerList: {e}")
            return [("", "-- Error loading dimmers --")]
    
    def getSceneRelayList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of relay devices for scene control"""
        try:
            items = []
            for dev in indigo.devices:
                if hasattr(dev, '__class__') and dev.__class__.__name__ == 'RelayDevice':
                    items.append((str(dev.id), dev.name))
            if not items:
                items.append(("", "-- No relays found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getSceneRelayList: {e}")
            return [("", "-- Error loading relays --")]
    
    def getSceneThermostatList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of thermostat devices for scene control"""
        try:
            items = []
            for dev in indigo.devices:
                if hasattr(dev, '__class__') and dev.__class__.__name__ == 'ThermostatDevice':
                    items.append((str(dev.id), dev.name))
            if not items:
                items.append(("", "-- No thermostats found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getSceneThermostatList: {e}")
            return [("", "-- Error loading thermostats --")]
    
    def getSceneFanList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of fan devices for scene control"""
        try:
            items = []
            for dev in indigo.devices:
                if hasattr(dev, '__class__') and dev.__class__.__name__ == 'SpeedControlDevice':
                    items.append((str(dev.id), dev.name))
            if not items:
                items.append(("", "-- No fans found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getSceneFanList: {e}")
            return [("", "-- Error loading fans --")]
    
    def getSceneBlindList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of blind/shade devices for scene control"""
        try:
            items = []
            for dev in indigo.devices:
                # Blinds typically have a position state
                if 'position' in [state.lower() for state in dev.states.keys()]:
                    items.append((str(dev.id), dev.name))
            if not items:
                items.append(("", "-- No blinds found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getSceneBlindList: {e}")
            return [("", "-- Error loading blinds --")]
    
    def getActionGroupList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of action groups"""
        try:
            items = [("none", "-- None --")]
            for ag in indigo.actionGroups:
                items.append((str(ag.id), ag.name))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getActionGroupList: {e}")
            return [("none", "-- Error loading action groups --")]
    
    def getSceneList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of Scene Controller devices"""
        try:
            items = []
            for dev in indigo.devices.iter(filter="self"):
                if dev.deviceTypeId == "SceneDevice":
                    items.append((str(dev.id), dev.name))
            
            if not items:
                items.append(("", "-- No Scene Controllers found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getSceneList: {e}")
            return [("", "-- Error loading scenes --")]
    
    def getDimmerList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of dimmer devices"""
        try:
            items = []
            for dev in indigo.devices:
                if hasattr(dev, '__class__') and dev.__class__.__name__ == 'DimmerDevice':
                    items.append((str(dev.id), dev.name))
            
            if not items:
                items.append(("", "-- No dimmers found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getDimmerList: {e}")
            return [("", "-- Error loading dimmers --")]
    
    ########################################
    # Relay2Dimmer/Fan Methods
    ########################################
    
    def getRelayList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Get list of relay devices"""
        try:
            items = []
            for dev in indigo.devices:
                if hasattr(dev, '__class__') and dev.__class__.__name__ == 'RelayDevice':
                    items.append((str(dev.id), dev.name))
            if not items:
                items.append(("0", "-- No relays found --"))
            return sorted(items, key=lambda x: x[1])
        except Exception as e:
            self.logger.error(f"Error in getRelayList: {e}")
            return [("0", "-- Error loading relays --")]
    
    def _get_relay_states(self, relay1_id, relay2_id):
        """Get the on/off states of both relays"""
        try:
            relay1 = indigo.devices[int(relay1_id)]
            relay2 = indigo.devices[int(relay2_id)]
            return relay1.onState, relay2.onState
        except Exception as e:
            self.logger.error(f"Error getting relay states: {e}")
            return False, False
    
    def _relay_states_to_level(self, relay1_on, relay2_on):
        """Convert relay states to dimmer/fan level (0, 33, 66, 100)"""
        if not relay1_on and not relay2_on:
            return 0
        elif relay1_on and not relay2_on:
            return 33
        elif not relay1_on and relay2_on:
            return 66
        else:  # both on
            return 100
    
    def _level_to_relay_states(self, level):
        """Convert dimmer/fan level to relay states, rounding to nearest valid level"""
        # Round to nearest valid level
        if level <= 16:
            rounded_level = 0
        elif level <= 49:
            rounded_level = 33
        elif level <= 83:
            rounded_level = 66
        else:
            rounded_level = 100
        
        # Convert to relay states
        if rounded_level == 0:
            return False, False
        elif rounded_level == 33:
            return True, False
        elif rounded_level == 66:
            return False, True
        else:  # 100
            return True, True
    
    def _apply_relay_states(self, relay1_id, relay2_id, relay1_should_be_on, relay2_should_be_on):
        """Apply the relay states"""
        try:
            relay1_id = int(relay1_id)
            relay2_id = int(relay2_id)
            
            # Apply both relay states
            if relay1_should_be_on:
                indigo.device.turnOn(relay1_id)
            else:
                indigo.device.turnOff(relay1_id)
            
            if relay2_should_be_on:
                indigo.device.turnOn(relay2_id)
            else:
                indigo.device.turnOff(relay2_id)
        
        except Exception as e:
            self.logger.error(f"Error applying relay states: {e}")
    
    def _get_device_scene_state(self, dev):
        """Get the controllable state of a device for scene comparison"""
        state = {}
        
        if hasattr(dev, '__class__'):
            class_name = dev.__class__.__name__
            
            if class_name == 'DimmerDevice':
                state['type'] = 'dimmer'
                state['brightness'] = dev.brightness
                state['onState'] = dev.onState
                
            elif class_name == 'RelayDevice':
                state['type'] = 'relay'
                state['onState'] = dev.onState
                
            elif class_name == 'ThermostatDevice':
                state['type'] = 'thermostat'
                # Convert enum values to their integer equivalents for storage
                state['hvacMode'] = int(dev.hvacMode)
                state['fanMode'] = int(dev.fanMode)
                state['coolSetpoint'] = float(dev.coolSetpoint)
                state['heatSetpoint'] = float(dev.heatSetpoint)
                
            elif class_name == 'SpeedControlDevice':
                state['type'] = 'fan'
                state['speedLevel'] = dev.speedLevel if hasattr(dev, 'speedLevel') else 0
                state['onState'] = dev.onState
                
            else:
                # Check if it's a blind/shade by looking for position
                if 'position' in [s.lower() for s in dev.states.keys()]:
                    state['type'] = 'blind'
                    # Find the actual position state key
                    for key in dev.states.keys():
                        if key.lower() == 'position':
                            state['position'] = dev.states[key]
                            break
        
        return state
    
    def saveSceneState(self, valuesDict, typeId="", devId=0):
        """Button callback to save current state of all selected devices and variables"""
        try:
            saved_states = {}
            
            # Get the single list of selected devices
            device_list = valuesDict.get('sceneDevices', [])
            
            self.log_detailed("=" * 60)
            self.log_detailed("Saving Scene State:")
            
            # Handle devices
            if device_list:
                # Handle Indigo.List objects
                if hasattr(device_list, '__iter__') and not isinstance(device_list, str):
                    device_list = list(device_list)
                
                if not isinstance(device_list, list):
                    device_list = [device_list]
                
                for dev_id in device_list:
                    if isinstance(dev_id, list):
                        dev_id = dev_id[0] if dev_id else None
                    
                    if not dev_id or dev_id == "":
                        continue
                    
                    try:
                        dev = indigo.devices[int(dev_id)]
                        state = self._get_device_scene_state(dev)
                        
                        if state:
                            saved_states[f"device_{dev_id}"] = state
                            
                            # Log the saved state
                            if state['type'] == 'dimmer':
                                self.log_detailed(f"  Device: {dev.name}: Brightness={state['brightness']}%")
                            elif state['type'] == 'relay':
                                self.log_detailed(f"  Device: {dev.name}: {'ON' if state['onState'] else 'OFF'}")
                            elif state['type'] == 'thermostat':
                                hvac_mode_name = str(dev.hvacMode).split('.')[-1] if hasattr(dev.hvacMode, '__class__') else str(state['hvacMode'])
                                fan_mode_name = str(dev.fanMode).split('.')[-1] if hasattr(dev.fanMode, '__class__') else str(state['fanMode'])
                                self.log_detailed(f"  Device: {dev.name}: Mode={hvac_mode_name}, Heat={state['heatSetpoint']}°, Cool={state['coolSetpoint']}°, Fan={fan_mode_name}")
                            elif state['type'] == 'fan':
                                self.log_detailed(f"  Device: {dev.name}: Speed={state['speedLevel']}")
                            elif state['type'] == 'blind':
                                self.log_detailed(f"  Device: {dev.name}: Position={state['position']}%")
                    
                    except Exception as e:
                        self.logger.error(f"Error saving state for device {dev_id}: {e}")
            
            # Handle variables
            variable_list = valuesDict.get('sceneVariables', [])
            self.log_detailed(f"DEBUG: variable_list raw = {variable_list}")
            
            if variable_list:
                # Handle Indigo.List objects
                if hasattr(variable_list, '__iter__') and not isinstance(variable_list, str):
                    variable_list = list(variable_list)
                
                if not isinstance(variable_list, list):
                    variable_list = [variable_list]
                
                for var_id in variable_list:
                    if isinstance(var_id, list):
                        var_id = var_id[0] if var_id else None
                    
                    if not var_id or var_id == "":
                        continue
                    
                    try:
                        var = indigo.variables[int(var_id)]
                        saved_states[f"variable_{var_id}"] = {
                            'type': 'variable',
                            'value': var.value
                        }
                        self.log_detailed(f"  Variable: {var.name}: {var.value}")
                    
                    except Exception as e:
                        self.logger.error(f"Error saving state for variable {var_id}: {e}")
            
            if saved_states:
                valuesDict['savedStates'] = json.dumps(saved_states)
                self.log_detailed(f"Scene state saved successfully ({len(saved_states)} items)")
            else:
                self.log_warning("No devices or variables selected - no state saved")
            
            self.log_detailed("=" * 60)
            
        except Exception as e:
            self.logger.error(f"Error in saveSceneState: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
        
        return valuesDict
    
    def compareSceneState(self, valuesDict, typeId="", devId=0):
        """Button callback to compare current state with saved state"""
        try:
            saved_states_str = valuesDict.get('savedStates', '')
            if not saved_states_str:
                self.log_warning("No saved state to compare against")
                return valuesDict
            
            # Parse saved states
            saved_states = json.loads(saved_states_str)
            
            # Get the ignore HVAC mode setting
            ignore_hvac = valuesDict.get('ignoreHvacMode', False)
            
            self.log_detailed("=" * 60)
            self.log_detailed("Scene State Comparison:")
            if ignore_hvac:
                self.log_detailed("(Ignoring HVAC Mode differences)")
            self.log_detailed("")
            
            all_match = True
            
            for item_key, saved_state in saved_states.items():
                try:
                    # Check if it's a device or variable
                    if item_key.startswith('device_'):
                        dev_id_str = item_key.replace('device_', '')
                        dev = indigo.devices[int(dev_id_str)]
                        current_state = self._get_device_scene_state(dev)
                        
                        device_matches = True
                        differences = []
                        
                        if saved_state['type'] != current_state.get('type'):
                            differences.append(f"Type mismatch: saved={saved_state['type']}, current={current_state.get('type')}")
                            device_matches = False
                        
                        elif saved_state['type'] == 'dimmer':
                            if saved_state['brightness'] != current_state['brightness']:
                                differences.append(f"Brightness: saved={saved_state['brightness']}%, current={current_state['brightness']}%")
                                device_matches = False
                            if saved_state['onState'] != current_state['onState']:
                                differences.append(f"OnState: saved={saved_state['onState']}, current={current_state['onState']}")
                                device_matches = False
                        
                        elif saved_state['type'] == 'relay':
                            if saved_state['onState'] != current_state['onState']:
                                differences.append(f"OnState: saved={saved_state['onState']}, current={current_state['onState']}")
                                device_matches = False
                        
                        elif saved_state['type'] == 'thermostat':
                            if saved_state['hvacMode'] != int(current_state['hvacMode']):
                                differences.append(f"HVAC Mode: saved={saved_state['hvacMode']}, current={int(current_state['hvacMode'])}")
                                device_matches = False
                            if saved_state['fanMode'] != int(current_state['fanMode']):
                                differences.append(f"Fan Mode: saved={saved_state['fanMode']}, current={int(current_state['fanMode'])}")
                                device_matches = False
                            if saved_state['coolSetpoint'] != float(current_state['coolSetpoint']):
                                differences.append(f"Cool Setpoint: saved={saved_state['coolSetpoint']}°, current={float(current_state['coolSetpoint'])}°")
                                device_matches = False
                            if saved_state['heatSetpoint'] != float(current_state['heatSetpoint']):
                                differences.append(f"Heat Setpoint: saved={saved_state['heatSetpoint']}°, current={float(current_state['heatSetpoint'])}°")
                                device_matches = False
                        
                        elif saved_state['type'] == 'fan':
                            if saved_state['speedLevel'] != current_state['speedLevel']:
                                differences.append(f"Speed Level: saved={saved_state['speedLevel']}, current={current_state['speedLevel']}")
                                device_matches = False
                            if saved_state['onState'] != current_state['onState']:
                                differences.append(f"OnState: saved={saved_state['onState']}, current={current_state['onState']}")
                                device_matches = False
                        
                        elif saved_state['type'] == 'blind':
                            if saved_state['position'] != current_state.get('position'):
                                differences.append(f"Position: saved={saved_state['position']}%, current={current_state.get('position')}%")
                                device_matches = False
                        
                        if device_matches:
                            self.log_detailed(f"✓ Device: {dev.name}: MATCHES")
                        else:
                            self.log_detailed(f"✗ Device: {dev.name}: DIFFERS")
                            for diff in differences:
                                self.log_detailed(f"    - {diff}")
                            all_match = False
                    
                    elif item_key.startswith('variable_'):
                        var_id_str = item_key.replace('variable_', '')
                        var = indigo.variables[int(var_id_str)]
                        current_value = var.value
                        
                        if saved_state['value'] == current_value:
                            self.log_detailed(f"✓ Variable: {var.name}: MATCHES (value: {current_value})")
                        else:
                            self.log_detailed(f"✗ Variable: {var.name}: DIFFERS")
                            self.log_detailed(f"    - Value: saved='{saved_state['value']}', current='{current_value}'")
                            all_match = False
                
                except Exception as e:
                    self.logger.error(f"Error comparing item {item_key}: {e}")
                    all_match = False
            
            self.log_detailed("")
            if all_match:
                self.log_detailed("Result: ALL DEVICES MATCH - Scene should be ON")
            else:
                self.log_detailed("Result: DIFFERENCES FOUND - Scene should be OFF")
            self.log_detailed("=" * 60)
        
        except Exception as e:
            self.logger.error(f"Error in compareSceneState: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
        
        return valuesDict
    
    def _check_scene_match(self, scene_dev):
        """Check if all monitored devices match the saved scene state"""
        try:
            saved_states_str = scene_dev.pluginProps.get('savedStates', '')
            if not saved_states_str:
                return False
            
            # Parse saved states from JSON
            try:
                saved_states = json.loads(saved_states_str)
            except (json.JSONDecodeError, ValueError) as e:
                self.logger.error(f"Scene '{scene_dev.name}': Invalid saved state data. Please save the scene state again.")
                return False
            
            # Check each item (device or variable)
            for item_key, saved_state in saved_states.items():
                if item_key.startswith('device_'):
                    dev_id_str = item_key.replace('device_', '')
                    try:
                        dev = indigo.devices[int(dev_id_str)]
                    except:
                        self.log_warning(f"Scene '{scene_dev.name}': Monitored device ID {dev_id_str} no longer exists. Please reconfigure the scene.")
                        return False
                    
                    current_state = self._get_device_scene_state(dev)
                    
                    # Compare states based on device type
                    if saved_state['type'] != current_state.get('type'):
                        return False
                    
                    if saved_state['type'] == 'dimmer':
                        if saved_state['brightness'] != current_state['brightness']:
                            return False
                        if saved_state['onState'] != current_state['onState']:
                            return False
                            
                    elif saved_state['type'] == 'relay':
                        if saved_state['onState'] != current_state['onState']:
                            return False
                            
                    elif saved_state['type'] == 'thermostat':
                        ignore_hvac = scene_dev.pluginProps.get('ignoreHvacMode', False)
                        
                        if not ignore_hvac and saved_state['hvacMode'] != int(current_state['hvacMode']):
                            return False
                        if saved_state['fanMode'] != int(current_state['fanMode']):
                            return False
                        if saved_state['coolSetpoint'] != float(current_state['coolSetpoint']):
                            return False
                        if saved_state['heatSetpoint'] != float(current_state['heatSetpoint']):
                            return False
                            
                    elif saved_state['type'] == 'fan':
                        if saved_state['speedLevel'] != current_state['speedLevel']:
                            return False
                        if saved_state['onState'] != current_state['onState']:
                            return False
                            
                    elif saved_state['type'] == 'blind':
                        if saved_state['position'] != current_state.get('position'):
                            return False
                
                elif item_key.startswith('variable_'):
                    var_id_str = item_key.replace('variable_', '')
                    try:
                        var = indigo.variables[int(var_id_str)]
                        current_value = str(var.value)
                        saved_value = str(saved_state['value'])
                        self.log_debug(f"Checking variable '{var.name}': saved='{saved_value}', current='{current_value}'")
                        if saved_value != current_value:
                            return False
                    except:
                        self.log_warning(f"Scene '{scene_dev.name}': Monitored variable ID {var_id_str} no longer exists. Please reconfigure the scene.")
                        return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error checking scene match for {scene_dev.name}: {e}")
            return False
    
    def _capture_current_state(self, scene_dev):
        """Capture current state of all monitored devices/variables before applying scene"""
        try:
            saved_states_str = scene_dev.pluginProps.get('savedStates', '')
            if not saved_states_str:
                return None
            
            try:
                saved_states = json.loads(saved_states_str)
            except (json.JSONDecodeError, ValueError):
                return None
            
            current_states = {}
            
            # Capture current state of each monitored device/variable
            for item_key in saved_states.keys():
                if item_key.startswith('device_'):
                    dev_id_str = item_key.replace('device_', '')
                    try:
                        dev = indigo.devices[int(dev_id_str)]
                        current_states[item_key] = self._get_device_scene_state(dev)
                    except Exception as e:
                        self.logger.error(f"Error capturing state for device {dev_id_str}: {e}")
                
                elif item_key.startswith('variable_'):
                    var_id_str = item_key.replace('variable_', '')
                    try:
                        var = indigo.variables[int(var_id_str)]
                        current_states[item_key] = {
                            'value': str(var.value)
                        }
                    except Exception as e:
                        self.logger.error(f"Error capturing state for variable {var_id_str}: {e}")
            
            return json.dumps(current_states)
        
        except Exception as e:
            self.logger.error(f"Error capturing current state for {scene_dev.name}: {e}")
            return None
    
    def _apply_scene_state(self, scene_dev):
        """Apply the saved scene state to all monitored devices"""
        try:
            saved_states_str = scene_dev.pluginProps.get('savedStates', '')
            if not saved_states_str:
                self.log_warning(f"Scene '{scene_dev.name}' has no saved state")
                return
            
            try:
                saved_states = json.loads(saved_states_str)
            except (json.JSONDecodeError, ValueError) as e:
                self.logger.error(f"Scene '{scene_dev.name}': Invalid saved state data. Please save the scene state again.")
                return
            
            for item_key, saved_state in saved_states.items():
                if item_key.startswith('device_'):
                    dev_id_str = item_key.replace('device_', '')
                    try:
                        dev_id = int(dev_id_str)
                        dev = indigo.devices[dev_id]
                        
                        if saved_state['type'] == 'dimmer':
                            indigo.dimmer.setBrightness(dev_id, value=saved_state['brightness'])
                            
                        elif saved_state['type'] == 'relay':
                            if saved_state['onState']:
                                indigo.device.turnOn(dev_id)
                            else:
                                indigo.device.turnOff(dev_id)
                                
                        elif saved_state['type'] == 'thermostat':
                            # Convert integer values back to enums
                            hvac_mode = indigo.kHvacMode(saved_state['hvacMode'])
                            fan_mode = indigo.kFanMode(saved_state['fanMode'])
                            
                            indigo.thermostat.setHvacMode(dev_id, value=hvac_mode)
                            indigo.thermostat.setFanMode(dev_id, value=fan_mode)
                            indigo.thermostat.setCoolSetpoint(dev_id, value=saved_state['coolSetpoint'])
                            indigo.thermostat.setHeatSetpoint(dev_id, value=saved_state['heatSetpoint'])
                            
                        elif saved_state['type'] == 'fan':
                            indigo.speedcontrol.setSpeedLevel(dev_id, value=saved_state['speedLevel'])
                            
                        elif saved_state['type'] == 'blind':
                            # Blinds typically use setBrightness for position
                            indigo.dimmer.setBrightness(dev_id, value=saved_state['position'])
                    
                    except Exception as e:
                        self.logger.error(f"Error applying state to device {dev_id_str}: {e}")
                
                elif item_key.startswith('variable_'):
                    # Set variable values
                    var_id_str = item_key.replace('variable_', '')
                    try:
                        var_id = int(var_id_str)
                        var = indigo.variables[var_id]
                        self.log_detailed(f"Setting variable '{var.name}' to '{saved_state['value']}'")
                        indigo.variable.updateValue(var_id, saved_state['value'])
                    except Exception as e:
                        self.logger.error(f"Error applying state to variable {var_id_str}: {e}")
                        import traceback
                        self.logger.error(traceback.format_exc())
        
        except Exception as e:
            self.logger.error(f"Error applying scene state for {scene_dev.name}: {e}")
    
    def _execute_action_group(self, action_group_id):
        """Execute an action group by ID"""
        if not action_group_id or action_group_id == "none":
            return
        
        try:
            indigo.actionGroup.execute(int(action_group_id))
        except Exception as e:
            self.logger.error(f"Error executing action group {action_group_id}: {e}")
    
    def _check_any_scene_on(self, monitor_dev):
        """Check if any of the monitored scenes are currently ON"""
        try:
            monitored_scenes = monitor_dev.pluginProps.get("monitoredScenes", [])
            if not monitored_scenes:
                return False
            
            # Convert to list if needed
            if hasattr(monitored_scenes, '__iter__') and not isinstance(monitored_scenes, str):
                try:
                    monitored_scenes = list(monitored_scenes)
                except TypeError:
                    return False
            
            # Parse scene IDs
            try:
                if isinstance(monitored_scenes, str):
                    scene_ids = [int(s.strip()) for s in monitored_scenes.split(",") if s.strip()]
                else:
                    scene_ids = [int(s) for s in monitored_scenes if s]
            except (ValueError, TypeError):
                return False
            
            # Check if any scene is ON
            for scene_id in scene_ids:
                try:
                    scene_dev = indigo.devices[scene_id]
                    if scene_dev.onState:
                        return True
                except:
                    pass
            
            return False
        
        except Exception as e:
            self.logger.error(f"Error checking scenes for {monitor_dev.name}: {e}")
            return False
    
    def _calculate_brightness_with_curve(self, start_brightness, target_brightness, progress, curve_type):
        """Calculate brightness at given progress with optional easing curve
        Args:
            start_brightness: Starting brightness (0-100)
            target_brightness: Target brightness (0-100)
            progress: Progress from 0.0 to 1.0
            curve_type: 'linear' or 'ease'
        Returns:
            Current brightness value
        """
        if curve_type == "ease":
            # Ease-in/ease-out using smoothstep function
            # smoothstep(x) = 3x² - 2x³
            progress = progress * progress * (3 - 2 * progress)
        
        # Calculate brightness
        brightness_range = target_brightness - start_brightness
        current = start_brightness + (brightness_range * progress)
        
        return int(round(current))
    
    def _sunrise_thread(self, dev_id):
        """Thread function to handle sunrise/sunset simulation"""
        try:
            dev = indigo.devices[dev_id]
            self.log_info(f"[SUNRISE] Starting thread for '{dev.name}'")
            
            # Get configuration
            try:
                start_brightness = int(dev.pluginProps.get("startingBrightness", "0"))
                target_brightness = int(dev.pluginProps.get("targetBrightness", "100"))
                duration = int(dev.pluginProps.get("duration", "300"))
                curve_type = dev.pluginProps.get("brightnessCurve", "linear")
                reset_devices = dev.pluginProps.get("resetDevicesAtStart", False)
            except (ValueError, TypeError) as e:
                self.logger.error(f"[SUNRISE] Invalid configuration for '{dev.name}': {e}")
                return
            
            # Clamp values
            start_brightness = max(0, min(100, start_brightness))
            target_brightness = max(0, min(100, target_brightness))
            duration = max(1, duration)
            
            # Determine mode
            if start_brightness < target_brightness:
                mode = "Sunrise"
            elif start_brightness > target_brightness:
                mode = "Sunset"
            else:
                self.log_warning(f"[SUNRISE] '{dev.name}': Starting and target brightness are the same, nothing to do")
                dev.updateStateOnServer("status", "Idle")
                dev.updateStateOnServer("onOffState", False)
                return
            
            self.log_info(f"[SUNRISE] Mode: {mode}, {start_brightness}% → {target_brightness}% over {duration}s using {curve_type} curve")
            
            # Get controlled devices
            controlled_devices = dev.pluginProps.get("controlledDevices", [])
            if not controlled_devices:
                self.log_warning(f"[SUNRISE] No controlled devices configured for '{dev.name}'")
                return
            
            # Parse device IDs
            if hasattr(controlled_devices, '__iter__') and not isinstance(controlled_devices, str):
                try:
                    controlled_devices = list(controlled_devices)
                except TypeError:
                    self.logger.error(f"[SUNRISE] Invalid device list for '{dev.name}'")
                    return
            
            try:
                if isinstance(controlled_devices, str):
                    device_ids = [int(d.strip()) for d in controlled_devices.split(",") if d.strip()]
                else:
                    device_ids = [int(d) for d in controlled_devices if d]
            except (ValueError, TypeError):
                self.logger.error(f"[SUNRISE] Invalid device list format for '{dev.name}'")
                return
            
            if not device_ids:
                self.log_warning(f"[SUNRISE] No valid devices in list for '{dev.name}'")
                return
            
            # Initialize device tracking
            device_states = {}
            for device_id in device_ids:
                try:
                    dimmer_dev = indigo.devices[device_id]
                    current_brightness = dimmer_dev.brightness
                    
                    # Check if device should be included
                    if mode == "Sunrise" and current_brightness >= target_brightness:
                        self.log_detailed(f"[SUNRISE] Ignoring '{dimmer_dev.name}' - already at or above target ({current_brightness}% >= {target_brightness}%)")
                        continue
                    elif mode == "Sunset" and current_brightness <= target_brightness:
                        self.log_detailed(f"[SUNRISE] Ignoring '{dimmer_dev.name}' - already at or below target ({current_brightness}% <= {target_brightness}%)")
                        continue
                    
                    if reset_devices:
                        # Reset to starting brightness
                        device_states[device_id] = {
                            'name': dimmer_dev.name,
                            'start_brightness': start_brightness,
                            'active': True,
                            'last_applied_brightness': None  # Set once we've actually commanded a brightness
                        }
                        self.log_detailed(f"[SUNRISE] Device '{dimmer_dev.name}': Will reset to {start_brightness}%")
                    else:
                        # Respect current brightness
                        device_states[device_id] = {
                            'name': dimmer_dev.name,
                            'start_brightness': current_brightness,
                            'active': False,  # Will activate when others catch up
                            'last_applied_brightness': None
                        }
                        self.log_detailed(f"[SUNRISE] Device '{dimmer_dev.name}': Starting at current {current_brightness}%, will sync when others reach this level")
                
                except KeyError:
                    self.logger.error(f"[SUNRISE] Device ID {device_id} not found")
                except Exception as e:
                    self.logger.error(f"[SUNRISE] Error initializing device {device_id}: {e}")
            
            if not device_states:
                self.log_warning(f"[SUNRISE] No devices to control for '{dev.name}'")
                dev.updateStateOnServer("status", "Idle")
                dev.updateStateOnServer("onOffState", False)
                return
            
            # Execute start action group
            start_action_group = dev.pluginProps.get('startActionGroup')
            if start_action_group and start_action_group != "none":
                self.log_detailed(f"[SUNRISE] Executing start action group")
                self._execute_action_group(start_action_group)
            
            # Reset devices if configured
            if reset_devices:
                self.log_detailed(f"[SUNRISE] Resetting {len(device_states)} devices to {start_brightness}%")
                for device_id in device_states.keys():
                    try:
                        if start_brightness == 0:
                            indigo.device.turnOff(device_id)
                        else:
                            indigo.dimmer.setBrightness(device_id, value=start_brightness)
                    except Exception as e:
                        self.logger.error(f"[SUNRISE] Error resetting device {device_id}: {e}")
                time.sleep(0.5)  # Brief pause after reset
            
            # Main simulation loop
            start_time = time.time()
            update_interval = max(0.5, duration / 200)  # Update at most 200 times, minimum 0.5s
            self.log_info(f"[SUNRISE] Update interval: {update_interval:.2f}s")
            
            stop_event = self.sunrise_stop_events.get(dev_id)
            pause_event = self.sunrise_pause_events.get(dev_id)
            
            while True:
                # Check for stop signal
                if stop_event and stop_event.is_set():
                    self.log_info(f"[SUNRISE] Stop signal received for '{dev.name}'")
                    break
                
                # Check for pause signal
                if pause_event and pause_event.is_set():
                    if dev.states['status'] != "Paused":
                        dev.updateStateOnServer("status", "Paused")
                        self.log_detailed(f"[SUNRISE] Paused '{dev.name}'")
                    time.sleep(0.5)
                    continue
                elif dev.states['status'] == "Paused":
                    dev.updateStateOnServer("status", "Running")
                    self.log_detailed(f"[SUNRISE] Resumed '{dev.name}'")
                    # Adjust start time to account for pause
                    elapsed_before_pause = time.time() - start_time - dev.states['durationRemaining']
                    start_time = time.time() - elapsed_before_pause
                
                # Calculate progress
                elapsed = time.time() - start_time
                progress = min(1.0, elapsed / duration)
                
                # Calculate current brightness
                current_brightness = self._calculate_brightness_with_curve(
                    start_brightness, target_brightness, progress, curve_type
                )
                
                # Update device states
                remaining = max(0, duration - int(elapsed))
                percentage_complete = int(progress * 100)
                
                # Format display string based on status
                if remaining > 0:
                    minutes = remaining // 60
                    seconds = remaining % 60
                    display_string = f"{minutes}:{seconds:02d}"
                else:
                    display_string = "Idle"
                
                dev.updateStateOnServer("currentBrightness", current_brightness)
                dev.updateStateOnServer("durationRemaining", remaining)
                dev.updateStateOnServer("percentageComplete", percentage_complete)
                dev.updateStateOnServer("displayString", display_string)
                
                # Check for manual intervention on devices we're actively controlling. If the
                # actual brightness no longer matches what we last commanded (beyond a small
                # tolerance for hardware rounding), someone changed it by hand - cancel the
                # whole simulation rather than fight the user for control of the light.
                manual_override_detected = False
                for device_id, state in device_states.items():
                    if not state['active'] or state['last_applied_brightness'] is None:
                        continue
                    try:
                        dimmer_dev = indigo.devices[device_id]
                        current_device_brightness = dimmer_dev.brightness
                        if abs(current_device_brightness - state['last_applied_brightness']) > 2:
                            self.log_info(
                                f"[SUNRISE] Manual brightness change detected on '{state['name']}' "
                                f"(expected {state['last_applied_brightness']}%, found {current_device_brightness}%) "
                                f"- cancelling simulation for '{dev.name}'"
                            )
                            manual_override_detected = True
                            break
                    except Exception as e:
                        self.logger.error(f"[SUNRISE] Error checking device {device_id}: {e}")
                
                if manual_override_detected:
                    # Cancel exactly like a manual "Turn Off"/Stop command would - run the
                    # Stop action group and reset device state - then exit the thread
                    # without touching any of the controlled devices further.
                    with self.sunrise_lock:
                        existing_stop_event = self.sunrise_stop_events.get(dev_id)
                        if existing_stop_event:
                            existing_stop_event.set()
                    
                    stop_action_group = dev.pluginProps.get('stopActionGroup')
                    if stop_action_group and stop_action_group != "none":
                        self.log_detailed(f"[SUNRISE] Executing stop action group")
                        self._execute_action_group(stop_action_group)
                    
                    dev.updateStateOnServer("status", "Idle")
                    dev.updateStateOnServer("onOffState", False)
                    dev.updateStateOnServer("durationRemaining", 0)
                    dev.updateStateOnServer("percentageComplete", 0)
                    dev.updateStateOnServer("displayString", "Idle")
                    break
                
                # Monitor inactive devices for manual pre-activation adjustments - this lets
                # someone turn on/adjust a light before the simulation reaches it, and the
                # simulation will treat that as its new starting threshold and sync to it
                # once the simulated brightness catches up (non-reset mode only).
                if not reset_devices:
                    for device_id, state in device_states.items():
                        if state['active']:
                            continue
                        try:
                            dimmer_dev = indigo.devices[device_id]
                            current_device_brightness = dimmer_dev.brightness
                            
                            if current_device_brightness != state['start_brightness']:
                                self.log_detailed(f"[SUNRISE] '{state['name']}' brightness manually changed from {state['start_brightness']}% to {current_device_brightness}% - updating threshold")
                                state['start_brightness'] = current_device_brightness
                                
                                # Check if current simulation brightness has already passed this new threshold
                                if mode == "Sunrise" and current_brightness >= current_device_brightness:
                                    state['active'] = True
                                    self.log_detailed(f"[SUNRISE] '{state['name']}' now active immediately (simulation already at {current_brightness}%, device at {current_device_brightness}%)")
                                elif mode == "Sunset" and current_brightness <= current_device_brightness:
                                    state['active'] = True
                                    self.log_detailed(f"[SUNRISE] '{state['name']}' now active immediately (simulation already at {current_brightness}%, device at {current_device_brightness}%)")
                        except Exception as e:
                            self.logger.error(f"[SUNRISE] Error checking device {device_id}: {e}")
                
                # Check which devices should be activated based on current brightness
                if not reset_devices:
                    for device_id, state in device_states.items():
                        if not state['active']:
                            # Check if we've reached this device's starting brightness
                            if mode == "Sunrise" and current_brightness >= state['start_brightness']:
                                state['active'] = True
                                self.log_detailed(f"[SUNRISE] Activating '{state['name']}' - simulation reached {current_brightness}%, device threshold {state['start_brightness']}%")
                            elif mode == "Sunset" and current_brightness <= state['start_brightness']:
                                state['active'] = True
                                self.log_detailed(f"[SUNRISE] Activating '{state['name']}' - simulation reached {current_brightness}%, device threshold {state['start_brightness']}%")
                
                # Apply brightness to active devices
                for device_id, state in device_states.items():
                    if state['active']:
                        try:
                            if current_brightness == 0:
                                indigo.device.turnOff(device_id)
                            else:
                                indigo.dimmer.setBrightness(device_id, value=current_brightness)
                            state['last_applied_brightness'] = current_brightness
                        except Exception as e:
                            self.logger.error(f"[SUNRISE] Error setting brightness for device {device_id}: {e}")
                
                # Check if complete
                if progress >= 1.0:
                    self.log_info(f"[SUNRISE] Complete for '{dev.name}'")
                    dev.updateStateOnServer("status", "Complete")
                    dev.updateStateOnServer("percentageComplete", 100)
                    dev.updateStateOnServer("durationRemaining", 0)
                    dev.updateStateOnServer("displayString", "Complete")
                    
                    # Execute complete action group
                    complete_action_group = dev.pluginProps.get('completeActionGroup')
                    if complete_action_group and complete_action_group != "none":
                        self.log_detailed(f"[SUNRISE] Executing complete action group")
                        self._execute_action_group(complete_action_group)
                    
                    # Wait 5 minutes then reset to Idle
                    for i in range(300):  # 5 minutes = 300 seconds
                        if stop_event and stop_event.is_set():
                            break
                        time.sleep(1)
                    
                    # Turn OFF and reset to Idle (whether stopped or not)
                    dev.updateStateOnServer("status", "Idle")
                    dev.updateStateOnServer("percentageComplete", 0)
                    dev.updateStateOnServer("onOffState", False)
                    dev.updateStateOnServer("displayString", "Idle")
                    
                    break
                
                # Sleep until next update
                time.sleep(update_interval)
        
        except Exception as e:
            self.logger.error(f"[SUNRISE] Error in sunrise thread for device {dev_id}: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
        
        finally:
            # Clean up thread tracking
            with self.sunrise_lock:
                if dev_id in self.sunrise_threads:
                    del self.sunrise_threads[dev_id]
                if dev_id in self.sunrise_stop_events:
                    del self.sunrise_stop_events[dev_id]
                if dev_id in self.sunrise_pause_events:
                    del self.sunrise_pause_events[dev_id]
            
            self.log_info(f"[SUNRISE] Thread stopped for '{indigo.devices[dev_id].name}'")
    
    def deviceStartComm(self, dev):
        # Handle variable-linked dimmers
        if dev.deviceTypeId == "myDimmerType":
            var_id = self._extract_var_id(dev.pluginProps.get("variableId", ""))
            if not var_id:
                return
                
            try:
                var = indigo.variables[var_id]
                scale_min, scale_max, is_float_scale = self._get_scale_params(dev)
                
                # Try to convert variable value to brightness
                result = self._variable_to_brightness(var.value, scale_min, scale_max)
                brightness, was_clamped, clamped_value = result if result[0] is not None else (None, False, None)
                
                if brightness is None:
                    # Invalid value - set variable to match current device state (which is 0)
                    self.log_warning(f"Invalid variable value '{var.value}' for {dev.name}, resetting to minimum")
                    new_var_value = self._brightness_to_variable(0, scale_min, scale_max, is_float_scale)
                    indigo.variable.updateValue(var_id, new_var_value)
                    brightness = 0
                elif was_clamped:
                    # Value was out of range - correct it
                    new_var_value = self._brightness_to_variable(brightness, scale_min, scale_max, is_float_scale)
                    self.log_warning(f"Variable value '{var.value}' out of range for {dev.name}, correcting to {new_var_value}")
                    indigo.variable.updateValue(var_id, new_var_value)
                    var_value = new_var_value
                else:
                    var_value = var.value
                
                # Initialize caches
                cache_key = f"{dev.id}_{var_id}"
                self.last_variable_values[cache_key] = var_value if not was_clamped else new_var_value
                self.last_device_brightness[dev.id] = brightness
                
                # Update device state
                dev.updateStateOnServer("brightnessLevel", brightness)
            except Exception as e:
                self.logger.error(f"Error in deviceStartComm: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
        
        # Handle scene devices
        elif dev.deviceTypeId == "SceneDevice":
            # Check if devices match the saved scene and set initial state
            try:
                matches = self._check_scene_match(dev)
                dev.updateStateOnServer("onOffState", matches)
                self.log_info(f"Scene '{dev.name}' initialized: {'ON' if matches else 'OFF'}")
            except Exception as e:
                self.logger.error(f"Error initializing scene {dev.name}: {e}")
        
        # Handle Relay2Dimmer devices
        elif dev.deviceTypeId == "Relay2Dimmer":
            try:
                relay1_id = dev.pluginProps.get("relay1Device")
                relay2_id = dev.pluginProps.get("relay2Device")
                
                if not relay1_id or not relay2_id:
                    self.log_warning(f"Relay2Dimmer '{dev.name}' is not fully configured")
                    return
                
                # Read current relay states and set dimmer level
                relay1_on, relay2_on = self._get_relay_states(relay1_id, relay2_id)
                level = self._relay_states_to_level(relay1_on, relay2_on)
                
                dev.updateStateOnServer("brightnessLevel", level)
                dev.updateStateOnServer("onOffState", level > 0)
                
                self.log_info(f"Relay2Dimmer '{dev.name}' initialized at {level}%")
            except Exception as e:
                self.logger.error(f"Error initializing Relay2Dimmer {dev.name}: {e}")
        
        # Handle Relay2Fan devices
        elif dev.deviceTypeId == "Relay2Fan":
            try:
                relay1_id = dev.pluginProps.get("relay1Device")
                relay2_id = dev.pluginProps.get("relay2Device")
                
                if not relay1_id or not relay2_id:
                    self.log_warning(f"Relay2Fan '{dev.name}' is not fully configured")
                    return
                
                # Read current relay states and set fan level
                relay1_on, relay2_on = self._get_relay_states(relay1_id, relay2_id)
                level = self._relay_states_to_level(relay1_on, relay2_on)
                
                # Convert percentage to speed index (0-3)
                speed_index = level // 33 if level > 0 else 0
                
                speed_names = ['off', 'low', 'medium', 'high']
                self.log_info(f"Relay2Fan '{dev.name}' initialized at {speed_names[speed_index]} (index: {speed_index})")
            except Exception as e:
                self.logger.error(f"Error initializing Relay2Fan {dev.name}: {e}")
        
        # Handle Scene Monitor devices
        elif dev.deviceTypeId == "SceneMonitor":
            try:
                # Check monitored scenes and set initial state
                any_scene_on = self._check_any_scene_on(dev)
                # Scene Monitor shows OFF when at least one scene is ON
                # Scene Monitor shows ON when NO scenes are ON
                initial_state = not any_scene_on
                dev.updateStateOnServer("onOffState", initial_state)
                self.log_info(f"Scene Monitor '{dev.name}' initialized: {'ON (no scenes active)' if initial_state else 'OFF (scene active)'}")
            except Exception as e:
                self.logger.error(f"Error initializing Scene Monitor {dev.name}: {e}")
        
        # Handle Sunrise/Sunset Simulator devices
        elif dev.deviceTypeId == "SunriseSimulator":
            try:
                # Initialize all states
                dev.updateStateOnServer("onOffState", False)
                dev.updateStateOnServer("status", "Idle")
                dev.updateStateOnServer("duration", int(dev.pluginProps.get("duration", "300")))
                dev.updateStateOnServer("durationRemaining", 0)
                dev.updateStateOnServer("currentBrightness", 0)
                dev.updateStateOnServer("percentageComplete", 0)
                dev.updateStateOnServer("displayString", "Idle")
                self.log_info(f"Sunrise/Sunset Simulator '{dev.name}' initialized")
            except Exception as e:
                self.logger.error(f"Error initializing Sunrise/Sunset Simulator {dev.name}: {e}")
    
    def deviceUpdated(self, old_dev, new_dev):
        """Called whenever a device is updated - we use this to catch brightness changes and relay changes"""
        # Handle variable-linked dimmers (only our plugin devices)
        if new_dev.pluginId == self.pluginId and new_dev.deviceTypeId == "myDimmerType":
            # Skip if this device is currently being flashed
            if new_dev.id in self.flashing_devices:
                return
            
            # Get the brightness level
            new_brightness = self._get_brightness(new_dev)
            old_brightness = self.last_device_brightness.get(new_dev.id, -1)
            
            # Check if brightness changed
            if new_brightness != old_brightness:
                self.last_device_brightness[new_dev.id] = new_brightness
                
                # Update the linked variable
                var_id = self._extract_var_id(new_dev.pluginProps.get("variableId", ""))
                if not var_id:
                    return
                
                try:
                    scale_min, scale_max, is_float_scale = self._get_scale_params(new_dev)
                    var_value = self._brightness_to_variable(new_brightness, scale_min, scale_max, is_float_scale)
                    
                    indigo.variable.updateValue(var_id, var_value)
                    
                    # Update cache to prevent re-trigger
                    cache_key = f"{new_dev.id}_{var_id}"
                    self.last_variable_values[cache_key] = var_value
                except Exception as e:
                    self.logger.error(f"Error updating variable: {e}")
        
        # Monitor ALL relay devices for Relay2Dimmer/Fan devices
        # Check by class name since relays can be from any plugin or native Indigo
        if hasattr(new_dev, '__class__'):
            class_name = new_dev.__class__.__name__
            
            if class_name == 'RelayDevice':
                # Only process if the state actually changed
                if hasattr(old_dev, 'onState') and hasattr(new_dev, 'onState'):
                    if old_dev.onState == new_dev.onState:
                        return  # No change, skip processing
                
                self.log_debug(f"Relay '{new_dev.name}' state changed to {'ON' if new_dev.onState else 'OFF'}")
                
                # Check if this relay is part of any Relay2 devices
                for dev in indigo.devices.iter(filter="self"):
                    if dev.deviceTypeId in ["Relay2Dimmer", "Relay2Fan"]:
                        relay1_id = dev.pluginProps.get("relay1Device")
                        relay2_id = dev.pluginProps.get("relay2Device")
                        
                        if str(new_dev.id) in [relay1_id, relay2_id]:
                            # This relay is part of a Relay2 device - update it
                            try:
                                relay1_on, relay2_on = self._get_relay_states(relay1_id, relay2_id)
                                level = self._relay_states_to_level(relay1_on, relay2_on)
                                
                                if dev.deviceTypeId == "Relay2Dimmer":
                                    self.log_debug(f"Relay change detected, updating Relay2Dimmer '{dev.name}' to {level}%")
                                    dev.updateStateOnServer("brightnessLevel", level)
                                    dev.updateStateOnServer("onOffState", level > 0)
                                else:  # Relay2Fan
                                    speed_index = level // 33 if level > 0 else 0
                                    speed_names = ['off', 'low', 'medium', 'high']
                                    self.log_debug(f"Relay change detected, updating Relay2Fan '{dev.name}' to {speed_names[speed_index]}")
                                    dev.updateStateOnServer("speedIndex", speed_index)
                                    dev.updateStateOnServer("speedLevel", level)
                                    dev.updateStateOnServer("onOffState", level > 0)
                            except Exception as e:
                                self.logger.error(f"Error updating Relay2 device {dev.name}: {e}")
    
    def actionControlDimmerRelay(self, action, dev):
        """Main entry point for dimmer/relay device control actions"""
        self.log_detailed(f"actionControlDimmerRelay called for {dev.name} (type: {dev.deviceTypeId}), action: {action.deviceAction}")
        
        # Handle Relay2Dimmer devices
        if dev.deviceTypeId == "Relay2Dimmer":
            self.log_detailed(f"Handling Relay2Dimmer device: {dev.name}")
            relay1_id = dev.pluginProps.get("relay1Device")
            relay2_id = dev.pluginProps.get("relay2Device")
            
            self.log_info(f"Relay1: {relay1_id}, Relay2: {relay2_id}")
            
            if not relay1_id or not relay2_id:
                self.logger.error(f"Relay2Dimmer '{dev.name}' is not fully configured")
                return
            
            target_level = None
            
            if action.deviceAction == indigo.kDimmerRelayAction.TurnOn:
                target_level = 100
            elif action.deviceAction == indigo.kDimmerRelayAction.TurnOff:
                target_level = 0
            elif action.deviceAction == indigo.kDimmerRelayAction.SetBrightness:
                value = action.actionValue
                if isinstance(value, list):
                    value = value[0] if value else 0
                target_level = value
            elif action.deviceAction == indigo.kDimmerRelayAction.BrightenBy:
                current = dev.brightness
                value = action.actionValue
                if isinstance(value, list):
                    value = value[0] if value else 0
                target_level = min(100, current + value)
            elif action.deviceAction == indigo.kDimmerRelayAction.DimBy:
                current = dev.brightness
                value = action.actionValue
                if isinstance(value, list):
                    value = value[0] if value else 0
                target_level = max(0, current - value)
            
            if target_level is not None:
                # Round to valid level and update device state immediately
                relay1_on, relay2_on = self._level_to_relay_states(target_level)
                rounded_level = self._relay_states_to_level(relay1_on, relay2_on)
                
                self.log_detailed(f"Relay2Dimmer '{dev.name}': setting to {rounded_level}%")
                
                # Update device state immediately so UI shows the change
                dev.updateStateOnServer("brightnessLevel", rounded_level)
                dev.updateStateOnServer("onOffState", rounded_level > 0)
                
                # Schedule relay changes in a thread with 1 second delay
                def apply_with_delay():
                    time.sleep(1)
                    self._apply_relay_states(relay1_id, relay2_id, relay1_on, relay2_on)
                
                thread = threading.Thread(target=apply_with_delay)
                thread.daemon = True
                thread.start()
            
            return
        
        # Handle variable-linked dimmers
        if action.deviceAction == indigo.kDimmerRelayAction.TurnOn:
            self.handleDimmerAction(action, dev, 100)
        elif action.deviceAction == indigo.kDimmerRelayAction.TurnOff:
            self.handleDimmerAction(action, dev, 0)
        elif action.deviceAction == indigo.kDimmerRelayAction.SetBrightness:
            value = action.actionValue
            if isinstance(value, list):
                value = value[0] if value else 0
            self.handleDimmerAction(action, dev, value)
        elif action.deviceAction == indigo.kDimmerRelayAction.BrightenBy:
            current = self._get_brightness(dev)
            value = action.actionValue
            if isinstance(value, list):
                value = value[0] if value else 0
            new_level = min(100, current + value)
            self.handleDimmerAction(action, dev, new_level)
        elif action.deviceAction == indigo.kDimmerRelayAction.DimBy:
            current = self._get_brightness(dev)
            value = action.actionValue
            if isinstance(value, list):
                value = value[0] if value else 0
            new_level = max(0, current - value)
            self.handleDimmerAction(action, dev, new_level)
    
    def handleDimmerAction(self, action, dev, level):
        """Handle the actual dimmer action"""
        var_id = self._extract_var_id(dev.pluginProps.get("variableId", ""))
        if not var_id:
            return
            
        try:
            level = max(0, min(100, int(level)))
            
            # Check minimum brightness threshold
            min_threshold = int(dev.pluginProps.get("minBrightnessThreshold", 0))
            if level > 0 and level < min_threshold:
                level = 0  # Treat below threshold as OFF
            
            scale_min, scale_max, is_float_scale = self._get_scale_params(dev)
            var_value = self._brightness_to_variable(level, scale_min, scale_max, is_float_scale)
            
            indigo.variable.updateValue(var_id, var_value)
            dev.updateStateOnServer("brightnessLevel", level)
            self.last_device_brightness[dev.id] = level
            
            # Update cached value to prevent immediate re-trigger
            cache_key = f"{dev.id}_{var_id}"
            self.last_variable_values[cache_key] = var_value
        except Exception as e:
            self.logger.error(f"Error in handleDimmerAction: {e}")
    
    def actionControlSpeedControl(self, action, dev):
        """Handle speed control actions for Relay2Fan devices"""
        if dev.deviceTypeId == "Relay2Fan":
            relay1_id = dev.pluginProps.get("relay1Device")
            relay2_id = dev.pluginProps.get("relay2Device")
            
            if not relay1_id or not relay2_id:
                self.logger.error(f"Relay2Fan '{dev.name}' is not fully configured")
                return
            
            target_speed_index = None
            
            if action.speedControlAction == indigo.kSpeedControlAction.TurnOn:
                target_speed_index = 3  # High
            elif action.speedControlAction == indigo.kSpeedControlAction.TurnOff:
                target_speed_index = 0  # Off
            elif action.speedControlAction == indigo.kSpeedControlAction.SetSpeedIndex:
                value = action.actionValue
                if isinstance(value, list):
                    value = value[0] if value else 0
                target_speed_index = max(0, min(3, int(value)))
            elif action.speedControlAction == indigo.kSpeedControlAction.IncreaseSpeedIndex:
                current = dev.states.get('speedIndex', 0)
                target_speed_index = min(3, current + 1)
            elif action.speedControlAction == indigo.kSpeedControlAction.DecreaseSpeedIndex:
                current = dev.states.get('speedIndex', 0)
                target_speed_index = max(0, current - 1)
            
            if target_speed_index is not None:
                # Convert speed index to level (0, 33, 66, 100)
                level = target_speed_index * 33 if target_speed_index > 0 else 0
                
                speed_names = ['off', 'low', 'medium', 'high']
                self.log_detailed(f"Relay2Fan '{dev.name}': setting to {speed_names[target_speed_index]}")
                
                # Schedule relay changes in a thread with 1 second delay
                relay1_on, relay2_on = self._level_to_relay_states(level)
                
                def apply_with_delay():
                    time.sleep(1)
                    self._apply_relay_states(relay1_id, relay2_id, relay1_on, relay2_on)
                
                thread = threading.Thread(target=apply_with_delay)
                thread.daemon = True
                thread.start()
    
    def _apply_multi_device_brightness(self, dev, brightness):
        """Apply brightness to all controlled devices in a multi-device virtual dimmer"""
        self.log_detailed(f"[MVDD-APPLY] Starting _apply_multi_device_brightness for '{dev.name}' with brightness={brightness}%")
        
        controlled_devices = dev.pluginProps.get("controlledDevices", [])
        if not controlled_devices:
            self.log_detailed(f"[MVDD-APPLY] No controlled devices configured")
            return
        
        # Get threshold
        try:
            threshold = int(dev.pluginProps.get("relayThreshold", "50"))
            threshold = max(0, min(100, threshold))  # Clamp to 0-100
            self.log_detailed(f"[MVDD-APPLY] Relay threshold: {threshold}%")
        except (ValueError, TypeError):
            threshold = 50
            self.log_warning(f"[MVDD-APPLY] Invalid threshold, using default: {threshold}%")
        
        # Convert Indigo.List to regular list if needed
        if hasattr(controlled_devices, '__iter__') and not isinstance(controlled_devices, str):
            try:
                controlled_devices = list(controlled_devices)
            except TypeError:
                self.logger.error(f"[MVDD-APPLY] Invalid device list")
                return
        
        # Parse controlled devices
        try:
            if isinstance(controlled_devices, str):
                device_ids = [int(d.strip()) for d in controlled_devices.split(",") if d.strip()]
            else:
                device_ids = [int(d) for d in controlled_devices if d]
            self.log_detailed(f"[MVDD-APPLY] Controlling {len(device_ids)} devices: {device_ids}")
        except (ValueError, TypeError):
            self.logger.error(f"[MVDD-APPLY] Invalid device list format")
            return
        
        # FIRST: Update all cache entries BEFORE applying changes
        # This prevents monitoring from detecting changes during the transition
        self.log_detailed(f"[MVDD-APPLY] Step 1: Pre-caching target states...")
        for device_id in device_ids:
            try:
                controlled_dev = indigo.devices[device_id]
                cache_key = f"{dev.id}_{device_id}"
                
                # Pre-update cache based on what we're about to do
                if hasattr(controlled_dev, 'brightness'):
                    # It's a dimmer - cache the target brightness
                    self.multi_device_controlled_states[cache_key] = brightness
                    self.log_detailed(f"[MVDD-APPLY]   Dimmer '{controlled_dev.name}' (ID:{device_id}) → cached brightness: {brightness}%")
                else:
                    # It's a relay - cache the target state
                    target_state = (brightness >= threshold)
                    self.multi_device_controlled_states[cache_key] = target_state
                    self.log_detailed(f"[MVDD-APPLY]   Relay '{controlled_dev.name}' (ID:{device_id}) → cached state: {'ON' if target_state else 'OFF'} (brightness {brightness}% {'≥' if target_state else '<'} threshold {threshold}%)")
            except Exception as e:
                self.logger.error(f"[MVDD-APPLY] Error caching device {device_id}: {e}")
        
        # THEN: Apply brightness to each controlled device
        self.log_detailed(f"[MVDD-APPLY] Step 2: Applying changes to devices...")
        for device_id in device_ids:
            try:
                controlled_dev = indigo.devices[device_id]
                current_state = controlled_dev.brightness if hasattr(controlled_dev, 'brightness') else controlled_dev.onState
                
                # Check if it's a dimmer device
                if hasattr(controlled_dev, 'brightness'):
                    # It's a dimmer - mirror the brightness exactly
                    if brightness == 0:
                        self.log_detailed(f"[MVDD-APPLY]   Turning OFF dimmer '{controlled_dev.name}' (was {current_state}%)")
                        indigo.device.turnOff(device_id)
                    else:
                        self.log_detailed(f"[MVDD-APPLY]   Setting dimmer '{controlled_dev.name}' to {brightness}% (was {current_state}%)")
                        indigo.dimmer.setBrightness(device_id, value=brightness)
                else:
                    # It's a relay - use threshold logic
                    if brightness >= threshold:
                        self.log_detailed(f"[MVDD-APPLY]   Turning ON relay '{controlled_dev.name}' (brightness {brightness}% ≥ threshold {threshold}%, was {'ON' if current_state else 'OFF'})")
                        indigo.device.turnOn(device_id)
                    else:
                        self.log_detailed(f"[MVDD-APPLY]   Turning OFF relay '{controlled_dev.name}' (brightness {brightness}% < threshold {threshold}%, was {'ON' if current_state else 'OFF'})")
                        indigo.device.turnOff(device_id)
            
            except Exception as e:
                self.logger.error(f"[MVDD-APPLY] Error controlling device {device_id}: {e}")
        
        self.log_detailed(f"[MVDD-APPLY] Completed _apply_multi_device_brightness for '{dev.name}'")
    
    def actionControlDevice(self, action, dev):

        """Handle general device actions"""
        # Handle Relay2Fan as a custom device
        if dev.deviceTypeId == "Relay2Fan":
            relay1_id = dev.pluginProps.get("relay1Device")
            relay2_id = dev.pluginProps.get("relay2Device")
            
            if not relay1_id or not relay2_id:
                self.logger.error(f"Relay2Fan '{dev.name}' is not fully configured")
                return
            
            target_speed_index = None
            
            if action.deviceAction == indigo.kDeviceAction.TurnOn:
                target_speed_index = 3  # High
            elif action.deviceAction == indigo.kDeviceAction.TurnOff:
                target_speed_index = 0  # Off
            elif action.deviceAction == indigo.kDeviceAction.Toggle:
                current = dev.states.get('speedIndex', 0)
                target_speed_index = 0 if current > 0 else 3
            
            if target_speed_index is not None:
                level = target_speed_index * 33 if target_speed_index > 0 else 0
                
                speed_names = ['off', 'low', 'medium', 'high']
                self.log_detailed(f"Relay2Fan '{dev.name}': setting to {speed_names[target_speed_index]}")
                
                dev.updateStateOnServer("speedIndex", target_speed_index)
                dev.updateStateOnServer("speedIndex.ui", speed_names[target_speed_index])
                dev.updateStateOnServer("speedLevel", level)
                dev.updateStateOnServer("onOffState", target_speed_index > 0)
                
                relay1_on, relay2_on = self._level_to_relay_states(level)
                
                def apply_with_delay():
                    time.sleep(1)
                    self._apply_relay_states(relay1_id, relay2_id, relay1_on, relay2_on)
                
                thread = threading.Thread(target=apply_with_delay)
                thread.daemon = True
                thread.start()
            
            return
    
    def runConcurrentThread(self):
        """Poll for variable changes and update devices accordingly, also monitor scenes"""
        try:
            while True:
                # Handle variable-linked dimmers
                for dev in indigo.devices.iter(filter="self"):
                    if dev.deviceTypeId == "myDimmerType":
                        var_id = self._extract_var_id(dev.pluginProps.get("variableId", ""))
                        if not var_id:
                            continue
                        
                        try:
                            var = indigo.variables[var_id]
                            current_value = var.value
                            
                            # Handle if value is a list
                            while isinstance(current_value, list):
                                current_value = current_value[0] if current_value else "0"
                            
                            current_value = str(current_value)
                            
                            # Check if value changed
                            cache_key = f"{dev.id}_{var_id}"
                            last_value = self.last_variable_values.get(cache_key)
                            
                            if last_value != current_value:
                                scale_min, scale_max, is_float_scale = self._get_scale_params(dev)
                                result = self._variable_to_brightness(current_value, scale_min, scale_max)
                                brightness, was_clamped, clamped_value = result if result[0] is not None else (None, False, None)
                                
                                if brightness is None:
                                    # Invalid value - reset variable to current device brightness
                                    current_brightness = self._get_brightness(dev)
                                    corrected_value = self._brightness_to_variable(current_brightness, scale_min, scale_max, is_float_scale)
                                    self.log_warning(f"Invalid variable value '{current_value}' for {dev.name}, resetting to {corrected_value}")
                                    indigo.variable.updateValue(var_id, corrected_value)
                                    self.last_variable_values[cache_key] = corrected_value
                                elif was_clamped:
                                    # Value was out of range - correct it
                                    corrected_value = self._brightness_to_variable(brightness, scale_min, scale_max, is_float_scale)
                                    self.log_warning(f"Variable value '{current_value}' out of range for {dev.name}, correcting to {corrected_value}")
                                    indigo.variable.updateValue(var_id, corrected_value)
                                    self.last_variable_values[cache_key] = corrected_value
                                    dev.updateStateOnServer("brightnessLevel", brightness)
                                    self.last_device_brightness[dev.id] = brightness
                                else:
                                    # Valid value, update device
                                    self.last_variable_values[cache_key] = current_value
                                    dev.updateStateOnServer("brightnessLevel", brightness)
                                    self.last_device_brightness[dev.id] = brightness
                        except Exception as e:
                            self.logger.error(f"Error checking variable: {e}")
                    
                    # Handle scene devices
                    elif dev.deviceTypeId == "SceneDevice":
                        try:
                            # Check if there's an activation timer (15-second grace period after turning ON)
                            activation_timer = self.scene_activation_timers.get(dev.id)
                            if activation_timer:
                                if time.time() < activation_timer:
                                    # Still within grace period, skip monitoring
                                    continue
                                else:
                                    # Grace period expired, remove timer
                                    with self.scene_lock:
                                        if dev.id in self.scene_activation_timers:
                                            del self.scene_activation_timers[dev.id]
                            
                            # Check if there's a pending timer for this scene
                            timer_info = self.scene_off_timers.get(dev.id)
                            if timer_info:
                                # Check if 10 seconds have elapsed
                                if time.time() >= timer_info['check_time']:
                                    # Remove the timer
                                    with self.scene_lock:
                                        if dev.id in self.scene_off_timers:
                                            del self.scene_off_timers[dev.id]
                                    
                                    # Check if devices match the scene
                                    if self._check_scene_match(dev):
                                        dev.updateStateOnServer("onOffState", True)
                                # If timer still active, skip normal monitoring
                                continue
                            
                            # Normal monitoring - check if current states match scene
                            matches = self._check_scene_match(dev)
                            
                            # Update device state if it changed
                            if matches != dev.onState:
                                dev.updateStateOnServer("onOffState", matches)
                        
                        except Exception as e:
                            self.logger.error(f"Error monitoring scene {dev.name}: {e}")
                
                # Handle Relay2Dimmer and Relay2Fan monitoring
                for dev in indigo.devices.iter(filter="self"):
                    if dev.deviceTypeId in ["Relay2Dimmer", "Relay2Fan"]:
                        try:
                            relay1_id = dev.pluginProps.get("relay1Device")
                            relay2_id = dev.pluginProps.get("relay2Device")
                            
                            if not relay1_id or not relay2_id:
                                continue
                            
                            # Get current relay states
                            relay1_on, relay2_on = self._get_relay_states(relay1_id, relay2_id)
                            
                            # Check if states changed from last check
                            cache_key = f"{dev.id}_relay_states"
                            last_states = self.relay2_last_states.get(cache_key)
                            current_states = (relay1_on, relay2_on)
                            
                            if last_states != current_states:
                                # States changed - update device
                                self.relay2_last_states[cache_key] = current_states
                                level = self._relay_states_to_level(relay1_on, relay2_on)
                                
                                if dev.deviceTypeId == "Relay2Dimmer":
                                    self.log_debug(f"Relay change detected, updating Relay2Dimmer '{dev.name}' to {level}%")
                                    dev.updateStateOnServer("brightnessLevel", level)
                                    dev.updateStateOnServer("onOffState", level > 0)
                                else:  # Relay2Fan
                                    speed_index = level // 33 if level > 0 else 0
                                    speed_names = ['off', 'low', 'medium', 'high']
                                    self.log_debug(f"Relay change detected, updating Relay2Fan '{dev.name}' to {speed_names[speed_index]}")
                                    dev.updateStateOnServer("speedIndex", speed_index)
                                    dev.updateStateOnServer("speedLevel", level)
                                    dev.updateStateOnServer("onOffState", level > 0)
                        
                        except Exception as e:
                            self.logger.error(f"Error monitoring Relay2 device {dev.name}: {e}")
                
                # Handle MultiDeviceVirtualDimmer monitoring
                for dev in indigo.devices.iter(filter="self"):
                    if dev.deviceTypeId == "MultiDeviceVirtualDimmer":
                        try:
                            # Extended Debug: dump VirtualDimmer state at start of tick
                            if self.log_level >= LOG_EXTENDED:
                                vd_brightness = getattr(dev, 'brightness', None)
                                vd_onstate = getattr(dev, 'onState', None)
                                vd_last = self.multi_device_last_brightness.get(dev.id)
                                cd_time = self.multi_device_cooldown.get(dev.id)
                                if cd_time:
                                    cd_str = f"{cd_time - time.time():.2f}s"
                                else:
                                    cd_str = "none"
                                self.log_extended(
                                    f"[MVDD-TICK] '{dev.name}': VirtualDimmer brightness={vd_brightness}%, "
                                    f"onState={vd_onstate}, last_set={vd_last}%, cooldown_remaining={cd_str}"
                                )
                            
                            controlled_devices = dev.pluginProps.get("controlledDevices", [])
                            if not controlled_devices:
                                continue
                            
                            # Convert Indigo.List to regular list if needed
                            if hasattr(controlled_devices, '__iter__') and not isinstance(controlled_devices, str):
                                try:
                                    controlled_devices = list(controlled_devices)
                                except TypeError:
                                    continue
                            
                            # Parse device IDs
                            try:
                                if isinstance(controlled_devices, str):
                                    device_ids = [int(d.strip()) for d in controlled_devices.split(",") if d.strip()]
                                else:
                                    device_ids = [int(d) for d in controlled_devices if d]
                            except (ValueError, TypeError):
                                continue
                            
                            # Check if device is in cooldown period
                            cooldown_time = self.multi_device_cooldown.get(dev.id)
                            if cooldown_time and time.time() < cooldown_time:
                                # Still in cooldown, skip monitoring
                                remaining = cooldown_time - time.time()
                                self.log_debug(f"[MVDD-MONITOR] '{dev.name}' in cooldown ({remaining:.1f}s remaining)")
                                continue
                            elif cooldown_time:
                                # Cooldown just expired. Devices (especially Z-Wave/relay hardware)
                                # don't always settle at the same speed, so rather than trusting the
                                # target brightness we told them to reach, re-baseline our per-device
                                # cache to whatever each device actually reports right now. Otherwise a
                                # device that's simply slow to catch up gets misread on the next tick as
                                # a fresh manual change, which re-triggers a sync using its stale value
                                # and flips the devices that DID update correctly back the wrong way -
                                # producing an ongoing ping-pong between the grouped devices.
                                self.log_debug(f"[MVDD-MONITOR] '{dev.name}' cooldown expired, re-baselining to actual device states")
                                del self.multi_device_cooldown[dev.id]
                                for device_id in device_ids:
                                    try:
                                        controlled_dev = indigo.devices[device_id]
                                        cache_key = f"{dev.id}_{device_id}"
                                        if hasattr(controlled_dev, 'brightness'):
                                            self.multi_device_controlled_states[cache_key] = controlled_dev.brightness
                                        else:
                                            self.multi_device_controlled_states[cache_key] = controlled_dev.onState
                                    except Exception as e:
                                        self.logger.error(f"[MVDD-MONITOR] Error re-baselining device {device_id}: {e}")
                                continue
                            
                            # Get threshold
                            try:
                                threshold = int(dev.pluginProps.get("relayThreshold", "50"))
                                threshold = max(0, min(100, threshold))
                            except (ValueError, TypeError):
                                threshold = 50
                            
                            # Check each controlled device for changes
                            for device_id in device_ids:
                                try:
                                    controlled_dev = indigo.devices[device_id]
                                    cache_key = f"{dev.id}_{device_id}"
                                    
                                    # Extended Debug: dump controlled device state
                                    if self.log_level >= LOG_EXTENDED:
                                        is_dimmer = hasattr(controlled_dev, 'brightness')
                                        cached = self.multi_device_controlled_states.get(cache_key)
                                        if is_dimmer:
                                            self.log_extended(
                                                f"[MVDD-TICK]   Dimmer '{controlled_dev.name}' "
                                                f"(ID:{device_id}): brightness={controlled_dev.brightness}%, "
                                                f"onState={controlled_dev.onState}, cached_last={cached}"
                                            )
                                        else:
                                            self.log_extended(
                                                f"[MVDD-TICK]   Relay '{controlled_dev.name}' "
                                                f"(ID:{device_id}): onState={controlled_dev.onState}, "
                                                f"cached_last={cached}"
                                            )
                                    
                                    # Get current state
                                    if hasattr(controlled_dev, 'brightness'):
                                        # It's a dimmer - check brightness
                                        current_brightness = controlled_dev.brightness
                                        last_brightness = self.multi_device_controlled_states.get(cache_key)
                                        
                                        if last_brightness != current_brightness:
                                            # Dimmer changed - update virtual dimmer and sync all devices
                                            self.multi_device_controlled_states[cache_key] = current_brightness
                                            self.log_debug(f"[MVDD-MONITOR] Dimmer '{controlled_dev.name}' changed: {last_brightness}% → {current_brightness}%")
                                            
                                            # Only respond if this isn't the change we just made
                                            vd_last_brightness = self.multi_device_last_brightness.get(dev.id)
                                            if vd_last_brightness != current_brightness:
                                                self.log_warning(f"[MVDD-MONITOR] ⚠️ Controlled dimmer changed externally! VirtualDimmer last={vd_last_brightness}%, Dimmer now={current_brightness}%")
                                                self.log_debug(f"[MVDD-MONITOR] Syncing VirtualDimmer '{dev.name}' to {current_brightness}%")
                                                
                                                # Set cooldown timer to prevent feedback loops (5 seconds)
                                                cooldown_time = time.time() + 5
                                                self.multi_device_cooldown[dev.id] = cooldown_time
                                                self.log_debug(f"[MVDD-MONITOR] Cooldown set until {time.strftime('%H:%M:%S', time.localtime(cooldown_time))}")
                                                
                                                # Update virtual dimmer
                                                dev.updateStateOnServer("brightnessLevel", current_brightness)
                                                dev.updateStateOnServer("onOffState", current_brightness > 0)
                                                self.multi_device_last_brightness[dev.id] = current_brightness
                                                
                                                # Sync all controlled devices
                                                self.log_debug(f"[MVDD-MONITOR] Syncing all controlled devices to {current_brightness}%")
                                                self._apply_multi_device_brightness(dev, current_brightness)
                                                
                                                # Break out of device checking loop to prevent conflicting changes
                                                break
                                            else:
                                                self.log_debug(f"[MVDD-MONITOR] Dimmer change matches expected (both {current_brightness}%), ignoring")
                                    else:
                                        # It's a relay - check on/off state
                                        current_state = controlled_dev.onState
                                        last_state = self.multi_device_controlled_states.get(cache_key)
                                        
                                        if last_state != current_state:
                                            # Relay changed
                                            self.multi_device_controlled_states[cache_key] = current_state
                                            self.log_debug(f"[MVDD-MONITOR] Relay '{controlled_dev.name}' changed: {'ON' if last_state else 'OFF'} → {'ON' if current_state else 'OFF'}")
                                            
                                            if current_state:
                                                # Relay turned ON - set virtual dimmer to threshold
                                                target_brightness = threshold
                                                self.log_warning(f"[MVDD-MONITOR] ⚠️ Relay turned ON externally! Setting VirtualDimmer '{dev.name}' to threshold ({threshold}%)")
                                            else:
                                                # Relay turned OFF - set virtual dimmer to 0
                                                target_brightness = 0
                                                self.log_warning(f"[MVDD-MONITOR] ⚠️ Relay turned OFF externally! Setting VirtualDimmer '{dev.name}' to 0%")
                                            
                                            # Only respond if this isn't the change we just made
                                            vd_last_brightness = self.multi_device_last_brightness.get(dev.id)
                                            if vd_last_brightness != target_brightness:
                                                self.log_warning(f"[MVDD-MONITOR] ⚠️ VirtualDimmer last={vd_last_brightness}%, new target={target_brightness}%")
                                                
                                                # Set cooldown timer to prevent feedback loops (5 seconds)
                                                cooldown_time = time.time() + 5
                                                self.multi_device_cooldown[dev.id] = cooldown_time
                                                self.log_debug(f"[MVDD-MONITOR] Cooldown set until {time.strftime('%H:%M:%S', time.localtime(cooldown_time))}")
                                                
                                                # Update virtual dimmer
                                                dev.updateStateOnServer("brightnessLevel", target_brightness)
                                                dev.updateStateOnServer("onOffState", target_brightness > 0)
                                                self.multi_device_last_brightness[dev.id] = target_brightness
                                                
                                                # Sync all controlled devices
                                                self.log_debug(f"[MVDD-MONITOR] Syncing all controlled devices to {target_brightness}%")
                                                self._apply_multi_device_brightness(dev, target_brightness)
                                                
                                                # Break out of device checking loop to prevent conflicting changes
                                                break
                                            else:
                                                self.log_debug(f"[MVDD-MONITOR] Relay change matches expected (both {target_brightness}%), ignoring")
                                
                                except KeyError:
                                    # Device doesn't exist anymore, skip it
                                    pass
                                except Exception as e:
                                    self.logger.error(f"[MVDD-MONITOR] Error monitoring controlled device {device_id}: {e}")
                        
                        except Exception as e:
                            self.logger.error(f"[MVDD-MONITOR] Error monitoring MultiDeviceVirtualDimmer {dev.name}: {e}")
                
                # Handle Scene Monitor monitoring
                for dev in indigo.devices.iter(filter="self"):
                    if dev.deviceTypeId == "SceneMonitor":
                        try:
                            monitored_scenes = dev.pluginProps.get("monitoredScenes", [])
                            if not monitored_scenes:
                                continue
                            
                            # Get bounce delay
                            try:
                                bounce_delay = int(dev.pluginProps.get("bounceDelay", "5"))
                                bounce_delay = max(1, min(30, bounce_delay))  # Clamp to 1-30 seconds
                            except (ValueError, TypeError):
                                bounce_delay = 5
                            
                            # Check if any monitored scene is ON
                            any_scene_on = self._check_any_scene_on(dev)
                            
                            # Scene Monitor logic:
                            # - Shows OFF when at least one scene is ON
                            # - Shows ON when NO scenes are ON
                            desired_state = not any_scene_on
                            
                            # Get current state
                            current_state = dev.onState
                            
                            # Handle state transitions with bounce delay
                            if desired_state != current_state:
                                # State wants to change
                                if desired_state:
                                    # Want to turn ON (no scenes active)
                                    # Check if we're in bounce delay
                                    bounce_timer = self.scene_monitor_bounce_timers.get(dev.id)
                                    if bounce_timer is None:
                                        # Start bounce timer
                                        self.scene_monitor_bounce_timers[dev.id] = time.time() + bounce_delay
                                        self.log_debug(f"Scene Monitor '{dev.name}': Starting {bounce_delay}s bounce delay (no scenes active)")
                                    elif time.time() >= bounce_timer:
                                        # Bounce delay expired, turn ON
                                        self.log_debug(f"Scene Monitor '{dev.name}': Turning ON (no scenes active)")
                                        dev.updateStateOnServer("onOffState", True)
                                        
                                        # Execute ON action group
                                        on_action_group = dev.pluginProps.get('onActionGroup')
                                        if on_action_group and on_action_group != "none":
                                            self._execute_action_group(on_action_group)
                                        
                                        # Clear bounce timer
                                        if dev.id in self.scene_monitor_bounce_timers:
                                            del self.scene_monitor_bounce_timers[dev.id]
                                    # else: still in bounce delay, do nothing
                                else:
                                    # Want to turn OFF (scene is now active)
                                    # No delay needed for turning OFF (immediate response)
                                    self.log_debug(f"Scene Monitor '{dev.name}': Turning OFF (scene active)")
                                    dev.updateStateOnServer("onOffState", False)
                                    
                                    # Execute OFF action group
                                    off_action_group = dev.pluginProps.get('offActionGroup')
                                    if off_action_group and off_action_group != "none":
                                        self._execute_action_group(off_action_group)
                                    
                                    # Clear any pending bounce timer
                                    if dev.id in self.scene_monitor_bounce_timers:
                                        del self.scene_monitor_bounce_timers[dev.id]
                            else:
                                # State matches desired state
                                # Clear any pending bounce timer
                                if dev.id in self.scene_monitor_bounce_timers:
                                    del self.scene_monitor_bounce_timers[dev.id]
                        
                        except Exception as e:
                            self.logger.error(f"Error monitoring Scene Monitor {dev.name}: {e}")
                
                self.sleep(1)
        except self.StopThread:
            pass
    
    ########################################
    def startup(self):
        self.logger.info("Plugin started")
        self.logger.info(f"Logging level: {LOG_LEVEL_NAMES.get(self.log_level, '?')}")
    
    def shutdown(self):
        self.logger.info("Plugin stopped")
        
    ########################################
    # deviceStartComm() is called on application launch for all of our plugin defined
    # devices, and it is called when a new device is created immediately after its
    # UI settings dialog has been validated. This is a good place to force any properties
    # we need the device to have, and to clean up old properties.
    def deviceStartComm(self, dev):
        # self.log_debug(f"deviceStartComm: {dev.name}")

        props = dev.pluginProps
        if dev.deviceTypeId == 'myColorType':
            # Set SupportsColor property so Indigo knows device accepts color actions and should use color UI.
            props["SupportsColor"] = True

            # Cleanup properties used by other device types. These can exist if user switches the device type.
            if "IsLockSubType" in props:
                del props["IsLockSubType"]

            dev.replacePluginPropsOnServer(props)
        elif dev.deviceTypeId == 'myLockType':
            # Set IsLockSubType property so Indigo knows device accepts lock actions and should use lock UI.
            props["IsLockSubType"] = True

            # Cleanup properties used by other device types. These can exist if user switches the device type.
            if "SupportsColor" in props:
                del props["SupportsColor"]

            dev.replacePluginPropsOnServer(props)

    ########################################
    def validateDeviceConfigUi(self, values_dict, type_id, dev_id):
        return (True, values_dict)

    ########################################
    # Relay / Dimmer Action callback
    ######################
    def actionControlDevice(self, action, dev):
        # Handle Relay2Dimmer devices
        if dev.deviceTypeId == "Relay2Dimmer":
            self.log_detailed(f"Handling Relay2Dimmer device control: {dev.name}")
            relay1_id = dev.pluginProps.get("relay1Device")
            relay2_id = dev.pluginProps.get("relay2Device")
            
            if not relay1_id or not relay2_id:
                self.logger.error(f"Relay2Dimmer '{dev.name}' is not fully configured")
                return
            
            target_level = None
            
            if action.deviceAction == indigo.kDeviceAction.TurnOn:
                target_level = 100
            elif action.deviceAction == indigo.kDeviceAction.TurnOff:
                target_level = 0
            elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
                target_level = action.actionValue
            elif action.deviceAction == indigo.kDeviceAction.Toggle:
                current = dev.brightness if hasattr(dev, 'brightness') else dev.states.get('brightnessLevel', 0)
                target_level = 0 if current > 0 else 100
            
            if target_level is not None:
                # Round to valid level
                relay1_on, relay2_on = self._level_to_relay_states(target_level)
                rounded_level = self._relay_states_to_level(relay1_on, relay2_on)
                
                self.log_info(f"Relay2Dimmer '{dev.name}': {target_level}% → {rounded_level}%")
                
                # Update device state immediately
                dev.updateStateOnServer("brightnessLevel", rounded_level)
                dev.updateStateOnServer("onOffState", rounded_level > 0)
                
                # Schedule relay changes with 1 second delay
                def apply_with_delay():
                    time.sleep(1)
                    self.log_detailed(f"Applying relay states: Relay1={'ON' if relay1_on else 'OFF'}, Relay2={'ON' if relay2_on else 'OFF'}")
                    self._apply_relay_states(relay1_id, relay2_id, relay1_on, relay2_on)
                
                thread = threading.Thread(target=apply_with_delay)
                thread.daemon = True
                thread.start()
            
            return
        
        # Handle MultiDeviceVirtualDimmer devices
        if dev.deviceTypeId == "MultiDeviceVirtualDimmer":
            current_brightness = dev.brightness if hasattr(dev, 'brightness') else dev.states.get('brightnessLevel', 0)
            self.log_detailed(f"[MVDD-ACTION] MultiDeviceVirtualDimmer '{dev.name}' action received: {action.deviceAction}, current brightness: {current_brightness}%")
            
            target_brightness = None
            
            if action.deviceAction == indigo.kDeviceAction.TurnOn:
                target_brightness = 100
                self.log_detailed(f"[MVDD-ACTION] TurnOn command → target: {target_brightness}%")
            elif action.deviceAction == indigo.kDeviceAction.TurnOff:
                target_brightness = 0
                self.log_detailed(f"[MVDD-ACTION] TurnOff command → target: {target_brightness}%")
            elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
                target_brightness = action.actionValue
                self.log_detailed(f"[MVDD-ACTION] SetBrightness command → target: {target_brightness}%")
            elif action.deviceAction == indigo.kDeviceAction.Toggle:
                current = dev.brightness if hasattr(dev, 'brightness') else dev.states.get('brightnessLevel', 0)
                target_brightness = 0 if current > 0 else 100
                self.log_detailed(f"[MVDD-ACTION] Toggle command (current: {current}%) → target: {target_brightness}%")
            elif action.deviceAction == indigo.kDeviceAction.BrightenBy:
                current = dev.brightness if hasattr(dev, 'brightness') else dev.states.get('brightnessLevel', 0)
                target_brightness = min(100, current + action.actionValue)
                self.log_detailed(f"[MVDD-ACTION] BrightenBy {action.actionValue}% (current: {current}%) → target: {target_brightness}%")
            elif action.deviceAction == indigo.kDeviceAction.DimBy:
                current = dev.brightness if hasattr(dev, 'brightness') else dev.states.get('brightnessLevel', 0)
                target_brightness = max(0, current - action.actionValue)
                self.log_detailed(f"[MVDD-ACTION] DimBy {action.actionValue}% (current: {current}%) → target: {target_brightness}%")
            
            if target_brightness is not None:
                # Set cooldown timer to prevent feedback loops (5 seconds)
                cooldown_time = time.time() + 5
                self.multi_device_cooldown[dev.id] = cooldown_time
                self.log_detailed(f"[MVDD-ACTION] Cooldown set until {time.strftime('%H:%M:%S', time.localtime(cooldown_time))}")
                
                # Update virtual dimmer state
                dev.updateStateOnServer("brightnessLevel", target_brightness)
                dev.updateStateOnServer("onOffState", target_brightness > 0)
                
                # Store for monitoring
                self.multi_device_last_brightness[dev.id] = target_brightness
                self.log_detailed(f"[MVDD-ACTION] Stored last brightness: {target_brightness}%")
                
                self.log_detailed(f"[MVDD-ACTION] Applying target brightness {target_brightness}% to controlled devices...")
                
                # Apply to all controlled devices
                self._apply_multi_device_brightness(dev, target_brightness)
                
                self.log_detailed(f"[MVDD-ACTION] Action complete for '{dev.name}'")
            
            return
        
        # Handle Relay2Fan as a custom device
        if dev.deviceTypeId == "Relay2Fan":
            relay1_id = dev.pluginProps.get("relay1Device")
            relay2_id = dev.pluginProps.get("relay2Device")
            
            if not relay1_id or not relay2_id:
                self.logger.error(f"Relay2Fan '{dev.name}' is not fully configured")
                return
            
            target_speed_index = None
            
            if action.deviceAction == indigo.kDeviceAction.TurnOn:
                target_speed_index = 3  # High
            elif action.deviceAction == indigo.kDeviceAction.TurnOff:
                target_speed_index = 0  # Off
            elif action.deviceAction == indigo.kDeviceAction.Toggle:
                current = dev.states.get('speedIndex', 0)
                target_speed_index = 0 if current > 0 else 3
            
            if target_speed_index is not None:
                level = target_speed_index * 33 if target_speed_index > 0 else 0
                
                speed_names = ['off', 'low', 'medium', 'high']
                self.log_detailed(f"Relay2Fan '{dev.name}': setting to {speed_names[target_speed_index]}")
                
                dev.updateStateOnServer("speedIndex", target_speed_index)
                dev.updateStateOnServer("speedLevel", level)
                dev.updateStateOnServer("onOffState", target_speed_index > 0)
                
                relay1_on, relay2_on = self._level_to_relay_states(level)
                
                def apply_with_delay():
                    time.sleep(1)
                    self._apply_relay_states(relay1_id, relay2_id, relay1_on, relay2_on)
                
                thread = threading.Thread(target=apply_with_delay)
                thread.daemon = True
                thread.start()
            
            return
        
        # Handle Scene Monitor devices
        if dev.deviceTypeId == "SceneMonitor":
            ###### TURN ON ######
            if action.deviceAction == indigo.kDeviceAction.TurnOn:
                self.log_detailed(f"Manually turning ON Scene Monitor \"{dev.name}\"")
                dev.updateStateOnServer("onOffState", True)
                
                # Execute the ON action group if configured
                on_action_group = dev.pluginProps.get('onActionGroup')
                if on_action_group and on_action_group != "none":
                    self._execute_action_group(on_action_group)
            
            ###### TURN OFF ######
            elif action.deviceAction == indigo.kDeviceAction.TurnOff:
                self.log_detailed(f"Manually turning OFF Scene Monitor \"{dev.name}\"")
                dev.updateStateOnServer("onOffState", False)
                
                # Execute the OFF action group if configured
                off_action_group = dev.pluginProps.get('offActionGroup')
                if off_action_group and off_action_group != "none":
                    self._execute_action_group(off_action_group)
            
            ###### TOGGLE ######
            elif action.deviceAction == indigo.kDeviceAction.Toggle:
                if dev.onState:
                    self.actionControlDevice(indigo.kDeviceAction.TurnOff, dev)
                else:
                    self.actionControlDevice(indigo.kDeviceAction.TurnOn, dev)
            
            return
        
        # Handle Sunrise/Sunset Simulator devices
        if dev.deviceTypeId == "SunriseSimulator":
            ###### TURN ON ######
            if action.deviceAction == indigo.kDeviceAction.TurnOn:
                current_status = dev.states.get('status', 'Idle')
                
                if current_status == "Paused":
                    # Resume from pause
                    self.log_detailed(f"Resuming Sunrise/Sunset Simulator \"{dev.name}\"")
                    with self.sunrise_lock:
                        pause_event = self.sunrise_pause_events.get(dev.id)
                        if pause_event:
                            pause_event.clear()
                    dev.updateStateOnServer("status", "Running")
                
                elif current_status in ["Idle", "Complete"]:
                    # Start new simulation
                    self.log_detailed(f"Starting Sunrise/Sunset Simulator \"{dev.name}\"")
                    
                    # Stop any existing thread
                    with self.sunrise_lock:
                        if dev.id in self.sunrise_threads:
                            self.log_detailed(f"Stopping existing thread for \"{dev.name}\"")
                            self.sunrise_stop_events[dev.id].set()
                            time.sleep(0.5)
                    
                    # Create new stop and pause events
                    with self.sunrise_lock:
                        self.sunrise_stop_events[dev.id] = threading.Event()
                        self.sunrise_pause_events[dev.id] = threading.Event()
                    
                    # Update device state
                    dev.updateStateOnServer("onOffState", True)
                    dev.updateStateOnServer("status", "Running")
                    dev.updateStateOnServer("duration", int(dev.pluginProps.get("duration", "300")))
                    dev.updateStateOnServer("percentageComplete", 0)
                    
                    # Start thread
                    thread = threading.Thread(target=self._sunrise_thread, args=(dev.id,))
                    thread.daemon = True
                    thread.start()
                    
                    with self.sunrise_lock:
                        self.sunrise_threads[dev.id] = thread
                
                else:
                    # Already running, do nothing
                    self.log_info(f"Sunrise/Sunset Simulator \"{dev.name}\" is already running, ignoring ON command")
            
            ###### TURN OFF ######
            elif action.deviceAction == indigo.kDeviceAction.TurnOff:
                self.log_detailed(f"Stopping Sunrise/Sunset Simulator \"{dev.name}\"")
                
                # Signal stop to thread
                with self.sunrise_lock:
                    if dev.id in self.sunrise_stop_events:
                        self.sunrise_stop_events[dev.id].set()
                
                # Execute stop action group
                stop_action_group = dev.pluginProps.get('stopActionGroup')
                if stop_action_group and stop_action_group != "none":
                    self._execute_action_group(stop_action_group)
                
                # Update device state
                dev.updateStateOnServer("onOffState", False)
                dev.updateStateOnServer("status", "Idle")
                dev.updateStateOnServer("durationRemaining", 0)
                dev.updateStateOnServer("percentageComplete", 0)
                dev.updateStateOnServer("displayString", "Idle")
            
            ###### TOGGLE ######
            elif action.deviceAction == indigo.kDeviceAction.Toggle:
                if dev.onState:
                    indigo.device.turnOff(dev.id)
                else:
                    indigo.device.turnOn(dev.id)
            
            return
        
        # Handle Scene Devices
        if dev.deviceTypeId == "SceneDevice":
            ###### TURN ON ######
            if action.deviceAction == indigo.kDeviceAction.TurnOn:
                self.log_detailed(f"Activating scene \"{dev.name}\"")
                
                # Check if we should capture previous state
                restore_previous = dev.pluginProps.get('restorePreviousState', False)
                if restore_previous:
                    # Capture current state before applying scene
                    previous_states = self._capture_current_state(dev)
                    if previous_states:
                        # Store previous state in plugin props
                        props = dev.pluginProps
                        props['previousStates'] = previous_states
                        dev.replacePluginPropsOnServer(props)
                        self.log_debug(f"Scene '{dev.name}': Captured previous state")
                
                # Apply the scene state to all devices
                self._apply_scene_state(dev)
                
                # Execute the ON action group if configured
                on_action_group = dev.pluginProps.get('onActionGroup')
                if on_action_group and on_action_group != "none":
                    self._execute_action_group(on_action_group)
                
                # Update scene device state
                dev.updateStateOnServer("onOffState", True)
                
                # Cancel any pending OFF timer
                with self.scene_lock:
                    if dev.id in self.scene_off_timers:
                        del self.scene_off_timers[dev.id]
                    
                    # Set activation timer - don't check scene state for 15 seconds
                    self.scene_activation_timers[dev.id] = time.time() + 15
            
            ###### TURN OFF ######
            elif action.deviceAction == indigo.kDeviceAction.TurnOff:
                self.log_detailed(f"Deactivating scene \"{dev.name}\"")
                
                # Check if we should restore previous state
                restore_previous = dev.pluginProps.get('restorePreviousState', False)
                if restore_previous:
                    previous_states_str = dev.pluginProps.get('previousStates', '')
                    if previous_states_str:
                        try:
                            previous_states = json.loads(previous_states_str)
                            self.log_detailed(f"Scene '{dev.name}': Restoring previous state")
                            
                            # Restore each device/variable to previous state
                            for item_key, previous_state in previous_states.items():
                                if item_key.startswith('device_'):
                                    dev_id_str = item_key.replace('device_', '')
                                    try:
                                        target_dev_id = int(dev_id_str)
                                        target_dev = indigo.devices[target_dev_id]
                                        
                                        if previous_state['type'] == 'dimmer':
                                            indigo.dimmer.setBrightness(target_dev_id, value=previous_state['brightness'])
                                            
                                        elif previous_state['type'] == 'relay':
                                            if previous_state['onState']:
                                                indigo.device.turnOn(target_dev_id)
                                            else:
                                                indigo.device.turnOff(target_dev_id)
                                                
                                        elif previous_state['type'] == 'thermostat':
                                            hvac_mode = indigo.kHvacMode(previous_state['hvacMode'])
                                            fan_mode = indigo.kFanMode(previous_state['fanMode'])
                                            
                                            indigo.thermostat.setHvacMode(target_dev_id, value=hvac_mode)
                                            indigo.thermostat.setFanMode(target_dev_id, value=fan_mode)
                                            indigo.thermostat.setCoolSetpoint(target_dev_id, value=previous_state['coolSetpoint'])
                                            indigo.thermostat.setHeatSetpoint(target_dev_id, value=previous_state['heatSetpoint'])
                                            
                                        elif previous_state['type'] == 'fan':
                                            indigo.speedcontrol.setSpeedLevel(target_dev_id, value=previous_state['speedLevel'])
                                            
                                        elif previous_state['type'] == 'blind':
                                            indigo.dimmer.setBrightness(target_dev_id, value=previous_state['position'])
                                    
                                    except Exception as e:
                                        self.logger.error(f"Error restoring state to device {dev_id_str}: {e}")
                                
                                elif item_key.startswith('variable_'):
                                    var_id_str = item_key.replace('variable_', '')
                                    try:
                                        var_id = int(var_id_str)
                                        var = indigo.variables[var_id]
                                        self.log_detailed(f"Restoring variable '{var.name}' to '{previous_state['value']}'")
                                        indigo.variable.updateValue(var_id, previous_state['value'])
                                    except Exception as e:
                                        self.logger.error(f"Error restoring state to variable {var_id_str}: {e}")
                        
                        except Exception as e:
                            self.logger.error(f"Error restoring previous state for scene '{dev.name}': {e}")
                            import traceback
                            self.logger.error(traceback.format_exc())
                
                # Execute the OFF action group if configured (only if not restoring previous state)
                if not restore_previous:
                    off_action_group = dev.pluginProps.get('offActionGroup')
                    if off_action_group and off_action_group != "none":
                        self._execute_action_group(off_action_group)
                
                # Update scene device state
                dev.updateStateOnServer("onOffState", False)
                
                # Set a timer to check in 10 seconds if devices match the scene
                with self.scene_lock:
                    self.scene_off_timers[dev.id] = {
                        'check_time': time.time() + 10
                    }
            
            ###### TOGGLE ######
            elif action.deviceAction == indigo.kDeviceAction.Toggle:
                if dev.onState:
                    self.actionControlDevice(indigo.kDeviceAction.TurnOff, dev)
                else:
                    self.actionControlDevice(indigo.kDeviceAction.TurnOn, dev)
            
            return
        
        # Original device control code for other device types
        ###### TURN ON ######
        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            # Command hardware module (dev) to turn ON here:
            # ** IMPLEMENT ME **
            send_success = True        # Set to False if it failed.

            if send_success:
                # If success then log that the command was successfully sent.
                self.log_detailed(f"sent \"{dev.name}\" on")

                # And then tell the Indigo Server to update the state.
                dev.updateStateOnServer("onOffState", True)
            else:
                # Else log failure but do NOT update state on Indigo Server.
                self.logger.error(f"send \"{dev.name}\" on failed")

        ###### TURN OFF ######
        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            # Command hardware module (dev) to turn OFF here:
            # ** IMPLEMENT ME **
            send_success = True        # Set to False if it failed.

            if send_success:
                # If success then log that the command was successfully sent.
                self.log_detailed(f"sent \"{dev.name}\" off")

                # And then tell the Indigo Server to update the state:
                dev.updateStateOnServer("onOffState", False)
            else:
                # Else log failure but do NOT update state on Indigo Server.
                self.logger.error(f"send \"{dev.name}\" off failed")




        ###### TOGGLE ######
        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            # Command hardware module (dev) to toggle here:
            # ** IMPLEMENT ME **
            new_on_state = not dev.onState
            send_success = True        # Set to False if it failed.

            if send_success:
                # If success then log that the command was successfully sent.
                self.log_detailed(f"sent \"{dev.name}\" toggle")

                # And then tell the Indigo Server to update the state:
                dev.updateStateOnServer("onOffState", new_on_state)
            else:
                # Else log failure but do NOT update state on Indigo Server.
                self.logger.error(f"send \"{dev.name}\" toggle failed")

        ###### SET BRIGHTNESS ######
        elif action.deviceAction == indigo.kDeviceAction.SetBrightness:
            # Command hardware module (dev) to set brightness here:
            # ** IMPLEMENT ME **
            new_brightness = action.actionValue
            send_success = True        # Set to False if it failed.

            if send_success:
                # If success then log that the command was successfully sent.
                self.log_detailed(f"sent \"{dev.name}\" set brightness to {new_brightness}")

                # And then tell the Indigo Server to update the state:
                dev.updateStateOnServer("brightnessLevel", new_brightness)
            else:
                # Else log failure but do NOT update state on Indigo Server.
                self.logger.error(f"send \"{dev.name}\" set brightness to {new_brightness} failed")

        ###### BRIGHTEN BY ######
        elif action.deviceAction == indigo.kDeviceAction.BrightenBy:
            # Command hardware module (dev) to do a relative brighten here:
            # ** IMPLEMENT ME **
            new_brightness = min(dev.brightness + action.actionValue, 100)
            send_success = True        # Set to False if it failed.

            if send_success:
                # If success then log that the command was successfully sent.
                self.log_detailed(f"sent \"{dev.name}\" brighten to {new_brightness}")

                # And then tell the Indigo Server to update the state:
                dev.updateStateOnServer("brightnessLevel", new_brightness)
            else:
                # Else log failure but do NOT update state on Indigo Server.
                self.logger.error(f"send \"{dev.name}\" brighten to {new_brightness} failed")

        ###### DIM BY ######
        elif action.deviceAction == indigo.kDeviceAction.DimBy:
            # Command hardware module (dev) to do a relative dim here:
            # ** IMPLEMENT ME **
            new_brightness = max(dev.brightness - action.actionValue, 0)
            send_success = True        # Set to False if it failed.

            if send_success:
                # If success then log that the command was successfully sent.
                self.log_detailed(f"sent \"{dev.name}\" dim to {new_brightness}")

                # And then tell the Indigo Server to update the state:
                dev.updateStateOnServer("brightnessLevel", new_brightness)
            else:
                # Else log failure but do NOT update state on Indigo Server.
                self.logger.error(f"send \"{dev.name}\" dim to {new_brightness} failed")