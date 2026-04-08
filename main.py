import time
import threading
import traceback  
from datetime import datetime, timedelta
import pandas as pd 
import pyupbit

# 💡 [V17.11] Monkey Patching
_original_get_current_price = pyupbit.get_current_price
def _safe_get_current_price(ticker, limit_info=False, verbose=False):
    try:
        res = _original_get_current_price(ticker, limit_info, verbose)
        return res
    except: return {} if isinstance(ticker, list) else None
pyupbit.get_current_price = _safe_get_current_price

from config import *
import db_manager
import analyzer
import telegram_handler 

print("🏆 [시스템] V17.17 (Aegis-Elite: 하이브리드 그리드 & 스마트 필터) 가동 중...\n")

upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)
SEED_MONEY = 0
bot_positions = {}
current_regime = "NORMAL"
core_targets, hunter_targets = {}, {}
next_day_core_targets, next_day_hunter_targets = {}, {}
top_grid_candidates = []
last_grid_eval_time = None

# -------------------------------------------------------------
# 🛡️ 유틸리티 함수
# -------------------------------------------------------------
def get_safe_balance(ticker):
    try:
        balances = upbit.get_balances()
        if not isinstance(balances, list): return 0.0
        currency = ticker.split("-")[1] if "-" in ticker else ticker
        for b in balances:
            if b['currency'] == currency: return float(b['balance'])
        return 0.0
    except: return 0.0

def safe_sell_order(ticker, target_vol):
    try:
        available_vol = get_safe_balance(ticker) 
        actual_sell = min(target_vol, available_vol)
        if actual_sell > 0: upbit.sell_market_order(ticker, actual_sell)
        return actual_sell
    except: return 0

def safe_buy_order(ticker, invest_amount, curr_price):
    try:
        pre_vol = get_safe_balance(ticker)
        upbit.buy_market_order(ticker, invest_amount)
        time.sleep(1) 
        post_vol = get_safe_balance(ticker)
        bought_vol = post_vol - pre_vol
        if bought_vol <= 0: bought_vol = (invest_amount * 0.9995) / curr_price
        return bought_vol
    except: return 0

def update_seed_money():
    global SEED_MONEY
    try:
        krw_bal = get_safe_balance("KRW") 
        bot_pos_value = sum(pos['vol'] * (pyupbit.get_current_price(ticker) or 0) for ticker, pos in bot_positions.items())
        raw_seed = krw_bal + bot_pos_value
        SEED_MONEY = min(raw_seed * 0.95, MAX_BOT_BUDGET) 
    except: pass

def get_net_profit(buy_price, sell_price, vol):
    return (sell_price * vol * 0.9995) - (buy_price * vol * 1.0005)

def notify_trade(action, ticker, price, vol, profit_rate=0.0, remaining_vol=0.0, realized_profit=0.0):
    coin = ticker.split('-')[1]
    total_amount = price * vol 
    if "매수" in action: icon, prof_str, amount_label, profit_info = "🟢", "", "투입대금", ""
    else: 
        icon = "🔵" if profit_rate > 0 else "🔴"
        if "방출" in action: icon = "⚖️"
        prof_str, amount_label = f" ({profit_rate:+.2f}%)", "회수대금"
        profit_info = f"- 실현수익: {realized_profit:+,.0f}원 (정산완료)\n" 

    msg = f"{icon} [{action}] {ticker}\n- 체결가: {price:,.0f}원{prof_str}\n- 체결량: {vol:.4f} {coin}\n- {amount_label}: {total_amount:,.0f}원\n{profit_info}- 봇전용잔고: {remaining_vol:.4f} {coin}"
    send_telegram(msg)

# -------------------------------------------------------------
# 🕸️ V17.17 하이브리드 그리드 후보 스캔 (ETH 제외)
# -------------------------------------------------------------
def evaluate_grid_candidates():
    global top_grid_candidates
    print("🕸️ [V17.17] Alpha 그리드 최적 사냥터 스캔 중...")
    scores = []
    # ETH를 제외한 나머지 후보군에서 탐색
    alpha_pool = [t for t in GRID_POOL if t != "KRW-ETH"]
    for ticker in alpha_pool:
        score = analyzer.get_grid_suitability_score(ticker)
        scores.append({'ticker': ticker, 'score': score})
        time.sleep(0.2)
        
    sorted_scores = sorted(scores, key=lambda x: x['score'], reverse=True)
    # 가장 점수가 높은 1개만 Alpha 후보로 선정
    top_grid_candidates = [item['ticker'] for item in sorted_scores[:1]]
    send_telegram(f"🔍 [그리드 레이더] Alpha 사냥터 선정: {top_grid_candidates[0]}")

