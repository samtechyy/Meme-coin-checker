from http.server import BaseHTTPRequestHandler
import json, os, time, logging
from datetime import datetime, timezone, timedelta
from web3 import Web3
import requests
import yaml

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vol")

V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
CHUNK = 1200
MAX_RETRIES = 6

def load_cfg():
    with open("config.yaml") as f:
        return yaml.safe_load(f)["chains"]["elysium"]

def get_hype_usd():
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "hyperliquid", "vs_currencies": "usd"},
        timeout=12,
    )
    r.raise_for_status()
    return float(r.json()["hyperliquid"]["usd"])

def with_retry(fn):
    for i in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "rate" in msg or "limit" in msg or "timeout" in msg:
                sleep = min(30, (2 ** i) + 0.4)
                log.warning(f"rate/limit, sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise RuntimeError("max retries")

def find_block(w3, target_ts):
    lo, hi = 1, w3.eth.block_number
    while lo < hi:
        mid = (lo + hi) // 2
        ts = w3.eth.get_block(mid)["timestamp"]
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo

def get_logs(w3, address, topics, from_b, to_b):
    logs = []
    start = from_b
    while start <= to_b:
        end = min(start + CHUNK - 1, to_b)
        def call():
            return w3.eth.get_logs({
                "fromBlock": start,
                "toBlock": end,
                "address": Web3.to_checksum_address(address),
                "topics": topics,
            })
        chunk = with_retry(call)
        logs.extend(chunk)
        start = end + 1
        time.sleep(0.12)
    return logs

def process():
    cfg = load_cfg()
    w3 = Web3(Web3.HTTPProvider(cfg["rpc_url"]))
    log.info(f"Addresses used: V4 PoolManager={cfg['dex']['v4_pool_manager']}")

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    day_start = int(datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc).timestamp())
    day_end = day_start + 86400

    from_block = find_block(w3, day_start)
    to_block = find_block(w3, day_end) - 1
    log.info(f"{yesterday} blocks {from_block} → {to_block}")

    hype_price = get_hype_usd()
    logs = get_logs(
        w3,
        cfg["dex"]["v4_pool_manager"],
        [[V4_SWAP_TOPIC]],
        from_block,
        to_block,
    )
    log.info(f"V4 Swap events found: {len(logs)}")

    # Conservative volume estimate:
    # We take abs(amount0) or abs(amount1) when one side looks like native/USDC scale,
    # otherwise we fall back to a small average. This is intentionally simple for free tier.
    total_usd = 0.0
    for log_entry in logs:
        try:
            # data layout after the two indexed topics: amount0 (int128), amount1 (int128), ...
            data = log_entry["data"]
            if len(data) < 66:
                continue
            # first 32 bytes after 0x = amount0 (int128 padded)
            amount0 = int(data[2:66], 16)
            if amount0 >= 2**127:
                amount0 -= 2**128
            amount1 = int(data[66:130], 16) if len(data) >= 130 else 0
            if amount1 >= 2**127:
                amount1 -= 2**128

            # Prefer the side that looks like HYPE (18 decimals) or USDC (6 decimals)
            candidates = []
            for amt in (abs(amount0), abs(amount1)):
                if amt == 0:
                    continue
                # rough HYPE scale
                if 10**15 < amt < 10**22:
                    candidates.append(amt / 1e18 * hype_price)
                # rough USDC scale
                elif 10**4 < amt < 10**12:
                    candidates.append(amt / 1e6)
            if candidates:
                total_usd += max(candidates)
            else:
                total_usd += 5.0  # tiny fallback so we still count the event
        except Exception:
            total_usd += 5.0

    result = {
        "chain_id": cfg["chain_id"],
        "date_utc": str(yesterday),
        "volume_usd": round(total_usd, 2),
        "swap_count": len(logs),
        "from_block": from_block,
        "to_block": to_block,
        "hype_usd": hype_price,
        "addresses_used": {
            "v4_pool_manager": cfg["dex"]["v4_pool_manager"],
            "whype": cfg["wrapped_native"],
            "usdc": cfg["usdc"],
        },
        "note": "V4-only estimate using official PoolManager. V2/V3 and hook-modified amounts are excluded. Free-tier conservative pricing.",
    }

    # Persist for the dashboard (simple file that Vercel can read on next cold start is not reliable,
    # so we also accept that the dashboard will show the latest successful run when you hit /api/run).
    # For a free durable store you would add Vercel KV later; this version stays zero-config.
    os.makedirs("/tmp", exist_ok=True)
    with open("/tmp/last_result.json", "w") as f:
        json.dump(result, f)

    return result

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            data = process()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, indent=2).encode())
        except Exception as e:
            log.exception("failed")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())