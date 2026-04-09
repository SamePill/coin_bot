import pyupbit
import pymysql
from datetime import datetime, timedelta
from config import UPBIT_ACCESS, UPBIT_SECRET, DB_CONF

def print_header():
    print("==================================================")
    print(f"📊 Aegis-Elite V17.17 시스템 상태 및 효율성 보고서")
    print(f"🕒 조회일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("==================================================\n")

def check_upbit_portfolio():
    """1. 지갑 상태 실시간 조회 (Upbit API)"""
    print("[1. 현재 포트폴리오 및 평가 수익률]")
    upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)
    
    try:
        balances = upbit.get_balances()
        has_coin = False
        
        for b in balances:
            coin = b['currency']
            # 현금 및 에어드랍/잡코인 제외
            if coin in ['KRW', 'VTHO', 'APENFT']: 
                continue 
            
            ticker = f"KRW-{coin}"
            buy_price = float(b['avg_buy_price'])
            vol = float(b['balance'])
            curr_price = pyupbit.get_current_price(ticker)
            
            if curr_price and buy_price > 0:
                rate = (curr_price - buy_price) / buy_price * 100
                total_value = curr_price * vol
                print(f" 🔹 {ticker:<10} | 수익률: {rate:>+6.2f}% | 평가금액: {total_value:>10,.0f}원")
                has_coin = True
                
        if not has_coin: 
            print(" 🔹 현재 보유 중인 암호화폐가 없습니다 (전액 현금 관망 중).")
            
    except Exception as e:
        print(f" ❌ 업비트 API 조회 실패: {e}")

def check_db_realized_profit():
    """2. DB 실현 수익 조회"""
    print("\n[2. 금일 실현 손익 (DB 기준)]")
    conn = None
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
                    profit = r['profit'] if r['profit'] is not None else 0
                    print(f" 🔹 {r['side']:<15} | 실현손익: {profit:>+10,.0f}원")
                    total += profit
                print("-" * 50)
                print(f" 💵 총 합계        | 누적수익: {total:>+10,.0f}원")
    except Exception as e:
        print(f" ❌ DB 연결 또는 쿼리 실패: {e}")
    finally:
        if conn: conn.close()

def analyze_fee_efficiency():
    """3. 수수료 대비 매매 효율성 분석 (Step 1 핵심 로직)"""
    print("\n[3. 수수료 대비 매매 효율성 분석 (최근 7일)]")
    conn = None
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
            
            # 7일간의 매도 건수 및 평균 실현 수익 조회
            sql = """
                SELECT COUNT(*) as sell_count, AVG(realized_profit) as avg_profit 
                FROM trade_logs 
                WHERE trade_time >= %s AND side LIKE '%%SELL%%'
            """
            cur.execute(sql, (seven_days_ago,))
            result = cur.fetchone()
            
            sell_count = result['sell_count'] if result['sell_count'] else 0
            avg_profit = float(result['avg_profit']) if result['avg_profit'] else 0.0
            
            if sell_count == 0:
                print(" 🔹 최근 7일간 분석할 매도 데이터가 없습니다.")
                return

            # 업비트 수수료: 매수 0.05%, 매도 0.05% (총 0.1% 수준)
            # 현재 기본 세팅인 6,000원 거래 시 예상 총 수수료는 약 6원
            assumed_trade_unit = 6000 
            estimated_fee = assumed_trade_unit * 0.001 
            
            # 순수익 계산 (평균 수익금 - 예상 수수료)
            net_profit_per_trade = avg_profit - estimated_fee
            
            print(f" 🔹 최근 7일 매도 체결 수 : {sell_count}회")
            print(f" 🔹 1회 체결당 평균 수익  : {avg_profit:,.1f}원")
            print(f" 🔹 체결당 예상 수수료    : 약 {estimated_fee:,.1f}원 (6천원 매매 기준)")
            print("-" * 50)
            
            if net_profit_per_trade > estimated_fee * 2:
                print(f" ✅ 진단: 우수. 1회당 순수익({net_profit_per_trade:,.1f}원)이 수수료를 여유롭게 상회합니다.")
            elif net_profit_per_trade > 0:
                print(f" ⚠️ 진단: 주의. 수익은 나고 있으나 수수료 비중이 높습니다. (순수익: {net_profit_per_trade:,.1f}원)")
                print("    -> 해결책: 그리드 간격을 넓히거나, 1회 투자 금액(Unit Size)을 늘리는 것을 권장합니다.")
            else:
                print(f" ❌ 진단: 위험! 잦은 매매로 인해 수수료가 수익을 갉아먹고 있습니다. (순손실: {net_profit_per_trade:,.1f}원)")
                print("    -> 해결책: 최소 매매 금액을 상향 조정해야 합니다.")

    except Exception as e:
        print(f" ❌ 데이터 분석 실패: {e}")
    finally:
        if conn: conn.close()

def main():
    print_header()
    check_upbit_portfolio()
    check_db_realized_profit()
    analyze_fee_efficiency()
    print("\n==================================================")

if __name__ == "__main__":
    main()