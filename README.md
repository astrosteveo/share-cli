# share-cli

`share PATH` puts a picture or video on the web and prints a link to paste. One Python file, no
dependencies beyond `python3`, plus either a Cloudflare R2 bucket or `git` and the GitHub CLI.
`exiftool`, `ffmpeg` and `wl-clipboard` add to it when they are installed.

```sh
share easel-teaser.mp4 hero.png     # upload, wait until the links work, print one link per file
share                               # share the screenshot, or the files, on the clipboard
grim - | share -                    # share what is piped in
share --expire 7d clip.mp4          # R2 deletes it 7 days from now
share --mp4 phone.mov               # convert video browsers may not play to mp4 first
share --list                        # what is up, newest first, with dates and links
share --rm LINK_OR_NAME             # take files down
share --setup                       # store Cloudflare R2 settings; from then on files go there
share --help                        # limits and caveats
```

Pictures and videos go up without the place, time and camera they were taken with. The
orientation and colour profile stay, so they look the same. A RAW photo keeps its date and camera,
which raw converters need to read it. `--keep-metadata` leaves a file as it is.

## Install

```sh
git clone git@github.com:astrosteveo/share-cli.git ~/Projects/share-cli
ln -sf ~/Projects/share-cli/share ~/.local/bin/share
```

Then set up one of the two places files go. Edit `share` in the repo and the command changes with
it. On Wayland, `wl-copy` puts the links on the clipboard.

### Cloudflare R2 (a link works the moment the upload ends)

You need a domain on Cloudflare. In the Cloudflare dashboard:

1. **R2 → Create bucket**, named `share`.
2. **The bucket → Settings → Custom Domains → Connect Domain**, such as `share.example.com`. Wait
   until it shows Active; Cloudflare makes the DNS record and the certificate.
3. **R2 object storage → Overview → API Tokens: Manage → Create Account API token**, with
   **Object Read & Write** on that bucket only. The next page shows the S3 endpoint, the Access
   Key ID and the Secret Access Key; the secret is shown only once.

Then run `share --setup` in a terminal and give it the S3 endpoint, the Access Key ID and the
Secret Access Key from the token page, and the custom domain. It checks them with one list of the
bucket and saves them in `~/.config/share/r2.json`, readable by you only.

For `--expire`, the bucket needs a lifecycle rule for each number of days. `share` adds it the first
time with the Cloudflare CLI (`cf`, logged in with `cf auth login`), because the share token may not
change bucket settings. Without `cf`, add it in the dashboard instead: **the bucket → Settings →
Object Lifecycle Rules → Add rule**, prefix `7d/`, delete objects 7 days after upload. Then run
`SHARE_EXPIRE_RULE_SET=1 share --expire 7d FILE` once, so `share` records that the rule is there.

Optionally, so that `--rm` takes a link down at once, make one more token: **Manage Account →
Account API Tokens → Create Token**, with **Zone → Cache Purge** on your domain's zone. Give it and
the zone's ID (on the domain's Overview page) to `share --setup`.

R2's free tier (10 GB stored, 1 million uploads and 10 million reads a month, no charge for
downloads) covers sharing clips with friends.

### GitHub Pages (free, no domain, about 20 s a link)

```sh
gh auth login                       # once
```

With no R2 settings, files go to GitHub. `SHARE_BACKEND=github` picks GitHub even when R2 is set
up, for example to list or take down what is still there. GitHub cannot delete a file on a timer,
so `--expire` needs R2.

## How it works

Both places serve a file at `<base>/f/<id>/<name>` with the right content type, so a video plays
in the browser. The id is the start of the SHA-256 of the bytes that go up, after any stripping or
converting, so the same file always gets the same link. Files shared with `--expire 7d` go in
`7d/` rather than `f/`, and a lifecycle rule on the bucket deletes what is in `7d/` seven days
after upload. R2 may take up to a day longer.

**Before an upload.** A copy of each picture or video is made in `~/.cache/share/` and deleted
when `share` ends. `exiftool` strips a picture when it is installed, keeping only the orientation
and colour profile. It handles nearly every picture format, camera RAW included, though in a RAW
photo it removes only the place: the date and camera stay, because raw converters need the rest.
Without it, `share` strips JPEG and PNG itself: it keeps the picture data, the colour profile and
an Exif block holding only the orientation, and drops everything else, including the extra
pictures some phones add after the end. Other formats then go up as they are, with a note.

`ffmpeg` strips a video by copying its picture and sound into a new file, encoding nothing again.
Everything else stays behind: metadata, the file's dates (they come out as zero), and other
tracks, such as the GPS route drones and action cameras record beside the video. Camcorder
`.mts` and dashcam `.ts` files keep the time and place inside the H.264 stream, in SEI messages,
and those go too, with `--mp4` as well.
The rotation stays. Without `ffmpeg`, a video goes up as it is, with a note.

`--mp4` also runs `ffmpeg`. A video stream that is already H.264 is copied rather than encoded
again, other video is encoded with `libx264`, and sound that is not AAC becomes AAC. The mp4 has
its index at the start, so it plays while it loads, and no metadata.

