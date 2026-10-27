"""Tests for the parts of `share` that need no network: names, sizes, the checks before an upload and the wait."""

import hashlib
import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class Workflow(unittest.TestCase):
    def test_the_branch_goes_in_and_the_expressions_survive(self):
        text = share.WORKFLOW % "trunk"
        self.assertIn("branches: [trunk]", text)
        self.assertIn("${{ steps.deployment.outputs.page_url }}", text)


class WaitForBuild(unittest.TestCase):
    """The wait reads the deploy status of a commit; these hand it answers instead of asking GitHub."""

    def wait(self, statuses, runs=()):
        site = share.Site.__new__(share.Site)
        site.repo = "me/share"
        statuses = iter(statuses)
        site.api = lambda path: {"workflow_runs": list(runs)} if "/actions/runs" in path else {"status": next(statuses)}
        with mock.patch.object(share.time, "sleep"), mock.patch.object(share, "say"):
            return site.wait_for_build("abc123")

    def test_live_once_the_deploy_succeeds(self):
        self.assertTrue(self.wait(["", "deployment_in_progress", "purging_cdn", "succeed"]))

    def test_a_failed_deploy_stops_the_wait(self):
        with self.assertRaises(SystemExit):
            self.wait(["", "deployment_failed"])

    def test_a_run_that_fails_before_it_deploys_stops_the_wait(self):
        with self.assertRaises(SystemExit):
            self.wait([""] * 5, runs=[{"status": "completed", "conclusion": "failure", "html_url": "https://github.com/me/share/actions/runs/1"}])

    def test_a_run_that_never_starts_stops_the_wait(self):
        with self.assertRaises(SystemExit):
            self.wait([""] * 5, runs=[{"status": "completed", "conclusion": "startup_failure", "html_url": "https://github.com/me/share/actions/runs/2"}])

    def test_a_run_still_going_keeps_the_wait_going(self):
        self.assertTrue(self.wait([""] * 5 + ["succeed"], runs=[{"status": "in_progress", "conclusion": None}]))

    def test_a_run_replaced_by_a_newer_push_ends_the_wait(self):
        self.assertFalse(self.wait([""] * 5, runs=[{"status": "completed", "conclusion": "cancelled"}]))


if __name__ == "__main__":
    unittest.main()
