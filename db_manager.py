from datetime import datetime
from dbutils.pooled_db import PooledDB
import pymysql
import pyupbit
import os
from config import DB_CONF, CORE_UNIVERSE, send_telegram

pool = PooledDB(
    creator=pymysql,
    maxconnections=10, # 최대 동시 연결 수
    mincached=0,       # 💡 [수정] 봇 구동 시점의 DB 연결 충돌(Boot Race) 방지를 위해 0으로 변경 (Lazy Connection)
    blocking=True,
    ping=1,            # 💡 [추가] 커넥션 풀에서 가져올 때 유효성(ping) 검사 수행 (좀비 커넥션 방지)
    charset='utf8mb4', # 💡 [추가] 한글/이모지 등 문자열 깨짐 방지 
    **DB_CONF
)


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
        # conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        conn = pool.connection()
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
    #conn = pymysql.connect(**DB_CONF)
    conn = pool.connection()
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
    #conn = pymysql.connect(**DB_CONF)
    conn = pool.connection()
    
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
        # conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        conn = pool.connection()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 💡 [수정] WHERE 조건에 engine_name 추가 (타 엔진의 종목을 가져오는 버그 방지)
            sql = """
                SELECT ticker, engine_name, slot_index, buy_price, volume, buy_level, invested_amount, created_at 
                FROM current_positions 
                WHERE account_id = %s AND engine_name = %s
            """
            cur.execute(sql, (ACCOUNT_ID, ENGINE_TYPE))
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
        # 💡 [수정] 출력문에도 어떤 엔진의 포지션을 복구했는지 명시하도록 개선
        print(f"🔄 [{ACCOUNT_ID} - {ENGINE_TYPE}] DB에서 {len(positions)}개의 포지션을 복구했습니다. (최초 진입일 포함)")
    except Exception as e:
        print(f"❌ 포지션 복구 실패: {e}")
    finally:
        if conn: conn.close()
    
    return positions

# -------------------------------------------------------------
# 📈 보고서 생성
# -------------------------------------------------------------
from datetime import datetime, timedelta  # 💡 파일 상단에 timedelta가 임포트되어 있는지 확인해 주세요!

# -------------------------------------------------------------
# 📈 일일 보고서 생성 (과거 날짜 조회 지원)
# -------------------------------------------------------------
def get_today_performance(days_ago=0):
    """
    💡 특정 일자(오늘, 어제 등) 하루 동안의 '현재 계정' 엔진별 실현 손익 통계를 계산합니다.
    - 파라미터(days_ago): 0 = 오늘, 1 = 어제, 2 = 그제
    """
    try:
        # conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        conn = pool.connection()
    
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            
            # 1. 입력받은 days_ago 만큼 과거의 날짜 계산
            target_date = datetime.now() - timedelta(days=days_ago)
            
            # 2. 해당 날짜의 00시 00분 00초 ~ 23시 59분 59초 구간 텍스트 생성
            start_time = target_date.strftime('%Y-%m-%d 00:00:00')
            end_time = target_date.strftime('%Y-%m-%d 23:59:59')

            # 💡 3. [수정] trade_time이 시작 시간과 종료 시간 사이에 있도록 BETWEEN 조건 적용
            sql = """
                SELECT 
                    engine_name as engine,
                    SUM(realized_profit) as total_profit,
                    AVG(profit_rate) as avg_rate,
                    COUNT(*) as trade_count
                FROM trade_logs 
                WHERE account_id = %s 
                  AND trade_time BETWEEN %s AND %s 
                  AND side LIKE 'SELL%%' 
                GROUP BY engine_name
            """
            print(f"{ACCOUNT_ID} 보고서 생성 파라메터: {days_ago} / {start_time} ~ {end_time} \n")
            print(f"쿼리: {sql}")

            # 쿼리에 계정 ID와 시작/종료 시간을 파라미터로 전달
            cur.execute(sql, (ACCOUNT_ID, start_time, end_time))
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
        # conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        conn = pool.connection()
    
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

# -------------------------------------------------------------
# ⏸️ 엔진 상태 제어 (도커 컨테이너 간 상태 공유용)
# -------------------------------------------------------------
def set_engine_pause_state(engine_name, is_paused):
    """특정 엔진의 일시 정지 상태를 DB에 기록합니다."""
    try:
        # conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        conn = pool.connection()
    
        with conn.cursor() as cur:
            # 테이블이 없으면 자동 생성
            cur.execute("""
                CREATE TABLE IF NOT EXISTS engine_status (
                    engine_name VARCHAR(50) PRIMARY KEY,
                    is_paused BOOLEAN DEFAULT FALSE
                )
            """)
            # 상태 업데이트 (없으면 삽입, 있으면 수정)
            sql = """
                INSERT INTO engine_status (engine_name, is_paused) 
                VALUES (%s, %s) 
                ON DUPLICATE KEY UPDATE is_paused = %s
            """
            cur.execute(sql, (engine_name, is_paused, is_paused))
        conn.commit()
    except Exception as e:
        print(f"❌ 상태 기록 오류: {e}")
    finally:
        if 'conn' in locals() and conn: conn.close()

def is_engine_paused(engine_name):
    """현재 엔진이 일시 정지 상태인지 DB에서 확인합니다."""
    try:
        # conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        conn = pool.connection()
    
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 에러 방지를 위해 테이블 존재 여부를 무시하고 셀렉트 시도
            cur.execute("SELECT is_paused FROM engine_status WHERE engine_name = %s", (engine_name,))
            row = cur.fetchone()
            return bool(row['is_paused']) if row else False
    except:
        return False # 테이블이 없거나 에러가 나면 기본값(가동) 반환
    finally:
        if 'conn' in locals() and conn: conn.close()

# -------------------------------------------------------------
# 🧹 매도/초기화 시 포지션 완전 삭제
# -------------------------------------------------------------
def delete_position(engine_name, ticker, slot_index=1):
    """전량 매도 또는 강제 리셋 시 DB 장부에서 유령 데이터를 완전히 삭제합니다."""
    try:
        # conn = pymysql.connect(**DB_CONF, charset='utf8mb4')
        conn = pool.connection()
    
        with conn.cursor() as cur:
            # 내 계정의 해당 엔진, 코인, 슬롯 데이터를 통째로 날림
            sql = """
                DELETE FROM current_positions 
                WHERE account_id = %s AND engine_name = %s AND ticker = %s AND slot_index = %s
            """
            cur.execute(sql, (ACCOUNT_ID, engine_name, ticker, slot_index))
        conn.commit()
    except Exception as e:
        print(f"❌ 포지션 완전 삭제 오류: {e}")
    finally:
        if 'conn' in locals() and conn: 
            conn.close()
            
