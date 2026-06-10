"""First-run verification wizard.

Walks an unfamiliar desk through the same supervised bring-up that proved the
protocol on the original Apex Pro: confirm the height decoding against the
desk's own display, one supervised nudge to confirm direction, then a small
absolute move verified against the height stream. Only after all three pass
is `device.verified` set, which is what allows absolute moves and the daemon.

Each step opens its own short BLE connection so interactive prompts never
block a live event loop.
"""

from __future__ import annotations

import asyncio

import typer

from . import config as cfgmod
from . import protocol
from .ble import DeskClient
from .config import Config

NUDGE_SECONDS = 0.8
GOTO_TEST_DELTA_INCHES = 1.0
GOTO_TOLERANCE_INCHES = 0.3


def derive_raw_units_per_inch(raw: int, display_value: float, unit: str) -> float:
    """Compute the raw->inches scale from what the desk display shows.

    unit is "in" or "cm". Raises ValueError on a non-positive reading.
    """
    inches = display_value if unit == "in" else display_value / 2.54
    if inches <= 0:
        raise ValueError("display height must be positive")
    return raw / inches


def _inches(cfg: Config, raw: int) -> float:
    cal = cfg.calibration
    return protocol.raw_to_inches(raw, cal.raw_units_per_inch, cal.offset_inches)


# --- BLE steps (each opens its own connection) --------------------------------

async def _read_raw(cfg: Config) -> int | None:
    async with DeskClient(cfg.device) as client:
        reading = await client.read_height(wait=4.0)
        return reading.raw if reading else None


async def _nudge(cfg: Config) -> tuple[int | None, int | None]:
    """Button-style up for a moment, then stop. Returns (before, after) raw."""
    async with DeskClient(cfg.device) as client:
        before = await client.read_height(wait=4.0)
        await client.move_up()
        await asyncio.sleep(NUDGE_SECONDS)
        await client.stop()
        await asyncio.sleep(2.0)
        after = client.latest_height
        return (before.raw if before else None, after.raw if after else None)


async def _goto_and_watch(cfg: Config, target_inches: float) -> int | None:
    """Issue a goto and watch the stream with a wrong-direction guard.

    Returns the settled raw height, or None if the desk never moved.
    """
    cal = cfg.calibration
    async with DeskClient(cfg.device) as client:
        before = await client.read_height(wait=4.0)
        if before is None:
            return None
        start_raw = before.raw
        going_up = target_inches > _inches(cfg, start_raw)
        await client.stop()
        await client.goto_mm(protocol.inches_to_goto_mm(target_inches, cal.offset_inches))

        moved = False
        last = start_raw
        stable = 0.0
        for _ in range(60):  # up to 30s
            await asyncio.sleep(0.5)
            cur = client.latest_height.raw if client.latest_height else start_raw
            wrong_way = (cur < start_raw - 5) if going_up else (cur > start_raw + 5)
            if wrong_way:
                await client.stop()
                typer.secho("Desk moved the WRONG direction; stopped.", fg=typer.colors.RED)
                return cur
            if cur != last:
                moved = True
                stable = 0.0
                last = cur
            else:
                stable += 0.5
                if moved and stable >= 2.0:
                    break
        return last if moved else None


# --- The wizard ----------------------------------------------------------------

