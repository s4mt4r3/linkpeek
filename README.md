# linkpeek

A small link-preview API. Give it a URL and it returns the page's title, description, image, site name and favicon, the same data chat apps show when you paste a link.

A link-preview service makes HTTP requests to arbitrary URLs supplied by strangers. It is a textbook SSRF target, so most of the code goes into three areas: **SSRF protection**, **timeouts and limits**, and **caching**.

```
GET /preview?url=https://www.python.org/

{
  "url": "https://www.python.org/",
  "title": "Welcome to Python.org",
  "description": "The official home of the Python Programming Language",
  "image": "https://www.python.org/static/opengraph-icon-200x200.png",
  "site_name": "Python.org",
  "favicon": "https://www.python.org/static/favicon.ico",
  "fetched_at": "2026-10-07T13:10:21.756420Z",
  "cached": false
}
```

## Setup

Requires Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env          # optional; every setting has a default
uvicorn app.main:app --reload
pytest                        # 180+ tests, no network access
```

Docker:

```bash
docker build -t linkpeek .
docker run -p 8000:8000 linkpeek
```

The image is `python:3.12-slim`, runs as an unprivileged user (uid 10001), and listens on `$PORT`, so it deploys as-is on Render or Fly.io. Behind either of those, set `LINKPEEK_TRUST_X_FORWARDED_FOR=true` so the rate limiter sees real client IPs.

## API

| Endpoint | Description |
|---|---|
| `GET /preview?url=<url>` | Preview metadata. `X-Cache: HIT` or `MISS` header. |
| `GET /health` | `{"status": "ok"}`. Not rate limited. |
| `GET /docs` | OpenAPI UI. |

Errors always have the shape `{"error": "<code>", "detail": "<message>"}`.

| Status | `error` | When |
|---|---|---|
| 400 | `blocked_url` | Bad scheme, credentials, disallowed port, internal address, unresolvable host |
| 415 | `unsupported_content_type` | Response isn't `text/html` or `application/xhtml+xml` |
| 422 | (FastAPI) | `url` missing or longer than 2048 chars |
| 429 | `rate_limited` | Over 30 requests/minute from this IP. `Retry-After` header set. |
| 502 | `upstream_error` | Connection refused, TLS failure, upstream 4xx/5xx, too many redirects |
| 504 | `upstream_timeout` | Any phase timed out, or the 8s total budget ran out |

### Examples

```bash
curl 'localhost:8000/preview?url=https://www.python.org/'

# Redirects are followed; "url" is the final destination
curl 'localhost:8000/preview?url=http://github.com'

# Blocked: cloud metadata endpoint
curl -i 'localhost:8000/preview?url=http://169.254.169.254/latest/meta-data/'
# HTTP/1.1 400 Bad Request
# {"error":"blocked_url","detail":"URL is not allowed or could not be resolved"}

# Blocked: decimal-encoded 127.0.0.1
curl -i 'localhost:8000/preview?url=http://2130706433/'

# Not HTML
curl -i 'localhost:8000/preview?url=https://httpbin.org/image/png'
# HTTP/1.1 415 Unsupported Media Type

# Second request is a cache hit
curl -si 'localhost:8000/preview?url=https://example.com' | grep -i x-cache
```

## Layout

```
app/
  main.py        FastAPI app factory, routes, error handlers, rate-limit dependency
  security.py    SSRF: URL validation, DNS resolution, IP classification
  fetcher.py     HTTP client, manual redirects, IP pinning, timeouts, body cap
  parser.py      OG → Twitter → HTML metadata extraction (selectolax/lexbor)
  cache.py       Cache backend interface, in-memory TTL/LRU, negative caching, coalescing
  ratelimit.py   Sliding-window per-IP limiter
  config.py      Settings from LINKPEEK_* env vars
  errors.py      Exception → HTTP status mapping
  models.py      Pydantic response models
