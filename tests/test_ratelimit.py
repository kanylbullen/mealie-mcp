import asyncio

from mealie_mcp.ratelimit import RateLimitMiddleware


def _scope(path, client=("10.0.0.1", 1), headers=()):
    return {"type": "http", "path": path, "client": client, "headers": list(headers)}


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def _run(mw, scope):
    statuses = []

    async def send(msg):
        if msg["type"] == "http.response.start":
            statuses.append(msg["status"])

    asyncio.run(mw(scope, None, send))
    return statuses[0]


def test_limits_guarded_paths_only():
    mw = RateLimitMiddleware(_ok, per_minute=2, trust_forwarded_for=False)
    assert [_run(mw, _scope("/token")) for _ in range(3)] == [200, 200, 429]
    assert _run(mw, _scope("/mcp")) == 200


def test_cf_connecting_ip_separates_clients_behind_tunnel():
    mw = RateLimitMiddleware(_ok, per_minute=1, trust_forwarded_for=True)
    a = _scope("/register", headers=[(b"cf-connecting-ip", b"1.1.1.1")])
    b = _scope("/register", headers=[(b"cf-connecting-ip", b"2.2.2.2")])
    assert _run(mw, a) == 200 and _run(mw, b) == 200 and _run(mw, a) == 429


def test_headers_ignored_when_not_trusted():
    mw = RateLimitMiddleware(_ok, per_minute=1, trust_forwarded_for=False)
    a = _scope("/register", headers=[(b"x-forwarded-for", b"1.1.1.1")])
    b = _scope("/register", headers=[(b"x-forwarded-for", b"2.2.2.2")])
    assert _run(mw, a) == 200 and _run(mw, b) == 429
