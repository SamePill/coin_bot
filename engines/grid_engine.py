import os
import time
from datetime import datetime, timedelta
import pyupbit
import pandas as pd
import worker
import db_manager
import analyzer
from config import send_telegram
from engines.base_engine import BaseEngine

class GridEngine(BaseEngine):
    def __init__(self, upbit, bot_positions, bot_positions_lock):
        super().__init__(upbit, bot_positions, bot_positions_lock)
        self.MAX_BUDGET = float(os.getenv('GRID_MAX_BUDGET', 0))
        self.GRID_TOTAL_SLOTS = int(os.getenv('GRID_TOTAL_SLOTS', 2))
        self.USE_MULTI_SLOT = os.getenv('USE_MULTI_SLOT', 'True').lower() == 'true'
        self.MAX_SLOTS_PER_COIN = int(os.getenv('MAX_SLOTS_PER_COIN', 2))
        self.UNIT_LIST = [float(x) for x in os.getenv('GRID_UNIT_SIZES', '10000,30000').split(',')]
        self.budget_lock_notified = False

    def run(self, now, current_regime, top_grid_candidates, is_panic_state, safe_balances):
        bot_positions = self.bot_positions
        grid_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'GRID'}
        active_tickers = {} 
        
        watch_list = list(set([pos['ticker'] for pos in grid_pos_items.values()] + top_grid_candidates))
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        krw_balance = safe_balances.get('KRW', 0.0)
        # [1] 기존 슬롯 관리
        for key, pos in list(grid_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker) 
            if not curr_p: continue
            
            active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
            profit_rate = (curr_p - pos['buy']) / pos['buy']
            
            currency = ticker.split('-')[1]
            if safe_balances.get(currency, 0.0) <= 0:
                print(f"🧹 [유령 장부 청소/GRID] {ticker} 실제 잔고 없음. DB에서 삭제합니다.")
                db_manager.delete_position('GRID', ticker, pos['slot_index'])
                with self.bot_positions_lock: del bot_positions[key]
                continue

            with self.bot_positions_lock:
                if 'peak_price' not in pos: pos['peak_price'] = curr_p
                pos['peak_price'] = max(pos['peak_price'], curr_p)

                last_update = pos.get('created_at', datetime.now())
                if datetime.now() - last_update > timedelta(days=7) and profit_rate < 0.01:
                    print(f"⏳ [타임 컷] {ticker} 슬롯 {pos['slot_index']} 장기 체류로 인한 강제 회수")
                    if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, 0, engine_name='GRID'):
                        send_telegram(f"✂️ [Time Cut] {ticker} 기회비용 확보를 위해 포지션 종료")
                        del bot_positions[key]
                    continue

                if ticker not in top_grid_candidates and profit_rate > 0.01:
                    realized_krw = (curr_p - pos['buy']) * pos['vol']
                    if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw, engine_name='GRID'):
                        print(f"⚖️ [슬롯 교체] {ticker} (수익권 방출 후 새 종목 대기)")
                        del bot_positions[key]
                    continue

                if current_regime == "SUPER_BULL" and analyzer.check_volume_spike(ticker):
                    if 0.015 < profit_rate < 0.03 and pos.get('buy_level', 1) == 1:
                        print(f"🔥 [불타기] {ticker} 추세 돌파 감지! 비중 확대")
                        success, exec_p, exec_v = worker.execute_buy(ticker, self.UNIT_LIST[0] * 1.5, self.MAX_BUDGET, pos['slot_index'], engine_name='GRID')
                        if success:
                            new_vol = pos['vol'] + exec_v
                            new_avg = ((pos['buy'] * pos['vol']) + (exec_p * exec_v)) / new_vol
                            bot_positions[key]['buy'] = new_avg
                            bot_positions[key]['vol'] = new_vol
                            bot_positions[key]['buy_level'] = 2
                            db_manager.update_position_state(key, new_avg, new_vol, 2, engine_name='GRID')
                        continue

                grid_step_percent = analyzer.get_dynamic_grid_step(ticker)
                current_level = pos.get('buy_level', 1) 
                target_buy_price = pos['buy'] * (1 - (grid_step_percent / 100))
                target_sell_price = pos['buy'] * (1 + (grid_step_percent / 100))
                
                if curr_p <= target_buy_price:
                    next_level = current_level + 1
                    weight = analyzer.get_pyramiding_weight(next_level, current_regime)
                    if weight <= 0: continue

                    base_unit = self.UNIT_LIST[pos['slot_index']-1] if (pos['slot_index']-1) < len(self.UNIT_LIST) else self.UNIT_LIST[-1]
                    invest_amount = base_unit * weight
                    
                    already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in grid_pos_items.values())
                    if (already_used + invest_amount) > self.MAX_BUDGET:
                        if not self.budget_lock_notified:
                            print(f"🛑 [GRID 예산 잠금] {ticker} {next_level}차 물타기 보류 (사용량: {already_used:,.0f} / 한도: {self.MAX_BUDGET:,.0f})")
                            self.budget_lock_notified = True
                        continue

                    if krw_balance < invest_amount:
                        if not self.budget_lock_notified:
                            print(f"❌ [예산 초과] {ticker} {next_level}차 진입 실패. (필요: {invest_amount:,.0f}원 / 잔고: {krw_balance:,.0f}원)")
                            self.budget_lock_notified = True
                        continue

                    self.budget_lock_notified = False
                    print(f"📉 [하락 방어] {ticker} {next_level}차 진입 시도 ({invest_amount:,.0f}원 / {weight}배 가중치)")
                    
                    success, exec_price, exec_vol = worker.execute_buy(ticker, invest_amount, self.MAX_BUDGET, pos['slot_index'], engine_name='GRID')
                    if success:
                        time.sleep(1.5) 
                        new_vol = pos['vol'] + exec_vol
                        new_avg_price = ((pos['buy'] * pos['vol']) + (exec_price * exec_vol)) / new_vol
                        bot_positions[key]['buy'] = new_avg_price
                        bot_positions[key]['vol'] = new_vol
                        bot_positions[key]['buy_level'] = next_level
                        bot_positions[key]['invested_amount'] = pos.get('invested_amount', 0) + (exec_price * exec_vol)
                        try: db_manager.update_position_state(key, new_avg_price, new_vol, next_level, engine_name='GRID')
                        except AttributeError: pass
                    continue

                elif curr_p >= target_sell_price:
                    realized_krw = (curr_p - pos['buy']) * pos['vol']
                    print(f"📈 [수익 실현] {ticker} 목표가 도달! 전량 익절 (수익률 {profit_rate*100:.2f}%)")
                    if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw, engine_name='GRID'):
                        del bot_positions[key]
                    continue

                drop_from_peak = (pos['peak_price'] - curr_p) / pos['peak_price']
                if profit_rate > 0.01 and drop_from_peak > 0.015:
                    realized_krw = (curr_p - pos['buy']) * pos['vol']
                    print(f"🛑 [익절 보존] {ticker} 고점 대비 하락으로 수익 확정 ({profit_rate*100:+.2f}%)")
                    if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw, engine_name='GRID'):
                        del bot_positions[key]
                    continue

        # [2] 빈 슬롯 채우기
        total_active_slots = sum(active_tickers.values())
        remaining_slots = self.GRID_TOTAL_SLOTS - total_active_slots
        if remaining_slots > 0 and self.MAX_BUDGET > 0 and current_regime != "ICE_AGE" and not is_panic_state:
            for ticker in top_grid_candidates:
                if remaining_slots <= 0: break
                current_count = active_tickers.get(ticker, 0)
                slot_limit = self.MAX_SLOTS_PER_COIN if self.USE_MULTI_SLOT else 1
                
                if current_count < slot_limit:
                    unit_size = self.UNIT_LIST[current_count] if current_count < len(self.UNIT_LIST) else self.UNIT_LIST[-1]
                    already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in grid_pos_items.values())
                    if (already_used + unit_size) > self.MAX_BUDGET:
                        if not self.budget_lock_notified:
                            print(f"🛑 [GRID 예산 잠금] 신규 진입 예산 초과. 사냥 보류. (한도: {self.MAX_BUDGET:,.0f}원)")
                            self.budget_lock_notified = True
                        break

                    self.budget_lock_notified = False
                    existing_slots = [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker and p['engine'] == 'GRID']
                    new_slot_idx = 1
                    while new_slot_idx in existing_slots: new_slot_idx += 1
                    
                    success, exec_price, exec_vol = worker.execute_buy(ticker, unit_size, self.MAX_BUDGET, new_slot_idx, engine_name='GRID')
                    if success:
                        time.sleep(1.5) 
                        key = f"{ticker}_slot_{new_slot_idx}"
                        with self.bot_positions_lock:
                            bot_positions[key] = {
                                'ticker': ticker, 'vol': exec_vol, 'buy': exec_price, 
                                'slot_index': new_slot_idx, 'engine': 'GRID', 'buy_level': 1,
                                'invested_amount': exec_price * exec_vol, 'created_at': now, 'peak_price': exec_price
                            }
                        try: db_manager.update_position_state(key, exec_price, exec_vol, 1, engine_name='GRID')
                        except AttributeError: pass
                        remaining_slots -= 1
                        active_tickers[ticker] = active_tickers.get(ticker, 0) + 1