def background_target_fetcher():
    global next_day_core_targets, next_day_hunter_targets
    if current_regime == "ICE_AGE":
        print("💤 [동면] 시장 빙하기로 인해 타겟 탐색 스킵.")
        return

    print("🕵️‍♂️ 4H 레이더 가동...")
    temp_core, temp_hunter_candidates = {}, []
    for ticker in CORE_UNIVERSE:
        time.sleep(0.15)
        df = pyupbit.get_ohlcv(ticker, interval="day", count=20)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 6: continue
        df['noise'] = 1 - abs(df['open'] - df['close']) / (df['high'] - df['low'])
        temp_core[ticker] = {'open': df.iloc[-1]['open'], 'range': analyzer.get_atr(df, 5), 'k': max(0.4, min(0.7, df['noise'].mean()))}
        
    try:
        tickers = pyupbit.get_tickers(fiat="KRW")
        for t in tickers:
            if t in CORE_UNIVERSE: continue 
            time.sleep(0.1)
            df = pyupbit.get_ohlcv(t, interval="day", count=6)
            if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 6: continue
            temp_hunter_candidates.append({'ticker': t, 'value': df.iloc[-2]['value'], 'open': df.iloc[-1]['open'], 'range': analyzer.get_atr(df, 5)})
        if temp_hunter_candidates:
            top3 = sorted(temp_hunter_candidates, key=lambda x: x['value'], reverse=True)[:3]
            next_day_hunter_targets = {item['ticker']: item for item in top3}
    except: pass
    next_day_core_targets = temp_core
    print("✅ [레이더] 갱신 완료.")

# -------------------------------------------------------------
# 🚀 메인 프로그램 초기화
# -------------------------------------------------------------
bot_positions = db_manager.recover_bot_positions(upbit)
now_init = datetime.now()

for t, p in bot_positions.items():
    if 'buy_time' not in p: p['buy_time'] = now_init
    if p['engine'] == 'HUNTER' and 'struct_stop' not in p: p['struct_stop'] = analyzer.get_structural_stop(t)
    if p['engine'] == 'GRID':
        if 'allocated_krw' not in p: p['allocated_krw'] = (SEED_MONEY / TOTAL_SLOTS) * 0.5
        if 'last_grid_price' not in p: p['last_grid_price'] = pyupbit.get_current_price(t) or p['buy']
        if 'grid_step' not in p: p['grid_step'] = analyzer.get_grid_step(t)

update_seed_money()

# 💡 텔레그램 백그라운드 리스너 가동
telegram_handler.start_telegram_listener(bot_positions, lambda: SEED_MONEY)

background_target_fetcher()
evaluate_grid_candidates() 
last_grid_eval_time = datetime.now()
core_targets, hunter_targets = next_day_core_targets.copy(), next_day_hunter_targets.copy()

next_regime_check_time = datetime.now()
last_report_time = datetime.now() - timedelta(hours=3)
last_target_fetch_time = None
consecutive_errors = 0

