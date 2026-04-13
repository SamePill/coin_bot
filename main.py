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
CG_USE_MULTI_SLOT = os.getenv('SCALP_USE_MULTI_SLOT', 'True').lower() == 'true'
CG_MAX_SLOTS_PER_COIN = int(os.getenv('SCALP_MAX_SLOTS_PER_COIN', 2))

# 💡 [동적 분산] ENABLED_ENGINES를 읽어와 활성화된 엔진 수만큼 API 호출 시간을 균등 분배합니다.
ENABLED_ENGINES_STR = os.getenv('ENABLED_ENGINES', 'CORE,HUNTER,GRID,SCALP,CLASSIC_GRID')
ACTIVE_ENGINES = [e.strip().upper() for e in ENABLED_ENGINES_STR.split(',') if e.strip()]

active_count = len(ACTIVE_ENGINES) if len(ACTIVE_ENGINES) > 0 else 1
regime_interval = max(1, 15 // active_count) # 15분 주기를 활성 엔진 수로 나눔 (예: 3개면 5분 간격)

startup_delays, regime_offsets = {}, {}
for i, engine in enumerate(ACTIVE_ENGINES):
    startup_delays[engine] = i * 4             # 시작 지연: 0초, 4초, 8초... 동적 할당
    regime_offsets[engine] = i * regime_interval # 백그라운드 스캔: 0분, 5분, 10분... 동적 할당

delay_sec = startup_delays.get(ENGINE_TYPE, 0)
if delay_sec > 0:
    print(f"🚦 [{ENGINE_TYPE}] API 병목 방지를 위해 {delay_sec}초 대기 후 가동합니다...")
    time.sleep(delay_sec)

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
            err_msg = str(e)
            if "Too Many Requests" in err_msg or "429" in err_msg:
                print(f"⚠️ [API 과부하] 호출 제한 도달. 2초 대기 후 재시도... ({i+1}/{retries})")
                time.sleep(2) # 숨 고르기
            elif "string indices must be integers" in err_msg or err_msg == "0" or "list index" in err_msg:
                print(f"⚠️ [API 차단 방어] 업비트 방화벽(WAF) 차단 응답 감지. 3초 대기... ({i+1}/{retries})")
                time.sleep(3)
            else:
                print(f"⚠️ [네트워크 에러] 시세 조회 지연 - 사유: {err_msg[:50]}... ({i+1}/{retries})")
                time.sleep(1)
    return {} if isinstance(ticker, list) else None

pyupbit.get_current_price = _safe_get_current_price

# 💡 [추가] 단일 잔고 조회(get_balance) 연결 타임아웃 오토 힐링 패치
_original_get_balance = pyupbit.Upbit.get_balance

def _safe_get_balance(self, *args, **kwargs):
    retries = 3
    for i in range(retries):
        try:
            res = _original_get_balance(self, *args, **kwargs)
            if res is not None:
                return res
        except Exception as e:
            err_msg = str(e)
            if "Too Many" in err_msg or "429" in err_msg or err_msg == "0" or "string indices" in err_msg:
                print(f"⚠️ [API 차단 방어] 단일 잔고 조회 WAF 차단. 2초 대기... ({i+1}/{retries})")
                time.sleep(2)
            else:
                print(f"⚠️ [네트워크 에러] 단일 잔고 조회 지연 - 사유: {err_msg[:50]}... ({i+1}/{retries})")
                time.sleep(1)
    return 0

pyupbit.Upbit.get_balance = _safe_get_balance

# 💡 [추가] 잔고 조회(get_balances) 연결 타임아웃 오토 힐링 패치
_original_get_balances = pyupbit.Upbit.get_balances

def _safe_get_balances(self, *args, **kwargs):
    retries = 3
    for i in range(retries):
        try:
            res = _original_get_balances(self, *args, **kwargs)
            if res is not None:
                return res
        except Exception as e:
            err_msg = str(e)
            if "Too Many Requests" in err_msg or "429" in err_msg or err_msg == "0" or "string indices" in err_msg:
                print(f"⚠️ [API 차단 방어] 전체 잔고 조회 WAF 차단. 2초 대기... ({i+1}/{retries})")
                time.sleep(2)
            else:
                print(f"⚠️ [네트워크 에러] 전체 잔고 조회 지연 - 사유: {err_msg[:50]}... ({i+1}/{retries})")
                time.sleep(1)
    return []

pyupbit.Upbit.get_balances = _safe_get_balances

# 💡 [추가] get_ohlcv 무한 호출 방지 (일봉 데이터 캐싱 패치)
_original_get_ohlcv = pyupbit.get_ohlcv
_ohlcv_cache = {}

def _safe_get_ohlcv(ticker, interval="day", count=200, to=None, period=0.1):
    now = datetime.now()
    if to is None:
        cache_key = f"{ticker}_{interval}_{count}"
        
        cache_duration = 0
        if interval == "day": cache_duration = 3600
        elif interval == "minute60": cache_duration = 1800 # 💡 [추가] 60분봉도 30분 동안 캐싱
        
        if cache_duration > 0 and cache_key in _ohlcv_cache:
            cached_time, cached_df = _ohlcv_cache[cache_key]
            if (now - cached_time).total_seconds() < cache_duration: 
                return cached_df

    time.sleep(0.1) # 기본 API 속도 조절
    for i in range(3):
        try:
            df = _original_get_ohlcv(ticker, interval=interval, count=count, to=to, period=period)
            if df is not None and not (isinstance(df, list) and len(df) == 0):
                if cache_duration > 0:
                    _ohlcv_cache[cache_key] = (now, df)
                return df
        except Exception as e:
            err_msg = str(e)
            if "Too Many" in err_msg or "429" in err_msg or "string" in err_msg or err_msg == "0":
                time.sleep(2)
            else:
                time.sleep(0.5)
    return None

pyupbit.get_ohlcv = _safe_get_ohlcv

# 사용자 정의 모듈 임포트
from config import *
import db_manager
import analyzer
import worker
import telegram_handler 

# --- [전역 변수 초기화] ---
upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)
SEED_MONEY = 0
bot_positions = {}
current_regime = "NORMAL"
core_targets, hunter_targets = {}, {}
top_grid_candidates = []
last_grid_eval_time = None
next_day_core_targets, next_day_hunter_targets = {}, {}
last_target_fetch_time = None
budget_lock_notified = {'SCALP': False, 'GRID': False, 'CLASSIC_GRID': False}
last_panic_check_time = datetime.now() + timedelta(seconds=delay_sec) # 💡 [분산] 10초 주기 체크를 컨테이너별로 오프셋 적용
is_panic_state = False
last_regime_check_time = None

# 💡 [API 몰림 방지] 활성 엔진 순서에 맞춰 최초 그리드 스캔 시간을 15분 간격으로 동적 분산
idx = ACTIVE_ENGINES.index(ENGINE_TYPE) if ENGINE_TYPE in ACTIVE_ENGINES else 0
last_grid_eval_time = datetime.now() - timedelta(hours=5, minutes=60 - (idx * 15))

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



