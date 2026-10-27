# share-cli

`share PATH` puts a picture or video on the web and prints a link to paste. One Python file, no
dependencies beyond `python3`, `git` and the GitHub CLI.

```sh
share easel-teaser.mp4 hero.png     # upload, wait until the links work, print one link per file
share --list                        # what is up, with links
share --rm LINK_OR_NAME             # take files down
share --help                        # limits and caveats
```

## Install

```sh
git clone git@github.com:astrosteveo/share-cli.git ~/Projects/share-cli
ln -sf ~/Projects/share-cli/share ~/.local/bin/share
gh auth login                       # once
```

Edit `share` in the repo and the command changes with it. On Wayland, `wl-copy` puts the links on
the clipboard.

## How it works

Files go into a public repo of your own, `<you>/share`, made on first use. GitHub Pages serves it
at `https://<you>.github.io/share/f/<id>/<name>`, with the right content type, so a video plays in
the browser. (Release assets were tried first and force a download.) The id is the start of the
file's SHA-256, so the same file always gets the same link. Several paths in one call are one
commit and one Pages deploy.

Pages deploys with a one-job Actions workflow, `.github/workflows/pages.yml`, that `share` writes
into the repo. A repo made by an older `share`, which used Pages' own branch build, is switched
over on the next run; that one run is slow, because two deploys run back to back.

The local clone is a cache in `~/.cache/share/share`. It is reset to the remote on every run and
holds no work of its own.

## Where the time goes

A new upload usually takes 17 to 23 s, and now and then over 30 s. Nearly all of that is the Pages
deploy, which `share` waits for so the link works the moment it prints. The rest (hashing, a
fetch, a commit and a push of a 1 MB file) is about 2 s. Re-sharing a file that is up takes under
1 s.

The deploy, from the push, over six runs each:

| Part                                                 | Pages' branch build (two jobs) | One-job workflow |
| ---------------------------------------------------- | ------------------------------ | ---------------- |
| Runners start, check out, upload, reach the deploy   | 17 to 20 s                     | 8 to 12 s        |
| GitHub copies the site and purges the CDN            | 6 to 26 s                      | 4 to 25 s        |

The second row is on GitHub's side and swings the most. `share` asks for the deploy status by
commit once a second and prints as soon as it says `succeed`, which comes after the CDN purge. It
never asks for the link itself before that: the CDN keeps a 404 it has served (45 s and more in a
test), and one fetched just before the purge could outlast it.

Ideas for going further, roughly by payoff:

- **Skip the deploy.** Put files on a host that serves them at once: an S3-compatible bucket
  (Cloudflare R2, Backblaze B2) behind a public URL, or your own box. This is the only big win
  left. It trades the free GitHub hosting for an account and a bill, and drops the 100 MB and
  1 GB limits.
- **Link first, wait in the background.** Print the link as soon as the push lands and let the
  deploy finish on its own, with `--no-wait` as the default. The link is dead for about 20 s; the
  prompt is back at once.

## Limits

100 MB a file (git refuses more) and about 1 GB for the whole site. Pages' limit of 10 builds an
hour does not apply to a site deployed by its own workflow. The repo is public: anyone can browse it. `--rm` rewrites the repo into one commit and
force-pushes, but GitHub may serve the old commit by its SHA for a while.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

They cover name cleaning, size messages and the checks that run before anything is uploaded. None
of them touch the network.

## Licence

MIT. See `LICENSE`.
