#!/bin/sh
# The proxy image's entrypoint, shared by its Linux and FreeBSD builds.
#
# IT DOES ONE THING BEFORE IT EXECS NGINX: GUARANTEE A CERTIFICATE EXISTS.
#
#   nginx EXITS at startup when `ssl_certificate` names a file that is not
#   there.  That single fact is why TLS used to be an example file the operator
#   mounted rather than a shipped configuration -- and it is why the shipped
#   configuration is safe now: this script makes the file exist first, so the
#   secure default is the one you get on a fresh clone with no certificate
#   anywhere.
#
#   A generated certificate is SELF-SIGNED and is announced as such, at length,
#   on every start.  It is not pretended to be anything else and it is not
#   quietly reused for ever: the point is that `docker compose up` works today,
#   and that the sentence telling you to replace it is in front of you every
#   time the stack starts rather than in a README.
#
# ONE PLACE FOR THE CERTIFICATE AND THE DOMAIN, AND THIS IS IT.
#
#   CLAUDIO_TLS_DIR   where the certificate lives (default /etc/claudio/tls).
#                     `claudio-tls.conf` names /etc/claudio/tls/fullchain.pem
#                     and privkey.pem, so moving this means moving the mount,
#                     not editing nginx.
#   CLAUDIO_DOMAIN    the name a GENERATED certificate is for (default
#                     localhost).  nginx itself uses `server_name _` and does
#                     not care; this is the CN and the subjectAltName, and it
#                     is the only place a domain is written down in this stack.
#
#   A REAL certificate ignores both beyond the directory: drop `fullchain.pem`
#   and `privkey.pem` in and nothing here runs at all.
#
# WHAT IT MUST NEVER DO
#
#   Overwrite a certificate that is already there, or carry on silently when it
#   cannot make one.  The first would replace a real certificate with a
#   self-signed one on a restart -- every client's trust broken by a container
#   coming back -- and the second would leave nginx to exit with a message
#   about a missing file, for a fault that is about `openssl` or a read-only
#   mount.

set -eu

TLS_DIR="${CLAUDIO_TLS_DIR:-/etc/claudio/tls}"
DOMAIN="${CLAUDIO_DOMAIN:-localhost}"
CERT="${TLS_DIR}/fullchain.pem"
KEY="${TLS_DIR}/privkey.pem"
DAYS="${CLAUDIO_TLS_DAYS:-825}"

# Escape hatch first, so it cannot be broken by anything below.
if [ "$#" -gt 0 ]; then
    case "$1" in
        -*)  : ;;
        *)   exec "$@" ;;
    esac
fi

say() { echo "[proxy] $*" >&2; }

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    say "TLS certificate ${CERT} is present; using it unchanged."
else
    # HALF A PAIR IS NOT A CERTIFICATE, and it is refused rather than
    # completed.  Generating the missing half would produce a key that does not
    # match a certificate somebody mounted on purpose, and nginx's complaint
    # about that ("key values mismatch") names neither file.
    if [ -f "$CERT" ] || [ -f "$KEY" ]; then
        say "REFUSING TO START -- exactly one of the two TLS files is present."
        say "  certificate: ${CERT}"
        say "  private key: ${KEY}"
        say "  Generating the other half would make a pair that does not"
        say "  match, and nginx's error for that names neither file. Mount"
        say "  both, or neither and a self-signed pair will be made."
        exit 10
    fi
    if ! mkdir -p "$TLS_DIR" 2>/dev/null; then
        say "REFUSING TO START -- cannot create ${TLS_DIR}."
        say "  Mount a writable directory there, or mount a certificate and"
        say "  key into it as fullchain.pem and privkey.pem."
        exit 10
    fi
    OPENSSL="$(command -v openssl 2>/dev/null || :)"
    if [ -z "$OPENSSL" ]; then
        # Named, and not left to nginx.  An operator who reads "cannot load
        # certificate" goes looking at their mount; the fault is that this
        # image has no openssl and therefore cannot make the default one.
        say "REFUSING TO START -- no certificate at ${CERT} and no openssl in"
        say "  this image to generate one with."
        say "  Mount a certificate and key there as fullchain.pem and"
        say "  privkey.pem, or generate a self-signed pair on the host:"
        say "    openssl req -x509 -newkey rsa:2048 -nodes -days ${DAYS} \\"
        say "      -subj /CN=${DOMAIN} -addext subjectAltName=DNS:${DOMAIN} \\"
        say "      -keyout privkey.pem -out fullchain.pem"
        exit 10
    fi
    say "no certificate at ${CERT} -- generating a SELF-SIGNED one for"
    say "  CN=${DOMAIN}, valid ${DAYS} days."
    # `-nodes` because there is nobody to type a passphrase to; a passphrase on
    # a key nginx must read unattended is a passphrase stored beside the key.
    if ! "$OPENSSL" req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
            -subj "/CN=${DOMAIN}" \
            -addext "subjectAltName=DNS:${DOMAIN},DNS:localhost,IP:127.0.0.1" \
            -keyout "$KEY" -out "$CERT" >/dev/null 2>&1; then
        say "REFUSING TO START -- openssl could not write the pair into"
        say "  ${TLS_DIR}. Is it mounted read-only?"
        exit 10
    fi
    chmod 600 "$KEY" 2>/dev/null || :
    say "  SELF-SIGNED. Every client will refuse it until you either trust it"
    say "  or replace it. It exists so that this stack starts on a fresh"
    say "  clone; it is not a certificate for anything real."
    say "  Replace it by mounting fullchain.pem and privkey.pem into"
    say "  ${TLS_DIR} -- nothing in the nginx configuration changes."