# -------------------------------------------------------------
# 🧠 하이브리드 엔진 코어 함수
# -------------------------------------------------------------
def get_dynamic_grid_step(ticker):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="day", count=7)
        if df is not None and len(df) > 1:
            amplitudes = (df['high'] - df['low']) / df['close'] * 100
            avg_volatility = amplitudes.mean()
            
            if avg_volatility >= 5.0: return 2.0   
            elif avg_volatility >= 2.0: return 1.0 
            else: return 0.5                       
    except Exception as e:
        print(f"⚠️ {ticker} 변동성 계산 오류 (기본값 1.0 적용): {e}")
    return 1.0

def background_target_fetcher():
    global core_targets, hunter_targets, current_regime
    if current_regime == "ICE_AGE":
        print("💤 [동면] 시장 빙하기로 인해 타겟 탐색 스킵.")
        return

    print("🕵️‍♂️ 4H 레이더 가동 (CORE/HUNTER 스캔 중)...")
    temp_core, temp_hunter_candidates = {}, []
    
    # 1. CORE 타겟 스캔 (돌파 매매용)
    for ticker in CORE_UNIVERSE:
        time.sleep(0.3)  # 🚀 [API 차단 방지] 0.15초 -> 0.3초 완화
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
            time.sleep(0.3)  # 🚀 [API 차단 방지] 0.1초 -> 0.3초 완화
            df = pyupbit.get_ohlcv(t, interval="day", count=6)
            if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 6: continue
            temp_hunter_candidates.append({'ticker': t, 'value': df.iloc[-2]['value'], 'open': df.iloc[-1]['open'], 'range': analyzer.get_atr(df, 5)})
        
        if temp_hunter_candidates:
            top10 = sorted(temp_hunter_candidates, key=lambda x: x['value'], reverse=True)[:3]
            hunter_targets = {item['ticker']: item for item in top3}
    except: pass
    
    core_targets = temp_core
    print("✅ [레이더] CORE/HUNTER 타겟 갱신 완료.")

def get_pyramiding_weight(buy_level, current_regime):
    """💡 시장 상황에 따라 공격적 배팅과 방어적 물타기 모드를 자동 스위칭합니다."""
    
    # 1. 상승/횡보장 (SUPER_BULL, NORMAL): 회전율 극대화 (치고 빠지기)
    if current_regime in ["SUPER_BULL", "NORMAL"]:
        # 기본 투자금(예: 6,000원)의 2배(12,000원)로 크게 진입하여 짤짤이 수익 극대화
        if buy_level <= 1: return 2.0     
        # 하락 시 가볍게 1배수(6,000원)만 타고 탈출 시도
        elif buy_level == 2: return 1.0   
        # 상승장에서는 3차 이상 물리지 않도록 추가 시드 투입 완전 차단 (예산 보호)
        elif buy_level >= 3: return 0.0   
        
    # 2. 하락장 (CAUTION, ICE_AGE): 하락장 방어 모드 (기획자님 제안 로직 적용)
    else:
        # 하락장이 감지되면 1차 진입(정찰병)을 1배수(6,000원)로 최소화
        if buy_level <= 1: return 1.0     
        # 이후 2, 4, 6, 8 배수로 부드럽게 평단가를 낮춤 (최대 소진액 제한)
        elif buy_level == 2: return 2.0   
        elif buy_level == 3: return 4.0   
        elif buy_level == 4: return 6.0   
        elif buy_level >= 5: return 8.0   
        
    return 1.0
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
            time.sleep(0.3) # 💡 [API 차단 방지] 다중 컨테이너 환경 고려 0.3초(약 3.3회/초)로 추가 완화
            
        sorted_scores = sorted(scores, key=lambda x: x['score'], reverse=True)
        top_grid_candidates = [item['ticker'] for item in sorted_scores[:GRID_TOTAL_SLOTS]]
        # 💡 [수정] 메신저 중복 발송 방지 (GRID 봇만 대표로 알림 전송)
        if ENGINE_TYPE == 'GRID':
            msg = f"🔍 [그리드 레이더] 신규 타겟 선정 완료\n- 후보: {', '.join(top_grid_candidates[:5])}..."
            send_telegram(msg)
    except Exception as e:
        print(f"❌ 후보 스캔 오류: {e}")


# -------------------------------------------------------------
# 🛡️ 엔진 1: 코어 (CORE) - 돌파/추세 추종 매매
# -------------------------------------------------------------
def run_core_engine(now):
    global bot_positions, core_targets, current_regime
    
    core_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'CORE'}
    watch_list = list(set([p['ticker'] for p in core_pos_items.values()] + list(core_targets.keys())))
    
    current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
    if not isinstance(current_prices, dict): current_prices = {}

    balances = upbit.get_balances()
    safe_balances = {b['currency']: float(b['balance']) for b in balances} if isinstance(balances, list) else {}

    # [1] 기존 포지션 관리 (매도)
    for key, pos in list(core_pos_items.items()):
        ticker = pos['ticker']
        curr_p = current_prices.get(ticker)
        if not curr_p: continue
        
        if 'peak_price' not in pos: pos['peak_price'] = curr_p
        pos['peak_price'] = max(pos['peak_price'], curr_p)
        profit_rate = (curr_p - pos['buy']) / pos['buy']
        
        # 💡 [안전장치] 개인 물량 침범 방지
        currency = ticker.split('-')[1]
        sell_vol = min(pos['vol'], safe_balances.get(currency, 0.0))
        if sell_vol <= 0:
            del bot_positions[key]; continue

        # 💡 [매도 1] 5% 도달 시 전량 익절 (DB 꼬임 방지)
        if profit_rate >= 0.05:
            realized_krw = (curr_p - pos['buy']) * sell_vol
            print(f"📈 [CORE 수익 실현] {ticker} 목표가 도달! 전량 익절")
            if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                del bot_positions[key]
            continue
            
        # 💡 [매도 2] 샹들리에 청산 (추세 꺾임)
        chandelier_exit_price = analyzer.get_chandelier_exit(ticker, pos['peak_price'], current_regime)
        if curr_p < chandelier_exit_price:
            realized_krw = (curr_p - pos['buy']) * sell_vol
            print(f"🛑 [CORE 샹들리에 청산] {ticker} 추세 꺾임 감지. ({profit_rate*100:+.2f}%)")
            if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                del bot_positions[key]
            continue

    # [2] 신규 진입 (매수)
    current_core_count = len([p for p in bot_positions.values() if p['engine'] == 'CORE'])
    if current_core_count < TARGET_SLOTS and current_regime not in ["ICE_AGE"]:
        # CORE에 할당된 예산 계산
        base_invest = (MAX_BUDGET / TOTAL_SLOTS) * REGIME_SETTINGS.get(current_regime, {}).get('ratio', 1.0)
        already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in core_pos_items.values())
        krw_balance = safe_balances.get('KRW', 0.0)

        for ticker, t_info in core_targets.items():
            if current_core_count >= TARGET_SLOTS: break
            if ticker in [p['ticker'] for p in bot_positions.values()]: continue
            
            curr_p = current_prices.get(ticker)
            if not curr_p: continue
            
            # 💡 [필터] 돌파 + 추세 강도(ADX) + 거래량 터짐
            if curr_p >= (t_info['open'] + t_info['range']*t_info['k']):
                if analyzer.check_keltner_breakout(ticker) and analyzer.get_adx(ticker) > 25 and analyzer.check_volume_spike(ticker):
                    
                    # 💡 [안전장치] 예산 락 (Lock)
                    if krw_balance < base_invest or (already_used + base_invest) > MAX_BUDGET:
                        print(f"🛑 [CORE 예산 잠금] {ticker} 보류 (사용량: {already_used:,.0f} / 한도: {MAX_BUDGET:,.0f})")
                        break
                        
                    new_slot_idx = 1
                    while new_slot_idx in [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker]: new_slot_idx += 1
                    
                    print(f"🚀 [CORE 신규 진입] {ticker} 강력한 추세 돌파 포착!")
                    success, exec_price, exec_vol = worker.execute_buy(ticker, base_invest, new_slot_idx)
                    if success:
                        key = f"{ticker}_slot_{new_slot_idx}"
                        bot_positions[key] = {
                            'ticker': ticker, 'vol': exec_vol, 'buy': exec_price, 
                            'peak_price': exec_price, 'slot_index': new_slot_idx, 
                            'engine': 'CORE', 'buy_level': 1, 'created_at': now,
                            'invested_amount': exec_price * exec_vol
                        }
                        try: db_manager.update_position_state(key, exec_price, exec_vol, 1)
                        except AttributeError: pass
                        current_core_count += 1
                        already_used += (exec_price * exec_vol)
                        time.sleep(1.5)

