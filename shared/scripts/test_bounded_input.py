from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from bounded_input import read_bytes


class BoundedInputTest(unittest.TestCase):
    def test_regular_file_and_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "input"
            path.write_bytes(b"good")
            self.assertEqual(read_bytes(path, 4), b"good")
            with self.assertRaisesRegex(ValueError, "exceeds"):
                read_bytes(path, 3)

    def test_fifo_is_refused_without_waiting_for_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "fifo"
            os.mkfifo(path)
            with self.assertRaisesRegex(ValueError, "regular file"):
                read_bytes(path, 10)

    def test_read_is_bounded_on_opened_file_after_path_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "input"
            path.write_bytes(b"0123456789")
            opened = os.open
            def replace_after_open(*args):
                descriptor = opened(*args)
                path.unlink()
                os.mkfifo(path)
                return descriptor
            with mock.patch("bounded_input.os.open", side_effect=replace_after_open):
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    read_bytes(path, 4)


if __name__ == "__main__":
    unittest.main()
