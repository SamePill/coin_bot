import os
import time
from datetime import datetime, timedelta
import pyupbit
import worker
import db_manager
import analyzer
from engines.base_engine import BaseEngine

class ScalpEngine(BaseEngine):
    def __init__(self, upbit, bot_positions, bot_positions_lock):
        super().__init__(upbit, bot_positions, bot_positions_lock)
        self.MAX_BUDGET = float(os.getenv('SCALP_MAX_BUDGET', os.getenv('MAX_BUDGET', 0)))
        self.SCALP_TOTAL_SLOTS = int(os.getenv('SCALP_TOTAL_SLOTS', 2))
        self.SCALP_USE_MULTI_SLOT = os.getenv('SCALP_USE_MULTI_SLOT', 'True').lower() == 'true'
        self.SCALP_MAX_SLOTS_PER_COIN = int(os.getenv('SCALP_MAX_SLOTS_PER_COIN', 2))
        
        scalp_units_str = os.getenv('SCALP_UNIT_SIZES')
        if scalp_units_str:
            self.SCALP_UNIT_LIST = [float(x.strip()) for x in scalp_units_str.split(',')]
        else:
            self.SCALP_UNIT_LIST = [float(x) for x in os.getenv('GRID_UNIT_SIZES', '10000,30000').split(',')]
        self.budget_lock_notified = False

    def run(self, now, current_regime, top_grid_candidates, is_panic_state, safe_balances):
        bot_positions = self.bot_positions
        scalp_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'SCALP'}
        active_tickers = {} 
        
        watch_list = list(set([pos['ticker'] for pos in scalp_pos_items.values()] + top_grid_candidates))
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        for key, pos in list(scalp_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker) 
            if not curr_p: continue
            
            active_tickers[ticker] = active_tickers.get(ticker, 0) + 1

            currency = ticker.split('-')[1]
            actual_balance = safe_balances.get(currency, 0.0)
            sell_vol = min(pos['vol'], actual_balance)

            if sell_vol <= 0:
                print(f"🧹 [유령 장부 청소/SCALP] {ticker} 실제 잔고 없음. DB에서 삭제합니다.")
                db_manager.delete_position('SCALP', ticker, pos['slot_index'])
                with self.bot_positions_lock: del bot_positions[key]
                continue

            with self.bot_positions_lock:
                if 'peak_price' not in pos: pos['peak_price'] = curr_p
                pos['peak_price'] = max(pos['peak_price'], curr_p)
                profit_rate = (curr_p - pos['buy']) / pos['buy']
                peak_profit_rate = (pos['peak_price'] - pos['buy']) / pos['buy']
                drop_from_peak = (pos['peak_price'] - curr_p) / pos['peak_price']

                adx_value = analyzer.get_adx(ticker, interval="minute15") # Scalp는 15분 ADX가 적합
                if adx_value >= 35: trigger_rate = 0.015
                elif adx_value >= 25: trigger_rate = 0.010
                else: trigger_rate = 0.006

                dynamic_callback = analyzer.get_volatility_factor(ticker)
                rsi_value = analyzer.get_rsi_value(ticker, interval="minute5")
                if rsi_value >= 70:
                    dynamic_callback = max(0.001, dynamic_callback * 0.5) 

            if peak_profit_rate >= trigger_rate and drop_from_peak >= dynamic_callback:
                realized_krw = (curr_p - pos['buy']) * sell_vol
                print(f"⚡ [스캘핑 트레일링] {ticker} 익절 완료 ({profit_rate*100:+.2f}%)")
                if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw, engine_name='SCALP'):
                    with self.bot_positions_lock:
                        if key in bot_positions: del bot_positions[key]
                continue

            time_elapsed = (now - pos.get('created_at', now)).total_seconds() / 3600
            if time_elapsed >= 4 and profit_rate < 0.003:
                print(f"⏳ [스캘핑 타임컷] {ticker} 순환을 위해 정리")
                if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, 0, engine_name='SCALP'):
                    with self.bot_positions_lock:
                        if key in bot_positions: del bot_positions[key]
                continue

            current_level = pos.get('buy_level', 1)
            if profit_rate <= -0.010 and current_level < 2:
                next_level = current_level + 1
                base_unit = self.SCALP_UNIT_LIST[pos['slot_index']-1] if (pos['slot_index']-1) < len(self.SCALP_UNIT_LIST) else self.SCALP_UNIT_LIST[-1]

                already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in scalp_pos_items.values())
                krw_balance = safe_balances.get('KRW', 0.0)

                if krw_balance >= base_unit * 1.0005 and (already_used + base_unit) <= self.MAX_BUDGET:
                    self.budget_lock_notified = False
                    print(f"📉 [스캘핑 방어] {ticker} {next_level}차 진입 시도")
                    success, exec_price, exec_vol = worker.execute_buy(ticker, base_unit, self.MAX_BUDGET, pos['slot_index'], engine_name='SCALP')
                    if success:
                        safe_balances['KRW'] = safe_balances.get('KRW', 0.0) - (base_unit * 1.0005)
                        time.sleep(1.5)
                        new_vol = pos['vol'] + exec_vol
                        new_avg_price = ((pos['buy'] * pos['vol']) + (exec_price * exec_vol)) / new_vol
                        with self.bot_positions_lock:
                            bot_positions[key].update({
                                'buy': new_avg_price,
                                'vol': new_vol,
                                'buy_level': next_level,
                                'invested_amount': pos.get('invested_amount', 0) + (exec_price * exec_vol),
                                'peak_price': new_avg_price # 💡 물타기 후 고점 초기화
                            })
                        try: db_manager.update_position_state(key, new_avg_price, new_vol, next_level, engine_name='SCALP')
                        except AttributeError: pass
                else:
                    if not self.budget_lock_notified:
                        print(f"🛑 [SCALP 예산 잠금] {ticker} 물타기 보류")
                        self.budget_lock_notified = True
                continue

        # [3] 신규 진입 (빈 슬롯 채우기)
        total_active_slots = sum(active_tickers.values())
        remaining_slots = self.SCALP_TOTAL_SLOTS - total_active_slots
        already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in scalp_pos_items.values())

        if remaining_slots > 0 and self.MAX_BUDGET > 0 and current_regime not in ["ICE_AGE", "CAUTION"] and not is_panic_state:
            for ticker in top_grid_candidates:
                if remaining_slots <= 0: break
                current_count = active_tickers.get(ticker, 0)
                slot_limit = self.SCALP_MAX_SLOTS_PER_COIN if self.SCALP_USE_MULTI_SLOT else 1

                if current_count < slot_limit:
                    unit_size = self.SCALP_UNIT_LIST[current_count] if current_count < len(self.SCALP_UNIT_LIST) else self.SCALP_UNIT_LIST[-1]
                    krw_balance = safe_balances.get('KRW', 0.0)
                    if krw_balance < unit_size * 1.0005:
                        print(f"❌ [실제 잔고 부족] {ticker} 신규 진입 불가 (필요: {unit_size:,.0f}원 / 잔고: {krw_balance:,.0f}원)")
                        break

                    if (already_used + unit_size) > self.MAX_BUDGET:
                        if not self.budget_lock_notified: 
                            print(f"🛑 [SCALP 예산 잠금] 신규 진입 예산 초과. 사냥 보류. (한도: {self.MAX_BUDGET:,.0f}원)")
                            self.budget_lock_notified = True
                        break

                    self.budget_lock_notified = False
                    new_slot_idx = 1
                    existing_slots = [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker and p['engine'] == 'SCALP']
                    while new_slot_idx in existing_slots: new_slot_idx += 1

                    success, exec_price, exec_vol = worker.execute_buy(ticker, unit_size, self.MAX_BUDGET, new_slot_idx, engine_name='SCALP')
                    if success:
                        safe_balances['KRW'] = safe_balances.get('KRW', 0.0) - (unit_size * 1.0005)
                        time.sleep(1.5) 
                        key = f"{ticker}_slot_{new_slot_idx}"
                        with self.bot_positions_lock:
                            bot_positions[key] = {
                                'ticker': ticker, 'vol': exec_vol, 'buy': exec_price, 'slot_index': new_slot_idx, 
                                'engine': 'SCALP', 'buy_level': 1, 'created_at': now, 'peak_price': exec_price, 'invested_amount': exec_price * exec_vol
                            }
                        try: db_manager.update_position_state(key, exec_price, exec_vol, 1, engine_name='SCALP')
                        except AttributeError: pass
                        remaining_slots -= 1
                        already_used += (exec_price * exec_vol)
                        active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
                        print(f"🚀 [SCALP 신규] {ticker} 스캘핑 슬롯 {new_slot_idx} 배치 완료 (투입: {unit_size:,.0f}원)")