# -------------------------------------------------------------
# 🏹 엔진 2: 헌터 (HUNTER) - 낙폭 과대 반등 매매
# -------------------------------------------------------------
def run_hunter_engine(now):
    global bot_positions, hunter_targets, current_regime
    
    hunter_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'HUNTER'}
    watch_list = list(set([p['ticker'] for p in hunter_pos_items.values()] + list(hunter_targets.keys())))
    
    current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
    if not isinstance(current_prices, dict): current_prices = {}

    balances = upbit.get_balances()
    safe_balances = {b['currency']: float(b['balance']) for b in balances} if isinstance(balances, list) else {}

    # [1] 기존 포지션 관리 (매도)
    for key, pos in list(hunter_pos_items.items()):
        ticker = pos['ticker']
        curr_p = current_prices.get(ticker)
        if not curr_p: continue
        
        profit_rate = (curr_p - pos['buy']) / pos['buy']
        
        currency = ticker.split('-')[1]
        sell_vol = min(pos['vol'], safe_balances.get(currency, 0.0))
        if sell_vol <= 0:
            del bot_positions[key]; continue

        # 💡 [매도 1] 익절 (반등 시 3% 수익 확정)
        if profit_rate >= 0.03:
            realized_krw = (curr_p - pos['buy']) * sell_vol
            print(f"🎯 [HUNTER 익절] {ticker} 낙폭과대 반등 목표가 달성!")
            if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                del bot_positions[key]
            continue

        # 💡 [매도 2] 구조적 손절 (저점 이탈) 또는 45분 시간 초과
        struct_stop = pos.get('struct_stop', 0)
        time_elapsed_mins = (now - pos.get('created_at', now)).total_seconds() / 60
        
        if curr_p < struct_stop or (time_elapsed_mins >= 45 and profit_rate <= 0):
            realized_krw = (curr_p - pos['buy']) * sell_vol
            reason = "구조적 저점 이탈" if curr_p < struct_stop else "반등 지연(타임아웃)"
            print(f"🛑 [HUNTER 손절] {ticker} {reason}. ({profit_rate*100:+.2f}%)")
            if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                del bot_positions[key]
            continue

    # [2] 신규 진입 (매수)
    current_hunter_count = len([p for p in bot_positions.values() if p['engine'] == 'HUNTER'])
    if current_hunter_count < TARGET_SLOTS and current_regime not in ["ICE_AGE"]:
        base_invest = (MAX_BUDGET / TOTAL_SLOTS) * REGIME_SETTINGS.get(current_regime, {}).get('ratio', 1.0)
        already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in hunter_pos_items.values())
        krw_balance = safe_balances.get('KRW', 0.0)

        for ticker in hunter_targets.keys():
            if current_hunter_count >= TARGET_SLOTS: break
            if ticker in [p['ticker'] for p in bot_positions.values()]: continue
            
            curr_p = current_prices.get(ticker)
            if not curr_p: continue
            
            # 💡 [필터] 과매도 VWAP 지지 + 아래꼬리 핀바 확인
            # if analyzer.check_hunter_dip_buy(ticker) and analyzer.is_pin_bar(ticker):
            # 헌터 투자 기준 완화용
            if analyzer.check_hunter_dip_buy(ticker) or analyzer.is_pin_bar(ticker):
                
                # 💡 [안전장치] 예산 락
                if krw_balance < base_invest or (already_used + base_invest) > MAX_BUDGET:
                    print(f"🛑 [HUNTER 예산 잠금] {ticker} 보류 (사용량: {already_used:,.0f} / 한도: {MAX_BUDGET:,.0f})")
                    break
                    
                new_slot_idx = 1
                while new_slot_idx in [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker]: new_slot_idx += 1
                
                print(f"🏹 [HUNTER 신규 진입] {ticker} 과매도 반등(핀바) 포착!")
                success, exec_price, exec_vol = worker.execute_buy(ticker, base_invest, new_slot_idx)
                if success:
                    key = f"{ticker}_slot_{new_slot_idx}"
                    bot_positions[key] = {
                        'ticker': ticker, 'vol': exec_vol, 'buy': exec_price, 
                        'slot_index': new_slot_idx, 'engine': 'HUNTER', 'buy_level': 1, 
                        'created_at': now, 'struct_stop': analyzer.get_structural_stop(ticker),
                        'invested_amount': exec_price * exec_vol
                    }
                    try: db_manager.update_position_state(key, exec_price, exec_vol, 1)
                    except AttributeError: pass
                    current_hunter_count += 1
                    already_used += (exec_price * exec_vol)
                    time.sleep(1.5)

