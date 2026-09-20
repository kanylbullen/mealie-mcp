#!/usr/bin/env python3
"""Create the dedicated Mealie user this server runs as, plus its API token.

Idempotent. Never prints secrets: the password and token go to a secret
store command you supply (default: Phase CLI via stdin), or to stdout only if
you explicitly pass --print (for piping into your own vault).

Environment:
    MEALIE_URL             Mealie base URL (default http://127.0.0.1:9000)
    MEALIE_ADMIN_USER      an existing Mealie admin (password login)
    <password env>         name given with --password-env; read directly from
                           os.environ, never re-exported or aliased in a shell

Example (Phase):
    phase run --app homelab --env Production -- \
        python3 scripts/setup_mealie_user.py --password-env MEALIE_APIADMIN_PASSWORD \
        --phase-app homelab
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "mealie-mcp-setup/0.1"


def req(base: str, method: str, path: str, data=None, token=None, form=False):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        sys.exit(f"✗ {method} {path}: {exc.code} {detail}")


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def store(template: str | None, name: str, value: str, do_print: bool) -> None:
    if do_print:
        print(f"{name}={value}")
        return
    if not template:
        sys.exit("✗ no --store command and no --print: refusing to drop the secret on the floor")
    cmd = shlex.split(template.format(name=name))
    res = subprocess.run(cmd, input=value, capture_output=True, text=True)
    if res.returncode != 0:
        # Most vaults fail on "already exists"; try an update variant if provided.
        sys.exit(f"✗ store {name}: {res.stderr.strip()[:200]}")
    print(f"  stored {name} (sha256 {fingerprint(value)}…, {len(value)} chars)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--password-env", required=True, help="env var holding the admin password")
    ap.add_argument("--username", default="mcp")
    ap.add_argument("--email", default="mcp@mealie.local")
    ap.add_argument("--full-name", default="MCP (AI assistant)")
    ap.add_argument("--token-name", default="mealie-mcp")
    ap.add_argument("--store", help="command template, {name} = secret name; value on stdin")
    ap.add_argument(
        "--phase-app", help="shortcut: store via `phase secrets create` in this Phase app"
    )
    ap.add_argument("--phase-env", default="Production")
    ap.add_argument("--print", action="store_true", help="print secrets to stdout instead")
    ap.add_argument("--password-secret", default="MEALIE_MCP_PASSWORD")
    ap.add_argument("--token-secret", default="MEALIE_MCP_TOKEN")
    args = ap.parse_args()
    if args.phase_app and not args.store:
        args.store = f"phase secrets create {{name}} --app {args.phase_app} --env {args.phase_env}"

    base = os.environ.get("MEALIE_URL", "http://127.0.0.1:9000").rstrip("/")
    admin_user = os.environ.get("MEALIE_ADMIN_USER") or sys.exit("✗ MEALIE_ADMIN_USER unset")
    admin_pw = os.environ.get(args.password_env) or sys.exit(f"✗ {args.password_env} unset")

    admin_tok = req(
        base, "POST", "/api/auth/token", {"username": admin_user, "password": admin_pw}, form=True
    )["access_token"]
    me = req(base, "GET", "/api/users/self", token=admin_tok)
    print(f"admin login ok: {me['username']} group={me['group']} household={me['household']}")

    users = req(base, "GET", "/api/admin/users?perPage=200", token=admin_tok)["items"]
    existing = next((u for u in users if u["username"] == args.username), None)
    password = secrets.token_urlsafe(24)
    if existing:
        print(f"user {args.username!r} exists (admin={existing['admin']}); resetting its password")
        if existing["admin"]:
            sys.exit("✗ refusing: the MCP user must not be an admin")
        # Admin PUT does not re-hash passwords; use the reset-token flow instead.
        reset = req(
            base,
            "POST",
            "/api/admin/users/password-reset-token",
            token=admin_tok,
            data={"email": existing["email"]},
        )
        req(
            base,
            "POST",
            "/api/users/reset-password",
            data={
                "token": reset["token"],
                "email": existing["email"],
                "password": password,
                "passwordConfirm": password,
            },
        )
    else:
        created = req(
            base,
            "POST",
            "/api/admin/users",
            token=admin_tok,
            data={
                "username": args.username,
                "fullName": args.full_name,
                "email": args.email,
                "password": password,
                "group": me["group"],
                "household": me["household"],
                "admin": False,
                "canInvite": False,
                "canManage": False,
                "canOrganize": True,
                "advanced": False,
                "authMethod": "Mealie",
            },
        )
        print(
            f"created user {created['username']} (id {created['id']}) in group={created['group']} household={created['household']}"
        )

    user_tok = req(
        base,
        "POST",
        "/api/auth/token",
        {"username": args.username, "password": password},
        form=True,
    )["access_token"]
    who = req(base, "GET", "/api/users/self", token=user_tok)
    assert not who["admin"], "new user unexpectedly admin"
    for t in who.get("tokens") or []:
        if t["name"] == args.token_name:
            req(base, "DELETE", f"/api/users/api-tokens/{t['id']}", token=user_tok)
            print(f"  removed old API token {args.token_name!r}")
    tok = req(
        base,
        "POST",
        "/api/users/api-tokens",
        token=user_tok,
        data={"name": args.token_name, "integrationId": "mealie-mcp"},
    )
    api_token = tok["token"]
    check = req(base, "GET", "/api/users/self", token=api_token)
    print(f"API token works as {check['username']} (admin={check['admin']})")

    store(args.store, args.password_secret, password, args.print)
    store(args.store, args.token_secret, api_token, args.print)


if __name__ == "__main__":
    main()
