import os
import time
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
        self.MAX_BUDGET = float(os.getenv('CORE_MAX_BUDGET', os.getenv('MAX_BUDGET', 0)))
        self.TARGET_SLOTS = int(os.getenv('TARGET_SLOTS', 3))
        self.budget_lock_notified = False

    def run(self, now, current_regime, core_targets, is_panic_state, safe_balances):
        bot_positions = self.bot_positions
        core_pos_items = {k: v for k, v in bot_positions.items() if v['engine'] == 'CORE'}
        watch_list = list(set([p['ticker'] for p in core_pos_items.values()] + list(core_targets.keys())))
        
        current_prices = pyupbit.get_current_price(watch_list) if watch_list else {}
        if not isinstance(current_prices, dict): current_prices = {}

        # [1] 기존 포지션 관리 (매도)
        for key, pos in list(core_pos_items.items()):
            ticker = pos['ticker']
            curr_p = current_prices.get(ticker)
            if not curr_p: continue
            
            with self.bot_positions_lock:
                if 'peak_price' not in pos: pos['peak_price'] = curr_p
                pos['peak_price'] = max(pos['peak_price'], curr_p)
                profit_rate = (curr_p - pos['buy']) / pos['buy']
                
                time_elapsed_hours = (now - pos.get('created_at', now)).total_seconds() / 3600
                
                currency = ticker.split('-')[1]
                sell_vol = min(pos['vol'], safe_balances.get(currency, 0.0))
                if sell_vol <= 0:
                    print(f"🧹 [유령 장부 청소/CORE] {ticker} 실제 잔고 없음. DB에서 삭제합니다.")
                    db_manager.delete_position('CORE', ticker, pos['slot_index'])
                    with self.bot_positions_lock:
                        if key in bot_positions: del bot_positions[key]
                    continue

                # 💡 [박스권 탈출 로직 1] Time Decay: 보유 시간이 길어질수록 익절 목표가를 하향
                dynamic_target = 0.05
                if time_elapsed_hours >= 24: dynamic_target = 0.02
                elif time_elapsed_hours >= 12: dynamic_target = 0.035

                # 💡 [V17.29] 스케일아웃(Scale-Out): 목표가 도달 시 절반을 팔아 수익을 확정하고, 나머지는 샹들리에에 맡김
                if profit_rate >= dynamic_target and not pos.get('is_scaled_out', False):
                    half_vol = sell_vol / 2.0
                    remaining_krw = (sell_vol - half_vol) * curr_p
                    
                    if remaining_krw < 6000: # 최소 거래 대금 6,000원 방어
                        realized_krw = (curr_p - pos['buy']) * sell_vol
                        print(f"📈 [CORE 수익 실현] {ticker} 목표가({dynamic_target*100:.1f}%) 도달! 남은 금액이 적어 전량 매도")
                        if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw, engine_name='CORE'):
                            if key in bot_positions: del bot_positions[key]
                    else:
                        realized_krw = (curr_p - pos['buy']) * half_vol
                        print(f"⚖️ [CORE 스케일아웃] {ticker} 목표가({dynamic_target*100:.1f}%) 도달! 50% 수익 실현 및 추세 홀딩")
                        if worker.execute_sell(ticker, half_vol, pos['slot_index'], profit_rate*100, realized_krw, engine_name='CORE', is_scale_out=True):
                            with self.bot_positions_lock:
                                bot_positions[key]['vol'] -= half_vol
                                bot_positions[key]['invested_amount'] -= (pos['buy'] * half_vol)
                                bot_positions[key]['is_scaled_out'] = True
                    continue
                    
                # 💡 [박스권 탈출 로직 2] 가짜 돌파 휩쏘 타임컷: 6시간 경과 시 약수익/보합이면 강제 정리
                if time_elapsed_hours >= 6 and profit_rate < 0.005:
                    realized_krw = (curr_p - pos['buy']) * sell_vol
                    print(f"🐌 [CORE 휩쏘 타임컷] {ticker} 상승 동력 상실(6H 지연). 자금 회전을 위해 정리 ({profit_rate*100:+.2f}%)")
                    if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw, engine_name='CORE'):
                        if key in bot_positions: del bot_positions[key]
                    continue

                chandelier_exit_price = analyzer.get_chandelier_exit(ticker, pos['peak_price'], current_regime)
                if curr_p < chandelier_exit_price:
                    realized_krw = (curr_p - pos['buy']) * sell_vol
                    print(f"🛑 [CORE 샹들리에 청산] {ticker} 추세 꺾임 감지. ({profit_rate*100:+.2f}%)")
                    if worker.execute_sell(ticker, sell_vol, pos['slot_index'], profit_rate*100, realized_krw, engine_name='CORE'):
                        if key in bot_positions: del bot_positions[key]
                    continue

        # [2] 신규 진입 (매수)
        current_core_count = len([p for p in bot_positions.values() if p['engine'] == 'CORE'])
        if current_core_count < self.TARGET_SLOTS and self.MAX_BUDGET > 0 and current_regime not in ["ICE_AGE"] and not is_panic_state:
            base_invest = (self.MAX_BUDGET / self.TARGET_SLOTS) if self.TARGET_SLOTS > 0 else self.MAX_BUDGET
            base_invest *= REGIME_SETTINGS.get(current_regime, {}).get('ratio', 1.0)
            already_used = sum(p.get('invested_amount', p['buy'] * p['vol']) for p in core_pos_items.values())

            for ticker, t_info in core_targets.items():
                if current_core_count >= self.TARGET_SLOTS: break
                if ticker in [p['ticker'] for p in bot_positions.values()]: continue
                curr_p = current_prices.get(ticker)
                if not curr_p: continue
                
                if curr_p >= (t_info['open'] + t_info['range']*t_info['k']):
                    if analyzer.check_keltner_breakout(ticker) and analyzer.get_adx(ticker) > 25 and analyzer.check_volume_spike(ticker):
                        krw_balance = safe_balances.get('KRW', 0.0)
                        max_affordable = min(self.MAX_BUDGET - already_used, krw_balance / 1.0005)
                        if max_affordable < 5500:
                            if not self.budget_lock_notified:
                                print(f"🛑 [CORE 예산/잔고 잠금] {ticker} 보류 (가용 예산: {max_affordable:,.0f}원)")
                                self.budget_lock_notified = True
                            break
                            
                        self.budget_lock_notified = False
                        new_slot_idx = 1
                        while new_slot_idx in [p['slot_index'] for p in bot_positions.values() if p['ticker'] == ticker]: new_slot_idx += 1
                        
                        print(f"🚀 [CORE 신규 진입] {ticker} 강력한 추세 돌파 포착!")
                        success, exec_price, exec_vol = worker.execute_buy(ticker, base_invest, self.MAX_BUDGET, new_slot_idx, engine_name='CORE')
                        if success:
                            safe_balances['KRW'] = safe_balances.get('KRW', 0.0) - (base_invest * 1.0005)
                            key = f"{ticker}_slot_{new_slot_idx}"
                            with self.bot_positions_lock:
                                bot_positions[key] = {
                                    'ticker': ticker, 'vol': exec_vol, 'buy': exec_price, 
                                    'peak_price': exec_price, 'slot_index': new_slot_idx, 
                                    'engine': 'CORE', 'buy_level': 1, 'created_at': now,
                                    'invested_amount': exec_price * exec_vol
                                }
                            try: db_manager.update_position_state(key, exec_price, exec_vol, 1, engine_name='CORE')
                            except AttributeError: pass
                            current_core_count += 1
                            already_used += (exec_price * exec_vol)
                            time.sleep(1.5)