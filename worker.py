import time
import os
import json
import requests
import psycopg2
import threading
from datetime import datetime

# --- CONFIG ---
DATABASE_URL = os.getenv("DATABASE_URL")
BLOCKSCOUT_API_KEY = os.getenv("BLOCKSCOUT_API_KEY") # Base Primary
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY")       # Solana Primary
BLOCKCHAIR_API_KEY = os.getenv("BLOCKCHAIR_API_KEY") # Emergency Backup

# --- LOAD LABELS (WHITELIST) ---
KNOWN_ENTITIES = {}
try:
    with open('labels.json', 'r') as f:
        KNOWN_ENTITIES = json.load(f)
    print("✅ Knowledge Base (labels.json) Loaded!")
except Exception as e:
    print(f"⚠️ Warning: Gagal load labels.json ({e}). Bot berjalan tanpa whitelist.")
    KNOWN_ENTITIES = {'base': {}, 'solana': {}}

# --- RPC CONFIG (BASE ONLY) ---
BASE_CONFIG = {
    'rpc': "https://base.llamarpc.com",
    'factory': "0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB", # BaseSwap
    'topic': "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
}

# Error Counter
FAIL_COUNTS = {'blockscout': 0, 'solscan': 0}

# --- DATABASE LOGIC ---
def save_to_db(deployer, funder_info, amount_usd, evidence, chain):
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        funder_addr = funder_info['address']
        risk_score = funder_info.get('risk', 3)
        status_label = funder_info.get('name', 'Detected by Bot')
        funder_type = funder_info.get('type', 'UNKNOWN')

        # JANGAN UPDATE JADI SCAMMER KALAU SUMBERNYA CEX/BRIDGE/DEX
        is_safe_entity = funder_type in ['CEX', 'BRIDGE', 'DEX', 'GOV']
        
        cur.execute("SELECT id FROM suspect WHERE address = %s", (funder_addr,))
        existing = cur.fetchone()
        timestamp = datetime.utcnow()
        
        if existing:
            if not is_safe_entity:
                cur.execute("""
                    UPDATE suspect SET risk_score=5, status='Serial Scammer', timestamp=%s 
                    WHERE address=%s
                """, (timestamp, funder_addr))
                print(f"[{chain.upper()}] 🚨 UPDATE: {funder_addr} is now SERIAL SCAMMER (Tier 5)")
        else:
            cur.execute("""
                INSERT INTO suspect (address, chain, risk_score, impact_usd, status, evidence_link, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (funder_addr, chain, risk_score, amount_usd, status_label, evidence, timestamp))
            print(f"[{chain.upper()}] ✅ NEW RECORD: {funder_addr} via {status_label}")
            
        conn.commit()
    except psycopg2.Error as e:
        print(f"❌ Database Error: {e}")
        if conn:
            conn.rollback()
    except Exception as e:
        print(f"❌ Unexpected Error in save_to_db: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

# --- TRACING LOGIC ---

def check_whitelist(chain, address):
    if chain in KNOWN_ENTITIES:
        addr_lower = address.lower()
        # Loop manual karena key di JSON mungkin uppercase/lowercase campuran
        for k, v in KNOWN_ENTITIES[chain].items():
            if k.lower() == addr_lower:
                return {'address': address, **v}
    return None

def fetch_blockchair_backup(chain, address):
    """Hanya dipanggil saat Emergency (API Utama Error)"""
    bc_chain = 'base' if chain == 'base' else 'solana'
    url = f"https://api.blockchair.com/{bc_chain}/dashboards/address/{address}?key={BLOCKCHAIR_API_KEY}"
    try:
        res = requests.get(url, timeout=10).json()
        if not isinstance(res, dict) or 'data' not in res or address not in res['data']:
            print(f"⚠️ Blockchair: Invalid response for {address}")
            return None, 0
        data = res['data'][address]
        # Balance USD is optional in Blockchair response
        usd = data.get('address', {}).get('balance_usd', 0)
        return {'address': address, 'type': 'UNKNOWN_BC', 'name': 'Unknown (Blockchair)', 'risk': 3}, usd
    except Exception as e:
        print(f"❌ Blockchair Error: {e}")
        return None, 0

def trace_base(address, depth=1):
    global FAIL_COUNTS
    
    # 1. Cek Whitelist
    known = check_whitelist('base', address)
    if known: return known, 0

    if depth > 5: return {'address': address, 'type': 'LIMIT', 'name': 'Trace Limit', 'risk': 3}, 0

    # 2. Blockscout (Primary)
    if FAIL_COUNTS['blockscout'] < 3:
        url = f"https://base.blockscout.com/api?module=account&action=txlist&address={address}&sort=asc&page=1&offset=10&apikey={BLOCKSCOUT_API_KEY}"
        try:
            res = requests.get(url, timeout=10).json()
            if res.get('result') and isinstance(res['result'], list):
                FAIL_COUNTS['blockscout'] = 0
                best_funder = None
                max_val = 0
                
                # Cari Funding Terbesar
                for tx in res['result']:
                    if tx['to'].lower() == address.lower():
                        val = float(tx['value']) / 10**18
                        if val > max_val:
                            max_val = val
                            best_funder = tx['from']
                
                if best_funder:
                    # REKURSIF: Lacak bapaknya
                    parent_info, _ = trace_base(best_funder, depth + 1)
                    if parent_info['type'] in ['CEX', 'BRIDGE', 'MIXER']:
                        return parent_info, max_val
                    
                    return {'address': best_funder, 'type': 'EOA', 'name': 'Private Wallet', 'risk': 3}, max_val
        except Exception as e:
            FAIL_COUNTS['blockscout'] += 1
            print(f"⚠️ Blockscout Fail ({FAIL_COUNTS['blockscout']}/3): {e}")

    # 3. Failover Blockchair
    print("⚠️ Using Blockchair Backup (Base)...")
    return fetch_blockchair_backup('base', address)

def trace_solana(address):
    global FAIL_COUNTS
    
    # 1. Whitelist
    known = check_whitelist('solana', address)
    if known: return known, 0

    # 2. Solscan (Primary)
    if FAIL_COUNTS['solscan'] < 3:
        url = f"https://public-api.solscan.io/account/transactions?account={address}&limit=5"
        headers = {"token": SOLSCAN_API_KEY}
        try:
            res = requests.get(url, headers=headers, timeout=10).json()
            if isinstance(res, list) and len(res) > 0:
                FAIL_COUNTS['solscan'] = 0
                last = res[-1]
                if 'signer' in last:
                    signer = last['signer'][0]
                    return {'address': signer, 'type': 'EOA', 'name': 'Solana Wallet', 'risk': 3}, 0
        except Exception as e:
            FAIL_COUNTS['solscan'] += 1
            print(f"⚠️ Solscan Fail ({FAIL_COUNTS['solscan']}/3): {e}")

    # 3. Failover Blockchair
    print("⚠️ Using Blockchair Backup (Solana)...")
    return fetch_blockchair_backup('solana', address)

# --- WORKER LOOPS ---

def monitor_base():
    print("🚀 Worker: BASE Network Started")
    rpc = BASE_CONFIG['rpc']
    
    try:
        curr = int(requests.post(rpc, json={"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}, timeout=10).json()['result'], 16)
    except Exception as e:
        print(f"⚠️ Failed to get initial block number: {e}")
        curr = 0
        
    while True:
        try:
            # Polling Logs
            payload = {"jsonrpc":"2.0","method":"eth_getLogs","params":[{"fromBlock": hex(curr), "toBlock": "latest", "address": BASE_CONFIG['factory'], "topics": [BASE_CONFIG['topic']]}],"id":1}
            res = requests.post(rpc, json=payload, timeout=10).json()
            
            if 'result' in res and res['result']:
                for log in res['result']:
                    tx_h = log['transactionHash']
                    tx_r = requests.post(rpc, json={"jsonrpc":"2.0","method":"eth_getTransactionByHash","params":[tx_h],"id":1}, timeout=10).json()
                    
                    if 'result' in tx_r and tx_r['result']:
                        deployer = tx_r['result']['from']
                        token_addr = "0x" + log['topics'][1][26:]
                        
                        print(f"\n[BASE] New Token: {token_addr}")
                        
                        # TRACE
                        funder_info, amount = trace_base(deployer)
                        
                        # SAVE (Asumsi harga ETH $2500)
                        evidence = f"https://dexscreener.com/base/{token_addr}"
                        save_to_db(deployer, funder_info, amount * 2500, evidence, 'base')
                        
                latest = int(requests.post(rpc, json={"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}, timeout=10).json()['result'], 16)
                if latest > curr: curr = latest + 1
            
            time.sleep(5)
        except Exception as e:
            print(f"[BASE] Error: {e}")
            time.sleep(10)

def monitor_solana():
    print("🚀 Worker: SOLANA Started")
    # Placeholder Logic: Di production gunakan Helius/Quicknode Webhook untuk real new token
    # Disini kita sleep biar thread jalan tapi tidak spam error
    while True:
        time.sleep(30)

if __name__ == "__main__":
    t_base = threading.Thread(target=monitor_base)
    t_sol = threading.Thread(target=monitor_solana)
    
    t_base.start()
    t_sol.start()
    
    t_base.join()
