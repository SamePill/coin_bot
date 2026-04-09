from datetime import datetime
import pymysql
import pyupbit
import os
from config import DB_CONF, CORE_UNIVERSE, send_telegram

# 💡 [추가/수정] 환경 변수에서 엔진 타입과 '계정 식별자(ACCOUNT_ID)' 로드
ENGINE_TYPE = os.getenv('ENGINE_TYPE', 'WHAT?').upper()
ACCOUNT_ID = os.getenv('ACCOUNT_ID', 'WHO?').upper()  # 설정 없으면 'MAIN'으로 기본 동작

# -------------------------------------------------------------
# 📊 매매 기록 (trade_logs)
# -------------------------------------------------------------
def log_trade(market, side, price, volume, profit_rate=0.0, realized_profit=0.0):
    """
    💡 매매 내역과 실현 수익금을 기록합니다. (다중 계정 격리 반영)
    """
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor() as cur:
            # 💡 [수정] account_id 컬럼 추가
            sql = """
                INSERT INTO trade_logs (account_id, market, engine_name, side, price, volume, profit_rate, realized_profit) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            cur.execute(sql, (
                ACCOUNT_ID, 
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
        print(f"❌ DB 기록 오류 ({ENGINE_TYPE} - {ACCOUNT_ID}): {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

# -------------------------------------------------------------
# 🗄️ 현장 장부 관리 (current_positions) - 다중 슬롯 및 계정 대응
# -------------------------------------------------------------
def get_engine_invested_total(engine_name):
    """💡 특정 계정의 특정 엔진이 사용 중인 총 투자 원금 조회"""
    conn = pymysql.connect(**DB_CONF)
    try:
        with conn.cursor() as cur:
            # 💡 [수정] account_id 조건 추가
            sql = "SELECT SUM(invested_amount) FROM current_positions WHERE account_id = %s AND engine_name = %s"
            cur.execute(sql, (ACCOUNT_ID, engine_name))
            result = cur.fetchone()
            return float(result[0]) if result[0] and result[0] is not None else 0.0
    except Exception as e:
        print(f"❌ 예산 조회 오류: {e}")
        return 0.0
    finally:
        conn.close()

# -------------------------------------------------------------
# 🗄️ 현장 장부 관리 (update_position)
# -------------------------------------------------------------
def update_position(engine_name, ticker, price, volume, side, slot_index=1):
    conn = pymysql.connect(**DB_CONF)
    try:
        with conn.cursor() as cur:
            if side == 'BUY':
                # 💡 [수정] INSERT 시 created_at은 DB DEFAULT를 사용하거나 NOW()를 명시할 수 있습니다.
                # ON DUPLICATE KEY UPDATE 시에는 created_at을 건드리지 않아 최초 진입 시간이 보존됩니다.
                sql = """
                    INSERT INTO current_positions (account_id, engine_name, ticker, slot_index, buy_price, volume, invested_amount, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    ON DUPLICATE KEY UPDATE 
                        buy_price = ((buy_price * volume) + (%s * %s)) / (volume + %s),
                        invested_amount = invested_amount + %s,
                        volume = volume + %s
                """
                invested = price * volume
                cur.execute(sql, (ACCOUNT_ID, engine_name, ticker, slot_index, price, volume, invested, price, volume, volume, invested, volume))
            else:
                sql = "DELETE FROM current_positions WHERE account_id = %s AND engine_name = %s AND ticker = %s AND slot_index = %s"
                cur.execute(sql, (ACCOUNT_ID, engine_name, ticker, slot_index))
        conn.commit()
    except Exception as e:
        print(f"❌ 장부 갱신 오류 ({ticker}): {e}")
    finally:
        conn.close()

# -------------------------------------------------------------
# 🔄 기억 복구 로직 (recover_bot_positions)
# -------------------------------------------------------------
def recover_bot_positions(upbit):
    positions = {}
    conn = None
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 💡 [수정] SELECT 절에 created_at 추가
            sql = "SELECT ticker, engine_name, slot_index, buy_price, volume, buy_level, invested_amount, created_at FROM current_positions WHERE account_id = %s"
            cur.execute(sql, (ACCOUNT_ID,))
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
                    'engine': r['engine_name'], 
                    'buy_level': r['buy_level'] if r['buy_level'] is not None else 1,
                    'created_at': r['created_at'], # 💡 [추가] 최초 진입 시간 복구
                    'invested_amount': float(r['invested_amount'])
                }
        print(f"🔄 [{ACCOUNT_ID}] DB에서 {len(positions)}개의 포지션을 복구했습니다. (최초 진입일 포함)")
    except Exception as e:
        print(f"❌ 포지션 복구 실패: {e}")
    finally:
        if conn: conn.close()
    
    return positions

# -------------------------------------------------------------
# 📈 보고서 생성
# -------------------------------------------------------------
def get_today_performance():
    """💡 오늘 하루 동안의 '현재 계정' 엔진별 실현 손익 통계를 계산합니다."""
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            today_start = datetime.now().strftime('%Y-%m-%d 00:00:00')
            # 💡 [수정] account_id 필터를 걸어 내 계정의 수익만 집계
            sql = """
                SELECT 
                    engine_name as engine,
                    SUM(realized_profit) as total_profit,
                    AVG(profit_rate) as avg_rate,
                    COUNT(*) as trade_count
                FROM trade_logs 
                WHERE account_id = %s AND trade_time >= %s AND side = 'SELL' 
                GROUP BY engine_name
            """
            cur.execute(sql, (ACCOUNT_ID, today_start))
            return cur.fetchall()
    except Exception as e:
        print(f"❌ 보고서 생성 오류: {e}")
        return []
    finally:
        if 'conn' in locals() and conn:
            conn.close()

def update_position_state(key, real_avg_price, real_vol, next_level):
    """물타기(피라미딩) 성공 후, 평단가와 매수 차수를 내 계정 장부에만 기록합니다."""
    parts = key.split('_slot_')
    if len(parts) != 2: return
    
    ticker = parts[0]
    slot_index = int(parts[1])
    conn = None
    
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        with conn.cursor() as cur:
            # 💡 [수정] account_id 조건 추가 및 안전성을 위해 engine_name 조건도 추가
            sql = """
                UPDATE current_positions 
                SET buy_price = %s, volume = %s, buy_level = %s
                WHERE account_id = %s AND engine_name = %s AND ticker = %s AND slot_index = %s
            """
            cur.execute(sql, (real_avg_price, real_vol, next_level, ACCOUNT_ID, ENGINE_TYPE, ticker, slot_index))
        conn.commit()
    except Exception as e:
        print(f"❌ DB 상태 업데이트 실패 ({ticker}): {e}")
    finally:
        if conn: conn.close()