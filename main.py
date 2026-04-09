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
print(f"🏆 [시스템] Aegis-Elite V17.17 무결성 패치 가동 (모드: {ENGINE_TYPE})")
if ENGINE_TYPE == 'GRID':
    print(f"🎰 그리드 슬롯: {GRID_TOTAL_SLOTS} | 다중슬롯: {USE_MULTI_SLOT} (Max {MAX_SLOTS_PER_COIN})")
else:
    print(f"🎰 타겟 슬롯: {TARGET_SLOTS}")
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

def get_pyramiding_weight(buy_level):
    if buy_level <= 1: return 1.0     
    elif buy_level == 2: return 1.5   
    elif buy_level == 3: return 2.0   
    elif buy_level >= 4: return 3.0   
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
            if t == "KRW-ETH": continue
            score = analyzer.get_grid_suitability_score(t)
            if score > 0:
                scores.append({'ticker': t, 'score': score})
            time.sleep(0.05) 
            
        sorted_scores = sorted(scores, key=lambda x: x['score'], reverse=True)
        top_grid_candidates = [item['ticker'] for item in sorted_scores[:GRID_TOTAL_SLOTS]]
        
        msg = f"🔍 [그리드 레이더] 신규 타겟 선정 완료\n- 후보: {', '.join(top_grid_candidates[:5])}..."
        send_telegram(msg)
    except Exception as e:
        print(f"❌ 후보 스캔 오류: {e}")

# -------------------------------------------------------------
# 🛡️ 엔진 1 & 2: 코어 / 헌터 로직
# -------------------------------------------------------------
def run_core_engine(now):
    pass 

def run_hunter_engine(now):
    pass 

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
        
        # --- [교체 판별 로직] ---
        if ticker != "KRW-ETH" and ticker not in top_grid_candidates and profit_rate > 0.01:
            # 💡 [수정] DB에 기록될 실제 원화(KRW) 실현 수익 계산
            realized_krw = (curr_p - pos['buy']) * pos['vol']
            if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw):
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
            
            # 💡 [수정] API 응답 에러(None) 처리 로직 (TypeError 방지)
            krw_balance = upbit.get_balance("KRW")
            if krw_balance is None:
                print(f"⚠️ [API 지연] 잔고 조회 실패. 다음 틱에 다시 시도합니다.")
                continue

            if krw_balance < invest_amount:
                print(f"❌ [예산 초과] {ticker} {next_level}차 진입 실패. (필요: {invest_amount:,.0f}원 / 잔고: {krw_balance:,.0f}원)")
                continue

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
                        'buy_level': 1  
                    }
                    
                    try:
                        db_manager.update_position_state(key, exec_price, exec_vol, 1)
                    except AttributeError:
                        pass

                    remaining_slots -= 1
                    active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
                    print(f"🚀 [신규 진입] {ticker} 슬롯 {new_slot_idx} 배치 완료 (1차 매수)")

# -------------------------------------------------------------
# 🔄 메인 제어 루프
# -------------------------------------------------------------
bot_positions = db_manager.recover_bot_positions(upbit)
for k, v in bot_positions.items():
    if 'buy_level' not in v:
        v['buy_level'] = 1

# 텔레그램 봇 백그라운드 가동 (이 한 줄 필수 추가!)
telegram_handler.start_telegram_listener(bot_positions, lambda: MAX_BUDGET)

while True:
    try:
        now = datetime.now()
        
        if now.minute % 15 == 0:
            current_regime = analyzer.get_market_regime(current_regime)

        if ENGINE_TYPE == 'GRID':
            if last_grid_eval_time is None or now >= last_grid_eval_time + timedelta(hours=6):
                evaluate_grid_candidates()
                last_grid_eval_time = now

        if analyzer.check_panic_fall():
            time.sleep(10); continue

        if ENGINE_TYPE == 'CORE': run_core_engine(now)
        elif ENGINE_TYPE == 'HUNTER': run_hunter_engine(now)
        elif ENGINE_TYPE == 'GRID': run_grid_engine(now)

        loop_delay = 1 if ENGINE_TYPE == 'HUNTER' else 3
        time.sleep(loop_delay)

    except Exception as e:
        print(f"🚨 [{ENGINE_TYPE}] 루프 에러: {e}")
        traceback.print_exc()
        time.sleep(5)