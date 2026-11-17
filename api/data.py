from http.server import BaseHTTPRequestHandler
import json, os

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = "/tmp/last_result.json"
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
            rows = [data]
        else:
            rows = [{
                "chain_id": 99901,
                "date_utc": "no-data-yet",
                "volume_usd": 0,
                "pct_change": None,
                "note": "Run /api/run first (or wait for the daily cron)"
            }]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(rows).encode())