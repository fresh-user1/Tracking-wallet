import time
import os
import requests
import psycopg2
import threading
from datetime import datetime

# --- KONFIGURASI API KEYS ---
DATABASE_URL = os.getenv("DATABASE_URL")
BLOCKSCOUT_API_KEY = os.getenv("BLOCKSCOUT_API_KEY") # Base Primary
SOLSCAN_API_KEY = os.getenv("SOLSCAN_API_KEY")       # Solana Primary
BLOCKCHAIR_API_KEY = os.getenv("BLOCKCHAIR_API_KEY") # Eth, BSC, & Backup

# --- KONFIGURASI RPC (Jalan Raya) ---
CHAINS = {
    'base': {
        'rpc': "https://base.llamarpc.com",
        'factory': "0xFDa619b6d20975be80A10332cD39b9a4b0FAa8BB", # BaseSwap
        'topic': "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
    },
    'ethereum': {
        'rpc': "https://eth.llamarpc.com",
        'factory': "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f", # Uniswap V2
        'topic': "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
    },
    'bsc': {
        'rpc': "https://binance.llamarpc.com",
        'factory': "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73", # PancakeSwap V2
        'topic': "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
    }
}

# Counter Error untuk Logika Pindah Jalur (Failover)
FAIL_COUNTS = {'blockscout': 0, 'solscan': 0}