fi

# The binary, named candidates first and `command -v` last, for the reason the
# other two entrypoints give: PATH inside a container is whatever the base image
# happened to set, and a wrong `nginx` earlier on it would be found in silence.
# Alpine puts it at /usr/sbin/nginx; a FreeBSD image staged from `pkg` puts it
# at /usr/local/sbin/nginx.
NGINX=""
for c in /usr/sbin/nginx /usr/local/sbin/nginx; do
    if [ -x "$c" ]; then NGINX="$c"; break; fi
done
if [ -z "$NGINX" ]; then
    NGINX="$(command -v nginx 2>/dev/null || :)"
fi
if [ -z "$NGINX" ]; then
    say "no nginx in this image, at any of the paths this entrypoint knows"
    say "  or on PATH. The image is broken; this is not a configuration you"
    say "  can correct."
    exit 6
fi

# ---------------------------------------------------------------------------
# THE UPSTREAM WATCHER
# ---------------------------------------------------------------------------
#
# `claudio.conf` argues at length that the upstream addresses must NOT be
# deferred into a variable with a `resolver`, because a deferred lookup turns
# nginx's loud startup refusal into a steady silent 502.  That argument is
# correct and it stands.  What it addresses is only the STARTUP direction.
#
# The other direction was reproduced by running the stack.  nginx resolves each
# upstream name once, at configuration load, so an address that MOVES afterwards
# is never noticed.  Restarting one service alone reclaims the same address;
# restarting two at once races them, and on the third attempt of
# `docker compose restart ingest api` they swapped:
#
#     before: ingest=172.28.0.2 api=172.28.0.3
#     after : ingest=172.28.0.3 api=172.28.0.2
#
# nginx kept the old pair and both routes crossed.  The split is what keeps
# that honest -- `POST /sender` answered 503 `no-ship` and `GET /api/v1/*`
# answered 503 `no-view`, two named refusals with nothing written and nothing
# answered out of the wrong half -- but the stack is `restart: unless-stopped`,
# so a crashed or recreated backend reaches that state with nobody watching,
# and the documented remedy was a human typing `nginx -s reload`.
#
# So this resolves the three names on a timer and reloads nginx when any of
# them moves.  It is the "tiny sidecar" without the sidecar: no extra
# container, no supervisor, and nothing at all in the nginx configuration --
# the addresses are still resolved once per configuration load, exactly as
# that file requires, and this only decides WHEN a load happens.
#
# WHAT IT REFUSES TO DO.  It never reloads on a resolution FAILURE, only on a
# resolution that succeeded and DISAGREES with the last one.  A backend that is
# briefly down resolves to nothing, and treating that as a change would reload
# nginx into the very startup refusal it is meant to avoid -- turning a
# recoverable blip into a dead proxy.  It also never exits the container: a
# watcher that dies leaves the proxy working exactly as it did before this
# existed, so its own failure must not be fatal.  It says so, once, on stderr.
#
# CLAUDIO_PROXY_WATCH=0 turns it off.  It is ON by default because the
# alternative is a stack that crosses its own routes and waits for somebody to
# notice.
WATCH="${CLAUDIO_PROXY_WATCH:-1}"
WATCH_SECONDS="${CLAUDIO_PROXY_WATCH_SECONDS:-15}"
WATCH_NAMES="${CLAUDIO_PROXY_WATCH_NAMES:-ingest api mcp}"

