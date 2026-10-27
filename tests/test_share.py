"""Tests for the parts of `share` that need no network: names, sizes, the checks before an upload,
the R2 settings and signing, and the wait for Pages."""

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("share", str(ROOT / "share"))
share = importlib.util.module_from_spec(importlib.util.spec_from_loader("share", loader))
loader.exec_module(share)

CONFIG = {
    "endpoint": "https://acct.r2.cloudflarestorage.com",
    "bucket": "share",
    "access_key_id": "key",
    "secret_access_key": "secret",
    "public_url": "https://share.example.com",
}


def setUpModule():
    # Point the config at an empty folder, so no test can read real R2 settings and upload with them.
    global config_home, environment
    config_home = tempfile.TemporaryDirectory()
    environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home.name})
    environment.start()
    os.environ.pop("SHARE_BACKEND", None)


def tearDownModule():
    environment.stop()
    config_home.cleanup()


def write_config(config):
    path = share.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


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

    def test_a_file_over_5_gb_when_r2_is_set_up(self):
        write_config(CONFIG)
        self.addCleanup(share.config_path().unlink)
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.mp4"
            with open(big, "wb") as handle:
                handle.truncate(share.R2_MAX_BYTES + 1)
            self.refused(big)


class ContentType(unittest.TestCase):
    def kind(self, name, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_bytes(data)
            return share.content_type(path, name)

    def test_markdown_is_shown_as_plain_text(self):
        self.assertEqual(self.kind("README.md", b"# hi\n"), "text/plain; charset=utf-8")

    def test_code_python_guesses_wrong_is_plain_text(self):
        self.assertEqual(self.kind("main.rs", b"fn main() {}\n"), "text/plain; charset=utf-8")

    def test_html_is_shown_not_run(self):
        self.assertEqual(self.kind("page.html", b"<script>alert(1)</script>"), "text/plain; charset=utf-8")

    def test_a_character_cut_at_the_end_of_the_sample_is_still_text(self):
        self.assertEqual(self.kind("notes.txt", b"a" * (64 * 1024 - 1) + "é".encode()), "text/plain; charset=utf-8")

    def test_pictures_and_video_keep_their_type(self):
        self.assertEqual(self.kind("pic.svg", b"<svg/>"), "image/svg+xml")
        self.assertEqual(self.kind("clip.mp4", b"\0\0\0 ftyp"), "video/mp4")

    def test_binary_is_not_text(self):
        self.assertEqual(self.kind("blob.bin", b"\x00\xff\xfe"), "application/octet-stream")


class R2Config(unittest.TestCase):
    def tearDown(self):
        share.config_path().unlink(missing_ok=True)

    def test_none_without_a_file(self):
        self.assertIsNone(share.r2_config())

    def test_the_stored_settings(self):
        write_config(CONFIG)
        self.assertEqual(share.r2_config(), CONFIG)

    def test_share_backend_github_ignores_them(self):
        write_config(CONFIG)
        with mock.patch.dict(os.environ, {"SHARE_BACKEND": "github"}):
            self.assertIsNone(share.r2_config())

    def test_a_missing_setting_stops_the_program(self):
        write_config(dict(CONFIG, secret_access_key=""))
        with self.assertRaises(SystemExit):
            share.r2_config()


class Setup(unittest.TestCase):
    def tearDown(self):
        share.config_path().unlink(missing_ok=True)

    def setup(self, answers, secret, files=()):
        with mock.patch("builtins.input", side_effect=answers), \
                mock.patch.object(share.getpass, "getpass", return_value=secret), \
                mock.patch.object(share.Bucket, "files", return_value=list(files)), \
                mock.patch.object(share.urllib.request, "urlopen"), \
                mock.patch.object(share, "say"):
            share.cmd_setup()
        return json.loads(share.config_path().read_text())

    def test_saves_the_settings_for_your_eyes_only(self):
        saved = self.setup(["https://acct.r2.cloudflarestorage.com/share", "", "key", "share.example.com", ""], "secret")
        self.assertEqual(saved, CONFIG)
        self.assertEqual(share.config_path().stat().st_mode & 0o777, 0o600)

    def test_enter_keeps_the_saved_secret(self):
        write_config(CONFIG)
        saved = self.setup(["", "", "", "", ""], "")
        self.assertEqual(saved, CONFIG)

    def test_enter_keeps_the_saved_purge_settings(self):
        write_config(dict(CONFIG, zone_id="zone", purge_token="tok"))
        saved = self.setup(["", "", "", "", ""], "")
        self.assertEqual(saved, dict(CONFIG, zone_id="zone", purge_token="tok"))

    def test_bad_settings_are_not_saved(self):
        with mock.patch("builtins.input", side_effect=["acct.r2.cloudflarestorage.com", "share", "key", "x.example.com", ""]), \
                mock.patch.object(share.getpass, "getpass", return_value="wrong"), \
                mock.patch.object(share.Bucket, "files", side_effect=SystemExit(1)), \
                mock.patch.object(share, "say"):
            with self.assertRaises(SystemExit):
                share.cmd_setup()
        self.assertFalse(share.config_path().exists())


class Sigv4(unittest.TestCase):
    """Cases from AWS's SigV4 test suite, as kept in botocore's tests/unit/auth/aws4_testsuite."""

    def sign(self, method, path, query):
        headers = {"host": "example.amazonaws.com", "x-amz-date": "20150830T123600Z"}
        return share.sigv4(method, path, query, headers, share.EMPTY_SHA256, "AKIDEXAMPLE",
                           "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20150830T123600Z", "us-east-1", "service")

    def expect(self, signature):
        return ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
                "SignedHeaders=host;x-amz-date, Signature=" + signature)

    def test_get_vanilla_query_order_key_case(self):
        self.assertEqual(self.sign("GET", "/", {"Param2": "value2", "Param1": "value1"}),
                         self.expect("b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"))

    def test_get_unreserved(self):
        path = "/-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        self.assertEqual(self.sign("GET", path, {}),
                         self.expect("07ef7494c76fa4850883e2b006601f940f8a34d404d0cfa977f52a65bbf5f24f"))

    def test_post_vanilla_query(self):
        self.assertEqual(self.sign("POST", "/", {"Param1": "value1"}),
                         self.expect("28038455d6de14eafc1f9222cf5aa6f1a96197d7deb8263271d420d138af7f11"))

    def test_a_slash_in_a_query_value_is_escaped(self):
        self.assertEqual(share.canonical_query({"prefix": "f/", "list-type": "2"}), "list-type=2&prefix=f%2F")


