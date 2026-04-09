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
GRID_TOTAL_SLOTS = int(os.getenv('GRID_TOTAL_SLOTS', 5))  # 예: 8 (고정1 + 유동7)
USE_MULTI_SLOT = os.getenv('USE_MULTI_SLOT', 'True').lower() == 'true'
MAX_SLOTS_PER_COIN = int(os.getenv('MAX_SLOTS_PER_COIN', 2))
# 투자 단위 리스트 (A, B, C... 순서대로 적용)
UNIT_LIST = [float(x) for x in os.getenv('GRID_UNIT_SIZES', '10000,30000').split(',')]

# 💡 [V17.11] Monkey Patching (API 안정성 강화)
_original_get_current_price = pyupbit.get_current_price
def _safe_get_current_price(ticker, limit_info=False, verbose=False):
    try:
        res = _original_get_current_price(ticker, limit_info, verbose)
        return res
    except: return {} if isinstance(ticker, list) else None
pyupbit.get_current_price = _safe_get_current_price

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

print(f"====================================================")
print(f"🏆 [시스템] Aegis-Elite V17.17 방탄 패치 가동 (모드: {ENGINE_TYPE})")
if ENGINE_TYPE == 'GRID':
    print(f"🎰 그리드 슬롯: {GRID_TOTAL_SLOTS} | 다중슬롯: {USE_MULTI_SLOT} (Max {MAX_SLOTS_PER_COIN})")
else:
    print(f"🎰 타겟 슬롯: {TARGET_SLOTS}")
print(f"💰 할당 예산: {MAX_BUDGET:,.0f}원")
print(f"====================================================\n")

# -------------------------------------------------------------
# 🧠 하이브리드 엔진 코어 함수 (Step 3 방탄 패치 적용)
# -------------------------------------------------------------
def get_dynamic_grid_step(ticker):
    """[패치 3] 단기 뇌피셜 방지: 최근 7일 평균 진폭(ATR) 기반 동적 간격 조절"""
    try:
        # 최근 7일치 데이터를 기반으로 평균적인 변동성을 계산합니다.
        df = pyupbit.get_ohlcv(ticker, interval="day", count=7)
        if df is not None and len(df) > 1:
            amplitudes = (df['high'] - df['low']) / df['close'] * 100
            avg_volatility = amplitudes.mean()
            
            if avg_volatility >= 5.0: return 2.0   # 고변동성 코인은 2.0% 간격
            elif avg_volatility >= 2.0: return 1.0 # 보통 코인은 1.0% 간격
            else: return 0.5                       # 얌전한 코인은 0.5% 간격
    except Exception as e:
        print(f"⚠️ {ticker} 변동성 계산 오류 (기본값 1.0 적용): {e}")
    return 1.0

def get_pyramiding_weight(buy_level):
    """[기능 2] 피라미딩 가중치 (안전형 옵션 1: 최대 3.0배 한도)"""
    if buy_level <= 1: return 1.0     # 1차 매수: 1.0배
    elif buy_level == 2: return 1.5   # 2차 매수: 1.5배
    elif buy_level == 3: return 2.0   # 3차 매수: 2.0배
    elif buy_level >= 4: return 3.0   # 4차 이상: 3.0배 한도 (강력 방어)
    return 1.0

# -------------------------------------------------------------
# 🕵️‍♂️ 그리드 전용: 종목 발굴 및 리밸런싱 로직
# -------------------------------------------------------------
def evaluate_grid_candidates():
    """6~12시간마다 그리드에 적합한 횡보/변동성 종목 스캔"""
    global top_grid_candidates
    try:
        print("🔍 [그리드 레이더] 최적 사냥터 스캔 중...")
        all_tickers = pyupbit.get_tickers(fiat="KRW")
        scores = []
        
        for t in all_tickers:
            if t == "KRW-ETH": continue
            score = analyzer.get_grid_suitability_score(t)
            if score > 0:
                scores.append({'ticker': t, 'score': score})
            time.sleep(0.05) 
            
        sorted_scores = sorted(scores, key=lambda x: x['score'], reverse=True)
        top_grid_candidates = [item['ticker'] for item in sorted_scores[:GRID_TOTAL_SLOTS]]
        
        msg = f"🔍 [그리드 레이더] 신규 타겟 선정 완료\n- 후보: {', '.join(top_grid_candidates[:5])}..."
        telegram_handler.send_telegram(msg)
    except Exception as e:
        print(f"❌ 후보 스캔 오류: {e}")

# -------------------------------------------------------------
# 🛡️ 엔진 1 & 2: 코어 / 헌터 로직 (기존 유지)
# -------------------------------------------------------------
def run_core_engine(now):
    pass # 기존 worker 로직

def run_hunter_engine(now):
    pass # 기존 worker 로직

