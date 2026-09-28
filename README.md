# LightTools

An Indigo Domotics plugin that bundles a handful of small, independent lighting utilities: linking a dimmer to a variable (handy for HomeKit-published brightness controls), technology-agnostic scenes with state tracking, a relay-to-dimmer/fan converter, a virtual dimmer that mirrors a group of real devices, a sunrise/sunset ramp simulator, and a flash/strobe action for alerts.

None of these need each other — pick and use whichever pieces are useful to you.

## Requirements

- Indigo Domotics (Server API 3.6 or later)
- No third-party Python dependencies

## Installation

1. Download the latest `LightTools_vX_Y_Z_indigoplugin.zip` from the [Releases](../../releases) page.
2. Unzip it and double-click the `.indigoplugin` bundle — Indigo will offer to install and enable it.
3. Optionally, open **Plugins → LightTools → Configure...** and set a logging level (see [Logging](#logging) below).

## Features

- [Variable Linked Dimmer](#variable-linked-dimmer) — publish a dimmer whose brightness drives (and is driven by) an Indigo variable
- [Scene Controller](#scene-controller) — save a snapshot of several devices' states and detect when reality matches (or drifts from) it
- [Scene Monitor](#scene-monitor) — a device that's ON only when none of your scenes are currently active
- [Relay to Dimmer Converter](#relay-to-dimmer--fan-converter) — two relays presented as a 4-level (0/33/66/100%) dimmer
- [Relay to Fan Converter](#relay-to-dimmer--fan-converter) — two relays presented as a 4-speed (Off/Low/Medium/High) fan
- [Multi-Device Virtual Dimmer](#multi-device-virtual-dimmer) — one dimmer that mirrors brightness (or on/off, via threshold) across a group of dimmers and relays
- [Sunrise/Sunset Simulator](#sunrisesunset-simulator) — gradually ramps a group of dimmers between two brightness levels over a chosen duration and curve
- [Flash Lamps](#flash-lamps--cancel-all-flashes) action — flash one or more devices a set number of times, then restore them
- [Cancel All Flashes](#flash-lamps--cancel-all-flashes) action — immediately stop every running flash sequence

---

## Device Types

### Variable Linked Dimmer

A simple dimmer device whose brightness is tied to an Indigo variable. Originally built to publish a HomeKit-controllable brightness value for wall-mounted iPad kiosks (e.g. GlennNZ's Kiosk Home app), but works for any case where you want a variable to move in lockstep with a dimmer.

- **Linked Variable** — the Indigo variable to read from and write to
- **Variable Scale Minimum / Maximum** — maps the dimmer's 0–100% range onto whatever range your variable expects (e.g. `0` to `1` for a fractional brightness value: setting the dimmer to 70% writes `0.7` to the variable)

The plugin polls the linked variable and keeps the dimmer and variable in sync in both directions. Out-of-range or invalid variable values are detected and corrected automatically (with a log warning).

### Scene Controller

A relay-type device representing a "scene" — a saved snapshot of the state of a group of other devices (dimmers, relays, thermostats). The scene shows as ON when every device currently matches the saved snapshot, and OFF as soon as any of them drifts.

- **Scene Devices / Scene Variables** — what to include in the snapshot
- **Save Current State of Selected Devices** — button that captures the present state of everything selected as the target snapshot
- **Compare Current State of Selected Devices** — button that checks current state against the saved snapshot without changing anything
- **Ignore HVAC Mode when comparing thermostats** — thermostats often report mode changes that shouldn't break scene matching; tick this to ignore mode when comparing
- **Restore previous state when manually turned OFF** — if enabled, turning the scene off manually restores whatever the devices were doing *before* the scene was activated, rather than just leaving them as-is
- **ON/OFF Action Group** — optional action groups to run on activation/deactivation

Turning the scene ON applies the saved snapshot to every device in it. A short grace period after activation (and a brief settle window after a triggering change) avoids false "scene broke" flickers while devices are still catching up.

### Scene Monitor

A relay device that inverts scene activity: it's **ON when none of your monitored scenes are active**, and **OFF as soon as any one of them is**. Useful for driving "no mood lighting is currently set" indicators or automations that should only run in the absence of any scene.

- **Monitored Scenes** — which Scene Controller devices to watch
- **Bounce Delay** — seconds to wait after all scenes go inactive before flipping ON, to avoid flapping during scene-to-scene transitions (default and recommended: 5s)
- **ON/OFF Action Group** — optional action groups on each transition

### Relay to Dimmer / Fan Converter

Two small devices that turn a pair of ordinary relays into a multi-level control:

- **Relay to Dimmer Converter** — 4-level dimmer: both relays off = 0%, Relay 1 only = 33%, Relay 2 only = 66%, both on = 100%.
- **Relay to Fan Converter** — same idea, presented as a 4-speed fan (Off/Low/Medium/High), for controlling multi-speed fans wired to two relays.

Both watch the underlying relays continuously and update their own state (and vice versa) whenever either side changes, with a short debounce so rapid relay toggles don't fight the device the other way.

### Multi-Device Virtual Dimmer

A single virtual dimmer device that keeps a group of underlying dimmers and relays in sync with each other. Set the virtual dimmer's brightness and every controlled device follows: real dimmers mirror the brightness value exactly, and relays turn ON/OFF based on a configurable threshold.

- **Controlled Devices** — the dimmers and/or relays to keep in sync
- **Relay Threshold (%)** — relays in the group turn ON at or above this brightness, OFF below it

The plugin also watches the controlled devices for changes made *outside* the virtual dimmer (a wall switch, another automation, HomeKit, etc.) and syncs the rest of the group to match, with a short cooldown after each sync to avoid feedback loops between the devices it's actively controlling and the ones reporting state back.

> **v1.9.5:** fixed an issue where a device that was simply slow to physically settle after a command could be mistaken for a fresh manual change once the sync cooldown ended, causing the group to oscillate on and off. See the [v1.9.5 release notes](../../releases/tag/v1.9.5) for details.

### Sunrise/Sunset Simulator

Gradually ramps a group of dimmers from a starting brightness to a target brightness over a configurable duration, either linearly or with an ease-in/ease-out curve — useful for wake-up lighting or gentle evening wind-downs.

- **Starting / Target Brightness** — the simulator figures out sunrise vs. sunset automatically from which is higher
- **Duration** — how long the full ramp should take
- **Brightness Curve** — Linear or Ease-in/Ease-out
- **Reset devices to starting brightness** — if checked, every controlled device snaps to the starting brightness immediately when the simulation begins; if unchecked, each device starts wherever it currently is and joins the ramp once the simulated brightness catches up to it (handy for lights that are already partway on)
- **Controlled Dimmer Devices** — the devices to ramp
- **Start / Complete / Stop Action Group** — optional action groups to fire at each stage

Can be started, paused/resumed, and stopped via device actions (`sunriseStart`, `sunrisePause`, `sunriseStop`) as well as by turning the device on/off directly.

> **v1.9.5:** manually adjusting a light mid-ramp now cancels the simulation (running the Stop Action Group, if set) instead of being silently overridden on the next update. See the [v1.9.5 release notes](../../releases/tag/v1.9.5) for details.

### Flash Lamps / Cancel All Flashes

Two plugin actions (not devices) for alerting via lights:

- **Flash Lamps** — flashes a chosen set of dimmers/relays a set number of times, with configurable flash duration and gap between flashes, then restores each device to its original state. Optional **Flash To Brightness** / **Flash To Minimum** let you control the on/off brightness levels used for dimmers during the flash (defaults: 100% / 0%).
- **Cancel All Flashes** — immediately stops every currently-running flash sequence and restores the affected devices, in case you need to abort one early.

---

## Logging

Configurable in **Plugins → LightTools → Configure...**:

| Level | What it logs |
|---|---|
| None | Errors only |
| Informational | High-level events — plugin start, scene saves, flash starts |
| **Detailed** *(default)* | Adds per-device action results, e.g. "Turning OFF relay X" |
| Debug | Adds monitor decision reasoning — per-tick change detection |
| Extended Debug | Full state dumps every monitor cycle (very verbose — use only while actively troubleshooting) |

## License

Released under the MIT License — see [LICENSE](LICENSE) for details.

## Credits

Mike — [@durosity](https://github.com/durosity). Thanks to everyone in the [Indigo Domotics forums](https://forums.indigodomo.com/) who offered help and testing, particularly DaveL17.
