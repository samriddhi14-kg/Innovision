import os, sys, socket
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('../.env')
load_dotenv('.env')

tok = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
print(f'Token: {tok}')

import requests
from requests.adapters import HTTPAdapter

class TelegramDNSAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        _orig = socket.getaddrinfo
        def _patch(host, port, *a, **kw):
            if host == "api.telegram.org":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ("149.154.167.220", port))]
            return _orig(host, port, *a, **kw)
        socket.getaddrinfo = _patch
        try:
            return super().send(request, **kwargs)
        finally:
            socket.getaddrinfo = _orig

session = requests.Session()
session.mount("https://api.telegram.org", TelegramDNSAdapter())

# Check if bot token is valid
resp = session.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=15)
print(f'getMe status: {resp.status_code}')
print(f'getMe response: {resp.json()}')
