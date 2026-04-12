import os
import time
import pyupbit
import pymysql
import worker
import db_manager
import analyzer
from config import ENABLE_TRADE_NOTI, send_telegram, DB_CONF
from engines.base_engine import BaseEngine

class ClassicGridEngine(BaseEngine):
    def __init__(self, upbit, bot_positions, bot_positions_lock):
        super().__init__(upbit, bot_positions, bot_positions_lock)
        self.MAX_BUDGET = float(os.getenv('MAX_BUDGET', 0))
        self.CG_TOTAL_SLOTS = int(os.getenv('CG_TOTAL_SLOTS', 2))
        self.ENGINE_NAME = 'CLASSIC_GRID'
        self.BASE_SLOT_BUDGET = self.MAX_BUDGET / self.CG_TOTAL_SLOTS if self.CG_TOTAL_SLOTS > 0 else self.MAX_BUDGET
        self.budget_lock_notified = False

    def run(self, now, current_regime, top_grid_candidates):
        bot_positions = self.bot_positions
        cg_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == self.ENGINE_NAME}
        active_tickers = {}
        
        watch_list = list(set([pos['ticker'] for pos in cg_pos_items.values()] + top_grid_candidates))
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        safe_balances = self.get_safe_balances()
        krw_balance = safe_balances.get('KRW', 0.0)

        for key, pos in list(cg_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker)
            if not curr_p: continue
            
            active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
            profit_rate = (curr_p - pos['buy']) / pos['buy']
            
            if 'last_grid_price' not in pos: pos['last_grid_price'] = curr_p
            if 'allocated_krw' not in pos: pos['allocated_krw'] = self.BASE_SLOT_BUDGET * 0.5 

            if ticker not in top_grid_candidates and profit_rate > 0.01:
                sell_vol = min(pos['vol'], safe_balances.get(ticker.split('-')[1], 0.0))
                if sell_vol > 0:
                    realized_krw = (curr_p - pos['buy']) * sell_vol
                    if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw):
                        with self.bot_positions_lock: del bot_positions[key]
                continue
            
            # (이하 부분 매도/매수, 다이어트 로직 등 기존 main.py 복원)