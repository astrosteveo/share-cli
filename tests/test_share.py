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


def item(path, ident, digest, original=None):
    """An upload of `path` under its own name, as cmd_share makes one."""
    return share.Item(path, "f", ident, path.name, digest, path.stat().st_size, original or path.name)


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
        self.assertEqual(share.human(5 * 1024**4), "5.0 TB")


class Sha256(unittest.TestCase):
    def test_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            data = b"easel" * 500_000
            path.write_bytes(data)
            self.assertEqual(share.file_sha256(path), hashlib.sha256(data).hexdigest())
            self.assertEqual(share.file_sha256(path, 3, 7), hashlib.sha256(data[3:10]).hexdigest())


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

    def bucket(self, *replies, config=CONFIG):
        bucket = share.Bucket(config)
        bucket.request = mock.Mock(side_effect=list(replies))
        return bucket

    def answering(self, answer, config=CONFIG):
        """A bucket whose requests go to answer(method, key, query), which may run in any order, as uploads do."""
        bucket = share.Bucket(config)
        bucket.request = mock.Mock(side_effect=lambda method, key="", query=None, **_: answer(method, key, query or {}))
        return bucket

    def calls(self, bucket, method):
        return [call for call in bucket.request.call_args_list if call.args[0] == method]

    def page(self, keys, token=None):
        items = "".join("<Contents><Key>%s</Key><Size>%d</Size><LastModified>2026-10-28T01:21:10.962Z</LastModified></Contents>"
                        % (key, size) for key, size in keys)
        more = "<IsTruncated>true</IsTruncated><NextContinuationToken>%s</NextContinuationToken>" % token if token else ""
        return 200, ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">%s%s</ListBucketResult>' % (items, more)).encode(), {}

    def test_files_reads_every_page(self):
        bucket = self.bucket(self.page([("f/aaaaaaaaaa/a.png", 10)], token="next"), self.page([("f/bbbbbbbbbb/b.mp4", 20)]))
        stamp = 1793150470  # 2026-10-28T01:21:10Z
        self.assertEqual(bucket.files(), [share.Entry("f", "aaaaaaaaaa", "a.png", 10, stamp),
                                          share.Entry("f", "bbbbbbbbbb", "b.mp4", 20, stamp)])
        self.assertEqual(bucket.request.call_args.kwargs["query"]["continuation-token"], "next")

    def test_files_skips_keys_outside_the_layout(self):
        self.assertEqual(self.bucket(self.page([("f/stray.txt", 1), ("other/aaaaaaaaaa/a.png", 1)])).files(), [])

    def test_upload_puts_only_what_is_not_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            new, old = Path(tmp) / "clip.mp4", Path(tmp) / "pic.png"
            new.write_bytes(b"video")
            old.write_bytes(b"picture")
            sent = []

            def answer(method, key, query):
                if method == "PUT":
                    body = bucket.request.call_args.kwargs["body"]
                    sent.append(body.read())
                return (404 if key.endswith("clip.mp4") and method == "HEAD" else 200), b"", {}
            bucket = self.answering(answer)
            with mock.patch.object(share, "say"):
                bucket.upload([item(new, "aaaaaaaaaa", "d1", "My Clip é.mp4"), item(old, "bbbbbbbbbb", "d2")], wait=True)
        self.assertEqual(len(self.calls(bucket, "HEAD")), 2)
        [put] = self.calls(bucket, "PUT")
        self.assertEqual(put.args, ("PUT", "f/aaaaaaaaaa/clip.mp4"))
        self.assertEqual(sent, [b"video"])
        self.assertEqual(put.kwargs["digest"], "d1")
        self.assertEqual(put.kwargs["headers"]["content-type"], "video/mp4")
        self.assertEqual(put.kwargs["headers"]["content-length"], "5")
        self.assertEqual(put.kwargs["headers"]["content-disposition"],
                         "inline; filename=\"clip.mp4\"; filename*=UTF-8''My%20Clip%20%C3%A9.mp4")

    def test_links_use_the_public_url(self):
        self.assertEqual(share.Bucket(CONFIG).url("f", "aaaaaaaaaa", "a.png"), "https://share.example.com/f/aaaaaaaaaa/a.png")


