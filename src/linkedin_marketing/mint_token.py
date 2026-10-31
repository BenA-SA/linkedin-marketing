"""``linkedin-marketing-token``: mint a refresh token with a localhost callback.

    export LINKEDIN_CLIENT_ID=... LINKEDIN_CLIENT_SECRET=...
    linkedin-marketing-token --modules ads,reporting,ad_library

It prints LinkedIn's consent URL (and tries to open a browser), listens on
the redirect URI's host and port for the single callback, checks ``state``,
exchanges the code, and prints the refresh token once. Nothing is written
to disk or logged. ``--paste`` skips the listener: approve in any browser
and paste back the address you were redirected to.

The redirect URI must exactly match one on the app's Auth tab. Scopes come
from ``--modules`` (each module's read and write scopes) unless ``--scopes``
lists them explicitly. LinkedIn may revoke a member's older tokens for the
app when a new consent asks for a different scope set, so mint one token
covering every module you use.

Docs: https://learn.microsoft.com/linkedin/shared/authentication/authorization-code-flow
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from linkedin_marketing.config import AUTHORIZE_URL, TOKEN_URL, parse_modules
from linkedin_marketing.modules import DEFAULT_MODULES, scopes_for

DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
CALLBACK_TIMEOUT_SECONDS = 300
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})


class MintError(Exception):
    """The consent or code exchange failed; the message says what to do."""


def authorisation_url(client_id: str, redirect_uri: str, state: str, scopes: list[str]) -> str:
    """The LinkedIn consent page for the given scopes."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(scopes),
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def code_from_query(query: str, expected_state: str) -> str:
    """The authorisation code from a callback query string, state-checked."""
    params = parse_qs(query)
    if "error" in params:
        description = params.get("error_description", [""])[0]
        raise MintError(f"LinkedIn refused consent: {params['error'][0]} {description}")
    if params.get("state", [""])[0] != expected_state:
        raise MintError("The callback is from a different run (state mismatch).")
    code = params.get("code", [""])[0]
    if not code:
        raise MintError("No ?code= in the callback.")
    return code


def exchange_code(
    http: httpx.Client, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict:
    """Swap the code for tokens; insist on a refresh token."""
    response = http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        raise MintError(
            f"Code exchange failed with HTTP {response.status_code}: {response.text[:200]}"
        )
    payload = response.json()
    if not payload.get("refresh_token"):
        raise MintError(
            "LinkedIn issued no refresh token: programmatic refresh tokens are not "
            "enabled for this app. Use the access_token (valid ~60 days) as "
            "LINKEDIN_ACCESS_TOKEN instead, or ask LinkedIn to enable refresh tokens."
        )
    return payload


def wait_for_callback(redirect_uri: str, timeout: float = CALLBACK_TIMEOUT_SECONDS) -> str:
    """Serve one request on the redirect URI's host/port; return its query string."""
    parts = urlsplit(redirect_uri)
    if parts.hostname not in _LOCAL_HOSTS or not parts.port:
        raise MintError("Listening needs an http://localhost:PORT/... redirect URI; use --paste.")
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            split = urlsplit(self.path)
            if split.path != (parts.path or "/"):
                self.send_error(404)
                return
            received["query"] = split.query
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"LinkedIn consent received. You can close this tab.")

        def log_message(self, *args):
            return

    server = HTTPServer((parts.hostname, parts.port), Handler)
    deadline = time.monotonic() + timeout
    try:
        while "query" not in received and time.monotonic() < deadline:
            server.timeout = max(deadline - time.monotonic(), 0.1)
            server.handle_request()
    finally:
        server.server_close()
    if "query" not in received:
        raise MintError("Timed out waiting for the LinkedIn callback.")
    return received["query"]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mint a LinkedIn refresh token.")
    parser.add_argument(
        "--redirect-uri",
        default=os.environ.get("LINKEDIN_REDIRECT_URI", DEFAULT_REDIRECT_URI),
        help="Must exactly match a redirect URL on the app's Auth tab.",
    )
    parser.add_argument(
        "--modules",
        default=os.environ.get("LINKEDIN_MODULES", ",".join(DEFAULT_MODULES)),
        help="Comma list of modules whose scopes to request (or 'all').",
    )
    parser.add_argument("--scopes", default="", help="Explicit space- or comma-separated scopes.")
    parser.add_argument(
        "--read-only-scopes", action="store_true", help="Request only the modules' read scopes."
    )
    parser.add_argument(
        "--paste", action="store_true", help="Do not listen; paste the redirected address instead."
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not try to open a browser.")
    return parser.parse_args(argv)


def _scopes(args: argparse.Namespace) -> list[str]:
    if args.scopes.strip():
        return [s for s in args.scopes.replace(",", " ").split() if s]
    return scopes_for(parse_modules(args.modules), include_writes=not args.read_only_scopes)


def _receive_query(args: argparse.Namespace) -> str:
    if args.paste:
        pasted = input("\nPaste the full address the browser was sent to: ")
        return urlsplit(pasted.strip()).query
    print(f"\nWaiting for the callback on {args.redirect_uri} ...")
    return wait_for_callback(args.redirect_uri)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    client_id = os.environ.get("LINKEDIN_CLIENT_ID", "")
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET first.", file=sys.stderr)
        return 1
    scopes = _scopes(args)
    state = secrets.token_urlsafe(24)
    url = authorisation_url(client_id, args.redirect_uri, state, scopes)
    print(f"Requesting scopes: {' '.join(scopes)}\nOpen this URL and approve:\n\n{url}")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        code = code_from_query(_receive_query(args), state)
        with httpx.Client(timeout=30) as http:
            payload = exchange_code(http, client_id, client_secret, code, args.redirect_uri)
    except MintError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    days = int(payload.get("refresh_token_expires_in", 0)) // 86400
    print(
        f"\nScopes granted: {payload.get('scope', '?')}. Refresh token expires in {days} days.\n"
        "Store it in your secret manager as LINKEDIN_REFRESH_TOKEN, then clear this terminal:\n"
    )
    print(payload["refresh_token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