# -------------------------------------------------------------
# 🕸️ 엔진 3: 스마트 그리드 (GRID) 동적 로직
# -------------------------------------------------------------
def run_grid_engine(now):
    global bot_positions, top_grid_candidates
    
    grid_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'GRID'}
    active_tickers = {} 
    
    watch_list = list(set([pos['ticker'] for pos in grid_pos_items.values()] + top_grid_candidates))
    
    current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
    if not isinstance(current_prices, dict): 
        current_prices = {} # None이나 float으로 올 경우를 완벽 차단


    # [1] 기존 슬롯 관리 (매매 및 교체)
    for key, pos in list(grid_pos_items.items()):
        ticker = pos['ticker']
        curr_p = current_prices.get(ticker) 
        if not curr_p: continue
        
        active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
        profit_rate = (curr_p - pos['buy']) / pos['buy']
        
        # -------------------------------------------------------------
        # 💡 [신규] 고점 기록 및 트레일링 스탑 준비
        # -------------------------------------------------------------
        if 'peak_price' not in pos: pos['peak_price'] = curr_p
        pos['peak_price'] = max(pos['peak_price'], curr_p)

        # -------------------------------------------------------------
        # ✂️ [신규] 1. 타임 컷 (Time Cut) 로직
        # -------------------------------------------------------------
        # 7일(168시간) 이상 보유 중인데 수익률이 1% 미만이면 강제 회수하여 기회비용 확보
        # db_manager.recover_bot_positions에서 last_update를 읽어와야 작동합니다.
        last_update = pos.get('created_at', datetime.now())
        if datetime.now() - last_update > timedelta(days=7) and profit_rate < 0.01:
            print(f"⏳ [타임 컷] {ticker} 슬롯 {pos['slot_index']} 장기 체류로 인한 강제 회수")
            if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, 0):
                send_telegram(f"✂️ [Time Cut] {ticker} 기회비용 확보를 위해 포지션 종료")
                del bot_positions[key]
                continue

        # --- [교체 판별 로직] ---
        if ticker not in top_grid_candidates and profit_rate > 0.01:
            # 💡 [수정] DB에 기록될 실제 원화(KRW) 실현 수익 계산
            realized_krw = (curr_p - pos['buy']) * pos['vol']
            if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw):
                print(f"⚖️ [슬롯 교체] {ticker} (수익권 방출 후 새 종목 대기)")
                del bot_positions[key]
                continue

        # -------------------------------------------------------------
        # 🚀 [신규] 2. 불타기 (Upward Pyramiding) 로직
        # -------------------------------------------------------------
        # 대세 상승장(SUPER_BULL)이면서 거래량 폭증 시 추가 베팅으로 수익 극대화
        if current_regime == "SUPER_BULL" and analyzer.check_volume_spike(ticker):
            # 수익 중(+1.5% 이상)이고 아직 1차 진입 상태인 경우만 실행
            if 0.015 < profit_rate < 0.03 and pos.get('buy_level', 1) == 1:
                print(f"🔥 [불타기] {ticker} 추세 돌파 감지! 비중 확대")
                # 기존 유닛 사이즈의 1.5배를 추가 매수
                success, exec_p, exec_v = worker.execute_buy(ticker, UNIT_LIST[0] * 1.5, pos['slot_index'])
                if success:
                    # 불타기 성공 시 평단가와 수량 메모리 갱신
                    new_vol = pos['vol'] + exec_v
                    new_avg = ((pos['buy'] * pos['vol']) + (exec_p * exec_v)) / new_vol
                    bot_positions[key]['buy'] = new_avg
                    bot_positions[key]['vol'] = new_vol
                    bot_positions[key]['buy_level'] = 2 # 레벨을 올려서 중복 방지
                    db_manager.update_position_state(key, new_avg, new_vol, 2)
                    continue


        # --- [하이브리드 매수/매도 코어 로직] ---
        grid_step_percent = get_dynamic_grid_step(ticker)
        current_level = pos.get('buy_level', 1) 
        
        target_buy_price = pos['buy'] * (1 - (grid_step_percent / 100))
        target_sell_price = pos['buy'] * (1 + (grid_step_percent / 100))
        
        # 1️⃣ 하락 시: 가중치 피라미딩 매수 (물타기)
        if curr_p <= target_buy_price:
            next_level = current_level + 1
            weight = get_pyramiding_weight(next_level, current_regime)
            
            # 💡 [추가] 가중치가 0.0이면 (상승장 3차 진입 제한 등) 매수를 시도하지 않고 스킵합니다.
            if weight <= 0:
                # 너무 자주 찍히지 않게 1시간에 한 번 정도만 로그를 남기거나 바로 pass 합니다.
                # print(f"⚠️ [{ticker}] {next_level}차 진입 제한 모드 (상승장 예산 보호)")
                continue

            base_unit = UNIT_LIST[pos['slot_index']-1] if (pos['slot_index']-1) < len(UNIT_LIST) else UNIT_LIST[-1]
            invest_amount = base_unit * weight
            
            # 💡 [수정] API 응답 에러(None) 처리 로직 (TypeError 방지)
            krw_balance = upbit.get_balance("KRW")
            if krw_balance is None:
                print(f"⚠️ [API 지연] 잔고 조회 실패. 다음 틱에 다시 시도합니다.")
                continue

            # 💡 [안전장치 추가] GRID 엔진 총 사용 예산 사전 검사
            already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in grid_pos_items.values())
            if (already_used + invest_amount) > MAX_BUDGET:
                # 💡 [수정] 알림이 아직 안 나갔을 때만 단 1회 출력
                if not budget_lock_notified.get('GRID', False):
                    print(f"🛑 [GRID 예산 잠금] {ticker} {next_level}차 물타기 보류 (사용량: {already_used:,.0f} / 한도: {MAX_BUDGET:,.0f})")
                    budget_lock_notified['GRID'] = True
                continue # worker로 보내지 않고 스킵 (5분 정지 방지)

            if krw_balance < invest_amount:
                # 잔고 부족 메시지도 스팸이 될 수 있으므로 동일하게 처리 가능합니다.
                if not budget_lock_notified.get('GRID', False):
                    print(f"❌ [예산 초과] {ticker} {next_level}차 진입 실패. (필요: {invest_amount:,.0f}원 / 잔고: {krw_balance:,.0f}원)")
                    budget_lock_notified['GRID'] = True
                continue

            budget_lock_notified['GRID'] = False
            print(f"📉 [하락 방어] {ticker} {next_level}차 진입 시도 ({invest_amount:,.0f}원 / {weight}배 가중치)")
            
            # 💡 수정: 정확히 봇이 매수한 수량과 단가만 받아와 누적 적용합니다.
            success, exec_price, exec_vol = worker.execute_buy(ticker, invest_amount, pos['slot_index'])
            if success:
                time.sleep(1.5) 
                
                new_vol = pos['vol'] + exec_vol
                new_avg_price = ((pos['buy'] * pos['vol']) + (exec_price * exec_vol)) / new_vol
                
                bot_positions[key]['buy'] = new_avg_price
                bot_positions[key]['vol'] = new_vol
                bot_positions[key]['buy_level'] = next_level
                bot_positions[key]['invested_amount'] = pos.get('invested_amount', 0) + (exec_price * exec_vol)
                
                try:
                    db_manager.update_position_state(key, new_avg_price, new_vol, next_level)
                except AttributeError:
                    print("⚠️ db_manager에 update_position_state 함수가 등록되지 않아 DB 저장이 스킵되었습니다.")

                print(f"✅ 물타기 성공! [{ticker}] 진짜 평단가: {new_avg_price:,.0f}원 (현재 {next_level}차)")
                continue

        # 2️⃣ 상승 시: 익절 매도
        elif curr_p >= target_sell_price:
            # 💡 [수정] DB에 기록될 실제 원화(KRW) 실현 수익 계산
            realized_krw = (curr_p - pos['buy']) * pos['vol']
            print(f"📈 [수익 실현] {ticker} 목표가 도달! 전량 익절 (수익률 {profit_rate*100:.2f}%)")
            
            if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw):
                print(f"🎉 {ticker} {current_level}차 진입 물량 청산 완료 (슬롯 개방)")
                del bot_positions[key]
                continue

            # -------------------------------------------------------------
            # 🛡️ [신규] 3. 수익 보존형 손절 (Trailing Stop)
            # -------------------------------------------------------------
            # 불타기를 했거나 수익이 충분할 때, 고점 대비 1.5% 하락하면 즉시 매도하여 수익 확정
            drop_from_peak = (pos['peak_price'] - curr_p) / pos['peak_price']
            if profit_rate > 0.01 and drop_from_peak > 0.015:
                realized_krw = (curr_p - pos['buy']) * pos['vol']
                print(f"🛑 [익절 보존] {ticker} 고점 대비 하락으로 수익 확정 ({profit_rate*100:+.2f}%)")
                if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw):
                    del bot_positions[key]
                    continue


    # [2] 빈 슬롯 채우기 (다중 슬롯 및 유동적 배분)
    total_active_slots = sum(active_tickers.values())
    remaining_slots = GRID_TOTAL_SLOTS - total_active_slots
    
    if remaining_slots > 0 and current_regime != "ICE_AGE":
        for ticker in top_grid_candidates:
            if remaining_slots <= 0: break
            
            current_count = active_tickers.get(ticker, 0)
            slot_limit = MAX_SLOTS_PER_COIN if USE_MULTI_SLOT else 1
            
            if current_count < slot_limit:
                unit_size = UNIT_LIST[current_count] if current_count < len(UNIT_LIST) else UNIT_LIST[-1]
                
                # 💡 [안전장치 추가] 신규 진입 시 예산 사전 검사
                already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in grid_pos_items.values())
                if (already_used + unit_size) > MAX_BUDGET:
                    if not budget_lock_notified['GRID']:
                        print(f"🛑 [GRID 예산 잠금] 신규 진입 예산 초과. 사냥 보류.")
                        budget_lock_notified['GRID'] = True
                    break # 예산이 꽉 찼으면 스캔 중단

                budget_lock_notified['GRID'] = False

                # 💡 [수정] 슬롯 인덱스 중복(덮어쓰기) 방지를 위한 안전한 번호 부여 로직
                existing_slots = [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker and p['engine'] == 'GRID']
                new_slot_idx = 1
                while new_slot_idx in existing_slots:
                    new_slot_idx += 1
                
                curr_p_new = current_prices.get(ticker)
                if not curr_p_new: continue

                # 💡 수정: 신규 진입 시 봇이 매수한 단가와 수량만 장부에 기록합니다.
                success, exec_price, exec_vol = worker.execute_buy(ticker, unit_size, new_slot_idx)
                if success:
                    time.sleep(1.5) 
                    
                    key = f"{ticker}_slot_{new_slot_idx}"
                    bot_positions[key] = {
                        'ticker': ticker, 
                        'vol': exec_vol, 
                        'buy': exec_price, 
                        'slot_index': new_slot_idx, 
                        'engine': 'GRID',
                        'buy_level': 1 ,
                        'invested_amount': exec_price * exec_vol
                    }
                    
                    try:
                        db_manager.update_position_state(key, exec_price, exec_vol, 1)
                    except AttributeError:
                        pass

                    remaining_slots -= 1
                    active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
                    already_used += (exec_price * exec_vol)
                    print(f"🚀 [신규 진입] {ticker} 슬롯 {new_slot_idx} 배치 완료 (1차 매수)")