# --- DATABASE FUNCTION ---
def save_to_db(deployer, funder, amount_usd, risk_score, evidence, chain):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        # Cek apakah dia sudah ada?
        cur.execute("SELECT id FROM suspect WHERE address = %s", (funder,))
        existing = cur.fetchone()
        timestamp = datetime.utcnow()
        
        if existing:
            cur.execute("""
                UPDATE suspect SET risk_score=5, status='Serial Scammer', timestamp=%s 
                WHERE address=%s
            """, (timestamp, funder))
            print(f"[{chain.upper()}] 🔄 UPDATE: {funder} -> Serial Scammer")
        else:
            cur.execute("""
                INSERT INTO suspect (address, chain, risk_score, impact_usd, status, evidence_link, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (funder, chain, risk_score, amount_usd, 'Detected by Bot', evidence, timestamp))
            print(f"[{chain.upper()}] ✅ NEW: {funder} (Tier {risk_score})")
            
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ DB Error: {e}")

# --- FUNGSI TRACING (DATA FETCHER) ---

def fetch_blockchair(chain_name, address):
    """
    Fungsi Cadangan (Backup) & Utama untuk ETH/BSC
    """
    # Mapping nama chain agar sesuai format URL Blockchair
    bc_map = {
        'base': 'base', # Pastikan plan kamu support base, kalau tidak fungsi ini return 0
        'ethereum': 'ethereum',
        'bsc': 'binance-smart-chain',
        'solana': 'solana'
    }
    
    target = bc_map.get(chain_name)
    url = f"https://api.blockchair.com/{target}/dashboards/address/{address}?key={BLOCKCHAIR_API_KEY}"
    
    try:
        res = requests.get(url, timeout=10).json()
        data = res['data'][address]
        # Ambil saldo USD sebagai indikator impact
        balance_usd = data['address'].get('balance_usd', 0)
        return "Unknown_Blockchair_Trace", balance_usd
    except:
        return None, 0

def trace_evm(chain, address):
    global FAIL_COUNTS
    
    # JIKA BASE: Coba Blockscout Dulu
    if chain == 'base':
        # Cek apakah error masih di bawah batas wajar (3x)
        if FAIL_COUNTS['blockscout'] < 3:
            url = f"https://base.blockscout.com/api?module=account&action=txlist&address={address}&sort=asc&page=1&offset=5&apikey={BLOCKSCOUT_API_KEY}"
            try:
                res = requests.get(url, timeout=10).json()
                if res['result']:
                    tx = res['result'][0]
                    if tx['to'].lower() == address.lower():
                        FAIL_COUNTS['blockscout'] = 0 # Reset error count karena sukses
                        return tx['from'], float(tx['value'])/10**18 * 2500 # Asumsi ETH $2500
            except:
                FAIL_COUNTS['blockscout'] += 1 # Tambah error count
                print(f"⚠️ Blockscout Error ({FAIL_COUNTS['blockscout']}/3)")
        
        # Jika error > 3x, Lanjut ke bawah (Blockchair)
        print("⚠️ Failover: Switching Base to Blockchair...")

    # JIKA ETH/BSC/BACKUP BASE: Pakai Blockchair
    funder, bal_usd = fetch_blockchair(chain, address)
    return funder if funder else address, bal_usd

def trace_solana(address):
    global FAIL_COUNTS
    
    # Coba SOLSCAN Dulu
    if FAIL_COUNTS['solscan'] < 3:
        url = f"https://public-api.solscan.io/account/transactions?account={address}&limit=5"
        headers = {"token": SOLSCAN_API_KEY}
        try:
            res = requests.get(url, headers=headers, timeout=10).json()
            if isinstance(res, list) and len(res) > 0:
                last_tx = res[-1]
                if 'signer' in last_tx:
                    FAIL_COUNTS['solscan'] = 0
                    return last_tx['signer'][0], 0
        except:
             FAIL_COUNTS['solscan'] += 1
    
    # Jika Solscan error, pakai Blockchair
    print("⚠️ Failover: Switching Solana to Blockchair...")
    return fetch_blockchair('solana', address)

# --- WORKER UTAMA (MONITORING) ---

def monitor_evm(chain_name):
    print(f"🚀 Worker Started: {chain_name.upper()}")
    rpc = CHAINS[chain_name]['rpc']
    factory = CHAINS[chain_name]['factory']
    topic = CHAINS[chain_name]['topic']
    
    # Ambil block awal
    try:
        curr_blk = int(requests.post(rpc, json={"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}, timeout=10).json()['result'], 16)
    except: curr_blk = 0
        
    while True:
        try:
            # Polling Logs dari Blockchain
            payload = {"jsonrpc":"2.0","method":"eth_getLogs","params":[{"fromBlock": hex(curr_blk), "toBlock": "latest", "address": factory, "topics": [topic]}],"id":1}
            res = requests.post(rpc, json=payload, timeout=10).json()
            
            if 'result' in res and res['result']:
                for log in res['result']:
                    # Ambil Deployer
                    tx_hash = log['transactionHash']
                    tx_res = requests.post(rpc, json={"jsonrpc":"2.0","method":"eth_getTransactionByHash","params":[tx_hash],"id":1}, timeout=10).json()
                    
                    if 'result' in tx_res and tx_res['result']:
                        deployer = tx_res['result']['from']
                        token_addr = "0x" + log['topics'][1][26:]
                        
                        print(f"\n[{chain_name.upper()}] New Token Detected!")
                        
                        # TRACE SUMBER DANA
                        funder, impact = trace_evm(chain_name, deployer)
                        
                        # SIMPAN KE DB
                        risk = 5 if impact > 10000 else 3
                        evid = f"https://dexscreener.com/{chain_name}/{token_addr}"
                        save_to_db(deployer, funder, impact, risk, evid, chain_name)
            
                # Update Block
                latest = int(requests.post(rpc, json={"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}, timeout=10).json()['result'], 16)
                if latest > curr_blk: curr_blk = latest + 1
            
            time.sleep(5)
            
        except Exception as e:
            print(f"[{chain_name}] Loop Error: {e}")
            time.sleep(10)

def monitor_solana():
    print("🚀 Worker Started: SOLANA")
    # Karena Solana susah via RPC biasa, kita gunakan DexScreener API untuk 'polling' token baru
    while True:
        try:
            # Mengambil data token terbaru di Solana
            url = "https://api.dexscreener.com/latest/dex/tokens/solana" 
            # (Note: Ini endpoint simplifikasi. Idealnya pakai Helius Webhook untuk real-time)
            
            # Placeholder Logic untuk menjaga thread tetap hidup
            time.sleep(15) 
        except:
            time.sleep(15)

if __name__ == "__main__":
    # Jalankan 4 Thread Sekaligus
    t1 = threading.Thread(target=monitor_evm, args=('base',))
    t2 = threading.Thread(target=monitor_evm, args=('ethereum',))
    t3 = threading.Thread(target=monitor_evm, args=('bsc',))
    t4 = threading.Thread(target=monitor_solana)
    
    t1.start()
    t2.start()
    t3.start()
    t4.start()
    
    t1.join() # Menjaga agar script tidak mati
