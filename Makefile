# claudio — install, uninstall, test.
#
# claudio is one POSIX sh script, but it is NOT one file any more: `claudio
# usage` and `claudio recv` are Python that ships beside it. Copying only the
# script produces a claudio whose two telemetry verbs cannot find themselves.
# claudio does say so -- both verbs refuse by name and `claudio run` prints it
# on stderr -- but stderr a moment before Claude Code takes the terminal is not
# where anybody reads it, so in practice the symptom is an empty ledger. Hence
# an install target rather than a sentence in a readme, and `test.sh` runs this
# very file into a temporary DESTDIR and then runs what it produced.
#
#   make install                       # /usr/local
#   sudo make install                  # ...when /usr/local is not yours
#   make install PREFIX="$HOME/.local" # no sudo, no /usr/local
#   make install DESTDIR="$pkgdir"     # stage a package, prefix unchanged
#   make uninstall                     # removes exactly what install wrote
#   make check                         # the four suites
#
# PREFIX is baked into nothing: `_tool_path` in claudio finds its Python by
# walking from the resolved path of the running script, so the tree relocates.
#
# `server/` IS DELIBERATELY NOT INSTALLED BY ANY TARGET HERE. It is the
# receiving end of `logging=remote` -- a separate program, run on a receiving
# host and not on a developer's laptop -- and it is the only part of this
# project with a non-stdlib dependency (DuckDB). Installing it beside claudio
# would put that dependency in front of every user of a tool that has none, to
# ship a server almost none of them run; it is containerised separately and
# `server/README.md` is its install. A test asserts that neither this file nor
# the Homebrew formula ever names duckdb, because the cheap mistake here is to
# add it "for completeness" and hand every claudio user a build dependency.

# `?=`, `.PHONY` and tab recipes only: GNU make and bmake both take all three.
# Verified under GNU Make 3.81 (macOS). NOT run under bmake on any BSD -- said
# here rather than assumed, because "portable make" is a claim people make
# without testing it.
PREFIX  ?= /usr/local
DESTDIR ?=

BINDIR  = $(DESTDIR)$(PREFIX)/bin
LIBDIR  = $(DESTDIR)$(PREFIX)/libexec
LIBEXEC = $(LIBDIR)/claudio

# `libexec/claudio/`, not `libexec/`, and that is the layout claudio's own
# resolver looks for: <dir of the real claudio>/../libexec/claudio/<name>.
# The extra component keeps a shared /usr/local/libexec free of a dozen files
# named after nothing in particular.

INSTALL      ?= install
INSTALL_PROG ?= $(INSTALL) -m 755
INSTALL_DATA ?= $(INSTALL) -m 644

# `make check` only. PYTHON is here so the suites can be pointed at an
# interpreter that has the reconciler's engine without editing this file;
# SERVER_SUITE_ARGS is the escape hatch for a machine that has not got one.
PYTHON            ?= python3
SERVER_SUITE_ARGS ?=

all:
	@echo "claudio has no build step. Targets: install, uninstall, check."

install:
	$(INSTALL) -d "$(BINDIR)" "$(LIBEXEC)" "$(LIBEXEC)/cu" "$(LIBEXEC)/recv"
	$(INSTALL_PROG) claudio "$(BINDIR)/claudio"
	$(INSTALL_PROG) usage/claudio-usage "$(LIBEXEC)/claudio-usage"
	$(INSTALL_DATA) usage/cu/*.py "$(LIBEXEC)/cu/"
	$(INSTALL_PROG) usage/recv/otlp-recv usage/recv/synth-otlp "$(LIBEXEC)/recv/"
	@echo ""
	@echo "Installed:"
	@echo "  $(BINDIR)/claudio"
	@echo "  $(LIBEXEC)/  (claudio usage, claudio recv)"
	@echo ""
	@echo "Check it: claudio --version && claudio usage doctor"

# `install -d` creates $(BINDIR) and $(LIBDIR) when they are absent, so an
# uninstall that removed only the files left two empty directories behind.
# Staging into a DESTDIR is how a package is built, and there the orphans go
# into the package -- so in a stage both are removed IF EMPTY: `rmdir` refuses
# a directory with anything in it, which is exactly the test wanted.
#
# ONLY IN A STAGE, and that guard is the whole of this comment's point. The
# justification above is "remove exactly the two directories `install -d`
# created" -- but `install -d` does not create a directory that already exists,
# and at uninstall time nothing here can tell the two cases apart. Unguarded,
# this removed any empty bin/ or libexec/ under the prefix, including one the
# user made themselves: `mkdir -p ~/.local/bin` (which readme.md recommends by
# name, and tells the reader to put on their $PATH), then `make install
# PREFIX=~/.local` and `make uninstall PREFIX=~/.local`, and BOTH ~/.local/bin
# and ~/.local/libexec were gone. A live prefix is therefore never pruned; the
# orphan-in-a-package problem only exists in a stage, and the stage is the only
# place where nothing of anybody's can be standing in a directory we created.
#
# $(PREFIX) itself and everything above it are deliberately left alone even in
# a stage. `mkdir -p` may have created them too, but nothing here can tell
# which of a prefix's ancestors existed beforehand, and a `make uninstall` that
# removes /usr/local on a machine where it happened to be empty is a far worse
# bug than an empty directory in a staging tree.
uninstall:
	rm -f "$(BINDIR)/claudio"
	rm -rf "$(LIBEXEC)"
	@[ -z "$(DESTDIR)" ] || rmdir "$(LIBDIR)" 2>/dev/null || :
	@[ -z "$(DESTDIR)" ] || rmdir "$(BINDIR)" 2>/dev/null || :

# `make check` runs the suites. Both shells, every time: /bin/sh on macOS is
# bash 3.2, so `sh test.sh` alone proves nothing about the no-bash-isms rule.
# dash is a hard requirement of this target rather than a skip — a suite that
# quietly halves itself when a dependency is missing is the failure this
# project is most careful about.
#
# `server/` is not installed, but it IS tested: its suite is the reconciler's,
# and skipping it here would mean the only way to run it is to know it exists.
# That suite needs an engine this Makefile deliberately never names, and until
# recently it SELF-SKIPPED 44 of its 192 test functions -- 918 assertions --
# printed a banner saying the run was not evidence about the engine, and exited
# 0. So `make check` exited 0 too, over 918 assertions that never ran, on the
# one command this project documents as the release gate. A banner on stdout is
# not what CI, `&&` chains and Make read.
#
# The ruling now lives in the suite rather than in a flag here, which is the
# right place for it: every caller inherits it, including a human running the
# file directly, and this file stays free of the dependency's name. It is the
# same ruling `claudio usage doctor` follows -- every WARN it prints sets a
# non-zero exit, because it used to print `WARN ledger rows: 0` and exit 0.
#
# The escape hatch is one word, and the suite's own banner names the two ways
# to supply the engine, so the refusal arrives with its remedy:
#
#   make check SERVER_SUITE_ARGS=--allow-skips
check:
	bash test.sh
	dash test.sh
	$(PYTHON) usage/tests/test_all.py
	$(PYTHON) server/tests/test_all.py $(SERVER_SUITE_ARGS)

# The name this target had first. Kept so nobody's habit breaks.
test: check

.PHONY: all install uninstall check test