# -------------------------------------------------------------
# ⚡ 엔진 4: 스캘핑 (SCALP) - 고회전 짤짤이 로직 (완전 독립형 설정 적용)
# -------------------------------------------------------------
def run_scalp_engine(now):
#    global bot_positions, top_grid_candidates, GRID_TOTAL_SLOTS, USE_MULTI_SLOT, MAX_SLOTS_PER_COIN, UNIT_LIST
    
    # 💡 [신규] SCALP 전용 환경변수 로드 (값이 없으면 GRID 설정을 기본값으로 사용합니다)
#    SCALP_TOTAL_SLOTS = int(os.getenv('SCALP_TOTAL_SLOTS', GRID_TOTAL_SLOTS))
#    SCALP_USE_MULTI_SLOT = os.getenv('SCALP_USE_MULTI_SLOT', str(USE_MULTI_SLOT)).lower() == 'true'
#    SCALP_MAX_SLOTS_PER_COIN = int(os.getenv('SCALP_MAX_SLOTS_PER_COIN', MAX_SLOTS_PER_COIN))
    
    scalp_units_str = os.getenv('SCALP_UNIT_SIZES')
    if scalp_units_str:
        SCALP_UNIT_LIST = [float(x.strip()) for x in scalp_units_str.split(',')]
    else:
        SCALP_UNIT_LIST = UNIT_LIST

    scalp_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'SCALP'}
    active_tickers = {} 
    
    watch_list = list(set([pos['ticker'] for pos in scalp_pos_items.values()] + top_grid_candidates))
    current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
    if not isinstance(current_prices, dict): current_prices = {}

    # 💡 [안전장치 1] 루프 시작 시 업비트 전체 잔고를 한 번만 가져와 API 호출을 아끼며 캐싱합니다.
    balances = upbit.get_balances()
    safe_balances = {b['currency']: float(b['balance']) for b in balances} if isinstance(balances, list) else {}

    # [1] 기존 슬롯 관리 (매매 및 교체)
    for key, pos in list(scalp_pos_items.items()):
        ticker = pos['ticker']
        curr_p = current_prices.get(ticker) 
        if not curr_p: continue
        
        active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
        profit_rate = (curr_p - pos['buy']) / pos['buy']

        # 💡 [안전장치 2] 장부 수량과 실제 수량 중 '더 작은 값'을 매도 수량으로 확정 (개인 자산 침범 방지)
        currency = ticker.split('-')[1]
        actual_balance = safe_balances.get(currency, 0.0)
        sell_vol = min(pos['vol'], actual_balance)

        # -------------------------------------------------------------
        # 📈 1. 짤짤이 익절 (0.6% 도달 시 즉각 전량 익절)
        # -------------------------------------------------------------
        if profit_rate >= 0.006: 
            if sell_vol <= 0:
                print(f"⚠️ [잔고 불일치] {ticker} 매도 불가 (장부: {pos['vol']} / 실제: {actual_balance}).")
                del bot_positions[key]
                continue

            realized_krw = (curr_p - pos['buy']) * sell_vol
            print(f"⚡ [스캘핑 익절] {ticker} 단기 수익 달성! ({profit_rate*100:+.2f}%)")
            
            if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                del bot_positions[key]
            continue

        # -------------------------------------------------------------
        # 📉 2. 짤짤이 물타기 (간격은 -1.0% / 최대 2차 진입 제한)
        # -------------------------------------------------------------
        current_level = pos.get('buy_level', 1) 
        if profit_rate <= -0.010 and current_level < 2:  
            next_level = current_level + 1
            # 💡 [적용] SCALP 전용 UNIT_LIST 사용
            base_unit = SCALP_UNIT_LIST[pos['slot_index']-1] if (pos['slot_index']-1) < len(SCALP_UNIT_LIST) else SCALP_UNIT_LIST[-1]
            
            # 💡 [안전장치 3] SCALP 엔진이 사용 중인 총 예산 계산
            already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in scalp_pos_items.values())
            krw_balance = safe_balances.get('KRW', 0.0)

            if krw_balance >= base_unit and (already_used + base_unit) <= MAX_BUDGET:
                budget_lock_notified['SCALP'] = False

                print(f"📉 [스캘핑 방어] {ticker} {next_level}차 진입 시도 (가볍게 1배수 투입)")
                success, exec_price, exec_vol = worker.execute_buy(ticker, base_unit, pos['slot_index'])
                if success:
                    time.sleep(1.5) 
                    new_vol = pos['vol'] + exec_vol
                    new_avg_price = ((pos['buy'] * pos['vol']) + (exec_price * exec_vol)) / new_vol
                    
                    bot_positions[key]['buy'] = new_avg_price
                    bot_positions[key]['vol'] = new_vol
                    bot_positions[key]['buy_level'] = next_level
                    bot_positions[key]['invested_amount'] = pos.get('invested_amount', 0) + (exec_price * exec_vol)
                    
                    try:
                        db_manager.update_position_state(key, new_avg_price, new_vol, next_level)
                    except AttributeError: pass
            else:
                if (already_used + base_unit) > MAX_BUDGET:
                    if not budget_lock_notified['SCALP']: 
                        print(f"🛑 [SCALP 예산 잠금] {ticker} 물타기 생략 (사용량: {already_used:,.0f} / 한도: {MAX_BUDGET:,.0f})")
                        budget_lock_notified['SCALP'] = True
            continue

    # [2] 빈 슬롯 채우기
    total_active_slots = sum(active_tickers.values())
    
    # 💡 [적용] SCALP 전용 최대 슬롯 제한
    remaining_slots = SCALP_TOTAL_SLOTS - total_active_slots
    
    already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in scalp_pos_items.values())
    
    if remaining_slots > 0 and current_regime not in ["ICE_AGE", "CAUTION"]:
        for ticker in top_grid_candidates:
            if remaining_slots <= 0: break
            
            current_count = active_tickers.get(ticker, 0)
            # 💡 [적용] SCALP 전용 멀티슬롯 토글 및 상한선
            slot_limit = SCALP_MAX_SLOTS_PER_COIN if SCALP_USE_MULTI_SLOT else 1
            
            if current_count < slot_limit: 
                # 💡 [적용] SCALP 전용 UNIT_LIST에서 차등 투자금 배정
                unit_size = SCALP_UNIT_LIST[current_count] if current_count < len(SCALP_UNIT_LIST) else SCALP_UNIT_LIST[-1]

                # 💡 [추가] DB 예산뿐만 아니라, 업비트 실제 잔고도 충분한지 이중 체크
                krw_balance = safe_balances.get('KRW', 0.0)
                if krw_balance < unit_size:
                    print(f"❌ [실제 잔고 부족] {ticker} 신규 진입 불가 (필요: {unit_size:,.0f}원 / 잔고: {krw_balance:,.0f}원)")
                    break

                if (already_used + unit_size) > MAX_BUDGET:
                    # 💡 알림이 아직 안 나갔을 때만 단 1회 출력하고 플래그 잠금
                    if not budget_lock_notified['SCALP']: 
                        print(f"🛑 [SCALP 예산 잠금] 신규 진입 예산 초과. 사냥 보류. (매도 발생 시까지 알림 음소거)")
                        budget_lock_notified['SCALP'] = True
                    break

                # 💡 예산 여유가 있어 매수 프로세스로 넘어가면 침묵 플래그 해제
                budget_lock_notified['SCALP'] = False
                
                existing_slots = [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker and p['engine'] == 'SCALP']
                new_slot_idx = 1
                while new_slot_idx in existing_slots: new_slot_idx += 1
                
                success, exec_price, exec_vol = worker.execute_buy(ticker, unit_size, new_slot_idx)
                if success:
                    time.sleep(1.5) 
                    key = f"{ticker}_slot_{new_slot_idx}"
                    bot_positions[key] = {
                        'ticker': ticker, 'vol': exec_vol, 'buy': exec_price, 
                        'slot_index': new_slot_idx, 'engine': 'SCALP', 'buy_level': 1,
                        'invested_amount': exec_price * exec_vol
                    }
                    try:
                        db_manager.update_position_state(key, exec_price, exec_vol, 1)
                    except AttributeError: pass
                    remaining_slots -= 1
                    active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
                    already_used += (exec_price * exec_vol)
                    
                    print(f"🚀 [SCALP 신규] {ticker} 스캘핑 슬롯 {new_slot_idx} 배치 완료 (투입: {unit_size:,.0f}원)")

