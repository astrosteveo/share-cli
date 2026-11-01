"""Tests for the parts of `share` that need no network: names, sizes, the checks before an upload,
the R2 settings and signing, and the wait for Pages."""

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
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

    def test_mp4_gets_to_shrink_a_big_camcorder_video_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.m2ts"
            with open(big, "wb") as handle:
                handle.truncate(share.MAX_BYTES + 1)
            with mock.patch.object(share, "to_mp4", side_effect=SystemExit("converting")):
                with self.assertRaisesRegex(SystemExit, "converting"):
                    share.cmd_share([str(big)], wait=False, copy=False, mp4=True)

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

    def test_the_same_file_named_twice_goes_up_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"video")
            bucket = self.answering(lambda method, key, query: (404 if method == "HEAD" else 200, b"", {}))
            with mock.patch.object(share, "say"):
                bucket.upload([item(clip, "aaaaaaaaaa", "d1"), item(clip, "aaaaaaaaaa", "d1")], wait=True)
        self.assertEqual(len(self.calls(bucket, "PUT")), 1)

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

    def test_a_name_that_matches_two_files_asks_even_beside_another_target(self):
        with self.assertRaises(SystemExit):
            self.rm(["clip.mp4", "aaaaaaaaaa"])

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


def segment(marker, payload):
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def exif(orientation, order="MM"):
    pack = (lambda f, *v: __import__("struct").pack((">" if order == "MM" else "<") + f, *v))
    tiff = order.encode() + pack("H", 42) + pack("I", 8) + pack("H", 2) + \
        pack("HHII", 0x010F, 2, 6, 38) + pack("HHIHH", 0x0112, 3, 1, orientation, 0) + pack("I", 0) + b"Apple\0"
    return segment(0xE1, b"Exif\0\0" + tiff)


class StripJpeg(unittest.TestCase):
    """The JPEG stripping used without exiftool, on a JPEG built by hand: every kind of segment, a scan with stuffed
    bytes and a restart marker, and a second picture after the end, as some phones add."""

    JFIF, ICC = segment(0xE0, b"JFIF\0\1\1"), segment(0xE2, b"ICC_PROFILE\0\1\1profile")
    TABLES = segment(0xDB, b"\0" + bytes(64)) + segment(0xC0, b"\x08\0\x10\0\x10\x01\x01\x11\0")
    SCAN = segment(0xDA, b"\x01\x01\0\0\x3f\0") + b"\x12\xff\x00\x34\xff\xd0\x56"

    def picture(self, *metadata):
        return b"\xff\xd8" + self.JFIF + b"".join(metadata) + self.ICC + self.TABLES + self.SCAN + b"\xff\xd9"

    def test_metadata_goes_and_the_picture_stays(self):
        dirty = self.picture(exif(1), segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0<x:creator>Steve"),
                             segment(0xED, b"Photoshop 3.0\0iptc"), segment(0xEB, b"JP\0c2pa"), segment(0xFE, b"a comment"),
                             segment(0xE2, b"MPF\0index")) + b"\xff\xd8second picture with its own Exif\xff\xd9"
        self.assertEqual(share.strip_jpeg(dirty), self.picture())

    def test_the_orientation_survives_in_a_small_exif_of_its_own(self):
        for order in ("MM", "II"):
            clean = share.strip_jpeg(self.picture(exif(6, order)))
            self.assertNotIn(b"Apple", clean)
            self.assertTrue(clean.startswith(b"\xff\xd8" + self.JFIF + b"\xff\xe1"))
            self.assertEqual(share.exif_orientation(clean[len(self.JFIF) + 2 + 10:]), 6)

    def test_not_a_jpeg(self):
        with self.assertRaises(ValueError):
            share.strip_jpeg(b"GIF89a")


