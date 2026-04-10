import threading
import pyupbit
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# 📦 설정 및 DB 모듈 로드
from config import TEL_TOKEN
import db_manager

# 메인 프로세스의 실시간 상태를 참조할 전역 변수
_bot_positions = {}
_get_seed_money = None

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 슬롯 운영 상태 및 상세 손익 보고 (/status)"""
    # 1. 오늘 누적 실현 수익 가져오기
    rows = db_manager.get_today_performance(0)
    total_today_profit = sum(row['total_profit'] for row in rows) if rows else 0
    
    msg = f"📊 [{db_manager.ACCOUNT_ID} 실시간 운용 현황]\n"
    msg += f"💰 금일 누적 수익: {total_today_profit:+,.0f}원\n"
    msg += "──────────────────\n"

    if not _bot_positions:
        msg += "- 현재 보유 중인 종목이 없습니다."
    else:
        # 💡 전체 평가 손익 합계를 위한 변수
        total_unrealized_profit = 0

        for key, p in list(_bot_positions.items()):
            ic = "🏹" if p['engine'] == 'HUNTER' else "🕸️" if p['engine'] == 'CLASSIC_GRID' else "🛡️" if p['engine'] == 'CORE' else "⚡" if p['engine'] == 'SCALP' else "🎰" if p['engine'] == 'GRID' else "🤖"
            #ic = "🛡️" if p['engine']=='CORE' else ("🏹" if p['engine']=='HUNTER' else "🕸️")
            
            # 💡 p['ticker']를 사용하여 정확한 현재가 조회 (버그 수정)
            curr_p = pyupbit.get_current_price(p['ticker']) or p['buy']
            
            # 수익률 및 평가 손익(원화) 계산
            rate = ((curr_p - p['buy']) / p['buy']) * 100
            profit_amt = (curr_p - p['buy']) * p['vol'] # 현재가 - 평단가 * 보유수량
            total_unrealized_profit += profit_amt
            
            # 투자 원금 (DB에서 가져온 값 사용)
            invested = p.get('invested_amount', p['buy'] * p['vol'])
            
            msg += f"{ic} {p['ticker']} ({p['buy_level']}차)\n"
            msg += f"  - 수익률: {rate:+.2f}% ({profit_amt:+,.0f}원)\n"
            msg += f"  - 투자금: {invested:,.0f}원 (슬롯 {p['slot_index']})\n"
            msg += "\n"
        
        msg += "──────────────────\n"
        msg += f"📈 총 평가 손익: {total_unrealized_profit:+,.0f}원"
            
    await update.message.reply_text(msg)

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """어제의 수익 현황 보고 (/report)"""
    rows = db_manager.get_today_performance(1)
    seed_money = _get_seed_money() if _get_seed_money else 0
    
    msg = "💰 [어제의 매매 결산 보고서]\n\n"
    total_krw = 0
    
    if not rows:
        msg += "- 어제 완료된 매매 내역이 없습니다."
    else:
        for row in rows:
            ic = "🏹" if row['engine'] == 'HUNTER' else "🕸️" if row['engine'] == 'CLASSIC_GRID' else "🛡️" if row['engine'] == 'CORE' else "⚡" if row['engine'] == 'SCALP' else "🎰" if row['engine'] == 'GRID' else "🤖"
            #ic = "🛡️" if row['engine']=='CORE' else ("🏹" if row['engine']=='HUNTER' else "🕸️")
            msg += f"{ic} {row['engine']}: {row['total_profit']:+,.0f}원 ({row['avg_rate']:+.2f}%)\n"
            total_krw += row['total_profit']
        
        total_rate = (total_krw / seed_money * 100) if seed_money > 0 else 0
        msg += f"\n──────────────\n"
        msg += f"💵 총 합계: {total_krw:+,.0f}원 ({total_rate:+.2f}%)\n"
    
    await update.message.reply_text(msg)

def _run_bot():
    """실제 텔레그램 폴링 실행"""
    app = ApplicationBuilder().token(TEL_TOKEN).build()
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("report", report_command))
    app.run_polling(stop_signals=None)    

def start_telegram_listener(positions_ref, seed_getter):
    """
    main.py에서 호출할 엔트리 포인트.
    실시간 메모리 참조를 전달받고 백그라운드 스레드에서 텔레그램 봇을 가동합니다.
    """
    global _bot_positions, _get_seed_money
    _bot_positions = positions_ref
    _get_seed_money = seed_getter
    
    # 데몬 스레드로 가동 (메인 봇이 꺼지면 같이 꺼짐)
    threading.Thread(target=_run_bot, daemon=True).start()
    print("🤖 [텔레그램 커맨드 센터] 백그라운드 리스너 가동 완료.")

