"""Logging configuration shared by the CLI and daemon."""

from __future__ import annotations

import logging
import sys

from . import paths


def setup(verbose: bool = False, to_file: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        paths.ensure_dirs()
        handlers.append(logging.FileHandler(paths.log_file()))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
