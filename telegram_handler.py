import threading
import pyupbit
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# 📦 설정 및 DB 모듈 로드
from config import TEL_TOKEN, UPBIT_ACCESS, UPBIT_SECRET # 💡 UPBIT 키 추가
import db_manager

# 메인 프로세스의 실시간 상태를 참조할 전역 변수
_bot_positions = {}
_bot_positions_lock = None
_get_seed_money = None

# 💡 [추가] 일시 정지된 엔진 목록을 저장하는 변수
_paused_engines = set()

# 💡 유효한 엔진 목록 (글로벌 변수처럼 활용)
VALID_ENGINES = ["CORE", "HUNTER", "GRID", "SCALP", "CLASSIC_GRID"]

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 슬롯 운영 상태 및 상세 손익 보고 (/status) - 엔진별 요약판"""
    # 1. 오늘 누적 실현 수익 가져오기 (trade_logs 테이블)
    rows = db_manager.get_today_performance(0)
    total_today_profit = sum(row['total_profit'] for row in rows) if rows else 0
    
    msg = f"📊 [{db_manager.ACCOUNT_ID} 실시간 운용 현황]\n"
    msg += f"💰 금일 누적 수익: {total_today_profit:+,.0f}원\n"
    msg += "──────────────────\n"

    # DB 장부에서 모든 엔진의 보유 코인 조회
    import pymysql
    from config import DB_CONF
    
    active_positions = []
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
        with conn.cursor() as cur:
            sql = "SELECT * FROM current_positions WHERE account_id = %s AND volume > 0"
            cur.execute(sql, (db_manager.ACCOUNT_ID,))
            active_positions = cur.fetchall()
        conn.close()
    except Exception as e:
        msg += f"❌ DB 장부 조회 오류: {e}"
        await update.message.reply_text(msg)
        return

    if not active_positions:
        msg += "- 현재 운용 중인(매수한) 엔진이 없습니다."
    else:
        # 💡 [속도 최적화] 보유 중인 모든 코인의 현재가를 한 번에 조회
        tickers = list(set([p['ticker'] for p in active_positions]))
        current_prices = pyupbit.get_current_price(tickers) if tickers else {}
        if not isinstance(current_prices, dict): 
            # 종목이 1개일 때 float으로 반환되는 업비트 API 예외 처리
            current_prices = {tickers[0]: current_prices} if isinstance(current_prices, (int, float)) else {}

        # 엔진별 통계를 모을 딕셔너리
        engine_stats = {}
        total_unrealized_profit = 0
        total_invested_all = 0

        # 데이터 그룹화 (엔진별로 합산)
        for p in active_positions:
            engine = p['engine_name']
            ticker = p['ticker']
            buy_price = float(p['buy_price'])
            vol = float(p['volume'])
            
            invested = float(p.get('invested_amount', buy_price * vol))
            curr_p = current_prices.get(ticker) or buy_price
            
            profit_amt = (curr_p - buy_price) * vol 
            
            if engine not in engine_stats:
                engine_stats[engine] = {'count': 0, 'invested': 0.0, 'profit': 0.0}
            
            engine_stats[engine]['count'] += 1
            engine_stats[engine]['invested'] += invested
            engine_stats[engine]['profit'] += profit_amt
            
            total_unrealized_profit += profit_amt
            total_invested_all += invested

        # 그룹화된 통계를 메시지로 출력
        for engine, stats in engine_stats.items():
            ic = "🏹" if engine == 'HUNTER' else "🕸️" if engine == 'CLASSIC_GRID' else "🛡️" if engine == 'CORE' else "⚡" if engine == 'SCALP' else "🎰" if engine == 'GRID' else "🤖"
            
            e_invested = stats['invested']
            e_profit = stats['profit']
            e_count = stats['count']
            e_rate = (e_profit / e_invested) * 100 if e_invested > 0 else 0
            
            msg += f"{ic} [{engine}] (운용중: {e_count}종목)\n"
            msg += f"  - 투자금: {e_invested:,.0f}원\n"
            msg += f"  - 평가손익: {e_profit:+,.0f}원 ({e_rate:+.2f}%)\n\n"
        
        # 전체 합계 출력
        msg += "──────────────────\n"
        total_rate = (total_unrealized_profit / total_invested_all) * 100 if total_invested_all > 0 else 0
        msg += f"💵 총 투자금: {total_invested_all:,.0f}원\n"
        msg += f"📈 총 평가 손익: {total_unrealized_profit:+,.0f}원 ({total_rate:+.2f}%)"
            
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


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """지정된 엔진의 모든 포지션을 강제 청산하고 초기화 (/reset 엔진명)"""
    if not context.args:
        await update.message.reply_text("⚠️ 엔진 이름을 입력해주세요.\n👉 사용법: /reset SCALP")
        return

    target_engine = context.args[0].upper()
    
    # 입력된 엔진 이름 유효성 검사
    if target_engine not in VALID_ENGINES:
        await update.message.reply_text(
            f"❌ '{target_engine}' 은(는) 존재하지 않는 엔진입니다.\n"
            f"👉 사용 가능한 엔진: {', '.join(VALID_ENGINES)}"
        )
        return

    await update.message.reply_text(f"⏳ [{target_engine}] 엔진 강제 청산을 시작합니다. (DB 장부 조회 중...)")

    upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)
    reset_count = 0
    total_realized = 0
    dust_cleaned = 0  # 잔돈 청소가 발생한 횟수 기록용

    # 💡 내 메모리가 아닌 공용 DB 장부(current_positions)에서 해당 엔진의 코인을 조회
    import pymysql
    from config import DB_CONF
    import time
    
    try:
        conn = pymysql.connect(**DB_CONF, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM current_positions WHERE engine_name = %s", (target_engine,))
            target_positions = cur.fetchall()
        conn.close()
    except Exception as e:
        await update.message.reply_text(f"❌ DB 조회 중 오류 발생: {e}")
        return

    # DB에서 찾은 타겟 엔진의 코인들을 하나씩 강제 매도 처리
    for p in target_positions:
        ticker = p['ticker']
        db_vol = float(p['volume'])
        buy_price = float(p['buy_price'])
        slot_index = p.get('slot_index', 1)
        
        # 실제 업비트 지갑 잔고 확인
        coin = ticker.split('-')[1]
        actual_vol = upbit.get_balance(coin)
        
        # 💡 [핵심 방어] 기본적으로 봇이 DB에 기록한 수량과 실제 수량 중 작은 것만 팔도록 설정 (수동 매수분 보호)
        sell_vol = min(db_vol, actual_vol) if actual_vol else 0

        if sell_vol > 0:
            # 💡 [추가 기능] 현재가를 조회하여 매도 후 남는 잔액 가치(KRW) 평가
            curr_p = pyupbit.get_current_price(ticker)
            if curr_p:
                remaining_vol = actual_vol - sell_vol
                remaining_krw = remaining_vol * curr_p
                
                # 매도하고 남은 코인의 가치가 6,000원 미만인 더스트(먼지)라면? -> 전량 매도로 싹쓸이!
                if remaining_vol > 0 and remaining_krw < 6000:
                    sell_vol = actual_vol  # 매도 목표량을 지갑에 있는 전체 수량으로 덮어씀
                    dust_cleaned += 1

            # 실제 업비트 매도 주문 전송
            res = upbit.sell_market_order(ticker, sell_vol)
            if res:
                time.sleep(1) # API 레이트리밋 방지
                curr_p_after = pyupbit.get_current_price(ticker) or curr_p or buy_price
                realized_krw = (curr_p_after - buy_price) * sell_vol
                profit_rate = ((curr_p_after - buy_price) / buy_price) * 100
                
                # 💡 [좀비 방지] 수량을 0으로 두지 않고 장부에서 레코드 자체를 완전 삭제!
                db_manager.delete_position(target_engine, ticker, slot_index)
                db_manager.log_trade(target_engine, ticker, "SELL_FORCE_RESET", curr_p_after, sell_vol, profit_rate, realized_krw)

                reset_count += 1
                total_realized += realized_krw

        else:
            # 💡 [버그 수정] 실제 지갑 잔고가 0이더라도 DB 장부의 유령 데이터를 지워 슬롯 점유를 해제합니다.
            db_manager.delete_position(target_engine, ticker, slot_index)
            reset_count += 1

    # 만약 타겟 엔진이 텔레그램을 돌리고 있는 본인(예: GRID)이라면, 자신의 램(RAM)도 비워줌
    if _bot_positions_lock:
        with _bot_positions_lock:
            keys_to_delete = [k for k, v in list(_bot_positions.items()) if v['engine'] == target_engine]
            for k in keys_to_delete:
                if k in _bot_positions:
                    del _bot_positions[k]

    if reset_count > 0:
        msg = (
            f"🧹 [{target_engine}] 초기화 완료!\n"
            f"- 청산된 종목 수: {reset_count}개\n"
            f"- 총 실현 손익: {total_realized:+,.0f}원\n"
        )
        if dust_cleaned > 0:
            msg += f"✨ (잔돈 청소 완료: {dust_cleaned}개 종목의 6천원 미만 더스트도 함께 정리되었습니다.)\n"
            
        msg += f"💡 팁: 청산 후 엔진이 다시 코인을 사지 않게 하려면 /pause {target_engine} 도 잊지 마세요!"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(f"⚠️ [{target_engine}] 엔진 장부에 청산할 코인이 없습니다.")


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """특정 엔진의 자동 매매 루프를 일시 정지 (/pause 엔진명)"""
    if not context.args:
        await update.message.reply_text("⚠️ 엔진 이름을 입력해주세요.\n👉 사용법: /pause SCALP")
        return
        
    target_engine = context.args[0].upper()
    
    # 💡 [핵심 추가] 입력된 엔진이 유효한지 검사
    if target_engine not in VALID_ENGINES:
        await update.message.reply_text(
            f"❌ '{target_engine}' 은(는) 존재하지 않는 엔진입니다.\n"
            f"👉 사용 가능한 엔진: {', '.join(VALID_ENGINES)}"
        )
        return
    
    # DB에 정지 상태 기록 (모든 컨테이너 공유)
    db_manager.set_engine_pause_state(target_engine, True)
    
    await update.message.reply_text(
        f"⏸️ [{target_engine}] 엔진 루프가 일시 정지되었습니다.\n"
        f"🚨 주의: 루프가 멈추므로 기존 보유 코인의 익절/손절 감시도 함께 정지됩니다."
    )

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """특정 엔진의 자동 매매 루프를 재개 (/resume 엔진명)"""
    if not context.args:
        await update.message.reply_text("⚠️ 엔진 이름을 입력해주세요.\n👉 사용법: /resume SCALP")
        return
        
    target_engine = context.args[0].upper()
    
    # 💡 [핵심 추가] 입력된 엔진이 유효한지 검사
    if target_engine not in VALID_ENGINES:
        await update.message.reply_text(
            f"❌ '{target_engine}' 은(는) 존재하지 않는 엔진입니다.\n"
            f"👉 사용 가능한 엔진: {', '.join(VALID_ENGINES)}"
        )
        return
    
    # DB에서 정지 상태 해제
    db_manager.set_engine_pause_state(target_engine, False)
    
    await update.message.reply_text(f"▶️ [{target_engine}] 엔진 루프가 다시 가동을 시작합니다!")

def _run_bot():
    """실제 텔레그램 폴링 실행"""
    app = ApplicationBuilder().token(TEL_TOKEN).build()
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("report", report_command))
    
    # 💡 새로 추가한 커맨드 핸들러 등록
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("help", help_command))

    # 💡 [추가] 정지 및 재가동 커맨드 핸들러 등록
    app.add_handler(CommandHandler("pause", pause_command))
    app.add_handler(CommandHandler("resume", resume_command))

    print("📲 텔레그램 봇 수신 대기 중...")
    app.run_polling(stop_signals=None)    

def start_telegram_listener(positions_ref, lock_ref, seed_getter):
    """
    main.py에서 호출할 엔트리 포인트.
    실시간 메모리 참조를 전달받고 백그라운드 스레드에서 텔레그램 봇을 가동합니다.
    """
    global _bot_positions, _bot_positions_lock, _get_seed_money
    _bot_positions = positions_ref
    _bot_positions_lock = lock_ref
    _get_seed_money = seed_getter
    
    # 데몬 스레드로 가동 (메인 봇이 꺼지면 같이 꺼짐)
    threading.Thread(target=_run_bot, daemon=True).start()
    print("🤖 [텔레그램 커맨드 센터] 백그라운드 리스너 가동 완료.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """봇 명령어 사용법 안내 (/help)"""
    msg = """🤖 [Aegis 봇 텔레그램 명령어 안내]

🔹 /status
현재 봇이 운용 중인 모든 슬롯의 상태와 수익률, 잔여 예산을 실시간으로 보여줍니다.

🔹 /report [숫자]
일일 매매 결산(실현 수익) 보고서를 출력합니다.
- /report : 오늘 수익
- /report 1 : 어제 수익
- /report 2 : 그제 수익

🔹 /reset [엔진명]
지정한 엔진이 쥐고 있는 모든 코인을 즉시 시장가로 강제 매도하고 DB와 슬롯을 초기화합니다.
- /reset SCALP
- /reset CLASSIC_GRID
- /reset CORE
- /reset GRID
- /reset HUNTER

🔹 /pause [엔진명] : 해당 엔진의 매매 루프 일시 정지 (관망)
🔹 /resume [엔진명] : 정지된 엔진 매매 루프 재가동

🔹 /help
현재 보고 계신 도움말을 출력합니다.
"""
    await update.message.reply_text(msg)