tests/           pytest; DNS and HTTP are fully mocked
```

`create_app()` takes the resolver, HTTP transport, cache backend and clock as parameters. Tests inject fakes through those parameters instead of monkeypatching, and `conftest.py` makes any real `getaddrinfo` call fail the test.

---

## Design decisions

### 1. SSRF threat model

**The threat:** an attacker asks linkpeek to fetch a URL that only linkpeek's network position can reach. Examples are the cloud metadata service (`169.254.169.254`, which hands out IAM credentials on AWS), an admin panel on `localhost`, a Redis instance on `10.0.0.5:6379`, or a Kubernetes API. Even a blind SSRF, where the attacker never sees the response body, can port-scan the internal network through timing and error differences.

Every URL is checked by `security.validate_url` before any connection is made. That includes each redirect hop.

| Check | Blocks | Why |
|---|---|---|
| Scheme must be `http` / `https` | `file://`, `gopher://`, `dict://`, `ftp://` | `gopher://` in particular can craft raw TCP payloads to Redis/SMTP |
| No userinfo | `http://user:pass@host` | Classic parser-confusion vector (`http://good.com@evil.com`), and we never want to forward credentials |
| Port allowlist (80, 443, 8080, 8443) | `:22`, `:6379`, `:25`, ... | Stops the service being used to probe or talk to non-web services |
| Hostname denylist | `localhost`, `*.localhost`, `*.local`, `*.internal`, `metadata.google.internal` | These names resolve to internal addresses. Checking them before DNS means no lookup is even made. |
| Single-label names rejected | `http://intranet/` | Resolved via DNS search domains, so they almost always mean an internal host |
| Legacy numeric IPv4 rejected | `2130706433`, `0x7f000001`, `0177.0.0.1`, `127.1` | Python's `ipaddress` rejects these, but `getaddrinfo` accepts them (inet_aton rules) and resolves them to 127.0.0.1. A string-based blocklist misses them entirely. |
| **Every** resolved IP must be public | Private, loopback, link-local, multicast, reserved, unspecified, CGNAT (`100.64/10`), and anything `ipaddress` says isn't global | If even one A/AAAA record is internal, the whole host is rejected. Otherwise an attacker publishes one public and one private record and wins whenever the client picks the private one. |
| IPv4 embedded in IPv6 is unwrapped and re-checked | `::ffff:127.0.0.1`, `::ffff:7f00:1`, `64:ff9b::7f00:1` (NAT64), `2002:7f00:1::` (6to4), Teredo, `::127.0.0.1` | These reach the IPv4 address on dual-stack hosts, and some `ipaddress` versions don't flag them as loopback/private |

**Redirects are followed manually** (`follow_redirects=False` in httpx). The fetcher follows up to 3 hops. Each `Location` is resolved against the current URL and then put through the same full `validate_url` call, so a public page that 302s to `http://169.254.169.254/` is blocked. Without this, redirects are the easiest SSRF bypass: the first URL passes the check, and the HTTP client quietly follows a redirect nobody validated.

**Error messages don't leak.** Errors from the static checks are specific ("Port is not allowed"), because they only describe input the caller already has. Errors from DNS or IP classification all return the same message: `URL is not allowed or could not be resolved`. A response never says "resolved to 10.0.0.5" or "is private", and an internal name is indistinguishable from a nonexistent one. Otherwise the service could be used to map internal DNS.

**`trust_env=False`** on the HTTP client. Otherwise an `HTTP_PROXY` variable in the deployment environment would send requests somewhere other than the IP that was validated.

**Images and favicons are not fetched.** linkpeek returns their URLs and the client loads them. If you add server-side image proxying later, those URLs need the same validation.

### 2. DNS rebinding mitigation

**The attack:** `evil.com` has a 0-second TTL. The validation lookup returns `93.184.215.14` (public, so it passes). When the HTTP client then does its own lookup to connect, the answer is `127.0.0.1`. Any design that validates a hostname and then hands the hostname to an HTTP client has this time-of-check/time-of-use gap.

**The mitigation: resolve once, connect to the IP we validated.** `validate_url` returns the validated IP list. The fetcher then:

1. rewrites the request URL's host to that IP (`https://93.184.215.14/path`), so httpx never resolves anything;
2. sets the `Host` header to the original hostname (with the port if it's non-default), so virtual hosting works;
3. for HTTPS, passes `extensions={"sni_hostname": "example.com"}`. httpcore uses that as the TLS `server_hostname`, so SNI is correct **and the certificate is verified against the real hostname**, not the IP. (`wrong.host.badssl.com` correctly fails.)

Because there's only one lookup per hop, there's nothing to rebind. Each redirect hop gets its own resolve-validate-pin cycle. The tests check that the mock transport receives requests addressed to the validated IP with the original `Host` header, and that the resolver is called exactly once per hop.

One subtle consequence: **connection keep-alive is disabled.** httpx pools connections by `(scheme, host, port)`, and the host is now an IP. If `a.com` and `b.com` share an IP, a pooled TLS connection whose certificate was verified for `a.com` could be reused for `b.com` without checking `b.com`'s certificate. Fresh connections cost some latency, but a preview service rarely hits the same origin twice within the cache TTL anyway.

If a host has several valid IPs, they're tried in order on connection failure. All of them were validated, so this changes availability, not security.

### 3. Timeout budget

```
connect  3s ─┐
read     5s ─┼─ per-phase (httpx.Timeout)
pool     3s ─┘
total    8s ─── hard deadline (asyncio.timeout) over DNS + every redirect hop + body read
```

Per-phase timeouts alone don't bound a request:
- A slowloris server that sends one byte every 4.9s never trips a 5s *read* timeout, so the request can be held open indefinitely.
- 4 hops × (3s connect + 5s read) is 32s before any single timeout fires.
- DNS goes through `getaddrinfo`, which httpx timeouts don't cover at all.

So the whole fetch runs inside `asyncio.timeout(total_timeout)`. Whatever is still in progress when the 8s run out (DNS, the third redirect, a trickling body) is cancelled and the caller gets a 504. Each of those cases has a test.

**Body cap:** the body is streamed and reading stops at 1 MB. The cap counts *decompressed* bytes (`aiter_bytes`, not `aiter_raw`), so a 5 KB gzip bomb that inflates to 5 GB is cut off at 1 MB. The page is truncated rather than rejected: metadata lives in `<head>`, so the first megabyte almost always has it.

**Content-Type** is checked from the headers before any of the body is read. Anything other than HTML gets a 415 without downloading the body.

### 4. Cache design

```
request ─► rate limit ─► static URL check ─► normalize ─► PreviewCache.get_or_load(key)
                                                             │
                              in-flight task for key? ──yes──┼─► await it (shared)
                              backend hit? ─────────────yes──┼─► return (cached=true)
                              else start one task ───────────┘─► fetch → parse → store
```

**Key normalization.** Scheme and host are lowercased, the fragment, a trailing dot on the host and default ports are removed, and an empty path becomes `/`. `HTTPS://Example.COM:443/#top` and `https://example.com/` share one entry. The query string is kept as-is, because reordering parameters can change what a server returns. The fetcher then requests the *normalized* URL, so the cache key and the request can never disagree.

**Storage.** `InMemoryTTLCache` is an `OrderedDict` that stores an expiry time with each entry and evicts least-recently-used entries past 1000. It needs no lock: its methods never `await`, so on one event loop each call runs to completion.

**Backend interface.** `CacheBackend` is a `Protocol` with `async get(key)` and `async set(key, value, ttl)`. Values are plain JSON-compatible dicts, including the error entries, so a Redis backend is about 15 lines: `GET`, plus `SET key json.dumps(value) EX ttl`. Pass it as `create_app(cache_backend=RedisCache(...))`.

**Negative caching.** Failures linkpeek understands (timeouts, 5xx, blocked, 415) are stored as `{"ok": false, "code": ..., "detail": ...}` for 60s instead of 1 hour, and replayed as the same error. This stops someone from pointing the service at a slow or dead host and making it hammer that host (or tie up our own sockets) over and over. Unexpected exceptions (bugs) are *not* cached. Rate-limit rejections are a separate exception type and are never cached.

**Request coalescing (thundering herd).** If 50 requests for the same uncached URL arrive at once, only one upstream fetch happens. The first caller creates an `asyncio.Task` and registers it in `_inflight[key]`. Everyone else awaits that same task. Checking `_inflight` and registering the task happen with no `await` in between, which is why this is race-free on a single event loop without a lock. Waiters use `asyncio.shield(task)`, so a client that disconnects cancels only its own wait: the shared fetch keeps going and still populates the cache. The task removes itself from `_inflight` in a `finally` block after it has written to the cache.

### 5. Rate limiting

Per client IP, sliding-window log: a deque of request timestamps in the last 60s, rejected at 30. Unlike a fixed window, it can't be gamed by sending 30 requests at :59 and 30 more at :00. Rejected requests aren't recorded, so a client hammering away doesn't extend its own block. `Retry-After` is the time until the oldest timestamp ages out, rounded up. Idle IPs are swept once per window so memory doesn't grow with every IP ever seen. Cache hits count too: the limit protects our capacity, not just upstream hosts.

`X-Forwarded-For` is ignored unless `LINKPEEK_TRUST_X_FORWARDED_FOR=true`, and then only the **rightmost** entry is used. That's the one your proxy appended. Everything to its left was sent by the client and can be forged.

---

## Known limitations

- **In-memory cache, coalescing and rate limits are per process.** With 4 uvicorn workers or 3 instances you get 4 or 3 independent caches, and a client effectively gets N × 30 requests/minute. The fix is the Redis `CacheBackend` plus a Redis-based limiter. Cross-instance coalescing would need a distributed lock (`SET NX` with expiry).
- **No JavaScript rendering.** Single-page apps that inject `<meta>` tags client-side return little or nothing. Most sites that care about previews render OG tags on the server for exactly this reason (crawlers don't run JS either).
- **Network-level egress controls are still recommended.** Application-layer SSRF checks are one layer. In production, also block metadata endpoints with the cloud provider's own controls (IMDSv2 with hop limit 1 on AWS) and egress firewall rules.
- **No keep-alive** (see the DNS rebinding section), so each fetch pays a full TCP+TLS handshake.
- **DNS lookups use `getaddrinfo` in a thread pool.** On timeout, the coroutine is cancelled but the thread runs until the OS resolver gives up. Under a flood of slow-resolving names, this could exhaust the default executor. A dedicated async resolver (aiodns) would fix it.
- **IP fallback is sequential**, not Happy Eyeballs, and only on connection errors (not connect timeouts).
- **No `/favicon.ico` guess.** If a page doesn't declare an icon, `favicon` is `null` rather than a URL that may 404.
- **Coalescing has a tiny window with a remote backend:** if a fetch finishes between a waiter's cache miss and its in-flight check, that waiter fetches again. This is harmless (one extra fetch), and it can't happen with the in-memory backend, whose `get` never yields.
