import os
import time
import threading
import traceback  
from datetime import datetime, timedelta
import pandas as pd 
import pyupbit
from dotenv import load_dotenv

# --- [1. 환경 변수 및 멀티 슬롯 설정 로드] ---
load_dotenv()
ENGINE_TYPE = os.getenv('ENGINE_TYPE', 'CORE').upper()
MAX_BUDGET = float(os.getenv('MAX_BUDGET', 0))
TARGET_SLOTS = int(os.getenv('TARGET_SLOTS', 3)) # CORE, HUNTER용 고정 슬롯

# 그리드 전용 확장 설정
GRID_TOTAL_SLOTS = int(os.getenv('GRID_TOTAL_SLOTS', 2))  # 예: 8 (고정1 + 유동7)
USE_MULTI_SLOT = os.getenv('USE_MULTI_SLOT', 'True').lower() == 'true'
MAX_SLOTS_PER_COIN = int(os.getenv('MAX_SLOTS_PER_COIN', 2))
# 투자 단위 리스트 (A, B, C... 순서대로 적용)
UNIT_LIST = [float(x) for x in os.getenv('GRID_UNIT_SIZES', '10000,30000').split(',')]

SCALP_TOTAL_SLOTS = int(os.getenv('SCALP_TOTAL_SLOTS', 2))
SCALP_USE_MULTI_SLOT = os.getenv('SCALP_USE_MULTI_SLOT', 'True').lower() == 'true'
SCALP_MAX_SLOTS_PER_COIN = int(os.getenv('SCALP_MAX_SLOTS_PER_COIN', 2))

CG_TOTAL_SLOTS = int(os.getenv('CG_TOTAL_SLOTS', 2))
CG_USE_MULTI_SLOT = os.getenv('CG_USE_MULTI_SLOT', 'True').lower() == 'true'
CG_MAX_SLOTS_PER_COIN = int(os.getenv('CG_MAX_SLOTS_PER_COIN', 2))

# 💡 [V17.18] Monkey Patching (API 호출 초과 시 오토 힐링 추가)
_original_get_current_price = pyupbit.get_current_price

def _safe_get_current_price(ticker, limit_info=False, verbose=False):
    retries = 3
    for i in range(retries):
        try:
            res = _original_get_current_price(ticker, limit_info, verbose)
            if res is not None: 
                return res
        except Exception as e:
            if "Too Many Requests" in str(e) or "429" in str(e):
                print(f"⚠️ [API 과부하] 호출 제한 도달. 0.5초 대기 후 재시도... ({i+1}/{retries})")
                time.sleep(0.5) # 숨 고르기
            else:
                pass
    return {} if isinstance(ticker, list) else None

pyupbit.get_current_price = _safe_get_current_price

# 사용자 정의 모듈 임포트
from config import *
import db_manager
import analyzer
import worker
import telegram_handler 

# 💡 [추가] 분리된 클래스 엔진 임포트
from engines.core_engine import CoreEngine
from engines.hunter_engine import HunterEngine
from engines.grid_engine import GridEngine
from engines.scalp_engine import ScalpEngine
from engines.classic_grid_engine import ClassicGridEngine

# --- [전역 변수 초기화] ---
upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)
SEED_MONEY = 0
bot_positions = {}
# 💡 상태 변경을 보호할 전역 Lock 생성
bot_positions_lock = threading.Lock() 
current_regime = "NORMAL"
core_targets, hunter_targets = {}, {}
top_grid_candidates = []
last_grid_eval_time = None
next_day_core_targets, next_day_hunter_targets = {}, {}
last_target_fetch_time = None

