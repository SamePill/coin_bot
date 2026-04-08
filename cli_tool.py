import pyupbit
import pymysql
from datetime import datetime
from config import UPBIT_ACCESS, UPBIT_SECRET, DB_CONF

print("==================================================")
print(f"📊 Aegis-Elite V17.17 시스템 상태 보고서")
print(f"🕒 조회일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("==================================================\n")

# 1. 지갑 상태 실시간 조회 (Upbit API)
print("[1. 현재 포트폴리오 및 평가 수익률]")
upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)
try:
    balances = upbit.get_balances()
    has_coin = False
    for b in balances:
        coin = b['currency']
        if coin in ['KRW', 'VTHO', 'APENFT']: continue # 현금 및 에어드랍 잡코인 제외
        
        ticker = f"KRW-{coin}"
        buy_price = float(b['avg_buy_price'])
        vol = float(b['balance'])
        curr_price = pyupbit.get_current_price(ticker)
        
        if curr_price and buy_price > 0:
            rate = (curr_price - buy_price) / buy_price * 100
            total_value = curr_price * vol
            print(f" 🔹 {ticker:<10} | 수익률: {rate:>+6.2f}% | 평가금액: {total_value:>10,.0f}원")
            has_coin = True
            
    if not has_coin: print(" 🔹 현재 보유 중인 암호화폐가 없습니다 (전액 현금 관망 중).")
except Exception as e:
    print(f" ❌ 업비트 API 조회 실패: {e}")

# 2. DB 실현 수익 조회
print("\n[2. 금일 실현 손익 (DB 기준)]")
try:
    conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        today_start = datetime.now().strftime('%Y-%m-%d 00:00:00')
        sql = """
            SELECT side, SUM(realized_profit) as profit 
            FROM trade_logs 
            WHERE trade_time >= %s AND side LIKE '%%SELL%%' 
            GROUP BY side
        """
        cur.execute(sql, (today_start,))
        rows = cur.fetchall()
        
        if not rows:
            print(" 🔹 오늘 발생한 매도(익절/손절) 내역이 없습니다.")
        else:
            total = 0
            for r in rows:
                print(f" 🔹 {r['side']:<15} | 실현손익: {r['profit']:>+10,.0f}원")
                total += r['profit']
            print("-" * 50)
            print(f" 💵 총 합계        | 누적수익: {total:>+10,.0f}원")
except Exception as e:
    print(f" ❌ DB 연결 또는 쿼리 실패: {e}")
finally:
    if 'conn' in locals() and conn: conn.close()

print("\n==================================================")