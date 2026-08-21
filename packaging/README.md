# Packaging claudio

claudio is a POSIX `sh` script plus a standard-library Python tree it drives.
There is nothing to compile, nothing to fetch and no dependency to vendor, so
packaging it is a copy — but it is a copy of **two** things, and that is the
whole reason this directory exists.

## The interface

```sh
make install DESTDIR="$pkgdir" PREFIX=/usr
```

That is the only contract. It writes exactly:

| path | mode | what |
|---|---|---|
| `$PREFIX/bin/claudio` | 755 | the script |
| `$PREFIX/libexec/claudio/claudio-usage` | 755 | `claudio usage` |
| `$PREFIX/libexec/claudio/cu/*.py` | 644 | the modules both Python entry points import |
| `$PREFIX/libexec/claudio/recv/otlp-recv` | 755 | `claudio recv` |
| `$PREFIX/libexec/claudio/recv/synth-otlp` | 755 | a development tool, not a verb |

`make uninstall` removes precisely those and nothing else. In a `DESTDIR`
stage it also removes `$PREFIX/bin` and `$PREFIX/libexec` **if they end up
empty**, so a built package carries no orphan directories; against a live
prefix it never touches either, because `install -d` does not create a
directory that already exists and at uninstall time nothing can tell the
two apart — on the `PREFIX=$HOME/.local` route those are the user's own.

**Nothing is baked in.** `PREFIX` is not written into any file: claudio finds
its Python by walking out from the *resolved* path of the running script
(`<dir>/../libexec/claudio/…`), so the tree relocates and a symlink into it —
which is what Homebrew and most distributions create — resolves correctly.

## What must not be split

`bin/claudio` alone is not claudio. `claudio usage` and `claudio recv` are
subcommands implemented by the files under `libexec/claudio/`, and `claudio
recv` is the receiver telemetry is exported **to**. A package that ships only
the script produces a tool whose two telemetry verbs refuse by name and whose
ledger then stays empty. Do not split this into `claudio` and `claudio-usage`
subpackages; if you must, make the split a hard dependency in both directions.

## Dependencies

| | |
|---|---|
| **required** | POSIX `sh`; `python3` 3.9 or newer (standard library only — no pip, no venv, no wheels) |
| **required at runtime, not by the package** | [Claude Code](https://code.claude.com) — claudio launches `claude` and does not bundle it |
| **optional** | `jq` (plan-usage recording, MCP mixed mode), `git` (`claudio update`), Docker (MCP Docker mode), any status-line renderer |

Every optional dependency is guarded in claudio itself: a missing one costs a
feature and never a command. Declaring `jq` a hard dependency is reasonable and
is what the Homebrew formula does.

**Do not add DuckDB.** It is the project's only non-stdlib dependency and it
belongs to `server/`, which is a separate program with its own container and is
**not** installed by `make install`. A test asserts that neither the `Makefile`
nor the formula ever names it.

## The Homebrew formula

`Formula/claudio.rb` is the tap's formula, kept in this repository so it cannot
drift from the tree it installs. It calls `make install` rather than listing
files of its own — two lists of what claudio consists of is how a new module
reaches a checkout and not a package.

It is at `Formula/claudio.rb` and not under this directory, because a top-level
`Formula/` is all Homebrew needs to treat a repository as a tap. So the same
file serves both routes: copied into `gabrielbelli/homebrew-tap` for
`brew install gabrielbelli/tap/claudio`, and used in place by

```sh
brew tap gabrielbelli/claudio https://github.com/gabrielbelli/claudio
brew install --HEAD gabrielbelli/claudio/claudio
```

which is the only one that works before a tag exists. Its dependency on
`claude-statusline` is written fully qualified for exactly that reason: a bare
name resolves inside `homebrew-tap` and not inside a tap of this repository.

`make check` is what the suites are run by, and it names both shells and both
Python suites. `server/`'s suite is included there and `server/` is still not
installed — the two are independent, and running a suite for something you do
not ship is the right way round.

**`make check` needs DuckDB, and `make install` does not.** That is not a
contradiction and it is worth stating once: the reconciler's suite refuses to
report success over a run in which it self-skipped 44 of its 192 test functions,
which is 918 assertions. Nothing that gets *packaged* has ever needed the
engine. On a build host without one, `make check SERVER_SUITE_ARGS=--allow-skips`
runs everything else and says out loud what it did not check — but a release is
cut from a run that checked all of it.

Two fields are set at tag time and cannot be computed before the tag exists: the
`url` and its `sha256`. The formula says so at the top, and it has **never been
run through `brew install`**, because there is no tag to install from.

`ruby -c` and `brew style` have been run against it. `brew style` is the half of
`brew audit` reachable today: on Homebrew 6 `brew audit` refuses a path outright
(*"Calling `brew audit [path ...]` is disabled"*) and wants a formula that is
already in a tap, which does not exist until the first push. `brew style` today
reports **exactly three offences, all on the `sha256` line** — the placeholder is
not a hash, said three ways. That is the intended failure and it clears itself
when the real checksum is written, so three is the number to expect before a tag
and zero after one. Anything else is a regression.

```sh
ruby -c Formula/claudio.rb
brew style Formula/claudio.rb        # 3 offences until the tag, 0 after
brew audit --strict --new claudio    # once it is in a tap
```

## Verifying a package

`test.sh` runs the real `Makefile` into a temporary `DESTDIR` and then runs the
installed `claudio usage` and `claudio recv` out of the result — with a decoy of
each Python entry point at the front of `$PATH`, so a resolver that fell through
to a bare name is caught rather than congratulated, and once more through a
symlink standing in for a package manager's shim. By hand, against a real
package:

```sh
claudio --version
claudio usage --help          # must print "claudio usage", not "not found"
claudio recv --help           # must print "claudio recv"
claudio usage doctor          # exits non-zero on a fresh machine, by design
```

The first two are the ones that catch a split package. `doctor` exits 1 until
recording is configured, which is correct: every WARN it prints sets the exit
status.
