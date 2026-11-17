from http.server import BaseHTTPRequestHandler
import json, os, time, logging
from datetime import datetime, timezone, timedelta
from web3 import Web3
from web3.exceptions import ContractLogicError
import requests
import yaml

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vol")

# ---------- Constants ----------
V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
V3_POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7119"
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

CHUNK = 1000          # smaller chunks for V3 safety
MAX_RETRIES = 6
MAX_V3_POOLS = 80     # hard limit so free RPC does not die

# ---------- Helpers ----------
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
            if any(x in msg for x in ["429", "rate", "limit", "timeout", "too many"]):
                sleep = min(25, (2 ** i) + 0.3)
                log.warning(f"rate/limit → sleep {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise RuntimeError("max retries exceeded")

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
                "address": Web3.to_checksum_address(address) if address else None,
                "topics": topics,
            })
        chunk = with_retry(call)
        logs.extend(chunk)
        start = end + 1
        time.sleep(0.15)
    return logs

# ---------- V3 helpers ----------
def discover_v3_pools(w3, factory, from_block, to_block):
    """Return list of pool addresses created in the given range (capped)."""
    logs = get_logs(w3, factory, [[V3_POOL_CREATED_TOPIC]], from_block, to_block)
    pools = []
    for lg in logs:
        # PoolCreated(address token0, address token1, uint24 fee, int24 tickSpacing, address pool)
        # pool is the last non-indexed 32-byte word
        if len(lg["data"]) >= 66:
            pool = "0x" + lg["data"][-40:]
            pools.append(Web3.to_checksum_address(pool))
    # also try a broader look if we got almost nothing (some pads create pools earlier)
    if len(pools) < 5:
        # look back ~2 days for more pools (still free-tier safe)
        older = max(1, from_block - 1_500_000)
        extra = get_logs(w3, factory, [[V3_POOL_CREATED_TOPIC]], older, from_block - 1)
        for lg in extra:
            if len(lg["data"]) >= 66:
                pool = "0x" + lg["data"][-40:]
                pools.append(Web3.to_checksum_address(pool))
    # unique + hard cap
    pools = list(dict.fromkeys(pools))[:MAX_V3_POOLS]
    log.info(f"V3 pools to scan: {len(pools)} (capped at {MAX_V3_POOLS})")
    return pools

def volume_from_v3_swaps(w3, pools, from_block, to_block, hype_price):
    total = 0.0
    count = 0
    for i, pool in enumerate(pools):
        try:
            logs = get_logs(w3, pool, [[V3_SWAP_TOPIC]], from_block, to_block)
            for lg in logs:
                # amount0 / amount1 are the first two 32-byte words (int256)
                data = lg["data"]
                if len(data) < 130:
                    continue
                amount0 = int(data[2:66], 16)
                if amount0 >= 2**255:
                    amount0 -= 2**256
                amount1 = int(data[66:130], 16)
                if amount1 >= 2**255:
                    amount1 -= 2**256

                candidates = []
                for amt in (abs(amount0), abs(amount1)):
                    if amt == 0:
                        continue
                    # HYPE-scale
                    if 10**15 < amt < 10**22:
                        candidates.append(amt / 1e18 * hype_price)
                    # USDC-scale
                    elif 10**3 < amt < 10**13:
                        candidates.append(amt / 1e6)
                if candidates:
                    total += max(candidates)
                else:
                    total += 3.0   # tiny fallback
                count += 1
        except Exception as e:
            log.warning(f"V3 pool {pool} failed: {e}")
        time.sleep(0.08)
        if (i + 1) % 10 == 0:
            log.info(f"  scanned {i+1}/{len(pools)} V3 pools…")
    return total, count

# ---------- Main processing ----------
def process():
    cfg = load_cfg()
    w3 = Web3(Web3.HTTPProvider(cfg["rpc_url"]))

    log.info("=" * 60)
    log.info("Addresses used this run:")
    log.info(f"  V3 Factory      : {cfg['dex']['v3_factory']}")
    log.info(f"  V4 PoolManager  : {cfg['dex']['v4_pool_manager']}")
    log.info(f"  WHYPE           : {cfg['wrapped_native']}")
    log.info(f"  USDC            : {cfg['usdc']}")
    log.info("=" * 60)

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    day_start = int(datetime(yesterday.year, yesterday.month, yesterday.day,
                             tzinfo=timezone.utc).timestamp())
    day_end = day_start + 86400

    from_block = find_block(w3, day_start)
    to_block = find_block(w3, day_end) - 1
    log.info(f"UTC day {yesterday} → blocks {from_block} → {to_block}")

    hype_price = get_hype_usd()
    log.info(f"HYPE/USD = {hype_price}")

    # ---------- V4 ----------
    v4_logs = get_logs(
        w3,
        cfg["dex"]["v4_pool_manager"],
        [[V4_SWAP_TOPIC]],
        from_block,
        to_block,
    )
    log.info(f"V4 Swap events: {len(v4_logs)}")

    v4_usd = 0.0
    for lg in v4_logs:
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

            candidates = []
            for amt in (abs(amount0), abs(amount1)):
                if amt == 0:
                    continue
                if 10**15 < amt < 10**22:
                    candidates.append(amt / 1e18 * hype_price)
                elif 10**3 < amt < 10**13:
                    candidates.append(amt / 1e6)
            if candidates:
                v4_usd += max(candidates)
            else:
                v4_usd += 5.0
        except Exception:
            v4_usd += 5.0

    # ---------- V3 ----------
    v3_pools = discover_v3_pools(
        w3,
        cfg["dex"]["v3_factory"],
        from_block,
        to_block,
    )
    v3_usd, v3_count = volume_from_v3_swaps(
        w3, v3_pools, from_block, to_block, hype_price
    )
    log.info(f"V3 volume estimate: ${v3_usd:,.0f} from {v3_count} swaps")

    total_usd = v4_usd + v3_usd

    result = {
        "chain_id": cfg["chain_id"],
        "date_utc": str(yesterday),
        "volume_usd": round(total_usd, 2),
        "v4_volume_usd": round(v4_usd, 2),
        "v3_volume_usd": round(v3_usd, 2),
        "v4_swap_count": len(v4_logs),
        "v3_swap_count": v3_count,
        "v3_pools_scanned": len(v3_pools),
        "from_block": from_block,
        "to_block": to_block,
        "hype_usd": hype_price,
        "addresses_used": {
            "v3_factory": cfg["dex"]["v3_factory"],
            "v4_pool_manager": cfg["dex"]["v4_pool_manager"],
            "whype": cfg["wrapped_native"],
            "usdc": cfg["usdc"],
        },
        "note": (
            "V4 (official PoolManager) + V3 (official factory, capped pools). "
            "Pricing is conservative (HYPE/USDC scale only). "
            "Hook-modified amounts and pure token-token pools are under-counted. "
            "Free-tier RPC may truncate very active days."
        ),
    }

    os.makedirs("/tmp", exist_ok=True)
    with open("/tmp/last_result.json", "w") as f:
        json.dump(result, f)

    log.info(f"TOTAL estimated volume: ${total_usd:,.0f}")
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
            log.exception("run failed")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())