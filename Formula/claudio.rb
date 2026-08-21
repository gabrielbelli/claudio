# Homebrew formula for claudio.
#
# **NOT INSTALLED, and deliberately said so at the top.** No tag exists yet, so
# there is nothing for `url` to point at and this formula has never been run
# through `brew install`. What HAS been verified is the layout it produces:
# `make install PREFIX=…` was staged and the installed `claudio`, `claudio
# usage` and `claudio recv` were all run from it, including through a symlink
# standing in for a Homebrew shim, with a decoy of each Python entry point at
# the front of `$PATH` to prove the resolution was the packaged tree and not a
# bare name — and with the packaged tree moved aside as a negative control, to
# prove the decoy was reachable at all. `test.sh` pins all of that.
#
# `ruby -c` and `brew style` HAVE been run against this file, and `brew style`
# is the useful half of `brew audit` here: on Homebrew 6 `brew audit` refuses a
# path outright ("Calling `brew audit [path ...]` is disabled") and wants a
# formula that is already in a tap, which does not exist until the first push.
#
# `brew style Formula/claudio.rb` today reports EXACTLY THREE offences, all of
# them on the `sha256` line and all three the same fact: the placeholder is not
# a hash. That is the intended failure and it clears itself the moment the real
# checksum is written — so three is the number to expect before a tag, zero is
# the number to expect after one, and anything else is a regression. Run
# `brew audit --strict --new claudio` once the formula is in a tap.
#
# It lives at `Formula/claudio.rb`, the location Homebrew looks for, and that
# is load-bearing twice. It keeps the formula beside the tree it installs, so
# the two cannot drift; and a repository with a top-level `Formula/` IS a tap,
# so `brew tap gabrielbelli/claudio https://github.com/gabrielbelli/claudio`
# works against this repository directly, with no second repository involved.
# Copy it to `homebrew-tap/Formula/claudio.rb` as well when you cut a release:
# that tap is where `claude-statusline` already lives and is the address the
# documentation gives.
#
# TWO fields have to be set at tag time and there is no way to compute them
# before the tag exists:
#
#   1. the `url`, which must name the tag you actually pushed;
#   2. the `sha256` of that tarball:
#
#        curl -sL "$URL_BELOW" | shasum -a 256
#
# The command is written with a variable rather than a literal tag URL on
# purpose: `claudio update` reads a tag out of THIS FILE with a regex, and a
# second tarball URL in a comment is a second answer it could pick up.
#
# The placeholder below is deliberately not a valid-looking hash. Homebrew
# refuses a checksum mismatch loudly, which is the right failure, but a
# plausible-looking wrong hash reads as a hash somebody checked — and a
# 64-character placeholder would also pass `brew style` in silence, which is
# the same defect one tool along.
#
# `claudio update` reads this file out of `<brew prefix>/Library/Taps/*/*/` to
# work out what version the tap offers. It looks for an explicit version line,
# then for a git tag on the url, then for the tag inside a release tarball
# path — so the url shape below is what makes the update check work here with
# no version line of its own.
#
# Those three patterns are matched with `sed` over the whole file, comments
# included, so DO NOT WRITE ANY OF THEM OUT AS AN EXAMPLE IN THIS FILE. An
# earlier draft of this very comment spelled the middle one literally, and
# `claudio update` then reported the tap as offering version "x".