class StripPng(unittest.TestCase):
    @staticmethod
    def chunk(kind, data):
        import zlib
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")

    def test_text_dates_and_exif_go_and_the_picture_and_its_profile_stay(self):
        keep = [self.chunk(b"IHDR", bytes(13)), self.chunk(b"iCCP", b"icc\0\0data"), self.chunk(b"IDAT", b"pixels"),
                self.chunk(b"IEND", b"")]
        drop = [self.chunk(b"tEXt", b"Comment\0where"), self.chunk(b"eXIf", b"MM\0*"), self.chunk(b"tIME", bytes(7)),
                self.chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0xmp")]
        dirty = share.PNG_SIGNATURE + keep[0] + drop[0] + keep[1] + drop[1] + drop[2] + keep[2] + drop[3] + keep[3]
        self.assertEqual(share.strip_png(dirty), share.PNG_SIGNATURE + b"".join(keep))


class StripMetadata(unittest.TestCase):
    MOV = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}, "streams": [{"codec_type": "video"}]}

    def strip(self, name, data, exiftool=False, ffmpeg=False, info=MOV):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path, folder = Path(tmp.name) / name, Path(tmp.name) / "out"
        path.write_bytes(data)
        folder.mkdir()
        said = []
        tools = (["exiftool"] if exiftool else []) + (["ffmpeg", "ffprobe"] if ffmpeg else [])

        def run(cmd, check):  # exiftool ends with "-o TARGET PATH", ffmpeg with TARGET
            Path(cmd[-1] if cmd[0] == "ffmpeg" else cmd[-2]).write_bytes(b"clean")
            return mock.Mock(returncode=0)
        with mock.patch.object(share, "exiftool_writes", return_value={".jpg", ".mov", ".nef"} if exiftool else set()), \
                mock.patch.object(share, "run", side_effect=run) as ran, mock.patch.object(share, "probe", return_value=info), \
                mock.patch.object(share, "say", side_effect=said.append), \
                mock.patch.object(share.shutil, "which", side_effect=lambda tool: tool if tool in tools else None):
            out = share.strip_metadata(path, folder)
        return path, out, ran, said

    def test_exiftool_strips_what_it_can_write_and_keeps_orientation_and_colour(self):
        path, out, run, _ = self.strip("pic.jpg", b"picture", exiftool=True)
        self.assertEqual(out.read_bytes(), b"clean")
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["exiftool", "-q", "-q"])
        for kept in ("--icc_profile:all", "-orientation", "-colorspacetags"):
            self.assertIn(kept, command)

    def test_without_exiftool_a_jpeg_is_stripped_here(self):
        path, out, run, _ = self.strip("pic.jpg", StripJpeg().picture(exif(1)))
        run.assert_not_called()
        self.assertEqual(out.read_bytes(), StripJpeg().picture())

    def test_what_cannot_be_stripped_goes_up_as_it_is_with_a_note(self):
        path, out, _, said = self.strip("pic.heic", b"heic")
        self.assertEqual(out, path)
        self.assertIn("install exiftool", said[0])

    def test_files_that_are_not_pictures_or_video_are_left_alone(self):
        for name in ("notes.md", "logo.svg"):
            path, out, run, _ = self.strip(name, b"<svg/>")
            self.assertEqual(out, path)
            run.assert_not_called()

    def test_a_raw_photo_loses_its_place_and_says_it_keeps_its_date_and_camera(self):
        path, out, run, said = self.strip("DSC_0059.NEF", b"raw", exiftool=True)
        self.assertEqual(out.read_bytes(), b"clean")
        self.assertIn("keeps its date and camera", said[0])
        path, out, _, said = self.strip("IMG_1.CR2", b"raw")
        self.assertEqual(out, path)
        self.assertIn("install exiftool", said[0])

    def test_a_video_keeps_only_its_picture_and_sound(self):
        path, out, run, _ = self.strip("drone.mov", b"video", exiftool=True, ffmpeg=True)
        self.assertEqual(out.read_bytes(), b"clean")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "ffmpeg")
        self.assertEqual(command[command.index("-f") + 1], "mov")
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file")
        self.assertEqual([command[i + 1] for i, a in enumerate(command) if a == "-map"], ["0:v?", "0:a?"])
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertEqual(command[command.index("-map_metadata") + 1], "-1")
        self.assertNotIn("-bsf:v:0", command)

    def test_a_camcorder_video_loses_the_sei_messages_in_its_h264(self):
        info = {"format": {"format_name": "mpegts"}, "streams": [{"codec_type": "video", "codec_name": "h264"}]}
        path, out, run, _ = self.strip("00001.MTS", b"video", ffmpeg=True, info=info)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-bsf:v:0") + 1], "filter_units=remove_types=6")

    def test_a_ts_file_is_video_only_when_its_bytes_are_mpeg_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            video, code = Path(tmp) / "dashcam.ts", Path(tmp) / "app.ts"
            video.write_bytes((b"\x47" + bytes(187)) * 2)
            code.write_text("export const x = 1;\n" * 20)
            self.assertEqual(share.media_kind(video), "video")
            self.assertIsNone(share.media_kind(code))

    def test_a_playlist_named_like_a_video_is_refused(self):
        for found in ("concat", "dash", "hls", ""):
            with self.assertRaises(SystemExit):
                self.strip("funny.mov", b"ffconcat version 1.0\nfile private.mov\n", ffmpeg=True,
                           info={"format": {"format_name": found}})

    def test_a_video_without_ffmpeg_goes_up_as_it_is_with_a_note(self):
        path, out, run, said = self.strip("clip.mov", b"video", exiftool=True)
        self.assertEqual(out, path)
        run.assert_not_called()
        self.assertIn("install ffmpeg", said[0])