symbol = "🏹" if ENGINE_TYPE == 'HUNTER' else "🕸️" if ENGINE_TYPE == 'CLASSIC_GRID' else "🛡️" if ENGINE_TYPE == 'CORE' else "⚡" if ENGINE_TYPE == 'SCALP' else "🎰" if ENGINE_TYPE == 'GRID' else "🤖"
print(f"====================================================")
print(f"🏆 [시스템] Aegis-Elite V17.17 무결성 패치 가동 (모드: {ENGINE_TYPE})")
if ENGINE_TYPE == 'GRID':
    print(f"{symbol} 그리드 슬롯: {GRID_TOTAL_SLOTS} | 다중슬롯: {USE_MULTI_SLOT} (Max {MAX_SLOTS_PER_COIN})")
    send_telegram(
        f"[{symbol}{ENGINE_TYPE} 시동 완료 !!!]\n"
        f"- 그리드 슬롯: {GRID_TOTAL_SLOTS} \n"
        f"- 다중슬롯: {USE_MULTI_SLOT} (Max {MAX_SLOTS_PER_COIN}\n"
        f"- 💰 할당 예산: {MAX_BUDGET:,.0f}원"
    )
elif ENGINE_TYPE == 'SCALP':
    print(f"{symbol} Scalp 슬롯: {SCALP_TOTAL_SLOTS} | 다중슬롯: {SCALP_USE_MULTI_SLOT} (Max {SCALP_MAX_SLOTS_PER_COIN})")
    send_telegram(
        f"[{symbol}{ENGINE_TYPE} 시동 완료 !!!]\n"
        f"- 그리드 슬롯: {SCALP_TOTAL_SLOTS} \n"
        f"- 다중슬롯: {SCALP_USE_MULTI_SLOT} (Max {SCALP_MAX_SLOTS_PER_COIN}\n"
        f"- 💰 할당 예산: {MAX_BUDGET:,.0f}원"
    )
elif ENGINE_TYPE == 'CLASSIC_GRID':
    print(f"{symbol} ClassicGrid 슬롯: {CG_TOTAL_SLOTS} ") #| 다중슬롯: {CG_USE_MULTI_SLOT} (Max {CG_MAX_SLOTS_PER_COIN})")    
    send_telegram(
        f"[{symbol}{ENGINE_TYPE} 시동 완료 !!!]\n"
        f"- 그리드 슬롯: {CG_TOTAL_SLOTS} \n"
#        f"- 다중슬롯: {CG_USE_MULTI_SLOT} (Max {CG_MAX_SLOTS_PER_COIN}\n"
        f"- 💰 할당 예산: {MAX_BUDGET:,.0f}원"
    )
else:
    print(f"{symbol} 타겟 슬롯: {TARGET_SLOTS}")
    send_telegram(
        f"[{symbol}{ENGINE_TYPE} 시동 완료 !!!]\n"
        f"- 💰 할당 예산: {MAX_BUDGET:,.0f}원"
    )
print(f"💰 할당 예산: {MAX_BUDGET:,.0f}원")
print(f"====================================================\n")


def background_target_fetcher():
    global core_targets, hunter_targets, current_regime
    if current_regime == "ICE_AGE":
        print("💤 [동면] 시장 빙하기로 인해 타겟 탐색 스킵.")
        return

    print("🕵️‍♂️ 4H 레이더 가동 (CORE/HUNTER 스캔 중)...")
    temp_core, temp_hunter_candidates = {}, []
    
    # 1. CORE 타겟 스캔 (돌파 매매용)
    for ticker in CORE_UNIVERSE:
        time.sleep(0.15)
        df = pyupbit.get_ohlcv(ticker, interval="day", count=20)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 6: continue
        df['noise'] = 1 - abs(df['open'] - df['close']) / (df['high'] - df['low'])
        temp_core[ticker] = {
            'open': df.iloc[-1]['open'], 
            'range': analyzer.get_atr(df, 5), 
            'k': max(0.4, min(0.7, df['noise'].mean()))
        }
        
    # 2. HUNTER 타겟 스캔 (낙폭과대 매매용)
    try:
        tickers = pyupbit.get_tickers(fiat="KRW")
        for t in tickers:
            if t in CORE_UNIVERSE: continue 
            time.sleep(0.1)
            df = pyupbit.get_ohlcv(t, interval="day", count=6)
            if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 6: continue
            temp_hunter_candidates.append({'ticker': t, 'value': df.iloc[-2]['value'], 'open': df.iloc[-1]['open'], 'range': analyzer.get_atr(df, 5)})
        
        if temp_hunter_candidates:
            top10 = sorted(temp_hunter_candidates, key=lambda x: x['value'], reverse=True)[:3]
            hunter_targets = {item['ticker']: item for item in top3}
    except: pass
    
    core_targets = temp_core
    print("✅ [레이더] CORE/HUNTER 타겟 갱신 완료.")