class Claudio < Formula
  # `rewrite_shebang` / `detected_python_shebang` live here.
  include Language::Python::Shebang

  desc "Claude Code profile and account manager, with usage recording"
  homepage "https://github.com/gabrielbelli/claudio"
  url "https://github.com/gabrielbelli/claudio/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_THE_TARBALL_SHA256_AT_TAG_TIME"
  license "BSD-2-Clause"
  head "https://github.com/gabrielbelli/claudio.git", branch: "master"

  # The status line is how claudio records stream B: `claudio statusline
  # --shim` captures the plan percentages and forwards the bytes to the
  # renderer named by `statusline=`. Recording works with no renderer at all
  # (a shim with nothing to forward to prints zero bytes, which is a
  # legitimate silent sensor), so this is not required for correctness — it is
  # here because the renderer is the reason most people set the status line up,
  # it lives in this same tap, and a `statusline=claude-statusline` line in the
  # documentation should not name a command the user has to go and find.
  # Fully qualified rather than bare: this file is also installable as a tap of
  # its own (see the top), where a bare name would not resolve.
  depends_on "gabrielbelli/tap/claude-statusline"

  # `jq` is what plan-usage recording and mixed-mode MCP need. claudio guards
  # every use of it — a missing `jq` costs a usage sample, never a command —
  # but a package manager is exactly the place to stop that trade being made.
  # It is declared here even though `claude-statusline` also pulls it in:
  # claudio's own uses of `jq` have nothing to do with any renderer, and a
  # dependency held only through a third party is one somebody later removes.
  depends_on "jq"

  # Python 3 is what `claudio usage` and `claudio recv` are written in, and
  # they are standard library only: no pip, no venv, no `resources` block.
  #
  # DECLARED RATHER THAN ASSUMED, deliberately, and the alternative was real:
  # relying on the system interpreter costs nothing on a Mac today and is a
  # guess about every machine. macOS ships a `python3` that Apple has removed
  # once already, and a claudio whose two telemetry verbs stop working on an OS
  # upgrade would fail exactly the way this project cares most about — `claudio
  # run` still launches, stream A silently stops being recorded. A pinned brew
  # Python cannot be taken away underneath it. The cost is a formula dependency
  # for a stdlib-only program, which is the smaller of the two prices.
  depends_on "python@3.12"

  def install
    # `make install` and not a hand-written copy list, so the formula and the
    # Makefile cannot disagree about which files claudio needs to find itself.
    system "make", "install", "PREFIX=#{prefix}"

    # The three Python entry points ship as `#!/usr/bin/env python3`, which is
    # right in a checkout and NOT a guarantee under Homebrew: `python@3.12` is
    # versioned, so it puts `python3.12` on the PATH and only the current
    # default python is linked as `python3`. Left alone, this would work on
    # most machines and fail on some, which is the worst of the three outcomes.
    # Point them at the interpreter brew actually installed.
    rewrite_shebang detected_python_shebang(self, use_python_from_path: false),
                    libexec/"claudio/claudio-usage",
                    libexec/"claudio/recv/otlp-recv",
                    libexec/"claudio/recv/synth-otlp"
  end

  def caveats
    <<~EOS
      claudio keeps its profiles, accounts and config under ~/.claudio.

      First run:
        claudio account new work      # an isolated login
        claudio run --account work

      Telemetry is off until you ask for it:
        echo logging=local >> ~/.claudio/claudio.conf

      The status line, whose renderer came with this formula:
        echo statusline=claude-statusline >> ~/.claudio/claudio.conf
        claudio statusline --all

      `server/` is not installed by this formula: it is the receiving end of
      `logging=remote`, runs on a receiving host rather than here, and is
      containerised separately. See server/README.md in the repository.
    EOS
  end

  test do
    # A version shape, NOT `version.to_s`. On a stable build the two agree; on
    # a `--HEAD` build Homebrew's `version` is `HEAD-<sha>` while the script
    # prints the `VERSION` baked into it, so comparing them would fail a
    # formula that is working perfectly -- and the head stanza is the only one
    # installable until a tag exists.
    assert_match(/\A\d+\.\d+\.\d+/, shell_output("#{bin}/claudio --version").strip)

    # THE ASSERTION THIS BLOCK EXISTS FOR: the shell script finding the Python
    # that ships beside it. This is precisely what a single-file install got
    # wrong, and it is invisible from `claudio help`, which lists both verbs
    # whether or not either can run. `shell_output` raises on a non-zero exit,
    # so an unresolved entry point fails here rather than being reported --
    # both verbs refuse by name and exit 1 when `_tool_path` comes up empty.
    assert_match "claudio usage", shell_output("#{bin}/claudio usage --help")
    assert_match "claudio recv", shell_output("#{bin}/claudio recv --help")

    # ...and that the resolution reached the PACKAGED tree rather than
    # something on PATH: these are the files `make install` wrote.
    assert_path_exists libexec/"claudio/claudio-usage"
    assert_path_exists libexec/"claudio/recv/otlp-recv"
  end
end
