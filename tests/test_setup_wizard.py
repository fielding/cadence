"""Tests for the setup wizard's pure calibration math."""

import pytest

from cadence.setup_wizard import derive_raw_units_per_inch


def test_derive_scale_inches():
    # Apex Pro reality: raw 268 while the display says 26.8 inches.
    assert derive_raw_units_per_inch(268, 26.8, "in") == pytest.approx(10.0)


def test_derive_scale_centimeters():
    # A metric display showing 68.1 cm for the same raw 0.1in firmware.
    scale = derive_raw_units_per_inch(268, 68.1, "cm")
    assert scale == pytest.approx(10.0, rel=0.01)


def test_derive_scale_rejects_nonpositive():
    with pytest.raises(ValueError):
        derive_raw_units_per_inch(268, 0, "in")