class Inputs(unittest.TestCase):
    """`-` reads stdin, and no path at all reads the Wayland clipboard."""

    def test_piped_bytes_get_an_extension_from_how_they_start(self):
        self.assertEqual(share.sniff_suffix(share.PNG_SIGNATURE + b"..."), ".png")
        self.assertEqual(share.sniff_suffix(b"\xff\xd8\xff\xe0"), ".jpg")
        self.assertEqual(share.sniff_suffix(b"\0\0\0\x20ftypisom"), ".mp4")
        self.assertEqual(share.sniff_suffix(b"\0\0\0\x14ftypqt  "), ".mov")
        self.assertEqual(share.sniff_suffix(b"\0\0\0\x18ftypheix\0\0\0\0mif1heix"), ".heic")
        self.assertEqual(share.sniff_suffix(b"\0\0\0\x1cftypavif\0\0\0\0mif1avifmiaf"), ".avif")
        self.assertEqual(share.sniff_suffix(b"\0\0\0\x1cftypmif1\0\0\0\0mif1avifmiaf"), ".avif")
        self.assertEqual(share.sniff_suffix(b"\0\0\0\x1cftypM4A \0\0\0\0M4A isom"), ".m4a")
        self.assertEqual(share.sniff_suffix(b"RIFF\0\0\0\0WEBPVP8 "), ".webp")
        self.assertEqual(share.sniff_suffix("# notes, café".encode()), ".txt")
        self.assertEqual(share.sniff_suffix(b"\x00\x01\x02"), ".bin")

    def clipboard(self, types, contents):
        def paste(cmd, **kwargs):
            if "--list-types" in cmd:
                return mock.Mock(stdout="\n".join(types))
            kind = cmd[cmd.index("--type") + 1]
            if "stdout" in kwargs:
                kwargs["stdout"].write(contents[kind])
                return mock.Mock()
            return mock.Mock(stdout=contents[kind])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-1"}), \
                mock.patch.object(share.shutil, "which", return_value="/usr/bin/wl-paste"), \
                mock.patch.object(share.subprocess, "run", side_effect=paste):
            return share.from_clipboard(Path(tmp.name))

    def test_a_picture_on_the_clipboard_becomes_a_file(self):
        [path] = self.clipboard(["text/html", "image/jpeg", "image/png"], {"image/png": b"\x89PNG data"})
        self.assertRegex(path.name, r"^pasted-\d{8}-\d{6}\.png$")
        self.assertEqual(path.read_bytes(), b"\x89PNG data")

    def test_files_copied_in_a_file_manager_are_shared_by_path(self):
        paths = self.clipboard(["text/uri-list", "image/png"],
                               {"text/uri-list": "file:///home/me/My%20Clip.mp4\r\nfile:///home/me/a.png\r\n"})
        self.assertEqual(paths, [Path("/home/me/My Clip.mp4"), Path("/home/me/a.png")])

    def test_text_on_the_clipboard_is_not_shared(self):
        self.assertEqual(self.clipboard(["text/plain"], {}), [])

    def test_no_wayland_no_clipboard(self):
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": ""}):
            self.assertEqual(share.from_clipboard(Path("/nowhere")), [])