# -------------------------------------------------------------
# 🕵️‍♂️ 그리드 전용: 종목 발굴 및 리밸런싱 로직
# -------------------------------------------------------------
def evaluate_grid_candidates():
    global top_grid_candidates
    try:
        print("🔍 [그리드 레이더] 최적 사냥터 스캔 중...")
        # 마켓 전체 스캔을 대상으로 할 경우 사용
        # all_tickers = pyupbit.get_tickers(fiat="KRW")
        # config.py에 정의된 GRID_POOL 목록만 가져와서 스캔 대상으로 삼습니다.
        all_tickers = GRID_POOL

        scores = []
        
        for t in all_tickers:
            #이더리움 단독 고정 슬롯 사용시만 필요
            #if t == "KRW-ETH": continue. 
            score = analyzer.get_grid_suitability_score(t)
            if score > 0:
                scores.append({'ticker': t, 'score': score})
            time.sleep(0.05) 
            
        sorted_scores = sorted(scores, key=lambda x: x['score'], reverse=True)
        top_grid_candidates = [item['ticker'] for item in sorted_scores[:GRID_TOTAL_SLOTS]]
        # 💡 [수정] 메신저 중복 발송 방지 (GRID 봇만 대표로 알림 전송)
        if ENGINE_TYPE == 'GRID':
            msg = f"🔍 [그리드 레이더] 신규 타겟 선정 완료\n- 후보: {', '.join(top_grid_candidates[:5])}..."
            send_telegram(msg)
    except Exception as e:
        print(f"❌ 후보 스캔 오류: {e}")

# -------------------------------------------------------------
# 🔄 메인 제어 루프
# -------------------------------------------------------------
bot_positions = db_manager.recover_bot_positions(upbit)
for k, v in bot_positions.items():
    if 'buy_level' not in v:
        v['buy_level'] = 1
    # 💡 [필수 복구] 헌터 엔진 구조적 손절가 세팅
    if v['engine'] == 'HUNTER' and 'struct_stop' not in v: 
        v['struct_stop'] = analyzer.get_structural_stop(v['ticker'])

# 텔레그램 봇 백그라운드 가동 (이 한 줄 필수 추가!)
# -------------------------------------------------------------
# 🔄 메신저 당번 지정 (Conflict 에러 방지)
# -------------------------------------------------------------
# 환경 변수에서 ENABLE_TELEGRAM_COMMANDS를 읽어옵니다. (기본값은 False)
ENABLE_TELEGRAM_COMMANDS = os.getenv('ENABLE_TELEGRAM_COMMANDS', 'False').lower() == 'true'

if ENABLE_TELEGRAM_COMMANDS:
    # 💡 도커 컴포즈에서 True로 설정된 단 하나의 엔진만 이 코드를 실행합니다.
    telegram_handler.start_telegram_listener(bot_positions, bot_positions_lock, lambda: MAX_BUDGET)
    print(f"🤖 [{ENGINE_TYPE}] 텔레그램 명령 수신 당번 가동 시작!")
else:
    print(f"🔇 [{ENGINE_TYPE}] 텔레그램 명령 수신을 스킵합니다. (중복 방지 모드)")

# -------------------------------------------------------------
# 💡 [추가] 환경 변수에 맞는 매매 엔진 인스턴스 생성 (Factory)
# -------------------------------------------------------------
active_engine = None
if ENGINE_TYPE == 'CORE':
    active_engine = CoreEngine(upbit, bot_positions, bot_positions_lock)
