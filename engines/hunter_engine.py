import os
import time
import pyupbit
import worker
import db_manager
import analyzer
from config import REGIME_SETTINGS, TOTAL_SLOTS
from engines.base_engine import BaseEngine

class HunterEngine(BaseEngine):
    def __init__(self, upbit, bot_positions, bot_positions_lock):
        super().__init__(upbit, bot_positions, bot_positions_lock)
        self.MAX_BUDGET = float(os.getenv('HUNTER_MAX_BUDGET', os.getenv('MAX_BUDGET', 0)))
        self.TARGET_SLOTS = int(os.getenv('TARGET_SLOTS', 3))
        self.budget_lock_notified = False

    def run(self, now, current_regime, hunter_targets, is_panic_state, safe_balances):
        bot_positions = self.bot_positions
        hunter_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'HUNTER'}
        watch_list = list(set([p['ticker'] for p in hunter_pos_items.values()] + list(hunter_targets.keys())))
        
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        # [1] 기존 포지션 관리 (매도)
        for key, pos in list(hunter_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker)
            if not curr_p: continue
            
            profit_rate = (curr_p - pos['buy']) / pos['buy']
            
            currency = ticker.split('-')[1]
            sell_vol = min(pos['vol'], safe_balances.get(currency, 0.0))
            if sell_vol <= 0:
                print(f"🧹 [유령 장부 청소/HUNTER] {ticker} 실제 잔고 없음. DB에서 삭제합니다.")
                db_manager.delete_position('HUNTER', ticker, pos['slot_index'])
                with self.bot_positions_lock: del bot_positions[key]
                continue

            with self.bot_positions_lock:
                if 'peak_price' not in pos: pos['peak_price'] = curr_p
                pos['peak_price'] = max(pos['peak_price'], curr_p)
                peak_profit_rate = (pos['peak_price'] - pos['buy']) / pos['buy']
                drop_from_peak = (pos['peak_price'] - curr_p) / pos['peak_price']

                adx_value = analyzer.get_adx(ticker)
                if adx_value >= 40: target_rate = 0.07
                elif adx_value >= 25: target_rate = 0.05
                else: target_rate = 0.03
                
                if current_regime == "SUPER_BULL":
                    target_rate = max(0.05, target_rate)
                    
                rsi_value = analyzer.get_rsi_value(ticker, interval="minute15")
                current_drop_limit = 0.015
                if rsi_value >= 70: current_drop_limit = 0.007
                    
                if peak_profit_rate >= target_rate and drop_from_peak >= current_drop_limit:
                    realized_krw = (curr_p - pos['buy']) * sell_vol
                    print(f"🎯 [HUNTER 트레일링 익절] {ticker} {target_rate*100:.0f}% 돌파 후 추세 꺾임. 수익 확정!")
                    if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw, engine_name='HUNTER'):
                        del bot_positions[key]
                    continue

                struct_stop = pos.get('struct_stop', 0)
                time_elapsed_mins = (now - pos.get('created_at', now)).total_seconds() / 60
                
                if curr_p < struct_stop or (time_elapsed_mins >= 45 and profit_rate <= 0):
                    realized_krw = (curr_p - pos['buy']) * sell_vol
                    reason = "구조적 저점 이탈" if curr_p < struct_stop else "반등 지연(타임아웃)"
                    print(f"🛑 [HUNTER 손절] {ticker} {reason}. ({profit_rate*100:+.2f}%)")
                    if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw, engine_name='HUNTER'):
                        del bot_positions[key]
                    continue

        # [2] 신규 진입 (매수)
        current_hunter_count = len([p for p in bot_positions.values() if p['engine'] == 'HUNTER'])
        if current_hunter_count < self.TARGET_SLOTS and self.MAX_BUDGET > 0 and current_regime not in ["ICE_AGE"] and not is_panic_state:
            base_invest = (self.MAX_BUDGET / self.TARGET_SLOTS) if self.TARGET_SLOTS > 0 else self.MAX_BUDGET
            base_invest *= REGIME_SETTINGS.get(current_regime, {}).get('ratio', 1.0)
            already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in hunter_pos_items.values())

            for ticker in hunter_targets.keys():
                if current_hunter_count >= self.TARGET_SLOTS: break
                if ticker in [p['ticker'] for p in bot_positions.values()]: continue

                if analyzer.check_hunter_dip_buy(ticker) or analyzer.is_pin_bar(ticker):
                    krw_balance = safe_balances.get('KRW', 0.0)
                    if krw_balance < base_invest * 1.0005 or (already_used + base_invest) > self.MAX_BUDGET:
                        if not self.budget_lock_notified:
                            print(f"🛑 [HUNTER 예산/잔고 잠금] {ticker} 보류 (사용량: {already_used:,.0f} / 잔고: {krw_balance:,.0f})")
                            self.budget_lock_notified = True
                        break

                    self.budget_lock_notified = False
                    new_slot_idx = 1
                    while new_slot_idx in [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker]: new_slot_idx += 1
                    
                    print(f"🏹 [HUNTER 신규 진입] {ticker} 과매도 반등 포착!")
                    success, exec_price, exec_vol = worker.execute_buy(ticker, base_invest, self.MAX_BUDGET, new_slot_idx, engine_name='HUNTER')
                    if success:
                        safe_balances['KRW'] = safe_balances.get('KRW', 0.0) - (base_invest * 1.0005)
                        key = f"{ticker}_slot_{new_slot_idx}"
                        with self.bot_positions_lock:
                            bot_positions[key] = {
                                'ticker': ticker, 'vol': exec_vol, 'buy': exec_price, 
                                'slot_index': new_slot_idx, 'engine': 'HUNTER', 'buy_level': 1, 
                                'created_at': now, 'struct_stop': analyzer.get_structural_stop(ticker),
                                'peak_price': exec_price, 'invested_amount': exec_price * exec_vol
                            }
                        try: db_manager.update_position_state(key, exec_price, exec_vol, 1, engine_name='HUNTER')
                        except AttributeError: pass
                        current_hunter_count += 1
                        already_used += (exec_price * exec_vol)
                        time.sleep(1.5)