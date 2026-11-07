"""Bound local input reads on the opened descriptor, including replacement races."""
from __future__ import annotations

import os
import pathlib
import stat


def read_bytes(path: pathlib.Path, maximum: int) -> bytes:
    # Nonblocking open lets us reject FIFOs before waiting for a writer.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("input must be a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError(f"input exceeds {maximum} bytes")
        return raw
    finally:
        if descriptor != -1:
            os.close(descriptor)