ffmpeg decides how to read a file from what is in it, not from its name, and some formats it
reads, such as playlists, pull in other files. So `share` first asks `ffprobe` what the file is,
and has `ffmpeg` read it only as a video container (`mov`, `matroska`, `avi`, `mpegts` and a few
more), from local files only. A playlist named `funny.mov` that lists another of your videos is
refused, not uploaded with that video inside.

**Input.** `share` with no path reads the Wayland clipboard with `wl-paste`. Files copied in a
file manager (`text/uri-list`) are shared by path. Otherwise a picture on the clipboard is saved
as `pasted-<date>-<time>.png`. `share -` saves stdin the same way, guessing the extension from
the first bytes.

**R2.** `share` talks to the bucket's S3 API itself, signing each request with AWS Signature
Version 4, so it needs no SDK. It asks whether `f/<id>/<name>` is there, puts it if not, and
checks the public link once. Up to four files go up at once, and big uploads show a progress
line. A file over 100 MB goes up in 16 MB parts with S3's multipart upload. If a run stops
partway, sharing the file again asks R2 for the unfinished upload and sends only the missing
parts. R2 keeps unfinished parts for 7 days. A dropped connection or an error on R2's side is
tried again up to four times. Each file has a `Content-Disposition` header naming its original
file, so a saved copy keeps its real name. Files are served with `Cache-Control: public, max-age=3600`: a link
always names the same bytes, and the hour bounds how long Cloudflare's cache serves a file after
`--rm` when there is no purge token. With one, `--rm` purges the removed links, and an upload
purges the new ones, because Cloudflare also caches a 404 and would otherwise keep a file shared
again after `--rm` dead for a few minutes. Pictures, video, sound and PDFs keep their own type. Any other file that reads as UTF-8
text, such as Markdown, code or HTML, is served as plain text, so every browser shows it instead
of downloading it, and an HTML file never runs as a page on your domain.

**GitHub.** Files go into a public repo of your own, `<you>/share`, made on first use, and
GitHub Pages serves it at `https://<you>.github.io/share/`. (Release assets were tried first and
force a download.) Several paths in one call are one commit and one Pages deploy. Pages deploys
with a one-job Actions workflow, `.github/workflows/pages.yml`, that `share` writes into the repo.
A repo made by an older `share`, which used Pages' own branch build, is switched over on the next
run; that one run is slow, because two deploys run back to back. The local clone is a cache in
`~/.cache/share/share`. It is reset to the remote on every run and holds no work of its own.

## Where the time goes

**R2:** there is no build. A new 1 MB picture takes about 0.9 s, from start to a link that
answers: one request asks whether the file is there, one puts it, and one checks the public link.
The cache purge, when set up, adds about 0.35 s. Stripping a picture's metadata with exiftool
adds about 0.2 s, and stripping a video with ffmpeg about 0.1 s per 100 MB. A bigger
file adds only its upload time: a 51 MB clip took 5.1 s on an 85 Mbit/s uplink. Re-sharing a file
that is up takes under 1 s.

**GitHub:** a new upload usually takes 17 to 23 s, and now and then over 30 s. Nearly all of that is
the Pages deploy, which `share` waits for so the link works the moment it prints. The rest
(hashing, a fetch, a commit and a push of a 1 MB file) is about 2 s. Re-sharing a file that is up
takes under 1 s. The deploy, from the push, over six runs each:

| Part                                                 | Pages' branch build (two jobs) | One-job workflow |
| ---------------------------------------------------- | ------------------------------ | ---------------- |
| Runners start, check out, upload, reach the deploy   | 17 to 20 s                     | 8 to 12 s        |
| GitHub copies the site and purges the CDN            | 6 to 26 s                      | 4 to 25 s        |

The second row is on GitHub's side and swings the most. `share` asks for the deploy status by
commit once a second and prints as soon as it says `succeed`, which comes after the CDN purge. It
never asks for the link itself before that: the CDN keeps a 404 it has served (45 s and more in a
test), and one fetched just before the purge could outlast it.

## Limits

**R2:** 5 TB a file. Anyone with a link can open the file, but nobody can browse the
bucket. `--rm` deletes at once; with a purge token Cloudflare stops serving the file at once too,
and without one its cache may serve it for up to an hour. Either way, a browser that opened it may
keep it for up to 4 hours (the zone's Browser Cache TTL, which Cloudflare uses when it is longer
than the file's own hour).

**GitHub:** 100 MB a file (git refuses more) and about 1 GB for the whole site. Pages' limit of 10
builds an hour does not apply to a site deployed by its own workflow. The repo is public: anyone
can browse it. `--rm` rewrites the repo's history without the file and force-pushes, keeping
the date each other file went up, but GitHub may serve the old commits by their SHA for a while.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

They cover name cleaning, size messages, the checks that run before anything is uploaded, the R2
settings, request signing (against cases from AWS's SigV4 test suite), reading bucket lists,
multipart uploads and resuming them, retries, expiry rules, metadata stripping (on JPEGs and PNGs
built byte by byte, and the exiftool and ffmpeg commands for other pictures, RAW photos and
videos), refusing playlists named like videos, `--mp4`'s choices, the clipboard and stdin, `--rm`'s question and the wait for
Pages. None of them touch the network or need `exiftool` or `ffmpeg`, and none read your real R2
settings.

## Licence

MIT. See `LICENSE`.
