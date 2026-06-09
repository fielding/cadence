"""cadence command-line interface.

Phases:
  scan                         discover the desk (Phase 1)
  status/up/down/stop/goto     manual control (Phase 2)
  save/calibrate               persist heights & calibration
  daemon/pause/resume/next/... the captain (Phase 4)
"""

from __future__ import annotations

import asyncio
import time

import typer

from . import ble, config as cfgmod, logging_setup, notify as notifymod, protocol, scheduler, state as statemod
from .config import Config

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local sit/stand desk captain for the Deskhaus Apex Pro.",
)


def _run(coro):
    return asyncio.run(coro)


def _load() -> Config:
    return cfgmod.load()


def _require_device(cfg: Config) -> None:
    if not cfg.device.address:
        typer.secho(
            "No desk configured. Run `cadence scan` first to discover it.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)


def _inches(cfg: Config, raw: int | None) -> float | None:
    if raw is None:
        return None
    cal = cfg.calibration
    return protocol.raw_to_inches(raw, cal.raw_units_per_inch, cal.offset_inches)


# --- Phase 1: discovery ------------------------------------------------------

@app.command()
def scan(
    timeout: float = typer.Option(8.0, help="Scan duration in seconds."),
    inspect: bool = typer.Option(
        True, help="Connect to the best desk candidate and dump services."
    ),
    save: bool = typer.Option(
        True, help="Save discovered device metadata to config."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Scan for BLE devices and identify the desk."""
    logging_setup.setup(verbose)
    devices = _run(ble.scan(timeout))
    if not devices:
        typer.secho("No BLE devices found.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)

    typer.echo("Found BLE devices:")
    candidates = []
    for d in devices:
        is_desk = ble.looks_like_desk(d)
        marker = "  <-- likely desk" if is_desk else ""
        typer.echo(f"- {d.name or '(unknown)'}  [{d.address}]  RSSI {d.rssi}{marker}")
        if d.service_uuids:
            for u in d.service_uuids:
                typer.echo(f"    service: {u}")
        if is_desk:
            candidates.append(d)

    if not candidates:
        typer.secho(
            "\nNo obvious desk found. Re-run with the desk awake, or inspect a "
            "device manually with `cadence inspect <address>`.",
            fg=typer.colors.YELLOW,
        )
        return

    best = candidates[0]
    typer.echo(f"\nBest candidate: {best.name or '(unknown)'} [{best.address}]")
    if not inspect:
        return

    typer.echo("Inspecting services/characteristics...")
    services = _run(ble.inspect(best.address))
    for svc, chars in services.items():
        typer.echo(f"  service {svc}")
        for ch in chars:
            props = ",".join(ch["properties"])
            typer.echo(f"    char {ch['uuid']}  ({props})")

    dev = ble.guess_characteristics(services)
    dev.address = best.address
    dev.name = best.name
    typer.echo(
        f"\nGuessed: service={dev.service_uuid} "
        f"cmd={dev.command_char_uuid} notify={dev.notify_char_uuid}"
    )

    if save:
        cfg = _load()
        cfg.device = dev
        path = cfgmod.save(cfg)
        typer.secho(f"Saved device to {path}", fg=typer.colors.GREEN)


@app.command()
def inspect(address: str, verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Connect to a specific address and dump its GATT services."""
    logging_setup.setup(verbose)
    services = _run(ble.inspect(address))
    for svc, chars in services.items():
        typer.echo(f"service {svc}")
        for ch in chars:
            typer.echo(f"  char {ch['uuid']}  ({','.join(ch['properties'])})")


# --- Phase 2: manual control -------------------------------------------------

@app.command()
def status(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Show config, connection, current height, and captain state."""
    logging_setup.setup(verbose)
    cfg = _load()
    st = statemod.load()

    typer.echo(f"Device:   {cfg.device.name or '(unset)'} [{cfg.device.address or 'not configured'}]")
    typer.echo(f"Heights:  sit={cfg.heights.sit_inches}in  stand={cfg.heights.stand_inches}in")
    typer.echo(f"Schedule: sit={cfg.schedule.sit_minutes}m stand={cfg.schedule.stand_minutes}m "
               f"warn={cfg.schedule.warning_seconds}s enabled={cfg.schedule.enabled}")
    typer.echo(f"Captain:  enabled={st.enabled} paused={st.paused} posture={st.posture} "
               f"pending={st.pending}")
    if st.phase_started_at:
        elapsed = (time.time() - st.phase_started_at) / 60
        typer.echo(f"          phase elapsed {elapsed:.1f}m")
    if st.last_manual_move_at:
        ago = (time.time() - st.last_manual_move_at) / 60
        typer.echo(f"          last manual move {ago:.1f}m ago")
    if cfg.presence.enabled:
        from .presence import hid_idle_seconds
        idle = hid_idle_seconds()
        if idle is not None:
            away = idle >= cfg.presence.idle_threshold_minutes * 60
            typer.echo(f"Presence: idle {idle:.0f}s ({'away' if away else 'active'})")

    if not cfg.device.address:
        return

    async def _read():
        async with ble.DeskClient(cfg.device) as client:
            r = await client.read_height(wait=3.0)
            return r

    try:
        reading = _run(_read())
    except Exception as e:  # noqa: BLE001
        typer.secho(f"Could not connect: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)

    if reading is None:
        typer.secho("Connected but no height reported (notifications unsupported?).",
                    fg=typer.colors.YELLOW)
    else:
        inches = _inches(cfg, reading.raw)
        typer.secho(f"Current height: {inches:.2f}in (raw {reading.raw})", fg=typer.colors.GREEN)


def _simple_command(coro_name: str):
    cfg = _load()
    _require_device(cfg)

    async def _do():
        async with ble.DeskClient(cfg.device) as client:
            await getattr(client, coro_name)()

    _run(_do())


@app.command()
def up():
    """Nudge the desk up (button-style; stop with `cadence stop`)."""
    logging_setup.setup()
    _simple_command("move_up")
    typer.echo("up")


@app.command()
def down():
    """Nudge the desk down (button-style; stop with `cadence stop`)."""
    logging_setup.setup()
    _simple_command("move_down")
    typer.echo("down")


@app.command()
def stop():
    """Stop any desk movement immediately."""
    logging_setup.setup()
    _simple_command("stop")
    typer.echo("stopped")


@app.command()
def goto(inches: float, verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Move the desk to an absolute height in inches (safety-checked)."""
    logging_setup.setup(verbose)
    cfg = _load()
    _require_device(cfg)

    from . import safety

    async def _do():
        async with ble.DeskClient(cfg.device) as client:
            reading = await client.read_height(wait=3.0)
            cur = _inches(cfg, reading.raw if reading else None)
            st = statemod.load()
            decision = safety.check_move(
                cfg, st, inches,
                height_known=cur is not None,
                is_moving=False,
            )
            if not decision.allowed:
                typer.secho(f"Refused: {decision.reason}", fg=typer.colors.RED)
                raise typer.Exit(1)
            target = decision.target_inches or inches
            if target != inches:
                typer.secho(decision.reason, fg=typer.colors.YELLOW)
            mm = protocol.inches_to_goto_mm(target, cfg.calibration.offset_inches)
            await client.goto_mm(mm)
            typer.secho(f"Moving to {target:.2f}in ({mm}mm)...", fg=typer.colors.GREEN)
            # Watch until the desk settles (the goto runs on the controller).
            last, stable, waited = None, 0.0, 0.0
            while waited < 40 and stable < 2.0:
                await asyncio.sleep(0.5)
                waited += 0.5
                r = client.latest_height
                cur2 = r.raw if r else None
                if cur2 == last:
                    stable += 0.5
                else:
                    stable, last = 0.0, cur2
                    if r:
                        typer.echo(f"  {_inches(cfg, r.raw):.1f}in")
            final = client.latest_height
            if final:
                typer.secho(f"Settled at {_inches(cfg, final.raw):.2f}in", fg=typer.colors.GREEN)

    _run(_do())


@app.command()
def save(
    which: str = typer.Argument(..., help="'sit' or 'stand'"),
    inches: float = typer.Argument(..., help="Height in inches"),
):
    """Save a sit or stand height preference to config."""
    logging_setup.setup()
    if which not in ("sit", "stand"):
        typer.secho("which must be 'sit' or 'stand'", fg=typer.colors.RED)
        raise typer.Exit(1)
    cfg = _load()
    if which == "sit":
        cfg.heights.sit_inches = inches
    else:
        cfg.heights.stand_inches = inches
    path = cfgmod.save(cfg)
    typer.secho(f"Saved {which}={inches}in to {path}", fg=typer.colors.GREEN)


@app.command()
def calibrate(
    inches: float = typer.Argument(..., help="True measured height in inches right now"),
):
    """Calibrate raw<->inches using a tape-measured height.

    Reads the current raw value and computes the offset so that raw maps to the
    measured height, keeping the assumed scale. Run once with the desk at a
    known, physically measured height.
    """
    logging_setup.setup()
    cfg = _load()
    _require_device(cfg)

    async def _do():
        async with ble.DeskClient(cfg.device) as client:
            return await client.read_height(wait=4.0)

    reading = _run(_do())
    if reading is None:
        typer.secho("No height reported; cannot calibrate.", fg=typer.colors.RED)
        raise typer.Exit(1)
    cal = cfg.calibration
    # inches = raw/scale + offset  ->  offset = inches - raw/scale
    cal.offset_inches = inches - reading.raw / cal.raw_units_per_inch
    path = cfgmod.save(cfg)
    typer.secho(
        f"Calibrated: raw {reading.raw} = {inches}in (offset {cal.offset_inches:.3f}in). "
        f"Saved to {path}",
        fg=typer.colors.GREEN,
    )


# --- Phase 3: warning test ---------------------------------------------------

@app.command(name="test-warn")
def test_warn():
    """Fire the configured warning once (notification/sound/tap) without moving."""
    logging_setup.setup()
    cfg = _load()
    if cfg.warning.mode in ("notify", "both"):
        notifymod.notify("cadence", "Test warning — desk would move now", sound=cfg.warning.sound)
        typer.echo("sent notification")
    if cfg.warning.mode in ("tap", "both"):
        typer.echo("tap mode requires a live connection; use the daemon to test taps safely")


# --- Phase 4: daemon + kill switch -------------------------------------------

@app.command()
def daemon(verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Run the captain daemon (foreground; use launchd to background it)."""
    logging_setup.setup(verbose, to_file=True)
    cfg = _load()
    _require_device(cfg)
    try:
        _run(scheduler.run(cfg))
    except KeyboardInterrupt:
        typer.echo("daemon stopped")


def _set_state(**changes):
    def apply(s: statemod.State):
        for k, v in changes.items():
            setattr(s, k, v)
    return statemod.mutate(apply)


@app.command()
def pause():
    """Kill switch: pause automation (desk is left where it is)."""
    logging_setup.setup()
    _set_state(paused=True)
    typer.secho("paused", fg=typer.colors.YELLOW)


@app.command()
def resume():
    """Arm/resume automation."""
    logging_setup.setup()
    _set_state(paused=False, enabled=True)
    typer.secho("resumed (armed)", fg=typer.colors.GREEN)


@app.command(name="next")
def next_():
    """Force the next sit/stand transition on the daemon's next tick."""
    logging_setup.setup()
    _set_state(pending="next")
    typer.echo("queued next transition")


@app.command()
def snooze(minutes: float = typer.Argument(None, help="Defaults to config snooze_minutes")):
    """Snooze automation for N minutes."""
    logging_setup.setup()
    cfg = _load()
    mins = minutes if minutes is not None else cfg.behavior.snooze_minutes
    _set_state(snooze_until=time.time() + mins * 60)
    typer.secho(f"snoozed {mins:.0f}m", fg=typer.colors.YELLOW)


@app.command(name="stop-daemon")
def stop_daemon():
    """Disarm automation entirely (daemon keeps running but won't move)."""
    logging_setup.setup()
    _set_state(enabled=False, paused=True)
    typer.secho("disarmed", fg=typer.colors.YELLOW)


@app.command(name="init-config")
def init_config():
    """Write a default config file if none exists."""
    logging_setup.setup()
    from .paths import config_file
    path = config_file()
    if path.exists():
        typer.secho(f"Config already exists at {path}", fg=typer.colors.YELLOW)
        raise typer.Exit(0)
    cfgmod.save(Config())
    typer.secho(f"Wrote default config to {path}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