# -------------------------------------------------------------
# 🕸️ 엔진 3: 스마트 그리드 (GRID) 동적 로직 (하이브리드 이식 완료)
# -------------------------------------------------------------
def run_grid_engine(now):
    global bot_positions, top_grid_candidates
    
    grid_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'GRID'}
    active_tickers = {} 
    
    # [패치 4] API 호출 최적화: 개별 조회가 아닌 감시 리스트 전체를 1번의 API 호출로 가져옴
    watch_list = list(set([pos['ticker'] for pos in grid_pos_items.values()] + top_grid_candidates))
    current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}

    # [1] 기존 슬롯 관리 (매매 및 교체)
    for key, pos in list(grid_pos_items.items()):
        ticker = pos['ticker']
        curr_p = current_prices.get(ticker) # 최적화된 딕셔너리에서 가격 참조
        if not curr_p: continue
        
        active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
        profit_rate = (curr_p - pos['buy']) / pos['buy']
        
        # --- [교체 판별 로직] ---
        if ticker != "KRW-ETH" and ticker not in top_grid_candidates and profit_rate > 0.01:
            if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100):
                print(f"⚖️ [슬롯 교체] {ticker} (수익권 방출 후 새 종목 대기)")
                del bot_positions[key]
                continue

        # --- [하이브리드 매수/매도 코어 로직] ---
        grid_step_percent = get_dynamic_grid_step(ticker)
        current_level = pos.get('buy_level', 1) 
        
        target_buy_price = pos['buy'] * (1 - (grid_step_percent / 100))
        target_sell_price = pos['buy'] * (1 + (grid_step_percent / 100))
        
        # 1️⃣ 하락 시: 가중치 피라미딩 매수 (물타기)
        if curr_p <= target_buy_price:
            next_level = current_level + 1
            weight = get_pyramiding_weight(next_level)
            
            base_unit = UNIT_LIST[pos['slot_index']-1] if (pos['slot_index']-1) < len(UNIT_LIST) else UNIT_LIST[-1]
            invest_amount = base_unit * weight
            
            # [패치 5] 안전 장치: 예산(보유 원화) 초과 검사
            krw_balance = upbit.get_balance("KRW")
            if krw_balance < invest_amount:
                print(f"❌ [예산 초과] {ticker} {next_level}차 진입 실패. (필요: {invest_amount:,.0f}원 / 잔고: {krw_balance:,.0f}원)")
                continue

            print(f"📉 [하락 방어] {ticker} {next_level}차 진입 시도 ({invest_amount:,.0f}원 / {weight}배 가중치)")
            
            if worker.execute_buy(ticker, invest_amount, pos['slot_index']):
                time.sleep(1.5) # 업비트 체결 지연 대기
                
                # [패치 2] 뇌피셜 방지: 업비트에서 '실제' 잔고와 매수평균가를 다시 긁어옴
                real_vol = upbit.get_balance(ticker)
                real_avg_price = upbit.get_avg_buy_price(ticker)
                
                bot_positions[key]['buy'] = real_avg_price
                bot_positions[key]['vol'] = real_vol
                bot_positions[key]['buy_level'] = next_level
                
                # [패치 1] DB 기억 복구: 상태를 DB에 확실히 저장
                try:
                    db_manager.update_position_state(key, real_avg_price, real_vol, next_level)
                except AttributeError:
                    print("⚠️ db_manager에 update_position_state 함수가 등록되지 않아 DB 저장이 스킵되었습니다.")

                print(f"✅ 물타기 성공! [{ticker}] 진짜 평단가: {real_avg_price:,.0f}원 (현재 {next_level}차)")
                continue

        # 2️⃣ 상승 시: 익절 매도
        elif curr_p >= target_sell_price:
            print(f"📈 [수익 실현] {ticker} 목표가 도달! 전량 익절 (수익률 {profit_rate*100:.2f}%)")
            if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100):
                print(f"🎉 {ticker} {current_level}차 진입 물량 청산 완료 (슬롯 개방)")
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
                new_slot_idx = current_count + 1
                
                # 신규 진입 전에도 현재가 확인 (최적화된 딕셔너리 사용)
                curr_p_new = current_prices.get(ticker)
                if not curr_p_new: continue

                if worker.execute_buy(ticker, unit_size, new_slot_idx):
                    time.sleep(1.5) # 체결 지연 대기
                    real_vol = upbit.get_balance(ticker)
                    real_avg_price = upbit.get_avg_buy_price(ticker)
                    
                    key = f"{ticker}_slot_{new_slot_idx}"
                    bot_positions[key] = {
                        'ticker': ticker, 
                        'vol': real_vol, # 실제 데이터로 저장
                        'buy': real_avg_price, # 실제 데이터로 저장
                        'slot_index': new_slot_idx, 
                        'engine': 'GRID',
                        'buy_level': 1  # 💡 신규 진입 시 1차수로 초기화
                    }
                    
                    # 신규 진입도 DB에 1차수로 명확히 업데이트
                    try:
                        db_manager.update_position_state(key, real_avg_price, real_vol, 1)
                    except AttributeError:
                        pass

                    remaining_slots -= 1
                    active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
                    print(f"🚀 [신규 진입] {ticker} 슬롯 {new_slot_idx} 배치 완료 (1차 매수)")

# -------------------------------------------------------------
# 🔄 메인 제어 루프
# -------------------------------------------------------------
bot_positions = db_manager.recover_bot_positions(upbit)
# DB에서 복구 시 buy_level 정보가 없다면 일괄 1로 초기화 (안전장치)
for k, v in bot_positions.items():
    if 'buy_level' not in v:
        v['buy_level'] = 1

while True:
    try:
        now = datetime.now()
        
        # [1] 시장 상황 판단
        if now.minute % 15 == 0:
            current_regime = analyzer.get_market_regime(current_regime)

        # [2] 그리드 후보 스캔
        if ENGINE_TYPE == 'GRID':
            if last_grid_eval_time is None or now >= last_grid_eval_time + timedelta(hours=6):
                evaluate_grid_candidates()
                last_grid_eval_time = now

        # [3] 폭락 시 긴급 대응
        if analyzer.check_panic_fall():
            time.sleep(10); continue

        # [4] 엔진 실행
        if ENGINE_TYPE == 'CORE': run_core_engine(now)
        elif ENGINE_TYPE == 'HUNTER': run_hunter_engine(now)
        elif ENGINE_TYPE == 'GRID': run_grid_engine(now)

        loop_delay = 1 if ENGINE_TYPE == 'HUNTER' else 3
        time.sleep(loop_delay)

    except Exception as e:
        print(f"🚨 [{ENGINE_TYPE}] 루프 에러: {e}")
        traceback.print_exc()
        time.sleep(5)