# -------------------------------------------------------------
# ⚡ 엔진 5: Classic Grid
# -------------------------------------------------------------
def run_classic_grid_engine(now):
    """
    🕸️ ASIS 완벽 복원판: 클래식 거미줄 그리드 (NOW 버전 연동 완료)
    - 다중 슬롯 배제 (1종목 1슬롯 원칙)
    - 예산 N등분 분할 및 50% 초기 진입 / 50% 예비비 할당
    - 그리드 다이어트(리밸런싱) 로직 포함
    - NOW 버전의 DB 장부, 로그, 텔레그램 알림 100% 통합
    """
    global bot_positions, top_grid_candidates, current_regime, budget_lock_notified
    
    # 설정된 슬롯 개수 로드 (1종목 = 1슬롯으로 활용됨)
    # CG_TOTAL_SLOTS = int(os.getenv('CG_TOTAL_SLOTS', 5))
    ENGINE_NAME = 'CLASSIC_GRID'
    
    # 💡 [ASIS 복원] 전체 시드(MAX_BUDGET)를 설정된 슬롯 수로 나누어 1코인당 기본 예산 산정
    BASE_SLOT_BUDGET = MAX_BUDGET / CG_TOTAL_SLOTS if CG_TOTAL_SLOTS > 0 else MAX_BUDGET
    
    cg_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == ENGINE_NAME}
    active_tickers = {}
    
    watch_list = list(set([pos['ticker'] for pos in cg_pos_items.values()] + top_grid_candidates))
    current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
    if not isinstance(current_prices, dict): current_prices = {}

    balances = upbit.get_balances()
    safe_balances = {b['currency']: float(b['balance']) for b in balances} if isinstance(balances, list) else {}
    krw_balance = safe_balances.get('KRW', 0.0)

    # =====================================================================
    # [1] 기존 슬롯 관리 (거미줄 매수/매도 및 종목 교체)
    # =====================================================================
    for key, pos in list(cg_pos_items.items()):
        ticker = pos['ticker']
        curr_p = current_prices.get(ticker)
        if not curr_p: continue
        
        active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
        profit_rate = (curr_p - pos['buy']) / pos['buy']
        
        # 💡 [ASIS 복원] 메모리 복구 시 필요 변수 초기화
        if 'last_grid_price' not in pos: 
            pos['last_grid_price'] = curr_p
        if 'allocated_krw' not in pos: 
            # 메모리에 없으면, 현재 코인당 할당된 전체 예산의 50%를 하단 물타기 예비비로 자동 세팅
            pos['allocated_krw'] = BASE_SLOT_BUDGET * 0.5 

        # -------------------------------------------------------------
        # ⚖️ 1. 스왑(교체) 로직: 타겟에서 밀려났고 1% 이상 수익이면 전량 매도
        # -------------------------------------------------------------
        if ticker not in top_grid_candidates and profit_rate > 0.01:
            sell_vol = min(pos['vol'], safe_balances.get(ticker.split('-')[1], 0.0))
            if sell_vol > 0:
                realized_krw = (curr_p - pos['buy']) * sell_vol
                # 💡 [NOW 연동] worker.execute_sell 사용 (DB 장부 깔끔하게 삭제, 로그, 알림 자동 처리)
                if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                    print(f"⚖️ [방출] {ticker} 타겟 제외로 인한 교체 방출 ({profit_rate*100:+.2f}%)")
                    del bot_positions[key]
            continue

        # -------------------------------------------------------------
        # 📈 2. 그리드 상단 익절 (15% 부분 매도) & 다이어트 로직
        # -------------------------------------------------------------
        step = pos.get('grid_step')
        if not step: # 💡 [API 최적화] 매 루프마다 호출되던 병목을 최초 1회 캐싱으로 100% 제거
            step = analyzer.get_grid_step(ticker) or (curr_p * 0.01)
            pos['grid_step'] = step
        
        if curr_p >= pos['last_grid_price'] + step:
            # 보유량의 15% 또는 최소 6000원치 매도
            target_sell_vol = max(pos['vol'] * 0.15, 6000 / curr_p)
            actual_sell_vol = min(target_sell_vol, safe_balances.get(ticker.split('-')[1], 0.0))
            
            if actual_sell_vol > 0:
                # 💡 [핵심 추가] 먼지(Dust) 방지 로직: 매도 후 남은 가치 평가
                remaining_vol = pos['vol'] - actual_sell_vol
                remaining_krw = remaining_vol * curr_p

                # 남은 금액이 6,000원 미만이라면 전량 매도로 스위칭
                if remaining_vol > 0 and remaining_krw < 6000:
                    print(f"🧹 [잔돈 청소] {ticker} 남은 잔고({remaining_krw:,.0f}원)가 최소 주문 금액 미달. 전량 익절로 전환합니다!")
                    realized_krw = (curr_p - pos['buy']) * pos['vol']
                    
                    # worker를 이용해 100% 매도 처리 및 DB/슬롯 완전히 비우기
                    if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw):
                        del bot_positions[key]
                    continue # 전량 매도했으므로 아래 부분 매도 로직은 건너뜀

                # 💡 [DB 보호] 부분 매도이므로 worker를 쓰지 않고 직접 안전하게 처리 (worker는 슬롯을 통째로 날려버림)
                res = upbit.sell_market_order(ticker, actual_sell_vol)
                if res:
                    time.sleep(1)
                    curr_p_after = pyupbit.get_current_price(ticker) or curr_p
                    realized_krw = (curr_p_after - pos['buy']) * actual_sell_vol
                    
                    # 메모리 갱신
                    pos['vol'] -= actual_sell_vol
                    pos['last_grid_price'] = curr_p_after
                    
                    # 💡 [ASIS 복원] 리밸런싱(다이어트) 로직 평가
                    # 내 슬롯의 현재 평가금 + 쥐고 있는 현금 예비비 = 총 슬롯 자산
                    current_slot_value = (pos['vol'] * curr_p_after) + pos['allocated_krw']
                    slot_max_limit = BASE_SLOT_BUDGET * 1.05
                    
                    if current_slot_value > slot_max_limit:
                        # 다이어트: 익절 수익금을 예비비에 넣지 않고 순수익으로 빼냄
                        trade_type = "SELL_REBALANCE"
                        noti_msg = f"⚖️ [🕸️ {ENGINE_NAME} 다이어트]\n- 슬롯 비대화 방지 수익금 회수"
                        print(f"🕸️ [그리드 다이어트] {ticker} 초과수익 회수 완료 (+{realized_krw:,.0f}원)")
                    else:
                        # 일반 익절: 수익금을 하단 물타기 예비비에 재투자 (복리)
                        pos['allocated_krw'] += (actual_sell_vol * curr_p_after)
                        trade_type = "SELL_GRID_PART"
                        noti_msg = f"[🕸️ {ENGINE_NAME} 부분 매도]"
                        print(f"🕸️ [그리드 상단] {ticker} 부분 매도 완료 (+{realized_krw:,.0f}원)")
                    
                    # 💡 [NOW 연동] 부분 매도 로그 및 DB 장부(수량/투자금) 차감 업데이트
                    db_manager.log_trade(ticker, trade_type, curr_p_after, actual_sell_vol, profit_rate*100, realized_krw)
                    import pymysql
                    from config import DB_CONF, ENABLE_TRADE_NOTI, send_telegram
                    try:
                        conn = pymysql.connect(**DB_CONF)
                        with conn.cursor() as cur:
                            sql = """
                                UPDATE current_positions 
                                SET volume = volume - %s, 
                                    invested_amount = invested_amount - %s 
                                WHERE account_id = %s AND engine_name = %s AND ticker = %s AND slot_index = %s
                            """
                            cur.execute(sql, (actual_sell_vol, (pos['buy'] * actual_sell_vol), db_manager.ACCOUNT_ID, ENGINE_NAME, ticker, pos['slot_index']))
                        conn.commit(); conn.close()
                    except Exception as e:
                        print(f"DB 부분 매도 업데이트 오류: {e}")

                    # 💡 [NOW 연동] 부분 매도 텔레그램 알림 발송 (잔여 예비비 정보 포함)
                    if ENABLE_TRADE_NOTI:
                        icon = "📈" if realized_krw > 0 else "📉"
                        send_telegram(
                            f"{icon} {noti_msg}\n"
                            f"- 종목: {ticker}\n"
                            f"- 실현 손익: {realized_krw:+,.0f}원\n"
                            f"- 수익률: {profit_rate*100:+.2f}%\n"
                            f"- 단가: {curr_p_after:,.2f}원\n"
                            f"- 잔여예산: {pos['allocated_krw']:,.0f}원"
                        )

        # -------------------------------------------------------------
        # 📉 3. 그리드 하단 물타기 (할당 예비비의 15% 부분 매수)
        # -------------------------------------------------------------
        elif curr_p <= pos['last_grid_price'] - step and curr_p > analyzer.get_ema200(ticker):
            buy_krw = max(pos['allocated_krw'] * 0.15, 6000)
            
            if krw_balance >= buy_krw and pos['allocated_krw'] >= buy_krw:
                # 💡 [NOW 연동] worker.execute_buy 호출 (부분 매수 시 DB 누적 기록, 알림 발송 완벽 지원)
                success, exec_price, exec_vol = worker.execute_buy(ticker, buy_krw, pos['slot_index'])
                if success:
                    time.sleep(1.5)
                    new_vol = pos['vol'] + exec_vol
                    new_avg = ((pos['buy'] * pos['vol']) + (exec_price * exec_vol)) / new_vol
                    
                    pos['buy'] = new_avg
                    pos['vol'] = new_vol
                    pos['last_grid_price'] = exec_price
                    pos['allocated_krw'] -= buy_krw
                    
                    print(f"🕸️ [그리드 하단] {ticker} 부분 매수(물타기) 완료. (새 평단: {new_avg:,.0f}원)")

    # =====================================================================
    # [2] 빈 슬롯 채우기 (다중 슬롯 방지 및 코인당 50% 분할 진입)
    # =====================================================================
    # 💡 [ASIS 복원] 1코인 = 1슬롯 철저 제한
    total_active_slots = len(active_tickers) 
    remaining_slots = CG_TOTAL_SLOTS - total_active_slots
    
    # 💡 [ASIS 복원] 첫 진입은 코인당 배정된 기본 예산의 딱 50%만 투입
    init_invest_amount = BASE_SLOT_BUDGET * 0.5
    already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in cg_pos_items.values())
    
    if remaining_slots > 0 and current_regime not in ["ICE_AGE"]:
        for ticker in top_grid_candidates:
            if remaining_slots <= 0: break
            
            # 💡 [안전장치] 이미 사냥 중인 종목이면 중복 진입 방지 (다중 슬롯 스킵)
            if ticker in active_tickers: 
                continue 
                
            # 💡 [잔고 검사]
            if krw_balance < init_invest_amount:
                break
                
            # 💡 [예산 검사] 전체 엔진 사용량 기반 예산 잠금
            if (already_used + init_invest_amount) > MAX_BUDGET:
                if not budget_lock_notified.get('CLASSIC_GRID', False):
                    print(f"🛑 [{ENGINE_NAME} 예산 잠금] 신규 진입 예산 초과.")
                    budget_lock_notified['CLASSIC_GRID'] = True
                break 
            
            budget_lock_notified['CLASSIC_GRID'] = False

            # 빈 슬롯 인덱스 발급
            existing_slots = [p['slot_index'] for p in bot_positions.values() if p['engine'] == ENGINE_NAME]
            new_slot_idx = 1
            while new_slot_idx in existing_slots: new_slot_idx += 1
            
            curr_p = current_prices.get(ticker)
            if not curr_p: continue

            # 💡 [NOW 연동] worker.execute_buy 사용 (신규 매수 시 DB 생성, 알림 발송 자동 처리)
            success, exec_price, exec_vol = worker.execute_buy(ticker, init_invest_amount, new_slot_idx)
            if success:
                time.sleep(1.5) 
                
                key = f"{ticker}_slot_{new_slot_idx}"
                bot_positions[key] = {
                    'ticker': ticker, 
                    'vol': exec_vol, 
                    'buy': exec_price, 
                    'slot_index': new_slot_idx, 
                    'engine': ENGINE_NAME, 
                    'buy_level': 1,
                    'last_grid_price': exec_price,
                    'grid_step': analyzer.get_grid_step(ticker),
                    'allocated_krw': init_invest_amount, # 💡 [ASIS 복원] 나머지 50% 금액을 하단 물타기 예비비로 충전
                    'invested_amount': exec_price * exec_vol
                }
                
                remaining_slots -= 1
                active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
                already_used += (exec_price * exec_vol)
                
                print(f"🚀 [{ENGINE_NAME} 신규 진입] {ticker} 거미줄 전개 완료! (하단 예비비: {init_invest_amount:,.0f}원 확보)")


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
    telegram_handler.start_telegram_listener(bot_positions, None, lambda: MAX_BUDGET)
    print(f"🤖 [{ENGINE_TYPE}] 텔레그램 명령 수신 당번 가동 시작!")