def run_wizard() -> None:
    cfg = cfgmod.load()

    typer.secho("cadence setup — supervised desk verification", bold=True)
    typer.echo(
        "This walks through three checks before automatic movement is enabled:\n"
        "  1. height decoding vs your desk's display\n"
        "  2. a brief supervised nudge (direction + stop)\n"
        "  3. a small absolute move, verified against the height stream\n"
    )

    if not cfg.device.address:
        typer.secho("No desk configured. Run `cadence scan` first.", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(f"Desk: {cfg.device.name or '(unknown)'} [{cfg.device.address}]\n")

    # Step 1: height decoding -------------------------------------------------
    typer.secho("Step 1/3: height decoding", bold=True)
    raw = asyncio.run(_read_raw(cfg))
    if raw is None:
        typer.secho(
            "Could not read a height from the desk. Wiggle it an inch with the "
            "handset and re-run setup; if it still fails, open an issue with "
            "your `cadence scan` output.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    decoded = _inches(cfg, raw)
    if not typer.confirm(f"Desk reports raw={raw}, decoded {decoded:.1f} inches. "
                         f"Does the display show about that?"):
        display_value = typer.prompt("What does the display show (number only)", type=float)
        unit = typer.prompt("Is that inches or centimeters? [in/cm]", default="in")
        try:
            scale = derive_raw_units_per_inch(raw, display_value, unit.strip().lower())
        except ValueError:
            typer.secho("That display value doesn't make sense; aborting.", fg=typer.colors.RED)
            raise typer.Exit(1)
        cfg.calibration.raw_units_per_inch = scale
        cfg.calibration.offset_inches = 0.0
        decoded = _inches(cfg, raw)
        if not typer.confirm(
            f"Recomputed: raw {raw} = {decoded:.1f} inches "
            f"(scale {scale:.1f} raw units/inch). Look right?"
        ):
            typer.secho("Decoding unresolved; aborting. Open an issue with your "
                        "scan output and a few frames.", fg=typer.colors.RED)
            raise typer.Exit(1)
        cfgmod.save(cfg)
        typer.secho("Calibration saved.", fg=typer.colors.GREEN)
    typer.secho("Height decoding confirmed.\n", fg=typer.colors.GREEN)

    # Step 2: supervised nudge -------------------------------------------------
    typer.secho("Step 2/3: supervised nudge", bold=True)
    typer.echo("The desk will move UP briefly, then stop. Clear the area above it.")
    typer.confirm("Ready?", abort=True)
    before, after = asyncio.run(_nudge(cfg))
    if before is not None and after is not None and after > before:
        typer.echo(f"Height stream agrees: {_inches(cfg, before):.1f} -> {_inches(cfg, after):.1f}")
    if not typer.confirm("Did the desk move UP and then stop?"):
        typer.secho(
            "Up/stop not confirmed; aborting before any absolute moves. "
            "Open an issue with your scan output.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    typer.secho("Movement and stop confirmed.\n", fg=typer.colors.GREEN)

    # Step 3: small verified goto ----------------------------------------------
    typer.secho("Step 3/3: small absolute move", bold=True)
    raw_now = asyncio.run(_read_raw(cfg))
    cur = _inches(cfg, raw_now) if raw_now is not None else None
    if cur is None:
        typer.secho("Lost the height reading; aborting.", fg=typer.colors.RED)
        raise typer.Exit(1)
    delta = GOTO_TEST_DELTA_INCHES
    if cur + delta + 0.5 > cfg.safety.max_height_inches:
        delta = -delta
    target = cur + delta
    typer.echo(f"The desk will move {abs(delta):.0f} inch {'up' if delta > 0 else 'down'} "
               f"to {target:.1f}in, then return.")
    typer.confirm("Ready?", abort=True)

    settled_raw = asyncio.run(_goto_and_watch(cfg, target))
    if settled_raw is None:
        typer.secho(
            "The desk ignored the absolute move. Your firmware may use different "
            "goto units. Open an issue with your scan output; up/down/stop still "
            "work meanwhile.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    settled = _inches(cfg, settled_raw)
    if abs(settled - target) > GOTO_TOLERANCE_INCHES:
        typer.secho(
            f"Desk settled at {settled:.1f}in, expected {target:.1f}in. Not "
            "verifying absolute moves; open an issue with these numbers.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    typer.echo(f"Landed at {settled:.1f}in. Returning to {cur:.1f}in...")
    asyncio.run(_goto_and_watch(cfg, cur))

    # Done -----------------------------------------------------------------------
    cfg.device.verified = True
    path = cfgmod.save(cfg)
    typer.secho(f"\nAll checks passed. Desk verified; saved to {path}.", fg=typer.colors.GREEN)
    typer.echo("Next: `cadence goto <height>`, or `cadence daemon` for the captain.")
