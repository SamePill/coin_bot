from datetime import datetime
import pymysql
import pyupbit
from config import DB_CONF, CORE_UNIVERSE, send_telegram


def log_trade(market, side, engine, price, volume, profit_rate=0.0, realized_profit=0.0):
    """💡 매매 내역과 실현 수익금(KRW)을 함께 기록합니다."""
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor() as cur:
            # realized_profit 컬럼에 값을 추가하도록 쿼리 수정
            cur.execute("""
                INSERT INTO trade_logs (market, side, price, volume, profit_rate, realized_profit) 
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (market, f"{engine}_{side}", price, volume, profit_rate, realized_profit))
        conn.commit(); conn.close()
    except Exception as e: 
        print(f"DB 오류: {e}")

def recover_bot_positions(upbit):
    """💡 [버그 완벽 패치] 개인 자산 보호를 위해 DB 거래내역 기반으로 순수 봇 수량만 복구"""
    bot_positions = {}
    try:
        balances = upbit.get_balances()
        if not isinstance(balances, list): return bot_positions
        
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
        
        with conn.cursor() as cur:
            for b in balances:
                if 'currency' not in b or b['currency'] == 'KRW': continue
                ticker = f"KRW-{b['currency']}"
                actual_vol = float(b['balance']) 
                
                # 최근 100개의 거래 내역을 역추적하여 봇의 순수 보유량 계산
                cur.execute("SELECT side, volume, price FROM trade_logs WHERE market = %s ORDER BY id DESC LIMIT 100", (ticker,))
                rows = cur.fetchall()
                
                if not rows: continue
                
                net_vol = 0.0
                buy_price = 0.0
                engine_type = "HUNTER"
                
                # 포지션이 완전히 청산되는 키워드 (이 키워드를 만나면 그 이전 과거는 무시)
                full_sell_keywords = ['858AM', 'NIGHT', 'CHANDELIER', 'STRUCT', 'TIME', 'TRAIL', 'SL']
                
                for row in rows:
                    side = row['side'].upper()
                    vol = float(row['volume'])
                    
                    if any(kw in side for kw in full_sell_keywords):
                        break # 현재 사이클의 끝 (이전 과거 단절)
                    
                    if 'BUY' in side:
                        net_vol += vol
                        buy_price = float(row['price']) # 루프의 마지막(가장 오래된) 매수가가 기준 평단가가 됨
                        if 'CORE' in side: engine_type = 'CORE'
                        elif 'GRID' in side: engine_type = 'GRID'
                        elif 'HUNTER' in side: engine_type = 'HUNTER'
                    elif 'SELL' in side:
                        net_vol -= vol # 절반 익절, 그리드 상단 타격 등 부분 매도 차감
                        
                # 봇이 계산한 수량이 유의미하게 존재할 때만 복구
                if net_vol > 0.00001:
                    # 💡 핵심 방어막: 봇 계산 수량과 업비트 실제 수량 중 작은 것을 택함 (개인 장투 물량 침범 원천 차단)
                    final_vol = min(net_vol, actual_vol)
                    
                    curr_price = pyupbit.get_current_price(ticker)
                    if curr_price and (curr_price * final_vol > 5000):
                        bot_positions[ticker] = {
                            'vol': final_vol, 
                            'buy': buy_price, 
                            'peak': curr_price, 
                            'half_sold': False, 
                            'engine': engine_type
                        }
        conn.close()
        if bot_positions: send_telegram(f"🔄 [기억 복구] 봇 전용 자산 {len(bot_positions)}개 종목 복원 완료. (개인 자산 격리)")
    except Exception as e: 
        print(f"복구 중 오류: {e}")
        
    return bot_positions

def get_today_performance():
    """💡 오늘 하루 동안의 엔진별 실현 손익 및 총합계를 계산합니다."""
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 오늘 00:00:00 기준
            today_start = datetime.now().strftime('%Y-%m-%d 00:00:00')
        
            sql = """
                SELECT 
                    SUBSTRING_INDEX(market, '_', 1) as engine,
                    SUM(realized_profit) as total_profit,
                    AVG(profit_rate) as avg_rate,
                    COUNT(*) as trade_count
                FROM trade_logs 
                WHERE trade_time >= %s AND side LIKE '%%SELL%%' 
                GROUP BY engine
            """
    
            cur.execute(sql, (today_start,))
            rows = cur.fetchall()
            return rows
    except Exception as e:
        print(f"DB 보고서 생성 오류: {e}")
        return []
    finally:
        if conn: conn.close()