elif ENGINE_TYPE == 'HUNTER':
    active_engine = HunterEngine(upbit, bot_positions, bot_positions_lock)
elif ENGINE_TYPE == 'GRID':
    active_engine = GridEngine(upbit, bot_positions, bot_positions_lock)
elif ENGINE_TYPE == 'SCALP':
    active_engine = ScalpEngine(upbit, bot_positions, bot_positions_lock)
elif ENGINE_TYPE == 'CLASSIC_GRID':
    active_engine = ClassicGridEngine(upbit, bot_positions, bot_positions_lock)

# 자동 일일 보고를 위한 변수
last_daily_report_day = None

# 💡 [추가] 연속 에러 알림 스팸 방지용 카운터
consecutive_errors = 0

last_daily_report_hour = -1  # 💡 추가: 마지막으로 보고서를 보낸 시간 기록
# 💡 [추가] 무한 루프 시작 '바로 위'에 카운터 변수를 하나 만들어 줍니다.
pause_log_counter = 0

while True:
    try:
        now = datetime.now()
        
        # 💡 매일 아침 8시 0분에 한 번만 자동 보고서 발송
        if ENGINE_TYPE == 'GRID' and now.hour == 8 and now.minute == 0 and last_daily_report_day != now.day:
            rows = db_manager.get_today_performance(1)
            report_msg = f"🌅 [아침 브리핑] 어제 총 결산\n\n"
            if not rows:
                report_msg += "어제는 완료된 매매가 없었습니다."
            else:
                total_krw = 0
                for r in rows:
                    report_msg += f"- {r['engine']}: {r['total_profit']:+,.0f}원\n"
                    total_krw += r['total_profit']
                report_msg += f"──────────────\n💵 총 실현 손익: {total_krw:+,.0f}원"
            
            send_telegram(report_msg)
            last_daily_report_day = now.day # 발송 완료 기록

        # 💡 [수정] 정기 보고서 발송 (8, 13, 18, 23시) - GRID에서만 보내기 (중복발송))
        report_hours = [13, 18, 23]
        if ENGINE_TYPE == 'GRID' and now.hour in report_hours and now.minute == 0 and last_daily_report_hour != now.hour:
            rows = db_manager.get_today_performance(0)
            # db_manager.ACCOUNT_ID를 사용하여 어떤 계정의 보고서인지 명시합니다.
            report_msg = f"📊 [{db_manager.ACCOUNT_ID}] 정기 수익 보고 ({now.hour}시)\n\n"
            
            if not rows:
                report_msg += "현재까지 완료된 매매 내역이 없습니다."
            else:
                total_krw = 0
                for r in rows:
                    # 수익금(total_profit)과 수익률(avg_rate)을 함께 표시합니다.
                    report_msg += f"- {r['engine']}: {r['total_profit']:+,.0f}원 ({r['avg_rate']:+.2f}%)\n"
                    total_krw += r['total_profit']
                report_msg += f"──────────────\n💵 당일 총 합계: {total_krw:+,.0f}원"
            
            send_telegram(report_msg)
            last_daily_report_hour = now.hour # 💡 해당 시간 발송 완료 기록


        if now.minute % 15 == 0:
            current_regime = analyzer.get_market_regime(current_regime)

        # 💡 [추가] 4시간마다 CORE/HUNTER 타겟 스캔 (50분 언저리에 실행하여 API 몰림 방지)
        # 💡 [수정] CORE와 HUNTER 엔진일 때만 레이더 가동
        if ENGINE_TYPE in ['CORE', 'HUNTER']:
            if now.hour % 4 == 0 and now.minute == 50 and (last_target_fetch_time is None or now >= last_target_fetch_time + timedelta(hours=3)):
                last_target_fetch_time = now 
                threading.Thread(target=background_target_fetcher).start()

        if ENGINE_TYPE in ['GRID', 'SCALP','CLASSIC_GRID']:
            if last_grid_eval_time is None or now >= last_grid_eval_time + timedelta(hours=6):
                evaluate_grid_candidates()
                last_grid_eval_time = now

        if analyzer.check_panic_fall():
            time.sleep(10); continue

        # 💡 [수정] DB를 조회하여 엔진 일시 정지 상태 확인
        if db_manager.is_engine_paused(ENGINE_TYPE):
            
            # 카운터가 0이거나 60의 배수일 때만(즉 1분마다) 출력
            if pause_log_counter % 60 == 0:  
                print(f"⏸️ [{ENGINE_TYPE}] 엔진 루프 일시 정지 중... (텔레그램 /resume 대기)")
            
            pause_log_counter += 1
            
            # 🚨 핵심: 여기는 반드시 1초로 두어야 텔레그램 명령에 즉각 반응합니다!
            time.sleep(10) 
            continue # 아래 엔진 로직을 스킵하고 무한 대기
        
        # 정지가 풀려서 일반 매매로 넘어가면 카운터 초기화
        pause_log_counter = 0

        # 💡 [극적 개선!] 이제 수백 줄짜리 분기문이 이 4줄로 완벽히 대체됩니다!
        if active_engine:
            if ENGINE_TYPE == 'CORE':
                active_engine.run(now, current_regime, core_targets)
            elif ENGINE_TYPE == 'HUNTER':
                active_engine.run(now, current_regime, hunter_targets)
            elif ENGINE_TYPE in ['GRID', 'SCALP', 'CLASSIC_GRID']:
                active_engine.run(now, current_regime, top_grid_candidates)

        # 💡 루프가 에러 없이 정상적으로 끝까지 도달하면 에러 카운터 초기화
        consecutive_errors = 0

        #loop_delay = 1 if ENGINE_TYPE == 'HUNTER' else 3
        # 💡 [수정] 엔진별 루프 대기 시간(심장 박동) 차등화로 API 병목 분산
        if ENGINE_TYPE == 'SCALP': 
            loop_delay = 0.5  # 짤짤이는 0.5초마다 아주 빠르게 반응!
        elif ENGINE_TYPE == 'HUNTER': 
            loop_delay = 1.5  # 헌터는 1.5초
        elif ENGINE_TYPE == 'GRID':
            loop_delay = 3.0  # 스윙 그물망은 3초
        elif ENGINE_TYPE == 'CLASSIC_GRID':
            loop_delay = 0.5  # 클래식 그물망은 0.5초
        else: # CORE
            loop_delay = 5.0  # 코어(추세)는 5초마다 천천히 확인해도 충분함
        time.sleep(loop_delay)

    except Exception as e:
        print(f"🚨 [{ENGINE_TYPE}] 루프 에러: {e}")
        traceback.print_exc()
        
        # 💡 [추가] 에러 발생 시 텔레그램 긴급 노티 발송 및 스팸 방지
        consecutive_errors += 1
        
        # 연속 3회까지만 텔레그램을 발송하고, 이후는 콘솔에만 기록하여 스팸을 방지합니다.
        if consecutive_errors <= 3:
            error_msg = (
                f"🚨 [{symbol}{ENGINE_TYPE} 봇 긴급 오류]\n"
                f"시스템 루프에서 에러가 발생했습니다.\n\n"
                f"원인: {str(e)[:150]}" # 텔레그램 메시지 길이 제한 방지를 위해 150자로 자름
            )
            
            if consecutive_errors == 3:
                error_msg += "\n\n⚠️ 동일 오류가 지속 반복되어 알림을 일시 중단합니다. 서버를 즉시 확인해 주세요!"
                
            try:
                send_telegram(error_msg)
            except:
                pass
                
        # 에러 발생 시 오토 힐링 및 과부하 방지를 위해 대기 시간을 기존 5초에서 10초로 연장
        time.sleep(10)
