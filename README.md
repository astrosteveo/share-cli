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
commit and one Pages build.

The local clone is a cache in `~/.cache/share/share`. It is reset to the remote on every run and
holds no work of its own.

## Where the time goes

A first upload takes 20 to 40 s. Nearly all of that is GitHub's Pages build, which `share` waits
for so the link works the moment it prints. The rest (hashing, one `gh api` call, a commit and a
push) is about a second. Re-sharing a file that is up takes about 1 s.

So a faster script alone will not make links appear sooner. Ideas, roughly by payoff:

- **Skip the build.** Put files on a host that serves them at once: an S3-compatible bucket
  (Cloudflare R2, Backblaze B2) behind a public URL, or your own box. This is the only big win.
  It trades the free GitHub hosting for an account and a bill, and drops the 100 MB and 1 GB limits.
- **Link first, wait in the background.** Print the link as soon as the push lands and let the build
  finish on its own, with `--no-wait` as the default. The link is dead for 30 s; the prompt is
  back at once.
- **Trim the fixed costs.** Cache the GitHub login instead of calling `gh api user` each run, and
  fetch with `--depth 1`.
- **Make it a shell script.** It would start faster than Python, but starting is not the cost.

## Limits

100 MB a file (git refuses more), about 1 GB for the whole site, about 10 Pages builds an hour.
The repo is public: anyone can browse it. `--rm` rewrites the repo into one commit and
force-pushes, but GitHub may serve the old commit by its SHA for a while.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

They cover name cleaning, size messages and the checks that run before anything is uploaded. None
of them touch the network.

## Licence

MIT. See `LICENSE`.
