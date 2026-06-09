# cadence

A local "sit/stand captain" for the **Deskhaus Apex Pro** standing desk. It
controls the desk over Bluetooth LE (Jiecang/Uplift-style controller) and
alternates between your saved sitting and standing heights on a configurable
schedule — giving a subtle warning before every move.

> ⚠️ **This moves physical furniture.** Start in the safe default mode
> (desktop-notification warnings, no auto-movement until you arm it) and prove
> manual control works before letting the daemon move anything.

## Status

Working and in daily use against a Deskhaus Apex Pro (Jiecang `L-BTMEB95`
BLE module). The full protocol — height polling and decoding, continuous
up/down, absolute moves, warning taps, collision behavior — has been verified
live on that hardware. Details and quirks are documented at the top of
[`src/cadence/protocol.py`](src/cadence/protocol.py).

## Will it work with my desk?

Maybe! Any desk using a Jiecang BLE controller/dongle (Uplift's BLE adapter,
AiDesk-compatible desks, many white-label brands) likely speaks the same frame
protocol. `cadence scan` looks for the known service UUIDs (`fe60`, `ff00`,
`ff12`) and picks the command/notify characteristics automatically.

**But verify before you trust it.** Firmware variants differ in ways that
matter — this very desk reports heights in 0.1-inch units while accepting
move targets in millimeters, despite community docs claiming 0.1mm for both.
The safe bring-up order:

1. `cadence scan` — find the desk, save its identity to config.
2. `cadence status` — confirm the reported height matches the desk display.
3. `cadence up` / `cadence stop` — one supervised nudge; confirm direction.
4. `cadence goto <near current height>` — small move; confirm it lands right.
5. Only then enable the daemon — and keep `require_manual_enable_on_start`.

If your desk behaves differently, please open an issue with your `cadence
scan` output and a few captured frames — the protocol layer is built to
absorb variants.

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

- First-run verification wizard (`cadence setup`) so unfamiliar desks are
  bring-up-checked before absolute moves are enabled.
- `cadence report` — paste-ready GATT + frame dump for compatibility issues.
- Cross-platform notifications (currently macOS `osascript`/`afplay`) and a
  systemd unit example alongside launchd.
- Driver abstraction for non-Jiecang controllers (Linak-based Jarvis/Fully).
- Optional Rust reimplementation (`btleplug`).

## License

MIT — see [LICENSE](LICENSE).