class Bucket(unittest.TestCase):
    """The bucket's requests are stubbed: these check what it asks for and how it reads the answers."""

    def bucket(self, *replies):
        bucket = share.Bucket(CONFIG)
        bucket.request = mock.Mock(side_effect=list(replies))
        return bucket

    def page(self, keys, token=None):
        items = "".join("<Contents><Key>%s</Key><Size>%d</Size></Contents>" % (key, size) for key, size in keys)
        more = "<IsTruncated>true</IsTruncated><NextContinuationToken>%s</NextContinuationToken>" % token if token else ""
        return 200, ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">%s%s</ListBucketResult>' % (items, more)).encode()

    def test_files_reads_every_page(self):
        bucket = self.bucket(self.page([("f/aaaaaaaaaa/a.png", 10)], token="next"), self.page([("f/bbbbbbbbbb/b.mp4", 20)]))
        self.assertEqual(bucket.files(), [("aaaaaaaaaa", "a.png", 10), ("bbbbbbbbbb", "b.mp4", 20)])
        self.assertEqual(bucket.request.call_args.kwargs["query"]["continuation-token"], "next")

    def test_files_skips_keys_outside_the_layout(self):
        self.assertEqual(self.bucket(self.page([("f/stray.txt", 1)])).files(), [])

    def test_upload_puts_only_what_is_not_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            new, old = Path(tmp) / "clip.mp4", Path(tmp) / "pic.png"
            new.write_bytes(b"video")
            old.write_bytes(b"picture")
            bucket = self.bucket((404, b""), (200, b""), (200, b""))
            with mock.patch.object(share, "say"):
                bucket.upload([(new, "aaaaaaaaaa", "clip.mp4", "d1"), (old, "bbbbbbbbbb", "pic.png", "d2")], wait=True)
        put = bucket.request.call_args_list[1]
        self.assertEqual(put.args, ("PUT", "f/aaaaaaaaaa/clip.mp4"))
        self.assertEqual(put.kwargs["digest"], "d1")
        self.assertEqual(put.kwargs["headers"]["content-type"], "video/mp4")
        self.assertEqual(put.kwargs["headers"]["content-length"], "5")
        self.assertEqual(bucket.request.call_args_list[2].args, ("HEAD", "f/bbbbbbbbbb/pic.png"))
        self.assertEqual(bucket.request.call_count, 3)

    def test_links_use_the_public_url(self):
        self.assertEqual(share.Bucket(CONFIG).url("aaaaaaaaaa", "a.png"), "https://share.example.com/f/aaaaaaaaaa/a.png")


