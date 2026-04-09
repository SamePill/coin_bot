from datetime import datetime
import pymysql
import pyupbit
import os
from config import DB_CONF, CORE_UNIVERSE, send_telegram

# 환경 변수에서 현재 엔진 타입 로드 (기본값 CORE)
ENGINE_TYPE = os.getenv('ENGINE_TYPE', 'CORE').upper()

# -------------------------------------------------------------
# 📊 매매 기록 (trade_logs)
# -------------------------------------------------------------
def log_trade(market, side, price, volume, profit_rate=0.0, realized_profit=0.0):
    """
    💡 매매 내역과 실현 수익금을 기록합니다.
    - engine_name: 도커 환경 변수에서 읽어온 엔진 이름
    - side: BUY 또는 SELL (순수 주문 방향)
    """
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor() as cur:
            sql = """
                INSERT INTO trade_logs (market, engine_name, side, price, volume, profit_rate, realized_profit) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            cur.execute(sql, (
                market, 
                ENGINE_TYPE,
                side, 
                price, 
                volume, 
                profit_rate, 
                realized_profit
            ))
        conn.commit()
    except Exception as e: 
        print(f"❌ DB 기록 오류 ({ENGINE_TYPE}): {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

# -------------------------------------------------------------
# 🗄️ 현장 장부 관리 (current_positions) - 다중 슬롯 대응
# -------------------------------------------------------------
def get_engine_invested_total(engine_name):
    """💡 특정 엔진이 현재 모든 슬롯에서 사용 중인 총 투자 원금(원화)을 조회합니다."""
    conn = pymysql.connect(**DB_CONF)
    try:
        with conn.cursor() as cur:
            sql = "SELECT SUM(invested_amount) FROM current_positions WHERE engine_name = %s"
            cur.execute(sql, (engine_name,))
            result = cur.fetchone()
            return float(result[0]) if result[0] and result[0] is not None else 0.0
    except Exception as e:
        print(f"❌ 예산 조회 오류: {e}")
        return 0.0
    finally:
        conn.close()

def update_position(engine_name, ticker, price, volume, side, slot_index=1):
    """💡 매수/매도 시 슬롯 인덱스별로 장부를 최신화합니다."""
    conn = pymysql.connect(**DB_CONF)
    try:
        with conn.cursor() as cur:
            if side == 'BUY':
                # 슬롯별로 독립적인 평단가와 수량을 기록 (Upsert)
                sql = """
                    INSERT INTO current_positions (engine_name, ticker, slot_index, buy_price, volume, invested_amount)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE 
                        buy_price = ((buy_price * volume) + (%s * %s)) / (volume + %s),
                        invested_amount = invested_amount + %s,
                        volume = volume + %s
                """
                invested = price * volume
                cur.execute(sql, (engine_name, ticker, slot_index, price, volume, invested, price, volume, volume, invested, volume))
            else:
                # 특정 엔진의 특정 종목, 특정 슬롯만 삭제
                sql = "DELETE FROM current_positions WHERE engine_name = %s AND ticker = %s AND slot_index = %s"
                cur.execute(sql, (engine_name, ticker, slot_index))
        conn.commit()
    except Exception as e:
        print(f"❌ 장부 갱신 오류 ({ticker} Slot {slot_index}): {e}")
    finally:
        conn.close()

# -------------------------------------------------------------
# 🔄 기억 복구 로직 (현장 장부 기반)
# -------------------------------------------------------------
def recover_bot_positions(upbit):
    """💡 [V17.17 업그레이드] current_positions 테이블을 기반으로 봇의 상태를 완벽히 복구합니다."""
    bot_positions = {}
    try:
        balances = upbit.get_balances()
        if not isinstance(balances, list): return bot_positions
        
        # 실제 계좌 잔고를 딕셔너리로 변환 (빠른 조회용)
        real_balances = {f"KRW-{b['currency']}": float(b['balance']) for b in balances if b['currency'] != 'KRW'}
        
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
        with conn.cursor() as cur:
            # 현재 이 엔진이 관리해야 할 장부 데이터 전체 로드
            sql = "SELECT * FROM current_positions WHERE engine_name = %s"
            cur.execute(sql, (ENGINE_TYPE,))
            rows = cur.fetchall()
            
            for row in rows:
                ticker = row['ticker']
                slot_idx = row['slot_index']
                db_vol = float(row['volume'])
                
                # 봇 식별자 키 생성 (예: KRW-BTC_slot_1)
                key = f"{ticker}_slot_{slot_idx}"
                
                # 업비트 실제 잔고 확인 (장투 물량 보호용)
                if ticker in real_balances:
                    actual_vol = real_balances[ticker]
                    # 장부 수량과 실제 수량 중 작은 것을 선택 (안전장치)
                    final_vol = min(db_vol, actual_vol)
                    
                    if final_vol > 0.00001:
                        curr_price = pyupbit.get_current_price(ticker)
                        bot_positions[key] = {
                            'ticker': ticker,
                            'vol': final_vol,
                            'buy': float(row['buy_price']),
                            'peak': curr_price if curr_price else float(row['buy_price']),
                            'slot_index': slot_idx,
                            'engine': ENGINE_TYPE,
                            'half_sold': False  # 복구 시 초기화
                        }
        conn.close()
        if bot_positions:
            send_telegram(f"🔄 [{ENGINE_TYPE}] {len(bot_positions)}개 슬롯 상태 복구 완료.")
    except Exception as e: 
        print(f"❌ 복구 중 오류: {e}")
        
    return bot_positions

# -------------------------------------------------------------
# 📈 보고서 생성
# -------------------------------------------------------------
def get_today_performance():
    """💡 오늘 하루 동안의 엔진별 실현 손익 통계를 계산합니다."""
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            today_start = datetime.now().strftime('%Y-%m-%d 00:00:00')
            sql = """
                SELECT 
                    engine_name as engine,
                    SUM(realized_profit) as total_profit,
                    AVG(profit_rate) as avg_rate,
                    COUNT(*) as trade_count
                FROM trade_logs 
                WHERE trade_time >= %s AND side = 'SELL' 
                GROUP BY engine_name
            """
            cur.execute(sql, (today_start,))
            return cur.fetchall()
    except Exception as e:
        print(f"❌ 보고서 생성 오류: {e}")
        return []
    finally:
        if 'conn' in locals() and conn:
            conn.close()