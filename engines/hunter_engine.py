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
        self.MAX_BUDGET = float(os.getenv('MAX_BUDGET', 0))
        self.TARGET_SLOTS = int(os.getenv('TARGET_SLOTS', 3))

    def run(self, now, current_regime, hunter_targets):
        bot_positions = self.bot_positions
        hunter_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'HUNTER'}
        watch_list = list(set([p['ticker'] for p in hunter_pos_items.values()] + list(hunter_targets.keys())))
        
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        safe_balances = self.get_safe_balances()

        # [1] 기존 포지션 관리 (매도)
        for key, pos in list(hunter_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker)
            if not curr_p: continue
            
            profit_rate = (curr_p - pos['buy']) / pos['buy']
            currency = ticker.split('-')[1]
            sell_vol = min(pos['vol'], safe_balances.get(currency, 0.0))
            
            if sell_vol <= 0:
                with self.bot_positions_lock:
                    del bot_positions[key]
                continue

            # 익절 (반등 시 3% 수익 확정)
            if profit_rate >= 0.03:
                realized_krw = (curr_p - pos['buy']) * sell_vol
                print(f"🎯 [HUNTER 익절] {ticker} 낙폭과대 반등 목표가 달성!")
                if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                    with self.bot_positions_lock: del bot_positions[key]
                continue

            # 구조적 손절 (저점 이탈) 또는 45분 시간 초과
            struct_stop = pos.get('struct_stop', 0)
            time_elapsed_mins = (now - pos.get('created_at', now)).total_seconds() / 60
            
            if curr_p < struct_stop or (time_elapsed_mins >= 45 and profit_rate <= 0):
                realized_krw = (curr_p - pos['buy']) * sell_vol
                reason = "구조적 저점 이탈" if curr_p < struct_stop else "반등 지연(타임아웃)"
                print(f"🛑 [HUNTER 손절] {ticker} {reason}. ({profit_rate*100:+.2f}%)")
                if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                    with self.bot_positions_lock: del bot_positions[key]
                continue

        # [2] 신규 진입 (매수)
        current_hunter_count = len([p for p in bot_positions.values() if p['engine'] == 'HUNTER'])
        if current_hunter_count < self.TARGET_SLOTS and current_regime not in ["ICE_AGE"]:
            base_invest = (self.MAX_BUDGET / TOTAL_SLOTS) * REGIME_SETTINGS.get(current_regime, {}).get('ratio', 1.0)
            already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in hunter_pos_items.values())
            krw_balance = safe_balances.get('KRW', 0.0)

            for ticker in hunter_targets.keys():
                if current_hunter_count >= self.TARGET_SLOTS: break
                if ticker in [p['ticker'] for p in bot_positions.values()]: continue
                curr_p = current_prices.get(ticker)
                if not curr_p: continue
                
                if analyzer.check_hunter_dip_buy(ticker) or analyzer.is_pin_bar(ticker):
                    if krw_balance < base_invest or (already_used + base_invest) > self.MAX_BUDGET:
                        break
                    # (나머지 매수 실행 및 락(Lock) 관리는 worker.execute_buy 내부에 위임, main과 동일 패턴 적용)