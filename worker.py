import os
import time
import pyupbit
import db_manager
from config import UPBIT_ACCESS, UPBIT_SECRET, send_telegram, ENABLE_TRADE_NOTI

# --- [도커 환경 변수 로드] ---
ENGINE_TYPE = os.getenv('ENGINE_TYPE', 'CORE').upper()
MAX_BUDGET = float(os.getenv('MAX_BUDGET', 0))

# 업비트 객체 초기화
upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)

def execute_buy(ticker, amount, slot_index=1):
    """
    💡 예산 한도를 체크한 후 실제 매수를 집행하고 슬롯별로 장부에 기록합니다.
    - slot_index: 다중 슬롯 운영 시 식별 번호 (기본값 1)
    """
    try:
        # 1. DB 장부에서 이 엔진(CORE/HUNTER/GRID)이 현재 점유 중인 총 자산 확인
        already_used = db_manager.get_engine_invested_total(ENGINE_TYPE)
        
        # 2. 이번 매수 금액을 합쳤을 때 할당된 MAX_BUDGET을 초과하는지 검사
        if already_used + amount > MAX_BUDGET:
            print(f"⚠️ [{ENGINE_TYPE}] 예산 한도 초과! (현재 사용: {already_used:,.0f} / 한도: {MAX_BUDGET:,.0f})")
            print("💤 5분간 대기 후 다시 확인합니다...")
            time.sleep(300)  # ⬅️ 300초(5분) 동안 루프를 멈춤
            return False, 0, 0  # 💡 수정: 실패 시 단가, 수량 0 반환

        # 3. 업비트 실제 시장가 매수 주문
        res = upbit.buy_market_order(ticker, amount)
        if res:
            # 체결 후 잔고 반영을 위한 짧은 대기
            time.sleep(1) 
            curr_p = pyupbit.get_current_price(ticker)
            
            # 수량 계산 (수수료 0.05% 반영)
            vol = (amount * 0.9995) / curr_p if curr_p else 0
            
            if vol > 0:
                # 4. 현장 장부(current_positions)에 슬롯 번호와 함께 기록
                db_manager.update_position(ENGINE_TYPE, ticker, curr_p, vol, 'BUY', slot_index)
                
                # 5. 영구 로그(trade_logs) 기록
                db_manager.log_trade(ticker, "BUY", curr_p, vol)
                
                print(f"✅ [{ENGINE_TYPE}] {ticker} 슬롯 {slot_index} 매수 성공: {amount:,.0f}원")
                # 💡 [추가] 매수 완료 알림 발송
                if ENABLE_TRADE_NOTI:
                    symbol = "🏹" if ENGINE_TYPE == 'HUNTER' else "🕸️" if ENGINE_TYPE == 'CLASSIC_GRID' else "🛡️" if ENGINE_TYPE == 'CORE' else "⚡" if ENGINE_TYPE == 'SCALP' else "🎰" if ENGINE_TYPE == 'GRID' else "🤖"
                    send_telegram(
                        f"✅ [{symbol}{ENGINE_TYPE} 매수 완료]\n"
                        f"- 종목: {ticker}\n"
                        f"- 단가: {curr_p:,.2f}원\n"
                        f"- 금액: {amount:,.0f}원\n"
                        f"- 슬롯: {slot_index}번"
                    )
                return True, curr_p, vol  # 💡 수정: 매수 성공 여부와 함께 단가, 수량 반환
            
    except Exception as e:
        print(f"❌ [{ENGINE_TYPE}] 매수 실행 오류 ({ticker}): {e}")
    return False, 0, 0  # 💡 수정: 실패 시 단가, 수량 0 반환

def execute_sell(ticker, volume, slot_index=1, profit_rate=0.0, realized_profit=0.0):
    """
    💡 실제 매도 후 해당 슬롯의 포지션을 장부에서 제거합니다.
    - slot_index: 매도하려는 물량이 속한 슬롯 번호
    """
    try:

        coin = ticker.split('-')[1]
        actual_total_vol = upbit.get_balance(coin)
        curr_p = pyupbit.get_current_price(ticker)
        
        if actual_total_vol > 0 and curr_p:
            remaining_vol = actual_total_vol - volume
            remaining_krw = remaining_vol * curr_p
            
            # 방어 1: DB 장부의 수량이 실제 지갑 수량보다 많을 경우 (수수료 오차 에러 방지)
            if volume > actual_total_vol:
                volume = actual_total_vol
                
            # 방어 2: 매도 후 남는 쪼가리 금액이 6,000원 미만일 경우 전량 매도로 스위칭
            elif remaining_vol > 0 and remaining_krw < 6000:
                print(f"🧹 [{ENGINE_TYPE} 잔돈 청소] {ticker} 남은 금액({remaining_krw:,.0f}원) 최소 주문 미달. 100% 전량 매도합니다!")
                volume = actual_total_vol

        # 1. 업비트 실제 시장가 매도 주문
        res = upbit.sell_market_order(ticker, volume)
        if res and 'uuid' in res:
            # 체결 대기 및 현재가 조회
            time.sleep(1)
            curr_p = pyupbit.get_current_price(ticker)
            
            # 2. 현장 장부(current_positions)에서 해당 슬롯 데이터 삭제
            db_manager.update_position(ENGINE_TYPE, ticker, 0, 0, 'SELL', slot_index)
            
            # 3. 영구 로그(trade_logs)에 실현 수익 기록
            db_manager.log_trade(ticker, "SELL", curr_p, volume, profit_rate, realized_profit)
            
            print(f"✅ [{ENGINE_TYPE}] {ticker} 슬롯 {slot_index} 매도 완료 (수익률: {profit_rate:+.2f}%)")
            # 💡 [추가] 매도 완료 알림 발송 (수익금 및 수익률 포함)
            if ENABLE_TRADE_NOTI:
                icon = "📈" if realized_profit > 0 else "📉"
                # p['engine'] 또는 ENGINE_TYPE 변수를 사용한다고 가정
                symbol = "🏹" if ENGINE_TYPE == 'HUNTER' else "🕸️" if ENGINE_TYPE == 'CLASSIC_GRID' else "🛡️" if ENGINE_TYPE == 'CORE' else "⚡" if ENGINE_TYPE == 'SCALP' else "🎰" if ENGINE_TYPE == 'GRID' else "🤖"
                send_telegram(
                    f"{icon} [{symbol}{ENGINE_TYPE} 매도 완료]\n"
                    f"- 종목: {ticker}\n"
                    f"- 실현 손익: {realized_profit:+,.0f}원\n"
                    f"- 수익률: {profit_rate:+.2f}%\n"
                    f"- 단가: {curr_p:,.2f}원"
                )
            return True
            
    except Exception as e:
        print(f"❌ [{ENGINE_TYPE}] 매도 실행 오류 ({ticker}): {e}")
    return False

def get_current_invested_by_slot(ticker, slot_index):
    """💡 특정 슬롯의 매수 평단가 및 수량을 조회합니다. (그리드 대응용)"""
    # 필요 시 db_manager에서 특정 슬롯의 정보만 가져오는 함수를 호출하여 리턴
    pass