# Adapter: HTTP / API / service

The handle is an HTTP client (`curl`, or a short Python/Node script when
`curl` is absent — e.g. on stock Windows use `Invoke-WebRequest` or Python).
The evidence is the full response.

## Lifecycle

1. Start the server in the background with an explicit cwd and a free port
   (pass `PORT=0` if supported and read the bound port from the log, or pick
   one). Redirect output to a log file in a temp dir.
2. Poll readiness with a bound: a health route if one exists; otherwise the
   route under test until it stops refusing connections. Give up after ~30s
   and report BLOCKED with the log tail.
3. Run the requests. 4. Kill the server (process group) and confirm the port
   is free. Remove temp data files the server wrote.

Point the server at throwaway storage (temp file, `:memory:`, a test DB) —
check the code for hard-coded data paths such as `Store('notes.json')` that
would write into the repo, and run from a temp copy or set the config if so.

## Cases

- The exact changed branch, with inputs that reach it.
- Negative paths: missing/invalid body, wrong method, unknown id (404),
  unauthenticated/forbidden when auth exists (with synthetic tokens only),
  oversized input.
- Contract: status code, `Content-Type`, required headers, body shape; one
  unchanged neighboring route.
- Idempotency/state: does a write show up on a subsequent read?

## Evidence per request

```
$ curl -si -X POST localhost:$PORT/notes -H 'content-type: application/json' -d '{"text":""}'
HTTP/1.0 400 Bad Request
Content-Type: application/json
{"error": "text required"}
expected 400 + error body → PASS
```

Capture `curl -si` (status + headers + body). A 200 with the wrong body is a
FAIL. A server crash / connection reset during a request is a FAIL with the
log tail as evidence.

## Real vs mocked dependencies

If the endpoint calls a third-party API, DB or queue you cannot run locally,
test with the project's fakes and mark the real integration BLOCKED or NOT
RUN — never PASS it from a mock.
