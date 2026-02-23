#!/usr/bin/env python3
"""
Obtain Fitbit OAuth tokens (run once to populate fitbit_tokens.json).

REQUIRED: Add this Redirect URL in Fitbit app settings:
  https://dev.fitbit.com/apps → Edit Application Settings → Redirect URLs
  Add: http://localhost:8080/callback
"""
import base64
import json
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

CLIENT_ID = '23TZQ2'
CLIENT_SECRET = '165fd221429d62de5d7093ee7796f39c'
REDIRECT = 'http://localhost:8080/callback'
SCOPES = 'activity heartrate nutrition profile settings sleep weight'
TOKEN_FILE = Path(__file__).resolve().parent / 'fitbit_tokens.json'

auth_url = f'https://www.fitbit.com/oauth2/authorize?response_type=code&client_id={CLIENT_ID}&redirect_uri={urllib.parse.quote(REDIRECT)}&scope={urllib.parse.quote(SCOPES)}'
code_holder: list[str] = []


def run_server():
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if 'error' in qs:
                err = qs.get('error', ['unknown'])[0]
                desc = qs.get('error_description', [''])[0]
                print(f"Fitbit error: {err} - {desc}")
            elif 'code' in qs:
                code_holder.append(qs['code'][0])
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(b'Authorization successful. You can close this tab.')
            # Don't call server.shutdown() - it deadlocks when not using serve_forever()

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(('127.0.0.1', 8080), Handler)
    httpd.handle_request()


print('Using redirect_uri: http://localhost:8080/callback')
print('Ensure this URL is in your Fitbit app Redirect URLs (Edit Application Settings)')
print('Listening on http://127.0.0.1:8080 (complete auth in browser)...')
webbrowser.open(auth_url)
t = threading.Thread(target=run_server, daemon=True)
t.start()
t.join(timeout=120)
if t.is_alive():
    print('Timed out after 2 minutes. Did you complete authorization?')
    print('Fitbit must redirect to http://localhost:8080/callback - check app settings.')
    exit(1)

if not code_holder:
    print('No authorization code received.')
    exit(1)

code = code_holder[0]
auth = 'Basic ' + base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()
r = requests.post(
    'https://api.fitbit.com/oauth2/token',
    headers={'Authorization': auth, 'Content-Type': 'application/x-www-form-urlencoded'},
    data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': REDIRECT,
    },
    timeout=15,
)
if r.status_code != 200:
    print(f"Token exchange failed ({r.status_code}): {r.text}")
    exit(1)
t_resp = r.json()
expires_in = t_resp.get('expires_in', 28800)
data = {
    'access_token': t_resp['access_token'],
    'refresh_token': t_resp['refresh_token'],
    'expires_at': int(time.time()) + expires_in,
}
with open(TOKEN_FILE, 'w') as f:
    json.dump(data, f, indent=2)
print(f'Tokens saved to {TOKEN_FILE}')