class Mp4(unittest.TestCase):
    """--mp4 asks ffprobe what a video holds and converts only what browsers may not play."""

    def convert(self, name, streams, kind="mov,mp4,m4a,3gp,3g2,mj2"):
        streams = [dict(stream, index=n) for n, stream in enumerate(streams)]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / name
        path.write_bytes(b"v")
        process = mock.Mock(stdout=iter(["out_time_us=1000000\n"]), stderr=io.StringIO(""))
        process.wait.return_value = 0
        with mock.patch.object(share.shutil, "which", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(share, "probe", return_value={"streams": streams, "format": {"duration": "2", "format_name": kind}}), \
                mock.patch.object(share.subprocess, "Popen", return_value=process) as popen, mock.patch.object(share, "say"):
            out = share.to_mp4(path, Path(tmp.name))
        self.popen_kwargs = popen.call_args.kwargs if popen.called else None
        return out, popen.call_args.args[0] if popen.called else None

    H264 = {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}
    HEVC = {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le"}
    AAC, PCM = {"codec_type": "audio", "codec_name": "aac"}, {"codec_type": "audio", "codec_name": "pcm_s16le"}

    def test_an_mp4_that_plays_is_left_alone(self):
        self.assertEqual(self.convert("a.mp4", [self.H264, self.AAC]), (None, None))

    def test_webm_and_pictures_are_left_alone(self):
        self.assertEqual(self.convert("a.webm", [self.HEVC]), (None, None))
        self.assertEqual(self.convert("a.png", []), (None, None))

    def test_a_mov_that_plays_has_its_video_copied_and_its_sound_made_aac(self):
        out, command = self.convert("phone.mov", [self.H264, self.PCM])
        self.assertEqual(out.name, "phone.mp4")
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertIn("+faststart", command)
        self.assertEqual(command[command.index("-map_metadata") + 1], "-1")

    def test_a_playlist_named_like_a_video_is_refused(self):
        with self.assertRaises(SystemExit):
            self.convert("funny.mov", [self.H264], kind="concat")

    def test_the_video_it_chose_is_the_one_it_maps_even_after_a_cover(self):
        cover = {"codec_type": "video", "codec_name": "png", "disposition": {"attached_pic": 1}}
        out, command = self.convert("phone.mov", [cover, self.H264, self.AAC])
        self.assertEqual(command[command.index("-map") + 1], "0:1")

    def test_h264_copied_out_of_mpeg_ts_loses_its_sei(self):
        out, command = self.convert("00001.MTS", [self.H264, self.AAC], kind="mpegts")
        self.assertEqual(command[command.index("-bsf:v:0") + 1], "filter_units=remove_types=6")

    def test_ffmpeg_errors_go_to_a_file_not_a_pipe(self):
        out, command = self.convert("clip.mkv", [self.HEVC, self.AAC], kind="matroska,webm")
        self.assertNotEqual(self.popen_kwargs["stderr"], share.subprocess.PIPE)

    def test_the_format_ffprobe_found_is_the_one_ffmpeg_reads(self):
        out, command = self.convert("clip.mkv", [self.HEVC, self.AAC], kind="matroska,webm")
        self.assertEqual(command[command.index("-f") + 1], "matroska")
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file")

    def test_hevc_is_encoded_to_h264_at_an_even_size(self):
        out, command = self.convert("clip.mkv", [self.HEVC, self.AAC])
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertIn("scale=trunc(iw/2)*2:trunc(ih/2)*2", command)
        self.assertEqual(command[command.index("-c:a") + 1], "copy")


class Workflow(unittest.TestCase):
    def test_the_branch_goes_in_and_the_expressions_survive(self):
        text = share.WORKFLOW % "trunk"
        self.assertIn("branches: [trunk]", text)
        self.assertIn("${{ steps.deployment.outputs.page_url }}", text)


class SiteRemove(unittest.TestCase):
    """--rm on GitHub rewrites the history without the file, in a local repo with a local "origin"."""

    def test_what_is_left_keeps_the_date_it_went_up(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        quiet = dict(check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(root / "origin.git")], **quiet)
        subprocess.run(["git", "clone", "-q", str(root / "origin.git"), str(root / "clone")], **quiet)
        clone = root / "clone"
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@example.com"], **quiet)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], **quiet)
        (clone / "README.md").write_text("site")
        for ident, name, when in (("aaaaaaaaaa", "a.png", 1767268800), ("bbbbbbbbbb", "b.png", 1769947200),
                                  ("cccccccccc", "c.png", 1772366400)):
            (clone / "f" / ident).mkdir(parents=True)
            (clone / "f" / ident / name).write_bytes(name.encode())
            subprocess.run(["git", "-C", str(clone), "add", "-A"], **quiet)
            env = dict(os.environ, GIT_AUTHOR_DATE="@%d" % when, GIT_COMMITTER_DATE="@%d" % when)
            subprocess.run(["git", "-C", str(clone), "commit", "-q", "-m", "add " + name], env=env, **quiet)
        subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], **quiet)
        site = share.Site.__new__(share.Site)
        site.clone, site.branch = clone, "main"
        with mock.patch.object(share, "say"):
            site.remove([e for e in site.files() if e.name == "b.png"], wait=False)
        self.assertEqual([(e.name, e.modified) for e in site.files()], [("a.png", 1767268800), ("c.png", 1772366400)])
        names = subprocess.run(["git", "-C", str(clone), "log", "--all", "--name-only", "--format="], text=True, **quiet).stdout
        self.assertNotIn("b.png", names)
        self.assertTrue((clone / "README.md").exists())


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
