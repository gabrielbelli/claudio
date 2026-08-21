# Testing the pre-release

Thank you for trying this. It is **pre-release**: no tag, no Homebrew, install
by hand. What is asked of you is to install it, work normally for a few days,
and say what broke.

## Install

```sh
git clone -b pre-release https://github.com/gabrielbelli/claudio
cd claudio
sudo make install                      # /usr/local, or:
make install PREFIX="$HOME/.local"     # no sudo; put ~/.local/bin on $PATH
claudio --version
```

Nothing else is needed. `claudio` is one POSIX `sh` script and the Python it
drives is stdlib only, so there is nothing to `pip install` and no virtualenv.
Python 3.9 or newer.

`make uninstall` removes exactly what `make install` wrote.

> **macOS is well tested. Linux is not.** No part of this has been run on Linux
> by anyone. The install is portable by construction -- no GNU-only `make`
> syntax, and BSD `install(1)` is the stricter of the two and works -- but that
> is an argument, not a measurement. If you are on Linux you are the first, and
> that is the single most useful thing you can report.

## Turn recording on

Two lines in `~/.claudio/claudio.conf`:

```conf
logging=local
statusline=claude-statusline
```

The second is optional and only if you have
[claude-statusline](https://github.com/gabrielbelli/claude-statusline). Do not
set `otel_endpoint` or `otel_protocol` -- the defaults already match the
receiver, and an old `otel=1` / `usage=1` pair is deprecated and will warn.

Then work normally, launching through `claudio run` rather than `claude`.

## Look at it

```sh
claudio usage show --by account
claudio usage show --by model
claudio usage doctor
```

> **`doctor` exits non-zero while anything warrants attention**, and before your
> first session that includes "you have recorded nothing yet". A `WARN` on a
> fresh install is expected, not a fault. It is deliberate: the previous
> version printed `WARN ledger rows: 0` and exited 0, which is how a broken
> setup looked healthy.

## What runs on your machine

Nothing, when you are not working. There is no daemon, no container, no service
and nothing at boot.

`claudio run` spawns a receiver on loopback before launching Claude Code, the
status line keeps it alive, and it exits by itself 30 minutes after your last
session. Everything is written to `~/.claudio-usage/` as append-only JSON lines.

**Nothing leaves your machine.** `logging=local` writes files and posts them
nowhere. Shipping to a server is `logging=remote` plus an explicit destination,
and it is not part of this test.

## What to report

Most useful first:

1. **It did not install.** The exact command and the exact error.
2. **It installed and a verb failed.** `claudio usage …` or `claudio recv …`
   especially -- those are new, and finding them is the point of this release.
3. **It recorded nothing.** Run `claudio usage doctor` and send the whole
   output. It compares what claudio exports against what the receiver listens
   on and names a mismatch.
4. **A number looks wrong.** Say which and what you expected. This matters more
   than a crash: a wrong figure that looks plausible is the failure this whole
   project is built to prevent.
5. **Anything that read as broken but was not.** Confusing output is a defect
   here.

`claudio usage doctor` and `claudio --version` in the report save a round trip.

## What it collects, if you later ship it anywhere

Not part of this test, but so you know what you would be agreeing to: per
request, the model, token counts, notional cost, duration, session and prompt
ids. Per account, the address you log in with, your machine's short hostname
and the project directory.

**Never prompts or responses.** The raw capture that could contain them is
excluded from shipping structurally, and a test asserts it.