else:
    print(f"🔇 [{ENGINE_TYPE}] 텔레그램 명령 수신을 스킵합니다. (중복 방지 모드)")

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

        # 💡 [동적 분산] 현재 실행 중인 엔진 리스트 기반으로 계산된 offset 적용
        offset = regime_offsets.get(ENGINE_TYPE, 0)
        if now.minute % 15 == offset and (last_regime_check_time is None or (now - last_regime_check_time).total_seconds() > 60):
            current_regime = analyzer.get_market_regime(current_regime)
            last_regime_check_time = now

        # 💡 [추가] 4시간마다 CORE/HUNTER 타겟 스캔 (50분 언저리에 실행하여 API 몰림 방지)
        # 💡 [수정] CORE와 HUNTER 엔진일 때만 레이더 가동
        if ENGINE_TYPE in ['CORE', 'HUNTER']:
            fetch_minute = 50 if ENGINE_TYPE == 'CORE' else 55 # 💡 [API 분산] CORE는 50분, HUNTER는 55분
            if now.hour % 4 == 0 and now.minute == fetch_minute and (last_target_fetch_time is None or now >= last_target_fetch_time + timedelta(hours=3)):
                last_target_fetch_time = now 
                threading.Thread(target=background_target_fetcher).start()

        if ENGINE_TYPE in ['GRID', 'SCALP','CLASSIC_GRID']:
            if last_grid_eval_time is None or now >= last_grid_eval_time + timedelta(hours=6):
                evaluate_grid_candidates()
                last_grid_eval_time = now

        # 💡 [수정] 패닉 체크를 매 루프마다 하지 않고 컨테이너별로 10초에 한 번만 수행하도록 완화하여 API 폭주 방지
        if last_panic_check_time is None or (now - last_panic_check_time).total_seconds() >= 10:
            is_panic_state = analyzer.check_panic_fall()
            last_panic_check_time = now

        if is_panic_state:
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

        if ENGINE_TYPE == 'CORE': run_core_engine(now)
        elif ENGINE_TYPE == 'HUNTER': run_hunter_engine(now)
        elif ENGINE_TYPE == 'GRID': run_grid_engine(now)
        elif ENGINE_TYPE == 'SCALP': run_scalp_engine(now)
        elif ENGINE_TYPE == 'CLASSIC_GRID': run_classic_grid_engine(now)

        # 💡 루프가 에러 없이 정상적으로 끝까지 도달하면 에러 카운터 초기화
        consecutive_errors = 0

        #loop_delay = 1 if ENGINE_TYPE == 'HUNTER' else 3
        # 💡 [수정] 엔진별 루프 대기 시간(심장 박동) 차등화로 API 병목 분산
        if ENGINE_TYPE == 'SCALP': 
            loop_delay = 1.5  # 0.5초 -> 1.5초 완화 (API 차단 방지)
        elif ENGINE_TYPE == 'HUNTER': 
            loop_delay = 2.0  # 1.5초 -> 2.0초 완화
        elif ENGINE_TYPE == 'GRID':
            loop_delay = 3.0  # 스윙 그물망은 3초
        elif ENGINE_TYPE == 'CLASSIC_GRID':
            loop_delay = 1.5  # 0.5초 -> 1.5초 완화 (API 차단 방지)
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
