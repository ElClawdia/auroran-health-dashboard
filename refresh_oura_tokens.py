#!/usr/bin/env python3
"""
Obtain Oura OAuth tokens and save them to oura_tokens.json.

Register the same redirect URI in the Oura app settings. The default is:
  http://localhost:8080/callback
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import threading
import urllib.parse
from pathlib import Path

from config import OURA_CLIENT_ID, OURA_CLIENT_SECRET, OURA_REDIRECT_URI
from oura_client import OuraClient


TOKEN_FILE = Path(__file__).resolve().parent / "oura_tokens.json"
DEFAULT_SCOPES = "email personal daily heartrate tag workout session spo2 ring_configuration stress heart_health"


def receive_code(port: int, expected_state: str, timeout: int = 180) -> str | None:
    code_holder: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            state = qs.get("state", [""])[0]
            if state != expected_state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid OAuth state.")
                return
            if "error" in qs:
                self.send_response(400)
                self.end_headers()
                message = f"Oura authorization failed: {qs.get('error', ['unknown'])[0]}"
                self.wfile.write(message.encode())
                return
            if "code" in qs:
                code_holder.append(qs["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"Authorization successful. You can close this tab.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    server.server_close()
    return code_holder[0] if code_holder else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Authorize Oura and save OAuth tokens.")
    parser.add_argument("--redirect-uri", default=OURA_REDIRECT_URI)
    parser.add_argument("--scopes", default=DEFAULT_SCOPES)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--code", help="Authorization code copied from the localhost callback URL.")
    parser.add_argument("--print-url", action="store_true", help="Only print the Oura authorization URL.")
    args = parser.parse_args()

    if not OURA_CLIENT_ID or not OURA_CLIENT_SECRET:
        raise SystemExit("Add oura_client_id and oura_client_secret to secrets.json or environment variables.")

    parsed = urllib.parse.urlparse(args.redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        raise SystemExit("This helper only supports localhost redirect URIs.")
    port = parsed.port or 80

    state = secrets.token_urlsafe(24)
    client = OuraClient(
        client_id=OURA_CLIENT_ID,
        client_secret=OURA_CLIENT_SECRET,
        token_file=TOKEN_FILE,
    )
    auth_url = client.authorization_url(args.redirect_uri, scopes=args.scopes, state=state)

    if args.code:
        client.exchange_code(args.code, args.redirect_uri)
        print(f"Tokens saved to {TOKEN_FILE}")
        return

    if args.print_url:
        print(auth_url)
        return

    print(f"Using redirect_uri: {args.redirect_uri}")
    print("Open this URL in your browser and approve access:")
    print(auth_url)
    print(f"Listening on 127.0.0.1:{port} for the OAuth callback...")

    code = receive_code(port=port, expected_state=state, timeout=args.timeout)
    if not code:
        raise SystemExit("Timed out waiting for Oura authorization callback.")
    client.exchange_code(code, args.redirect_uri)
    print(f"Tokens saved to {TOKEN_FILE}")


if __name__ == "__main__":
    main()