# `getent hosts` and nothing else.  It is in glibc on Debian and in FreeBSD
# base, which is the whole of the set of bases this image is built from; `dig`,
# `host` and `nslookup` are in neither by default.  A base that turns out not
# to have it is NAMED rather than silently unwatched.
GETENT="$(command -v getent 2>/dev/null || :)"

_addrs() {
    # ONE LINE, `name=addr,addr;` per name, addresses SORTED so a
    # multi-address name does not read as a change every tick -- and one line
    # so that "did any name resolve to nothing" is a single `case` on a string
    # rather than a loop over lines, which is the shape a POSIX `case` can
    # actually express.  An empty entry is therefore exactly `=;`.
    for _n in $WATCH_NAMES; do
        _a="$("$GETENT" hosts "$_n" 2>/dev/null | awk '{print $1}' | sort \
              | tr '\n' ',' || :)"
        printf '%s=%s;' "$_n" "$_a"
    done
    printf '\n'
}

if [ "$WATCH" = "0" ]; then
    say "upstream watcher DISABLED (CLAUDIO_PROXY_WATCH=0). If a backend's"
    say "  address moves -- which a restart of two services at once can do --"
    say "  nginx will keep the old one until you run: nginx -s reload"
elif [ -z "$GETENT" ]; then
    say "upstream watcher UNAVAILABLE -- no getent in this image, so it cannot"
    say "  tell whether an upstream address has moved. nginx resolves them once"
    say "  at startup; after restarting a backend, run: nginx -s reload"
else
    say "upstream watcher on: ${WATCH_NAMES} every ${WATCH_SECONDS}s."
    say "  nginx resolves an upstream once per configuration load; this"
    say "  reloads it when one of those names moves. CLAUDIO_PROXY_WATCH=0"
    say "  turns it off."
    _WATCH_SEEN="$(_addrs)"
    (
        # `set +e` inside the subshell: this loop must outlive any single
        # failure, and the script runs under `set -eu`.
        set +e
        while :; do
            sleep "$WATCH_SECONDS"
            _now="$(_addrs)"
            # A name that resolves to NOTHING is a backend that is down, not a
            # backend that moved. Reloading on it would walk nginx into the
            # startup refusal that this whole design relies on not happening
            # unattended.
            case "$_now" in
                *"=;"*) continue ;;
            esac
            if [ "$_now" != "$_WATCH_SEEN" ]; then
                say "an upstream address moved -- reloading nginx."
                printf '%s\n' "$_WATCH_SEEN" | sed 's/^/[proxy]   was /' >&2
                printf '%s\n' "$_now"        | sed 's/^/[proxy]   now /' >&2
                if "$NGINX" -s reload 2>/dev/null; then
                    _WATCH_SEEN="$_now"
                else
                    # NOT recorded as seen, so the next tick tries again. A
                    # reload that failed has not been applied, and pretending
                    # otherwise is how the proxy ends up permanently crossed
                    # with a log line saying it was fixed.
                    say "  nginx -s reload FAILED; will retry. Until it"
                    say "  succeeds this proxy is pointing at the OLD"
                    say "  addresses, and /sender and /api/ may answer 503."
                fi
            fi
        done
    ) &
fi

# `daemon off;` belongs here and NOT in the configuration: the two are mutually
# exclusive, and nginx refuses to start on a duplicate `daemon` directive.
exec "$NGINX" -g "daemon off;" "$@"