class Parts(unittest.TestCase):
    """A big file goes up in parts, and a run after one that broke off sends only the parts R2 lacks."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "big.mp4"
        self.path.write_bytes(b"a" * 5 + b"b" * 5 + b"c" * 3)  # parts of 5, 5 and 3 bytes
        patches = [mock.patch.object(share, "PART_BYTES", 5), mock.patch.object(share, "MULTIPART_BYTES", 4),
                   mock.patch.object(share, "say")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def put(self, unfinished=""):
        sent = {}

        def answer(method, key="", query=None, body=None, digest=None, headers=None, tries=3):
            query = query or {}
            if method == "GET" and "uploads" in query:
                return 200, ("<ListMultipartUploadsResult>%s</ListMultipartUploadsResult>" % unfinished).encode(), {}
            if method == "GET":
                return 200, (b"<ListPartsResult><Part><PartNumber>1</PartNumber><ETag>\"e1\"</ETag><Size>5</Size></Part>"
                             b"<Part><PartNumber>3</PartNumber><ETag>\"short\"</ETag><Size>2</Size></Part></ListPartsResult>"), {}
            if method == "POST" and "uploads" in query:
                return 200, b"<InitiateMultipartUploadResult><UploadId>new</UploadId></InitiateMultipartUploadResult>", {}
            if method == "PUT":
                number = int(query["partNumber"])
                sent[number] = (query["uploadId"], body.read(), digest)
                return 200, b"", {"ETag": '"e%d"' % number}
            return 200, b"<CompleteMultipartUploadResult/>", {}
        bucket = share.Bucket(CONFIG)
        bucket.request = mock.Mock(side_effect=answer)
        bucket.put(item(self.path, "aaaaaaaaaa", "d"), share.Progress(13))
        done = bucket.request.call_args
        return sent, done

    def test_a_new_upload_sends_every_part_and_puts_them_together(self):
        sent, done = self.put()
        self.assertEqual({n: body for n, (_, body, _) in sent.items()}, {1: b"aaaaa", 2: b"bbbbb", 3: b"ccc"})
        self.assertEqual(sent[2][2], hashlib.sha256(b"bbbbb").hexdigest())
        self.assertEqual(done.kwargs["query"], {"uploadId": "new"})
        self.assertIn(b"<PartNumber>3</PartNumber><ETag>\"e3\"</ETag>", done.kwargs["body"])

    def test_an_unfinished_upload_goes_on_from_the_parts_r2_has(self):
        sent, done = self.put("<Upload><Key>f/aaaaaaaaaa/big.mp4</Key><UploadId>old</UploadId></Upload>")
        # Part 1 is there; part 3 is there but the wrong size, so it goes again.
        self.assertEqual(sorted(sent), [2, 3])
        self.assertEqual(sent[2][0], "old")
        self.assertIn(b"<PartNumber>1</PartNumber><ETag>\"e1\"</ETag>", done.kwargs["body"])


class Stopping(unittest.TestCase):
    def test_ctrl_c_stops_an_upload_at_its_next_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"data")
            body = share.Slice(path, 0, 4, share.Progress(4))
            self.addCleanup(body.close)
            share.stopping.set()
            self.addCleanup(share.stopping.clear)
            with self.assertRaises(KeyboardInterrupt):
                body.read(2)


class PartBytes(unittest.TestCase):
    def test_part_size_grows_so_10000_parts_hold_the_file(self):
        self.assertEqual(share.part_bytes(5 * 1024**3), 16 * 1024**2)
        self.assertEqual(share.part_bytes(1024**4), 105 * 1024**2)


class Retries(unittest.TestCase):
    """A dropped connection or an error on R2's side is tried again; a refusal is not."""

    def test_a_dropped_connection_is_tried_again_from_the_start_of_the_body(self):
        progress = share.Progress(4)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            path.write_bytes(b"data")
            body = share.Slice(path, 0, 4, progress)
            reads = []

            def flaky(request, timeout):
                reads.append(request.data.read())
                if len(reads) == 1:
                    raise ConnectionResetError("reset")
                reply = mock.MagicMock()
                reply.__enter__.return_value.status = 200
                reply.__enter__.return_value.read.return_value = b""
                return reply
            with mock.patch.object(share.urllib.request, "urlopen", side_effect=flaky), \
                    mock.patch.object(share.time, "sleep"), mock.patch.object(share, "say"):
                share.Bucket(CONFIG).request("PUT", "f/a/a.bin", body=body, digest="d", tries=4)
            body.close()
        self.assertEqual(reads, [b"data", b"data"])
        self.assertEqual(progress.done, 4)

    def test_a_refusal_ends_the_program_at_once(self):
        error = share.urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b"<Error><Code>AccessDenied</Code></Error>"))
        with mock.patch.object(share.urllib.request, "urlopen", side_effect=error) as urlopen, \
                mock.patch.object(share, "say"), self.assertRaises(SystemExit):
            share.Bucket(CONFIG).request("GET")
        self.assertEqual(urlopen.call_count, 1)


