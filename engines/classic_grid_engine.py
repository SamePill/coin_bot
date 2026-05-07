import os
import time
from datetime import datetime, timedelta
import pyupbit
import worker
import db_manager
import analyzer
from config import ENABLE_TRADE_NOTI, send_telegram, DB_CONF
from engines.base_engine import BaseEngine

class ClassicGridEngine(BaseEngine):
    def __init__(self, upbit, bot_positions, bot_positions_lock):
        super().__init__(upbit, bot_positions, bot_positions_lock)
        self.MAX_BUDGET = float(os.getenv('CG_MAX_BUDGET', 0))
        self.CG_TOTAL_SLOTS = int(os.getenv('CG_TOTAL_SLOTS', 2))
        self.ENGINE_NAME = 'CLASSIC_GRID'
        self.BASE_SLOT_BUDGET = self.MAX_BUDGET / self.CG_TOTAL_SLOTS if self.CG_TOTAL_SLOTS > 0 else self.MAX_BUDGET
        self.budget_lock_notified = False

    def run(self, now, current_regime, top_grid_candidates, is_panic_state, safe_balances):
        bot_positions = self.bot_positions
        cg_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == self.ENGINE_NAME}
        active_tickers = {}
        
        watch_list = list(set([pos['ticker'] for pos in cg_pos_items.values()] + top_grid_candidates))
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        for key, pos in list(cg_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker)
            if not curr_p: continue
            
            active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
            profit_rate = (curr_p - pos['buy']) / pos['buy']
            
            currency = ticker.split('-')[1]
            if safe_balances.get(currency, 0.0) <= 0:
                print(f"🧹 [유령 장부 청소/CLASSIC_GRID] {ticker} 실제 잔고 없음. DB에서 삭제합니다.")
                db_manager.delete_position(self.ENGINE_NAME, ticker, pos['slot_index'])
                with self.bot_positions_lock: del bot_positions[key]
                continue

            with self.bot_positions_lock:
                if 'last_grid_price' not in pos: pos['last_grid_price'] = curr_p
                if 'allocated_krw' not in pos: pos['allocated_krw'] = self.BASE_SLOT_BUDGET * 0.5 
                if 'peak_price' not in pos: pos['peak_price'] = curr_p
                pos['peak_price'] = max(pos['peak_price'], curr_p)

                last_update = pos.get('created_at', now)
                if (now - last_update) > timedelta(days=7) and profit_rate < 0.01:
                    print(f"⏳ [CG 타임컷] {ticker} 슬롯 회수")
                    if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, 0, engine_name='CLASSIC_GRID'):
                        del bot_positions[key]
                    continue
                
                drop_from_peak = (pos['peak_price'] - curr_p) / pos['peak_price']
                if profit_rate > 0.02 and drop_from_peak > 0.01:
                    print(f"🛑 [CG 익절보존] {ticker} 고점 대비 하락 매도")
                    realized_krw = (curr_p - pos['buy']) * pos['vol']
                    if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw, engine_name='CLASSIC_GRID'):
                        del bot_positions[key]
                    continue

                if ticker not in top_grid_candidates and profit_rate > 0.01:
                    sell_vol = min(pos['vol'], safe_balances.get(ticker.split('-')[1], 0.0))
                    if sell_vol > 0:
                        realized_krw = (curr_p - pos['buy']) * sell_vol
                        if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw, engine_name='CLASSIC_GRID'):
                            print(f"⚖️ [방출] {ticker} 타겟 제외로 인한 교체 방출 ({profit_rate*100:+.2f}%)")
                            del bot_positions[key]
                    continue

                step = pos.get('grid_step')
                if not step:
                    step = analyzer.get_grid_step(ticker) or (curr_p * 0.01)
                    pos['grid_step'] = step
                
                if curr_p >= pos['last_grid_price'] + step:
                    target_sell_vol = max(pos['vol'] * 0.15, 6000 / curr_p)
                    actual_sell_vol = min(target_sell_vol, safe_balances.get(ticker.split('-')[1], 0.0))
                    
                    if actual_sell_vol > 0:
                        remaining_vol = pos['vol'] - actual_sell_vol
                        remaining_krw = remaining_vol * curr_p

                        if remaining_vol > 0 and remaining_krw < 6000:
                            print(f"🧹 [잔돈 청소] {ticker} 남은 잔고({remaining_krw:,.0f}원)가 최소 주문 금액 미달. 전량 익절로 전환합니다!")
                            realized_krw = (curr_p - pos['buy']) * pos['vol']
                            if worker.execute_sell(ticker, pos['vol'], pos['slot_index'], profit_rate*100, realized_krw, engine_name='CLASSIC_GRID'):
                                del bot_positions[key]
                            continue

                        res = self.upbit.sell_market_order(ticker, actual_sell_vol)
                        if res:
                            time.sleep(1)
                            curr_p_after = pyupbit.get_current_price(ticker) or curr_p
                            realized_krw = (curr_p_after - pos['buy']) * actual_sell_vol
                            
                            pos['vol'] -= actual_sell_vol
                            pos['last_grid_price'] = curr_p_after
                            
                            current_slot_value = (pos['vol'] * curr_p_after) + pos['allocated_krw']
                            slot_max_limit = self.BASE_SLOT_BUDGET * 1.05
                            
                            if current_slot_value > slot_max_limit:
                                trade_type = "SELL_REBALANCE"
                                noti_msg = f"⚖️ [🕸️ {self.ENGINE_NAME} 다이어트]\n- 슬롯 비대화 방지 수익금 회수"
                                print(f"🕸️ [그리드 다이어트] {ticker} 초과수익 회수 완료 (+{realized_krw:,.0f}원)")
                            else:
                                pos['allocated_krw'] += (actual_sell_vol * curr_p_after)
                                trade_type = "SELL_GRID_PART"
                                noti_msg = f"[🕸️ {self.ENGINE_NAME} 부분 매도]"
                                print(f"🕸️ [그리드 상단] {ticker} 부분 매도 완료 (+{realized_krw:,.0f}원)")
                            
                            db_manager.log_trade('CLASSIC_GRID', ticker, trade_type, curr_p_after, actual_sell_vol, profit_rate*100, realized_krw)
                            conn = None
                            try:
                                conn = db_manager.get_connection()
                                with conn.cursor() as cur:
                                    sql = """
                                        UPDATE current_positions 
                                        SET volume = volume - %s, 
                                            invested_amount = invested_amount - %s 
                                        WHERE account_id = %s AND engine_name = %s AND ticker = %s AND slot_index = %s
                                    """
                                    cur.execute(sql, (actual_sell_vol, (pos['buy'] * actual_sell_vol), db_manager.ACCOUNT_ID, self.ENGINE_NAME, ticker, pos['slot_index']))
                                conn.commit()
                            except Exception as e:
                                print(f"DB 부분 매도 업데이트 오류: {e}")
                            finally:
                                if conn: conn.close()

                elif curr_p <= pos['last_grid_price'] - step and curr_p > analyzer.get_ema200(ticker):
                    buy_krw = max(pos['allocated_krw'] * 0.15, 6000)
                    krw_balance = safe_balances.get('KRW', 0.0)
                    if krw_balance >= buy_krw * 1.0005 and pos['allocated_krw'] >= buy_krw:
                        success, exec_price, exec_vol = worker.execute_buy(ticker, buy_krw, self.MAX_BUDGET, pos['slot_index'], engine_name='CLASSIC_GRID')
                        if success:
                            safe_balances['KRW'] = safe_balances.get('KRW', 0.0) - (buy_krw * 1.0005)
                            time.sleep(1.5)
                            new_vol = pos['vol'] + exec_vol
                            new_avg = ((pos['buy'] * pos['vol']) + (exec_price * exec_vol)) / new_vol
                            pos['buy'] = new_avg
                            pos['vol'] = new_vol
                            pos['last_grid_price'] = exec_price
                            pos['allocated_krw'] -= buy_krw
                            pos['peak_price'] = exec_price
                            print(f"🕸️ [그리드 하단] {ticker} 부분 매수(물타기) 완료. (새 평단: {new_avg:,.0f}원)")

        # [2] 빈 슬롯 채우기
        total_active_slots = len(active_tickers) 
        remaining_slots = self.CG_TOTAL_SLOTS - total_active_slots
        init_invest_amount = self.BASE_SLOT_BUDGET * 0.5
        already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in cg_pos_items.values())
        
        if remaining_slots > 0 and self.MAX_BUDGET > 0 and current_regime not in ["ICE_AGE"] and not is_panic_state:
            for ticker in top_grid_candidates:
                if remaining_slots <= 0: break
                if ticker in active_tickers: continue 
                krw_balance = safe_balances.get('KRW', 0.0)
                if krw_balance < init_invest_amount * 1.0005: break
                
                if (already_used + init_invest_amount) > self.MAX_BUDGET:
                    if not self.budget_lock_notified:
                        print(f"🛑 [{self.ENGINE_NAME} 예산 잠금] 신규 진입 예산 초과. (한도: {self.MAX_BUDGET:,.0f}원)")
                        self.budget_lock_notified = True
                    break 
                
                self.budget_lock_notified = False
                existing_slots = [p['slot_index'] for p in bot_positions.values() if p['engine'] == self.ENGINE_NAME]
                new_slot_idx = 1
                while new_slot_idx in existing_slots: new_slot_idx += 1
                
                success, exec_price, exec_vol = worker.execute_buy(ticker, init_invest_amount, self.MAX_BUDGET, new_slot_idx, engine_name='CLASSIC_GRID')
                if success:
                    safe_balances['KRW'] = safe_balances.get('KRW', 0.0) - (init_invest_amount * 1.0005)
                    time.sleep(1.5) 
                    key = f"{ticker}_slot_{new_slot_idx}"
                    with self.bot_positions_lock:
                        bot_positions[key] = {
                            'ticker': ticker, 'vol': exec_vol, 'buy': exec_price, 'slot_index': new_slot_idx, 
                            'engine': self.ENGINE_NAME, 'buy_level': 1, 'last_grid_price': exec_price,
                            'grid_step': analyzer.get_grid_step(ticker), 'allocated_krw': init_invest_amount, 
                            'invested_amount': exec_price * exec_vol, 'created_at': now, 'peak_price': exec_price
                        }
                    remaining_slots -= 1
                    active_tickers[ticker] = active_tickers.get(ticker, 0) + 1
                    print(f"🚀 [{self.ENGINE_NAME} 신규 진입] {ticker} 거미줄 전개 완료! (하단 예비비: {init_invest_amount:,.0f}원 확보)")