"""An MCP server over the read API, so a model can ask about usage.

**It is a CLIENT of `/api/v1/`, not a second reader.**  It holds a reader
token and makes HTTP calls, which is the whole of its authority: it cannot see
an account the token's scope excludes, cannot reach the store's files, and
cannot answer a question the API refuses.  Giving it the DuckDB handle instead
would have been fewer moving parts and a privilege escalation -- an agent with
a database handle is not bounded by anything the operator configured.

**Every tool is generated from `/api/v1/openapi`.**  Nothing here holds a copy
of the route list, the parameter names or their types, because a copy is a
place to forget: this package already derives its route list, its refusal
vocabulary and its coverage bases from source rather than restating them.  A
tool list built at startup from the server it will call cannot describe an
endpoint that endpoint does not have.

**The refusals are passed through, not flattened.**  `no-data`,
`filtered-to-nothing` and `unanswerable` are three different answers and a
model must be able to tell them apart: "you have recorded nothing", "your
filter excluded everything" and "that question has no answer here" lead to
three different next actions.  Returning an empty list for all three is how an
agent concludes there is no data when there is, and it is the exact failure the
envelope exists to prevent.  So a refusal is returned as its own object, with
its `remedy`, and the tool result says plainly that it is not an empty result.

**Two transports, one handler.**  Stdio is what a desktop client speaks and
what composes with `docker run -i`: there the launcher IS the caller, so an
ambient `CLAUDIO_API_TOKEN` is exactly right -- one process, one client, one
identity.  Streamable HTTP (`POST /mcp`) is what sits behind the proxy beside
the api, and there every caller presents their OWN bearer token, which is
forwarded to the read API untouched.  An ambient token in HTTP mode is refused
rather than ignored: a caller who sent no credential would inherit the
process's, and a served MCP would answer questions the caller was never
entitled to ask.

Both transports call `handle()` and nothing else, so a tool cannot exist on one
and not the other, and a fix to a refusal cannot land on one transport only.

**It runs flat.**  No sibling module is imported -- not one -- so
`server/srv/mcp.py` is copied on its own into an image that carries no store,
no query layer and no DuckDB.  That is not tidiness: the boundary is what the
image's `.containerignore` enforces, and it can only enforce it because this
file needs nothing else.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL = "2025-06-18"
NAME = "claudio-usage"
TIMEOUT = 30

# One JSON-RPC object.  The proxy is configured to the same number, and a test
# reads this constant rather than a copy of it -- a SMALLER limit at the proxy
# means a caller meets a 413 carrying none of this process's explanation, and a
# LARGER one means the proxy buffers a body this process is about to refuse.
MAX_BODY = 1 << 20

PATH = "/mcp"

# The one thing not derived: which routes are worth handing to a model. The
# API serves thirteen and a model does not want `health` or `diagnostics` --
# they are operator questions, and every extra tool is context a model spends
# before it has read anything. `capabilities` IS included: it is how the model
# learns what a null means here rather than assuming zero.
TOOLS = ("search", "aggregate", "aggregate/per-account", "histogram",
         "values", "fields", "lookup", "accounts", "windows", "window",
         "capabilities")


class Api(object):
    """The HTTP client.  One place that knows the token exists."""

    def __init__(self, base, token=None, timeout=TIMEOUT):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def get(self, path, params=None):
        url = self.base + path
        clean = {k: v for k, v in (params or {}).items()
                 if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
        req = urllib.request.Request(url)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as fh:
                return json.loads(fh.read())
        except urllib.error.HTTPError as exc:
            # A refusal is an ANSWER, not a transport failure: the body carries
            # the reason and the remedy, and both are more useful to a model
            # than the status code.
            try:
                return json.loads(exc.read())
            except Exception:                        # noqa: BLE001
                return {"outcome": "unanswerable",
                        "refusal": {"reason": "reader-failed",
                                    "detail": "HTTP %s" % exc.code,
                                    "remedy": "check the door is reachable at "
                                              + self.base}}
        except Exception as exc:                     # noqa: BLE001
            return {"outcome": "unanswerable",
                    "refusal": {"reason": "reader-failed",
                                "detail": type(exc).__name__,
                                "remedy": "check the door is reachable at "
                                          + self.base}}


def tools_from_spec(spec):
    """MCP tool definitions, built from the served OpenAPI document."""
    out = []
    for path, item in sorted((spec.get("paths") or {}).items()):
        route = path.rsplit("/api/v1/", 1)[-1]
        if route not in TOOLS:
            continue
        get = item.get("get") or {}
        props, required = {}, []
        for p in get.get("parameters") or []:
            schema = dict(p.get("schema") or {"type": "string"})
            schema["description"] = p.get("description") or ""
            props[p["name"]] = schema
            if p.get("required"):
                required.append(p["name"])
        out.append({
            "name": route.replace("/", "_").replace("-", "_"),
            "description": _describe(route, get),
            "inputSchema": {"type": "object", "properties": props,
                            "required": required},
            "_path": path,
        })
    return out


def _describe(route, get):
    """The tool description a model reads before choosing.

    The API's own response description is reused rather than paraphrased, and
    the warning about the three empties is repeated on EVERY tool -- not once
    in the server description, which a model may never see, and not in a README
    it certainly will not.
    """
    base = {
        "search": "Rows, one per API request (stream a) or plan observation "
                  "(stream b).",
        "aggregate": "Group rows and total them. Refuses to total across "
                     "accounts: different plans have different denominators.",
        "aggregate_per_account": "The same breakdown, per account, never summed.",
        "histogram": "Counts or sums over time buckets.",
        "values": "Per-field value distributions -- what values a column "
                  "actually holds, and how often.",
        "fields": "The columns that exist, their types and how many rows carry "
                  "a value.",
        "lookup": "One request_id, session_id or prompt_id to its rows.",
        "accounts": "The accounts in this store.",
        "windows": "Reconciled plan windows: attributed usage, residual and "
                   "coverage.",
        "window": "One plan window in detail.",
        "capabilities": "READ THIS FIRST. What the server can answer, the "
                        "closed set of outcomes, and what a null means at each "
                        "JSON path.",
    }.get(route.replace("/", "_").replace("-", "_"), route)
    return (base + "\n\nThe response carries `outcome`. `no-data`, "
            "`filtered-to-nothing` and `unanswerable` are THREE DIFFERENT "
            "ANSWERS and must not be treated as an empty result: they mean "
            "'nothing was ever recorded', 'data exists but this query excluded "
            "all of it', and 'this question has no answer here'. A refusal "
            "carries a remedy; report it rather than inferring from emptiness. "
            "A null coverage means nobody said, never zero.")


def call(api, tools, name, args):
    """Run one tool.  Returns the envelope untouched."""
    for t in tools:
        if t["name"] == name:
            return api.get(t["_path"], args or {})
    return {"outcome": "unanswerable",
            "refusal": {"reason": "unknown-endpoint", "detail": name,
                        "remedy": "call tools/list; this server generates its "
                                  "tools from the door it talks to"}}


def _result(env):
    """An MCP tool result.  A refusal is marked as an ERROR so a model cannot
    read it as a successful empty answer."""
    text = json.dumps(env, indent=1, sort_keys=True)
    return {"content": [{"type": "text", "text": text}],
            "isError": env.get("outcome") == "unanswerable"}


def tools_for(api):
    """(tools, refusal).  The tool list, or WHY there is not one.

    NEVER `([], None)`, AND THAT IS THE WHOLE FUNCTION.

      The list is built from `/api/v1/openapi`, and that route sits behind the
      same reader auth as everything else -- so the ordinary first failure is a
      401, whose envelope has no `result` key at all.  The line this replaces
      was `(api.get(...) or {}).get("result") or {}`, which turns a 401, a
      503, an unreachable host and a renamed `/api/` prefix into `{}`, and `{}`
      into an EMPTY TOOL LIST served with `"jsonrpc": "2.0"` and no error.  A
      model reading that concludes the server has no tools, which is this
      project's cardinal sin with an agent in the loop instead of a person.

      The third answer is the interesting one: a spec that parsed, carried
      paths, and matched none of `TOOLS`.  That is prefix drift -- somebody
      served the API under a different external path -- and it is precisely
      the failure the proxy configuration's `/api/` paragraph says breaks the
      MCP "first and silently".  Named here, so it is neither.
    """
    env = api.get("/api/v1/openapi")
    spec = env.get("result") if isinstance(env, dict) else None
    if not isinstance(spec, dict):
        refusal = (env.get("refusal") if isinstance(env, dict) else None) or {}
        return [], {
            "reason": "no-spec",
            "detail": "the read API did not return an OpenAPI document: %s"
                      % (refusal.get("detail") or refusal.get("reason")
                         or "no `result` in the response"),
            "remedy": refusal.get("remedy")
                      or ("this server builds every tool from GET "
                          "/api/v1/openapi. Send a reader token this API "
                          "accepts, and check the api service is up."),
        }
    tools = tools_from_spec(spec)
    if not tools:
        return [], {
            "reason": "no-tools",
            "detail": "the OpenAPI document names %d path(s) and none of them "
                      "is a route this server offers as a tool"
                      % len(spec.get("paths") or {}),
            "remedy": "the API is being served under a prefix other than "
                      "/api/v1/, or its route list has changed. Tools are "
                      "generated from those paths; a rewritten prefix breaks "
                      "every one of them.",
        }
    return tools, None


def _rpc_error(mid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": err}


def handle(api, msg, tools=None):
    """One JSON-RPC message in, one reply out, or None for a notification.

    Transport-free ON PURPOSE. Both front ends -- the stdio loop below and
    `serve_http`'s `POST /mcp` -- call this and nothing else, so a tool cannot
    exist on one and not the other, and a fix to a refusal cannot land on one
    transport only. That is the same rule the tool list already follows by
    being generated from the served OpenAPI rather than written down twice.

    THE TOOL LIST IS NOT CACHED, AND THE REASON IS THE TOKEN.  Over HTTP the
    credential arrives on the request, so a list fetched once at startup would
    have to be fetched with somebody else's -- or with none, which is the 401
    that used to become an empty list. Fetching per call costs one loopback
    round trip against a service in the same compose network and buys: the
    caller's own scope decides what the spec says, a tool added to the API
    appears without restarting this process, and both transports behave
    identically because neither has any state to differ about. `tools` stays a
    parameter so a test can hand one in.
    """
    mid, method = msg.get("id"), msg.get("method")
    # `initialize` is answered without touching the API. It is a handshake
    # about THIS process, and a client that cannot complete it cannot be told
    # anything else -- including why the API is unreachable.
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": "1"}}}
    if method in ("tools/list", "tools/call"):
        refusal = None
        if tools is None:
            tools, refusal = tools_for(api)
        if refusal is not None:
            # -32000 is the JSON-RPC application-error range. An ERROR and not
            # an empty `tools` array, and not an `isError` tool result either:
            # the tool never ran, and saying it did would be a second wrong
            # answer on top of the first.
            return _rpc_error(mid, -32000,
                              "this server cannot describe its tools: %s"
                              % refusal["reason"], refusal)
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "tools": [{k: v for k, v in t.items() if k != "_path"}
                          for t in tools]}}
        params = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": mid,
                "result": _result(call(api, tools, params.get("name"),
                                       params.get("arguments")))}
    if mid is None:
        # A notification. No reply, by the spec.
        return None
    return _rpc_error(mid, -32601, "no such method")


def serve(api, stdin=None, stdout=None):
    """The stdio loop.  One JSON-RPC object per line.

    Kept alongside the HTTP transport rather than replaced by it: this is how a
    laptop with no network route to the server still works, and it is what a
    desktop client speaks natively.  Here the launcher IS the caller, so the
    token comes from the environment and one process means one identity.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            # -32700 is JSON-RPC's own parse error, and it is REPORTED rather
            # than skipped: a line this loop silently dropped is a client
            # waiting for a reply that is never coming.  `id` is null because
            # there is no parsed message to take one from, which is what the
            # spec says to do.
            stdout.write(json.dumps(
                _rpc_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        reply = handle(api, msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


# --------------------------------------------------------------------------
# Streamable HTTP
# --------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """`POST /mcp`, and nothing else at all.

    Stateless: no `Mcp-Session-Id`, because this tool surface is read-only and
    holds nothing between calls.  A session id would be state to expire,
    invalidate and explain, bought for nothing.

    AND IT AUTHENTICATES NOTHING ITSELF.  The caller's `Authorization` header
    is forwarded to the read API verbatim and the API decides, from the same
    `readers.json` that gates `/api/v1/*` -- so a served MCP shows exactly what
    a person holding that token would get, and an account outside the token's
    scope is refused BY NAME rather than filtered out.  A second gate here
    would be a second place for scope to be decided, and the two would
    eventually disagree.
    """

    protocol_version = "HTTP/1.1"
    server_version = "claudio-mcp/1"
    sys_version = ""
    base = None
    quiet = False

    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        if not self.quiet:
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def _send(self, code, obj):
        body = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _not_here(self):
        # A 404 with no route out of it is how a client author concludes the
        # server is broken.  This one names the route that exists.
        self._send(404, {"outcome": "unanswerable",
                         "refusal": {
                             "reason": "no-such-route",
                             "detail": "this service is the MCP surface and "
                                       "speaks one route",
                             "remedy": "POST %s with one JSON-RPC object. The "
                                       "usage data itself is the read API's, "
                                       "at GET /api/v1/*." % PATH}})

    def do_GET(self):
        self._not_here()

    do_PUT = do_DELETE = do_GET

    # NOT `do_HEAD`.  `_not_here` sends a `Content-Length` and then a body,
    # which for a HEAD is a protocol violation the client answers by waiting
    # for bytes that are not coming.  Left undefined, `BaseHTTPRequestHandler`
    # answers 501, which is both correct and honest about a method this
    # service does not implement.

    def do_POST(self):
        if self.path.split("?")[0] != PATH:
            self._not_here()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(413 if length > MAX_BODY else 400, {
                "outcome": "unanswerable",
                "refusal": {"reason": "bad-body",
                            "detail": "one JSON-RPC object, at most %d bytes; "
                                      "this request declared %d"
                                      % (MAX_BODY, length),
                            "remedy": "send exactly one JSON-RPC object with a "
                                      "Content-Length"}})
            return
        raw = self.rfile.read(length)
        try:
            msg = json.loads(raw)
        except ValueError:
            # The transport worked and the payload did not, so this is
            # JSON-RPC's own -32700 carried on a 200 rather than an HTTP error.
            self._send(200, _rpc_error(None, -32700, "parse error"))
            return
        if not isinstance(msg, dict):
            # A BATCH (a JSON array) lands here too, and it is refused by name.
            # Answering the first element would silently drop the rest.
            self._send(200, _rpc_error(
                None, -32600,
                "one JSON-RPC object per request; this server does not take "
                "batches"))
            return
        token = None
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip() or None
        reply = handle(Api(self.base, token), msg)
        if reply is None:
            # A notification takes no response, and 202 is what says so.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, reply)


def serve_http(base, host="127.0.0.1", port=8788, ready=None, quiet=False):
    """Bind and serve `POST /mcp` until interrupted."""
    _Handler.base = base
    _Handler.quiet = quiet
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    bound = httpd.server_address[1]
    sys.stderr.write("[mcp] listening on http://%s:%d%s\n"
                     "[mcp] api     %s\n"
                     "[mcp] auth    the CALLER's bearer token, forwarded to "
                     "the api; this process holds none\n"
                     % (host, bound, PATH, base))
    sys.stderr.flush()
    if ready is not None:
        ready(bound)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    """Two transports, and the flag spelling is a contract.

        stdio:  mcp.py --api <url>
        http:   mcp.py --http --host <h> --port <p> --api <url>

    `entrypoint-mcp.sh` writes exactly those, and a test compares the two.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    base = os.environ.get("CLAUDIO_API", "http://127.0.0.1:8787")
    token = os.environ.get("CLAUDIO_API_TOKEN")
    host, port, http = "127.0.0.1", 8788, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--http":
            http = True
        elif a == "--api" and i + 1 < len(argv):
            base = argv[i + 1]
            i += 1
        elif a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 1
        elif a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 1
        i += 1

    if http:
        # SERVED OVER HTTP, A TOKEN IN THE ENVIRONMENT IS A PRIVILEGE
        # ESCALATION, AND IT IS REFUSED IN THE PROCESS THAT WOULD LEAK IT.
        #
        # `entrypoint-mcp.sh` refuses it too, and this is not a duplicate for
        # the sake of belt and braces: the entrypoint has a documented escape
        # hatch (`docker run ... IMAGE sh -c ...`), so the shell check is the
        # one that can be walked around and this one is not.  Same exit code,
        # because an operator reading either message is looking at one fault.
        #
        # Refused rather than ignored: ignoring it leaves a token in `docker
        # inspect` and in the compose file, doing nothing, waiting for someone
        # to "fix" the code that ignores it.
        if token:
            sys.stderr.write(
                "[mcp] CLAUDIO_API_TOKEN is set and this is --http.\n"
                "[mcp]   Served over HTTP, every caller presents their own "
                "bearer token and\n"
                "[mcp]   the api applies that reader's scope. An ambient "
                "token would be\n"
                "[mcp]   inherited by a caller who sent none, so this surface "
                "would answer\n"
                "[mcp]   questions the caller was never entitled to ask.\n"
                "[mcp]   Unset it here. It belongs to the stdio transport, "
                "where the\n"
                "[mcp]   launcher is the caller.\n")
            return 7
        return serve_http(base, host, port)

    if not token:
        sys.stderr.write(
            "[mcp] no CLAUDIO_API_TOKEN. The read API refuses without one "
            "unless the api was started with no readers file on loopback.\n")
    serve(Api(base, token))
    return 0


if __name__ == "__main__":
    sys.exit(main())
