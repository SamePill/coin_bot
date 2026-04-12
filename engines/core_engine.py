import os
import time
import pyupbit
import worker
import db_manager
import analyzer
from config import REGIME_SETTINGS, TOTAL_SLOTS
from engines.base_engine import BaseEngine

class CoreEngine(BaseEngine):
    def __init__(self, upbit, bot_positions, bot_positions_lock):
        super().__init__(upbit, bot_positions, bot_positions_lock)
        self.MAX_BUDGET = float(os.getenv('MAX_BUDGET', 0))
        self.TARGET_SLOTS = int(os.getenv('TARGET_SLOTS', 3))

    def run(self, now, current_regime, core_targets):
        bot_positions = self.bot_positions
        
        core_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'CORE'}
        watch_list = list(set([p['ticker'] for p in core_pos_items.values()] + list(core_targets.keys())))
        
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        safe_balances = self.get_safe_balances()

        # [1] 기존 포지션 관리 (매도)
        for key, pos in list(core_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker)
            if not curr_p: continue
            
            if 'peak_price' not in pos: pos['peak_price'] = curr_p
            pos['peak_price'] = max(pos['peak_price'], curr_p)
            profit_rate = (curr_p - pos['buy']) / pos['buy']
            
            currency = ticker.split('-')[1]
            sell_vol = min(pos['vol'], safe_balances.get(currency, 0.0))
            if sell_vol <= 0:
                with self.bot_positions_lock:
                    if key in bot_positions: del bot_positions[key]
                continue

            # 매도 1: 5% 도달 시 전량 익절
            if profit_rate >= 0.05:
                realized_krw = (curr_p - pos['buy']) * sell_vol
                print(f"📈 [CORE 수익 실현] {ticker} 목표가 도달! 전량 익절")
                if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                    with self.bot_positions_lock:
                        if key in bot_positions: del bot_positions[key]
                continue
                
            # 매도 2: 샹들리에 청산 (추세 꺾임)
            chandelier_exit_price = analyzer.get_chandelier_exit(ticker, pos['peak_price'], current_regime)
            if curr_p < chandelier_exit_price:
                realized_krw = (curr_p - pos['buy']) * sell_vol
                print(f"🛑 [CORE 샹들리에 청산] {ticker} 추세 꺾임 감지. ({profit_rate*100:+.2f}%)")
                if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                    with self.bot_positions_lock:
                        if key in bot_positions: del bot_positions[key]
                continue

        # [2] 신규 진입 (매수)
        current_core_count = len([p for p in bot_positions.values() if p['engine'] == 'CORE'])
        if current_core_count < self.TARGET_SLOTS and current_regime not in ["ICE_AGE"]:
            base_invest = (self.MAX_BUDGET / TOTAL_SLOTS) * REGIME_SETTINGS.get(current_regime, {}).get('ratio', 1.0)
            already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in core_pos_items.values())
            krw_balance = safe_balances.get('KRW', 0.0)

            for ticker, t_info in core_targets.items():
                if current_core_count >= self.TARGET_SLOTS: break
                if ticker in [p['ticker'] for p in bot_positions.values()]: continue
                
                curr_p = current_prices.get(ticker)
                if not curr_p: continue
                
                if curr_p >= (t_info['open'] + t_info['range']*t_info['k']):
                    if analyzer.check_keltner_breakout(ticker) and analyzer.get_adx(ticker) > 25 and analyzer.check_volume_spike(ticker):
                        if krw_balance < base_invest or (already_used + base_invest) > self.MAX_BUDGET:
                            print(f"🛑 [CORE 예산 잠금] {ticker} 보류")
                            break
                            
                        new_slot_idx = 1
                        while new_slot_idx in [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker]: new_slot_idx += 1
                        
                        print(f"🚀 [CORE 신규 진입] {ticker} 강력한 추세 돌파 포착!")
                        success, exec_price, exec_vol = worker.execute_buy(ticker, base_invest, new_slot_idx)
                        if success:
                            key = f"{ticker}_slot_{new_slot_idx}"
                            with self.bot_positions_lock:
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