class Remove(unittest.TestCase):
    """--rm finds what each target names, and asks before one target takes down several files."""

    FILES = [share.Entry("f", "aaaaaaaaaa", "clip.mp4", 1, 0), share.Entry("f", "bbbbbbbbbb", "clip.mp4", 1, 0),
             share.Entry("f", "cccccccccc", "pic.png", 1, 0)]

    def rm(self, targets, yes=False, tty=False, answer=""):
        store = mock.Mock()
        store.files.return_value = self.FILES
        store.url.side_effect = lambda prefix, ident, name: "https://x/%s/%s/%s" % (prefix, ident, name)
        with mock.patch.object(share, "open_store", return_value=store), mock.patch.object(share, "say"), \
                mock.patch.object(share.sys.stdin, "isatty", return_value=tty), \
                mock.patch("builtins.input", return_value=answer):
            share.cmd_rm(targets, wait=False, yes=yes)
        return store.remove.call_args.args[0]

    def test_a_link_an_id_and_a_unique_name_each_take_one_file(self):
        doomed = self.rm(["https://x/f/aaaaaaaaaa/clip.mp4", "bbbbbbbbbb", "pic.png"])
        self.assertEqual(doomed, self.FILES)

    def test_a_name_that_matches_two_files_is_refused_without_a_terminal(self):
        with self.assertRaises(SystemExit):
            self.rm(["clip.mp4"])

    def test_yes_takes_every_match(self):
        self.assertEqual(self.rm(["clip.mp4"], yes=True), self.FILES[:2])

    def test_the_question_takes_every_match_on_y_and_nothing_otherwise(self):
        self.assertEqual(self.rm(["clip.mp4"], tty=True, answer="y"), self.FILES[:2])
        with self.assertRaises(SystemExit):
            self.rm(["clip.mp4"], tty=True, answer="")


class Expire(unittest.TestCase):
    """--expire puts files in `<n>d/`, which a lifecycle rule on the bucket empties; the rule is added once, with cf."""

    def tearDown(self):
        share.config_path().unlink(missing_ok=True)

    def test_days_are_read_with_or_without_the_d(self):
        self.assertEqual(share.parse_days("7d"), 7)
        self.assertEqual(share.parse_days("30"), 30)
        for bad in ("0d", "7h", "-1", "d"):
            with self.assertRaises(share.argparse.ArgumentTypeError):
                share.parse_days(bad)

    def test_expiring_keys_are_in_the_layout(self):
        self.assertEqual(share.parse_key("7d/aaaaaaaaaa/a.png"), ("7d", "aaaaaaaaaa", "a.png"))
        self.assertIsNone(share.parse_key("0d/aaaaaaaaaa/a.png"))
        self.assertEqual(share.expiry_days("30d"), 30)
        self.assertIsNone(share.expiry_days("f"))

    def test_github_cannot_expire(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            path.write_bytes(b"x")
            with mock.patch.object(share, "say"), mock.patch.object(share, "Site") as site, self.assertRaises(SystemExit):
                share.cmd_share([str(path)], wait=False, copy=False, expire=7)
            site.assert_not_called()

    def run_rule(self, existing):
        write_config(CONFIG)
        calls = []

        def cf(cmd, **kwargs):
            calls.append((cmd, kwargs["env"]["CLOUDFLARE_ACCOUNT_ID"]))
            return mock.Mock(returncode=0, stdout=json.dumps({"rules": existing}) if "get" in cmd else "", stderr="")
        config = dict(CONFIG)
        with mock.patch.object(share.shutil, "which", return_value="/usr/bin/cf"), \
                mock.patch.object(share.subprocess, "run", side_effect=cf), mock.patch.object(share, "say"):
            share.expiry_rule(config, 7)
            share.expiry_rule(config, 7)  # known now, so no second call
        return calls

    def test_the_rule_is_added_beside_the_rules_already_there_once(self):
        other = {"id": "Default Multipart Abort Rule", "enabled": True, "conditions": {}}
        calls = self.run_rule([other, {"id": "share-expire-7d", "enabled": False}])
        self.assertEqual(len(calls), 2)
        update, account = calls[1]
        self.assertEqual(account, "acct")
        rules = json.loads(update[update.index("--rules") + 1])
        self.assertEqual(rules, [other, {"id": "share-expire-7d", "enabled": True, "conditions": {"prefix": "7d/"},
                                         "deleteObjectsTransition": {"condition": {"type": "Age", "maxAge": 604800}}}])
        self.assertEqual(share.r2_config()["expire_days"], [7])


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