# -------------------------------------------------------------
# 🔄 메인 무한 루프
# -------------------------------------------------------------
while True:
    try:
        now = datetime.now()
        update_seed_money()
        
        if analyzer.check_panic_fall():
            send_telegram("🚨🚨 [DEFCON-1] 비트코인 대폭락 감지! 알트코인 전량 긴급 탈출!")
            for ticker, pos in list(bot_positions.items()):
                if pos['engine'] == 'GRID': continue
                curr_p = pyupbit.get_current_price(ticker) or pos['buy']
                actual_sell = safe_sell_order(ticker, pos['vol'])
                if actual_sell > 0:
                    net_prof = get_net_profit(pos['buy'], curr_p, actual_sell)
                    db_manager.log_trade(ticker, "PANIC_SELL", pos['engine'], curr_p, actual_sell, (curr_p-pos['buy'])/pos['buy']*100, net_prof)
                    notify_trade(f"긴급탈출: {pos['engine']}", ticker, curr_p, actual_sell, (curr_p-pos['buy'])/pos['buy']*100, 0.0, net_prof)
                    del bot_positions[ticker]
            time.sleep(10); continue

        is_turbulence = (now.hour == 8 and now.minute >= 50) or (now.hour == 9 and now.minute <= 10)

        if now >= next_regime_check_time:
            old_regime = current_regime
            current_regime = analyzer.get_market_regime(current_regime)
            if old_regime == "ICE_AGE" and current_regime != "ICE_AGE":
                send_telegram(f"🌅 [해빙기] 시장 회복! 레이더 재가동.")
                background_target_fetcher() 
                evaluate_grid_candidates()
            next_regime_check_time = now + timedelta(minutes=15)

        if 8 <= now.hour < 23 and now >= last_report_time + timedelta(hours=3):
            pos_info = "".join([f"{'🛡️' if p['engine']=='CORE' else ('🏹' if p['engine']=='HUNTER' else '🕸️')} {t}: {((pyupbit.get_current_price(t) or p['buy'])-p['buy'])/p['buy']*100:+.2f}%\n" for t,p in bot_positions.items()])
            # 너무 잦은 알림을 줄이기 위해 정기보고는 생략하거나 간소화 가능
            last_report_time = now

        if now.hour % 4 == 0 and now.minute == 50 and (last_target_fetch_time is None or now >= last_target_fetch_time + timedelta(hours=3)):
            last_target_fetch_time = now 
            threading.Thread(target=background_target_fetcher).start()

        # ==============================================================
        # 🕸️ 하이브리드 그리드 엔진 (Fixed ETH + Alpha 1)
        # ==============================================================
        if last_grid_eval_time is None or now >= last_grid_eval_time + timedelta(hours=12):
            evaluate_grid_candidates()
            last_grid_eval_time = now

        grid_positions = {t: p for t, p in bot_positions.items() if p['engine'] == 'GRID'}
        grid_count = len(grid_positions)

        # [1] 스티키 교체 및 그리드 매매 (공통)
        for t, pos in list(grid_positions.items()):
            curr_p = pyupbit.get_current_price(t)
            if not curr_p: continue
            profit_rate = (curr_p - pos['buy']) / pos['buy']
            
            # Alpha 슬롯 교체 로직 (ETH는 절대 교체 안함, 타겟에 없고 수익권일때만 스왑)
            if t != "KRW-ETH" and t not in top_grid_candidates and profit_rate > 0.01:
                actual_sell = safe_sell_order(t, pos['vol'])
                if actual_sell > 0:
                    net_prof = get_net_profit(pos['buy'], curr_p, actual_sell)
                    db_manager.log_trade(t, "SWAP_GRID", "GRID", curr_p, actual_sell, profit_rate*100, net_prof)
                    notify_trade("방출: ⚖️ Alpha 사냥터 이전 (교체)", t, curr_p, actual_sell, profit_rate*100, 0, net_prof)
                    del bot_positions[t]
                    grid_count -= 1
                    continue 

            # 그리드 상/하단 매매
            step = analyzer.get_grid_step(t) or pos['grid_step']
            if curr_p >= pos['last_grid_price'] + step:
                actual_sell = safe_sell_order(t, max(pos['vol']*0.15, 6000/curr_p))
                if actual_sell > 0:
                    pos['vol'] -= actual_sell; pos['last_grid_price'] = curr_p
                    net_prof = get_net_profit(pos['buy'], curr_p, actual_sell)
                    if (pos['vol']*curr_p + pos['allocated_krw']) > (SEED_MONEY/TOTAL_SLOTS)*1.05:
                        db_manager.log_trade(t, "SELL_REBALANCE", "GRID", curr_p, actual_sell, profit_rate*100, net_prof)
                        notify_trade("방출: ⚖️ 그리드 다이어트", t, curr_p, actual_sell, profit_rate*100, pos['vol'], net_prof)
                    else:
                        pos['allocated_krw'] += (actual_sell * curr_p)
                        db_manager.log_trade(t, "SELL_GRID", "GRID", curr_p, actual_sell, profit_rate*100, net_prof)
                        notify_trade("익절: 🕸️ 그리드 상단", t, curr_p, actual_sell, profit_rate*100, pos['vol'], net_prof)
            elif curr_p <= pos['last_grid_price'] - step and curr_p > analyzer.get_ema200(t):
                buy_krw = max(pos['allocated_krw']*0.15, 6000)
                if get_safe_balance("KRW") >= buy_krw and pos['allocated_krw'] >= buy_krw:
                    vol = safe_buy_order(t, buy_krw, curr_p)
                    if vol > 0:
                        pos['vol'] += vol; pos['last_grid_price'] = curr_p; pos['allocated_krw'] -= buy_krw
                        db_manager.log_trade(t, "BUY_GRID", "GRID", curr_p, vol, 0.0, 0.0)
                        notify_trade("매수: 🕸️ 그리드 하단", t, curr_p, vol, (curr_p-pos['buy'])/pos['buy']*100, pos['vol'])

        # [2] 빈 슬롯 채우기 (Fixed ETH & Alpha)
        if not is_turbulence and current_regime != "ICE_AGE" and len(bot_positions) < TOTAL_SLOTS:
            base_invest = (SEED_MONEY / TOTAL_SLOTS) * 0.5
            
            # Fixed 슬롯 (ETH) 체크 및 가동
            if "KRW-ETH" not in grid_positions:
                curr_p = pyupbit.get_current_price("KRW-ETH")
                if curr_p:
                    vol = safe_buy_order("KRW-ETH", base_invest, curr_p)
                    if vol > 0:
                        bot_positions["KRW-ETH"] = {'vol':vol, 'buy':curr_p, 'peak':curr_p, 'engine':'GRID', 'last_grid_price':curr_p, 'grid_step':analyzer.get_grid_step("KRW-ETH"), 'allocated_krw':base_invest}
                        db_manager.log_trade("KRW-ETH", "BUY_GRID_INIT", "GRID", curr_p, vol, 0.0, 0.0)
                        notify_trade("매수: 🕸️ Fixed 그리드 (ETH) 가동", "KRW-ETH", curr_p, vol, 0.0, vol)
            
            # Alpha 슬롯 체크 및 가동
            alpha_count = sum(1 for t, p in bot_positions.items() if p['engine'] == 'GRID' and t != "KRW-ETH")
            if alpha_count < 1 and len(bot_positions) < TOTAL_SLOTS:
                for candidate in top_grid_candidates:
                    if candidate not in bot_positions:
                        curr_p = pyupbit.get_current_price(candidate)
                        if curr_p:
                            vol = safe_buy_order(candidate, base_invest, curr_p)
                            if vol > 0:
                                bot_positions[candidate] = {'vol':vol, 'buy':curr_p, 'peak':curr_p, 'engine':'GRID', 'last_grid_price':curr_p, 'grid_step':analyzer.get_grid_step(candidate), 'allocated_krw':base_invest}
                                db_manager.log_trade(candidate, "BUY_GRID_INIT", "GRID", curr_p, vol, 0.0, 0.0)
                                notify_trade("매수: 🏹 Alpha 그리드 가동", candidate, curr_p, vol, 0.0, vol)

        # ==============================================================
        # 🛡️🏹 코어 및 헌터 매도 구역
        # ==============================================================
        for ticker, pos in list(bot_positions.items()):
            if pos['engine'] == 'GRID': continue
            curr_p = pyupbit.get_current_price(ticker)
            if not curr_p: continue
            if curr_p > pos['peak']: pos['peak'] = curr_p
            
            prof_rate = (curr_p - pos['buy']) / pos['buy']
            
            if pos['engine'] == "CORE":
                if prof_rate >= 0.05 and not pos.get('half_sold', False):
                    actual_sell = safe_sell_order(ticker, pos['vol'] * 0.5)
                    if actual_sell > 0:
                        pos['vol'] -= actual_sell; pos['half_sold'] = True
                        net_prof = get_net_profit(pos['buy'], curr_p, actual_sell)
                        notify_trade("매도: 코어 절반익절", ticker, curr_p, actual_sell, prof_rate*100, pos['vol'], net_prof)
                if curr_p < analyzer.get_chandelier_exit(ticker, pos['peak'], current_regime):
                    actual_sell = safe_sell_order(ticker, pos['vol'])
                    if actual_sell > 0:
                        net_prof = get_net_profit(pos['buy'], curr_p, actual_sell)
                        notify_trade(f"매도: 코어 샹들리에 청산", ticker, curr_p, actual_sell, prof_rate*100, 0.0, net_prof)
                        del bot_positions[ticker]

            elif pos['engine'] == "HUNTER":
                if curr_p < pos['struct_stop'] or ( (now - pos['buy_time']).total_seconds()/60 >= 45 and prof_rate <= 0 ):
                    actual_sell = safe_sell_order(ticker, pos['vol'])
                    if actual_sell > 0:
                        net_prof = get_net_profit(pos['buy'], curr_p, actual_sell)
                        notify_trade(f"매도: 헌터 원칙 청산", ticker, curr_p, actual_sell, prof_rate*100, 0.0, net_prof)
                        del bot_positions[ticker]

        # ==============================================================
        # 🚀 코어 및 헌터 매수 구역 (V17.17 스마트 필터 적용)
        # ==============================================================
        if not is_turbulence and current_regime != "ICE_AGE" and not analyzer.check_btc_flash_crash():
            core_count = sum(1 for p in bot_positions.values() if p['engine'] == 'CORE')
            hunter_count = sum(1 for p in bot_positions.values() if p['engine'] == 'HUNTER')
            total_occupied = len(bot_positions)
            
            base_invest = (SEED_MONEY / TOTAL_SLOTS) * REGIME_SETTINGS[current_regime]['ratio']
            
            if core_count < CORE_SLOTS and total_occupied < TOTAL_SLOTS:
                for ticker, t_info in core_targets.items():
                    if ticker in bot_positions or core_count >= CORE_SLOTS or total_occupied >= TOTAL_SLOTS: continue
                    curr_p = pyupbit.get_current_price(ticker)
                    if curr_p and curr_p >= (t_info['open'] + t_info['range']*t_info['k']):
                        # 💡 [V17.17 CORE 필터] 돌파 + ADX 추세 강도 + 거래량 스파이크 동시 확인
                        if analyzer.check_keltner_breakout(ticker) and analyzer.get_adx(ticker) > 25 and analyzer.check_volume_spike(ticker):
                            vol = safe_buy_order(ticker, base_invest, curr_p)
                            if vol > 0:
                                bot_positions[ticker] = {'vol':vol, 'buy':curr_p, 'peak':curr_p, 'engine':'CORE', 'buy_time':now}
                                db_manager.log_trade(ticker, "BUY", "CORE", curr_p, vol, 0.0, 0.0)
                                notify_trade("매수: 🛡️ 코어 진입 (추세+거래량 확인)", ticker, curr_p, vol, 0.0, vol)
                                core_count += 1; total_occupied += 1 

            if hunter_count < HUNTER_SLOTS and total_occupied < TOTAL_SLOTS:
                for ticker in hunter_targets.keys():
                    if ticker in bot_positions or hunter_count >= HUNTER_SLOTS or total_occupied >= TOTAL_SLOTS: continue
                    # 💡 [V17.17 HUNTER 필터] 과매도 진입 후 바닥 지지 캔들(아래꼬리 핀바) 확인
                    if analyzer.check_hunter_dip_buy(ticker) and analyzer.is_pin_bar(ticker):
                        curr_p = pyupbit.get_current_price(ticker)
                        vol = safe_buy_order(ticker, base_invest, curr_p)
                        if vol > 0:
                            bot_positions[ticker] = {'vol':vol, 'buy':curr_p, 'peak':curr_p, 'engine':'HUNTER', 'buy_time':now, 'struct_stop':analyzer.get_structural_stop(ticker)}
                            db_manager.log_trade(ticker, "BUY", "HUNTER", curr_p, vol, 0.0, 0.0)
                            notify_trade("매수: 🏹 헌터 진입 (바닥 지지 확인)", ticker, curr_p, vol, 0.0, vol)
                            hunter_count += 1; total_occupied += 1 

        time.sleep(1) 
    except Exception as e:
        consecutive_errors += 1
        if consecutive_errors < 5: time.sleep(5)
        else:
            send_telegram(f"🚨 [다운] {e}\n{traceback.format_exc()[-200:]}"); break
