# cadence

A local "sit/stand captain" for the **Deskhaus Apex Pro** standing desk. It
controls the desk over Bluetooth LE (Jiecang/Uplift-style controller) and
alternates between your saved sitting and standing heights on a configurable
schedule — giving a subtle warning before every move.

> ⚠️ **This moves physical furniture.** Start in the safe default mode
> (desktop-notification warnings, no auto-movement until you arm it) and prove
> manual control works before letting the daemon move anything.

## Status

This is a working Python prototype. The BLE wire protocol is reverse-engineered
from the Uplift/AiDesk app and community projects and is **unconfirmed against
the Apex Pro** — values are validated by running `cadence scan` and watching a
real height notification before trusting absolute moves.

## Install

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/). On macOS, grant
your terminal Bluetooth permission (System Settings → Privacy & Security →
Bluetooth) the first time you scan.

```bash
uv sync                       # create the venv and install deps
uv run cadence --help         # run without installing globally
# or install the CLI on PATH:
uv tool install --editable .
```

## Quick start

```bash
cadence init-config           # write ~/.config/cadence/config.toml
cadence scan                  # find the desk, save its BLE address
cadence status                # connect + read current height
cadence goto 26.8             # move to sitting height (safety-checked)
cadence goto 44.9             # move to standing height
```

### Calibrate (recommended)

The Apex Pro display has been miscalibrated before, so cadence never trusts it.
Put the desk at a height you measure with a tape, then:

```bash
cadence calibrate 26.8        # maps the current raw reading to 26.8 inches
```

## The captain (scheduler)

```bash
cadence daemon                # run in foreground (logs to ~/.local/state/cadence/)
cadence resume                # arm automation (daemon starts DISARMED)
cadence pause                 # kill switch — stop automating, leave desk as-is
cadence next                  # force the next sit<->stand transition now
cadence snooze 15             # snooze automation 15 minutes
cadence status                # see posture, phase timer, manual-move state
```

Run it in the background via launchd:

```bash
./scripts/install-launchd.sh  # loads com.justfielding.cadence; then `cadence resume`
```

The default cycle: **sit 45m → warn → stand → stand 15m → warn → sit → repeat.**
Edit `~/.config/cadence/config.toml` to change timing, heights, warning mode,
safety bounds, and working hours. See [`examples/config.toml`](examples/config.toml).

## Safety model

- Never moves outside `[min_height_inches, max_height_inches]` (targets are clamped).
- Refuses absolute moves when the current height can't be read.
- Won't move while the desk is already moving.
- Detects manual moves (a height change it didn't command) and resets the timer,
  honoring a grace period before automating again.
- Starts **disarmed**; nothing moves until you run `cadence resume`.
- Warnings default to a desktop notification + sound. Physical "tap" nudges are
  opt-in (`warning.mode = "tap"` or `"both"`) and only run when height is known.
- Every automatic move is logged with timestamp, from/to height, and reason.

## Architecture

```
src/cadence/
  cli.py          typer CLI (all commands)
  config.py       TOML config (dataclasses, load/save)
  paths.py        XDG config/state/log locations
  protocol.py     Jiecang/Uplift frame encode/decode + calibration math
  ble.py          bleak transport: scan, inspect, DeskClient
  safety.py       movement guardrails (check_move)
  notify.py       macOS desktop notification + sound
  scheduler.py    pure `decide()` + Captain executor + daemon `run()`
  state.py        state.json blackboard (CLI <-> daemon)
tests/            protocol + scheduler/safety unit tests (no hardware)
scripts/          launchd installer
examples/         default config
```

The protocol details (UUIDs, opcodes, frame format) and the BLE references that
informed them are documented at the top of `src/cadence/protocol.py`.

## Development

```bash
uv run pytest                 # unit tests (pure logic, no desk required)
```

## Roadmap

- Confirm the Apex Pro protocol against real hardware (`tix` issue `cad-c0376e`).
- Optional Rust reimplementation (`btleplug`) once the protocol is proven.
- Adaptive schedules; "snooze until next meeting" integrations.
```
