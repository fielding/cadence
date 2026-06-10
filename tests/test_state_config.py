"""Round-trip and resilience tests for state.json and config.toml."""

from cadence import config as cfgmod
from cadence import state as statemod
from cadence.state import State


# --- state -------------------------------------------------------------------

def test_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    st = State(enabled=True, posture="stand", phase_started_at=123.0)
    statemod.save(st, path)
    loaded = statemod.load(path)
    assert loaded.enabled is True
    assert loaded.posture == "stand"
    assert loaded.phase_started_at == 123.0
    assert loaded.updated_at is not None


def test_state_missing_file_returns_defaults(tmp_path):
    st = statemod.load(tmp_path / "nope.json")
    assert st.enabled is False
    assert st.posture == "unknown"


def test_state_corrupt_file_returns_defaults(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json at all")
    st = statemod.load(path)
    assert st.posture == "unknown"


def test_state_ignores_unknown_keys(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"posture": "sit", "from_the_future": 42}')
    st = statemod.load(path)
    assert st.posture == "sit"


def test_mutate_applies_and_persists(tmp_path):
    path = tmp_path / "state.json"
    statemod.mutate(lambda s: setattr(s, "paused", True), path)
    assert statemod.load(path).paused is True


# --- config ------------------------------------------------------------------

def test_config_missing_file_gives_defaults(tmp_path):
    cfg = cfgmod.load(tmp_path / "nope.toml")
    assert cfg.schedule.sit_minutes == 45.0
    assert cfg.safety.min_height_inches == 22.0
    assert cfg.device.address is None


def test_config_round_trip_preserves_device(tmp_path):
    path = tmp_path / "config.toml"
    cfg = cfgmod.Config()
    cfg.device.address = "AA:BB"
    cfg.device.name = "Test Desk"
    cfg.heights.sit_inches = 27.5
    cfgmod.save(cfg, path)
    loaded = cfgmod.load(path)
    assert loaded.device.address == "AA:BB"
    assert loaded.device.name == "Test Desk"
    assert loaded.heights.sit_inches == 27.5


def test_config_ignores_unknown_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[schedule]\nsit_minutes = 30\nnot_a_real_key = true\n")
    cfg = cfgmod.load(path)
    assert cfg.schedule.sit_minutes == 30


def test_device_verified_defaults_false_and_round_trips(tmp_path):
    path = tmp_path / "config.toml"
    cfg = cfgmod.Config()
    assert cfg.device.verified is False
    cfg.device.verified = True
    cfgmod.save(cfg, path)
    assert cfgmod.load(path).device.verified is True
