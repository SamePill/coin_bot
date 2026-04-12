import os
import time
import pyupbit
import worker
import db_manager
from engines.base_engine import BaseEngine

class ScalpEngine(BaseEngine):
    def __init__(self, upbit, bot_positions, bot_positions_lock):
        super().__init__(upbit, bot_positions, bot_positions_lock)
        self.MAX_BUDGET = float(os.getenv('MAX_BUDGET', 0))
        self.SCALP_TOTAL_SLOTS = int(os.getenv('SCALP_TOTAL_SLOTS', 2))
        self.SCALP_USE_MULTI_SLOT = os.getenv('SCALP_USE_MULTI_SLOT', 'True').lower() == 'true'
        self.SCALP_MAX_SLOTS_PER_COIN = int(os.getenv('SCALP_MAX_SLOTS_PER_COIN', 2))
        
        scalp_units_str = os.getenv('SCALP_UNIT_SIZES')
        if scalp_units_str:
            self.SCALP_UNIT_LIST = [float(x.strip()) for x in scalp_units_str.split(',')]
        else:
            self.SCALP_UNIT_LIST = [float(x) for x in os.getenv('GRID_UNIT_SIZES', '10000,30000').split(',')]
        self.budget_lock_notified = False

    def run(self, now, current_regime, top_grid_candidates):
        bot_positions = self.bot_positions
        scalp_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'SCALP'}
        active_tickers = {} 
        
        watch_list = list(set([pos['ticker'] for pos in scalp_pos_items.values()] + top_grid_candidates))
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        safe_balances = self.get_safe_balances()

        for key, pos in list(scalp_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker) 
            if not curr_p: continue
            
            active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
            profit_rate = (curr_p - pos['buy']) / pos['buy']

            currency = ticker.split('-')[1]
            actual_balance = safe_balances.get(currency, 0.0)
            sell_vol = min(pos['vol'], actual_balance)

            if profit_rate >= 0.006: 
                if sell_vol <= 0:
                    with self.bot_positions_lock: del bot_positions[key]
                    continue

                realized_krw = (curr_p - pos['buy']) * sell_vol
                if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                    with self.bot_positions_lock: del bot_positions[key]
                continue

            # 2. 짤짤이 물타기 등 ... (기존 main.py 로직 유지)