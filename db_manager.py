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
    """DB에서 현재 포지션과 매수 차수(buy_level)를 복구합니다."""
    positions = {}
    conn = None
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # buy_level 컬럼 추가 조회
            sql = "SELECT ticker, engine_name, slot_index, buy_price, volume, buy_level FROM current_positions"
            cur.execute(sql)
            rows = cur.fetchall()
            
            for r in rows:
                ticker = r['ticker']
                slot_idx = r['slot_index']
                key = f"{ticker}_slot_{slot_idx}"
                
                positions[key] = {
                    'ticker': ticker,
                    'buy': float(r['buy_price']),
                    'vol': float(r['volume']),
                    'slot_index': slot_idx,
                    'engine_name': r['engine'],
                    'buy_level': r['buy_level'] if r['buy_level'] is not None else 1
                }
        print(f"🔄 DB에서 {len(positions)}개의 포지션을 성공적으로 복구했습니다.")
    except Exception as e:
        print(f"❌ 포지션 복구 실패: {e}")
    finally:
        if conn: conn.close()
    
    return positions

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

def update_position_state(key, real_avg_price, real_vol, next_level):
    """물타기(피라미딩) 성공 후, 진짜 평단가와 매수 차수를 DB에 안전하게 기록합니다."""
    # key 예시: "KRW-BTC_slot_1" -> 여기서 ticker와 slot_index를 분리
    parts = key.split('_slot_')
    if len(parts) != 2: return
    
    ticker = parts[0]
    slot_index = int(parts[1])
    conn = None
    
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor() as cur:
            sql = """
                UPDATE current_positions 
                SET buy_price = %s, volume = %s, buy_level = %s, updated_at = NOW()
                WHERE ticker = %s AND slot_index = %s
            """
            cur.execute(sql, (real_avg_price, real_vol, next_level, ticker, slot_index))
        conn.commit()
    except Exception as e:
        print(f"❌ DB 상태 업데이트 실패 ({ticker}): {e}")
    finally:
        if conn: conn.close()