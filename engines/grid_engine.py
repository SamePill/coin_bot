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
        self.MAX_BUDGET = float(os.getenv('MAX_BUDGET', 0))
        self.GRID_TOTAL_SLOTS = int(os.getenv('GRID_TOTAL_SLOTS', 2))
        self.USE_MULTI_SLOT = os.getenv('USE_MULTI_SLOT', 'True').lower() == 'true'
        self.MAX_SLOTS_PER_COIN = int(os.getenv('MAX_SLOTS_PER_COIN', 2))
        self.UNIT_LIST = [float(x) for x in os.getenv('GRID_UNIT_SIZES', '10000,30000').split(',')]
        self.budget_lock_notified = False

    def get_dynamic_grid_step(self, ticker):
        try:
            df = pyupbit.get_ohlcv(ticker, interval="day", count=7)
            if df is not None and len(df) > 1:
                amplitudes = (df['high'] - df['low']) / df['close'] * 100
                avg_volatility = amplitudes.mean()
                if avg_volatility >= 5.0: return 2.0   
                elif avg_volatility >= 2.0: return 1.0 
                else: return 0.5                       
        except: pass
        return 1.0

    def get_pyramiding_weight(self, buy_level, current_regime):
        if current_regime in ["SUPER_BULL", "NORMAL"]:
            if buy_level <= 1: return 2.0     
            elif buy_level == 2: return 1.0   
            elif buy_level >= 3: return 0.0   
        else:
            if buy_level <= 1: return 1.0     
            elif buy_level == 2: return 2.0   
            elif buy_level == 3: return 4.0   
            elif buy_level == 4: return 6.0   
            elif buy_level >= 5: return 8.0   
        return 1.0

    def run(self, now, current_regime, top_grid_candidates):
        bot_positions = self.bot_positions
        grid_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'GRID'}
        active_tickers = {} 
        
        watch_list = list(set([pos['ticker'] for pos in grid_pos_items.values()] + top_grid_candidates))
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        # [1] 기존 슬롯 관리
        for key, pos in list(grid_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker) 
            if not curr_p: continue
            
            active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
            profit_rate = (curr_p - pos['buy']) / pos['buy']
            
            if 'peak_price' not in pos: pos['peak_price'] = curr_p
            pos['peak_price'] = max(pos['peak_price'], curr_p)

            last_update = pos.get('created_at', datetime.now())
            if datetime.now() - last_update > timedelta(days=7) and profit_rate < 0.01:
                if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, 0):
                    with self.bot_positions_lock: del bot_positions[key]
                    continue

            if ticker not in top_grid_candidates and profit_rate > 0.01:
                realized_krw = (curr_p - pos['buy']) * pos['vol']
                if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw):
                    with self.bot_positions_lock: del bot_positions[key]
                    continue

            # (불타기, 물타기 로직 등 기존 main.py 코드를 들고 와 동일하게 락을 감싸서 적용)
            grid_step_percent = self.get_dynamic_grid_step(ticker)
            current_level = pos.get('buy_level', 1) 
            target_buy_price = pos['buy'] * (1 - (grid_step_percent / 100))
            target_sell_price = pos['buy'] * (1 + (grid_step_percent / 100))
            
            if curr_p <= target_buy_price:
                next_level = current_level + 1
                weight = self.get_pyramiding_weight(next_level, current_regime)
                if weight <= 0: continue
                
                base_unit = self.UNIT_LIST[pos['slot_index']-1] if (pos['slot_index']-1) < len(self.UNIT_LIST) else self.UNIT_LIST[-1]
                invest_amount = base_unit * weight
                # 물타기 실행 (worker) ...
                # 락 적용: with self.bot_positions_lock: self.bot_positions[key]['buy'] = new_avg_price 

            elif curr_p >= target_sell_price:
                realized_krw = (curr_p - pos['buy']) * pos['vol']
                if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw):
                    with self.bot_positions_lock: del bot_positions[key]
                    continue

        # [2] 빈 슬롯 채우기 ... (기존 main.py 로직)