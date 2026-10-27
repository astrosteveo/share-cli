"""Tests for the parts of `share` that need no network: names, sizes and the checks before an upload."""

import hashlib
import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("share", str(ROOT / "share"))
share = importlib.util.module_from_spec(importlib.util.spec_from_loader("share", loader))
loader.exec_module(share)


class CleanName(unittest.TestCase):
    def test_spaces_and_brackets_become_dashes(self):
        self.assertEqual(share.clean_name("My Clip (test).mp4"), "My-Clip-test.mp4")

    def test_accents_are_dropped_to_ascii(self):
        self.assertEqual(share.clean_name("pic é.png"), "pic-e.png")

    def test_the_extension_is_lower_case(self):
        self.assertEqual(share.clean_name("CLIP.MP4"), "CLIP.mp4")

    def test_a_name_with_nothing_left_still_has_a_name(self):
        self.assertEqual(share.clean_name("***.png"), "file.png")

    def test_a_leading_dot_does_not_survive(self):
        self.assertEqual(share.clean_name(".hidden.png"), "hidden.png")


class Human(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(share.human(512), "512 B")
        self.assertEqual(share.human(2048), "2.0 KB")
        self.assertEqual(share.human(5 * 1024**2), "5.0 MB")
        self.assertEqual(share.human(3 * 1024**3), "3.0 GB")


class Sha256(unittest.TestCase):
    def test_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            data = b"easel" * 500_000
            path.write_bytes(data)
            self.assertEqual(share.sha256(path), hashlib.sha256(data).hexdigest())


class RefusesBeforeUploading(unittest.TestCase):
    """These all fail before `Site()` is made, so no network or GitHub login is touched."""

    def refused(self, path):
        with self.assertRaises(SystemExit):
            share.cmd_share([str(path)], wait=False, copy=False)

    def test_a_missing_file(self):
        self.refused("/no/such/file.mp4")

    def test_an_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.mp4"
            empty.write_bytes(b"")
            self.refused(empty)

    def test_a_file_over_100_mb(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.mp4"
            with open(big, "wb") as handle:
                handle.truncate(share.MAX_BYTES + 1)  # sparse, so it costs no disk
            self.refused(big)


if __name__ == "__main__":
    unittest.main()
