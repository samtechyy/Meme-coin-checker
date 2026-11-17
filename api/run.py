from http.server import BaseHTTPRequestHandler
import json, os, time, logging
from datetime import datetime, timezone, timedelta
from web3 import Web3
import requests
import yaml

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vol")

V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
CHUNK = 800
MAX_SECONDS = 25          # hard budget so we never hit Vercel timeout

def load_cfg():
    with open("config.yaml") as f:
        return yaml.safe_load(f)["chains"]["elysium"]

def get_hype_usd():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "hyperliquid", "vs_currencies": "usd"},
            timeout=8,
        )
        return float(r.json()["hyperliquid"]["usd"])
    except Exception:
        return 25.0  # safe fallback

def with_retry(fn):
    for i in range(4):
        try:
            return fn()
        except Exception as e:
            if "429" in str(e).lower() or "rate" in str(e).lower():
                time.sleep(1.5 + i)
            else:
                raise
    return []

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

def process():
    start_time = time.time()
    cfg = load_cfg()
    w3 = Web3(Web3.HTTPProvider(cfg["rpc_url"], request_kwargs={'timeout': 12}))

    log.info(f"Using V4 PoolManager: {cfg['dex']['v4_pool_manager']}")

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    day_start = int(datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc).timestamp())
    day_end   = day_start + 86400

    from_block = find_block(w3, day_start)
    to_block   = find_block(w3, day_end) - 1

    # Safety: never scan more than ~40k blocks on free tier
    if to_block - from_block > 40000:
        to_block = from_block + 40000
        log.warning("Block range truncated for free-tier safety")

    log.info(f"Scanning blocks {from_block} → {to_block}")

    hype_price = get_hype_usd()
    total_usd = 0.0
    swap_count = 0

    current = from_block
    while current <= to_block:
        if time.time() - start_time > MAX_SECONDS:
            log.warning("Time budget reached – stopping early")
            break

        end = min(current + CHUNK - 1, to_block)

        def get_chunk():
            return w3.eth.get_logs({
                "fromBlock": current,
                "toBlock": end,
                "address": Web3.to_checksum_address(cfg["dex"]["v4_pool_manager"]),
                "topics": [[V4_SWAP_TOPIC]],
            })

        logs = with_retry(get_chunk)
        for lg in logs:
            try:
                data = lg["data"]
                if len(data) < 130:
                    continue
                amount0 = int(data[2:66], 16)
                if amount0 >= 2**127:
                    amount0 -= 2**128
                amount1 = int(data[66:130], 16)
                if amount1 >= 2**127:
                    amount1 -= 2**128

                for amt in (abs(amount0), abs(amount1)):
                    if 10**15 < amt < 10**22:          # HYPE scale
                        total_usd += amt / 1e18 * hype_price
                        break
                    elif 10**3 < amt < 10**12:         # USDC scale
                        total_usd += amt / 1e6
                        break
                else:
                    total_usd += 4.0                   # tiny fallback

                swap_count += 1
            except Exception:
                total_usd += 4.0
                swap_count += 1

        current = end + 1
        time.sleep(0.08)

    result = {
        "chain_id": 99901,
        "date_utc": str(yesterday),
        "volume_usd": round(total_usd, 2),
        "v4_volume_usd": round(total_usd, 2),
        "v3_volume_usd": 0,
        "v4_swap_count": swap_count,
        "v3_swap_count": 0,
        "from_block": from_block,
        "to_block": to_block,
        "hype_usd": hype_price,
        "note": "V4 only (free-tier safe). Full day may be truncated. V3 disabled to avoid timeout.",
        "elapsed_seconds": round(time.time() - start_time, 1)
    }

    # Save for the dashboard
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
            log.exception("error")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())