class Purge(unittest.TestCase):
    """--rm on R2 deletes, then asks Cloudflare to drop the links from its cache; the calls are stubbed."""

    def remove(self, config, count, purge_error=None):
        bucket = share.Bucket(config)
        bucket.request = mock.Mock(return_value=(204, b""))
        said = []
        with mock.patch.object(share.urllib.request, "urlopen", side_effect=purge_error) as urlopen, \
                mock.patch.object(share, "say", side_effect=said.append):
            bucket.remove([("%010d" % i, "f%d.png" % i) for i in range(count)], wait=True)
        return bucket, urlopen, said

    def test_without_a_token_nothing_is_purged(self):
        bucket, urlopen, said = self.remove(CONFIG, 2)
        self.assertEqual(bucket.request.call_count, 2)
        urlopen.assert_not_called()
        self.assertIn("up to an hour", said[-1])

    def test_the_removed_links_are_purged_30_at_a_time(self):
        bucket, urlopen, said = self.remove(dict(CONFIG, zone_id="zone", purge_token="tok"), 31)
        self.assertEqual(urlopen.call_count, 2)
        first = urlopen.call_args_list[0].args[0]
        self.assertEqual(first.full_url, "https://api.cloudflare.com/client/v4/zones/zone/purge_cache")
        self.assertEqual(first.get_header("Authorization"), "Bearer tok")
        files = json.loads(first.data)["files"]
        self.assertEqual(len(files), 30)
        self.assertEqual(files[0], "https://share.example.com/f/0000000000/f0.png")
        self.assertIn("purged", said[-1])

    def test_new_uploads_are_purged_so_a_cached_404_goes(self):
        bucket = share.Bucket(dict(CONFIG, zone_id="zone", purge_token="tok"))
        bucket.request = mock.Mock(side_effect=[(404, b""), (200, b""), (200, b"")])
        bucket.purge = mock.Mock(return_value=True)
        with tempfile.TemporaryDirectory() as tmp:
            new, old = Path(tmp) / "new.png", Path(tmp) / "old.png"
            new.write_bytes(b"n")
            old.write_bytes(b"o")
            with mock.patch.object(share, "say"):
                bucket.upload([(new, "aaaaaaaaaa", "new.png", "d1"), (old, "bbbbbbbbbb", "old.png", "d2")], wait=True)
        bucket.purge.assert_called_once_with(["https://share.example.com/f/aaaaaaaaaa/new.png"])

    def test_a_refused_purge_only_warns(self):
        error = share.urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(
            b'{"success": false, "errors": [{"code": 10000, "message": "Authentication error"}]}'))
        bucket, urlopen, said = self.remove(dict(CONFIG, zone_id="zone", purge_token="tok"), 1, purge_error=error)
        self.assertIn("10000 Authentication error", said[-2])
        self.assertIn("up to an hour", said[-1])


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
