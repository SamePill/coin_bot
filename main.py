import os
import time
import threading
import traceback  
from datetime import datetime, timedelta
import json
import pickle
import pandas as pd 
import pyupbit
from dotenv import load_dotenv

# 💡 [전역 타임스탬프 패치] 모든 print 출력 메시지 앞에 현재 시간을 자동으로 붙여줍니다.
import builtins
_original_print = builtins.print
def _timestamped_print(*args, **kwargs):
    _original_print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]", *args, **kwargs)
builtins.print = _timestamped_print

# --- [1. 환경 변수 및 멀티 슬롯 설정 로드] ---
load_dotenv()
# ENGINE_TYPE = os.getenv('ENGINE_TYPE', 'CORE').upper() # 💡 [제거] 통합 엔진 모드에서는 불필요
TARGET_SLOTS = int(os.getenv('TARGET_SLOTS', 3)) # CORE, HUNTER용 고정 슬롯

# 그리드 전용 확장 설정
GRID_TOTAL_SLOTS = int(os.getenv('GRID_TOTAL_SLOTS', 2))  # 예: 8 (고정1 + 유동7)
USE_MULTI_SLOT = os.getenv('USE_MULTI_SLOT', 'True').lower() == 'true'
MAX_SLOTS_PER_COIN = int(os.getenv('MAX_SLOTS_PER_COIN', 2))
# 투자 단위 리스트 (A, B, C... 순서대로 적용)
UNIT_LIST = [float(x) for x in os.getenv('GRID_UNIT_SIZES', '10000,30000').split(',')]

SCALP_TOTAL_SLOTS = int(os.getenv('SCALP_TOTAL_SLOTS', 2))
SCALP_USE_MULTI_SLOT = os.getenv('SCALP_USE_MULTI_SLOT', 'True').lower() == 'true'
SCALP_MAX_SLOTS_PER_COIN = int(os.getenv('SCALP_MAX_SLOTS_PER_COIN', 2))

CG_TOTAL_SLOTS = int(os.getenv('CG_TOTAL_SLOTS', 2))
# 💡 [버그 수정] 잘못 참조된 SCALP 변수명을 CG 변수명으로 수정 및 1코인 1슬롯 원칙에 맞게 기본값 변경
CG_USE_MULTI_SLOT = os.getenv('CG_USE_MULTI_SLOT', 'False').lower() == 'true'
CG_MAX_SLOTS_PER_COIN = int(os.getenv('CG_MAX_SLOTS_PER_COIN', 1))

# 💡 복리 재투자(Auto-Compound) 설정
AUTO_COMPOUND = os.getenv('AUTO_COMPOUND', 'False').lower() == 'true'
COMPOUND_RATE = float(os.getenv('COMPOUND_RATE', '0.5')) # 번 돈의 몇 %를 재투자할 것인가? (기본 50%)

# 💡 [동적 분산] ENABLED_ENGINES를 읽어와 활성화된 엔진 수만큼 API 호출 시간을 균등 분배합니다.
ENABLED_ENGINES_STR = os.getenv('ENABLED_ENGINES', 'CORE,HUNTER,GRID,SCALP,CLASSIC_GRID')

# 💡 [버그 방지] 사용자가 .env에 'CLASSIC'으로 줄여서 적은 경우 'CLASSIC_GRID'로 자동 매핑합니다.
ACTIVE_ENGINES = ['CLASSIC_GRID' if e.strip().upper() == 'CLASSIC' else e.strip().upper() for e in ENABLED_ENGINES_STR.split(',') if e.strip()]

# 💡 [버그 수정] 구버전 `.env` 호환성 유지: 개별 엔진 예산이 없으면 기존 MAX_BUDGET을 그대로 사용하도록 폴백(Fallback) 처리
LEGACY_BUDGET = float(os.getenv('MAX_BUDGET', 0))
ENGINE_BUDGETS = {
    'CORE': float(os.getenv('CORE_MAX_BUDGET', LEGACY_BUDGET)),
    'HUNTER': float(os.getenv('HUNTER_MAX_BUDGET', LEGACY_BUDGET)),
    'GRID': float(os.getenv('GRID_MAX_BUDGET', LEGACY_BUDGET)),
    'SCALP': float(os.getenv('SCALP_MAX_BUDGET', LEGACY_BUDGET)),
    'CLASSIC_GRID': float(os.getenv('CG_MAX_BUDGET', LEGACY_BUDGET)),
}
# 전체 예산은 모든 활성 엔진 예산의 합
TOTAL_BUDGET = sum(ENGINE_BUDGETS[e] for e in ACTIVE_ENGINES if e in ENGINE_BUDGETS)

# 💡 [V17.21] 지능형 컨트롤 타워: 동적 예산 할당 (Dynamic Allocation)
DYNAMIC_ALLOCATION = os.getenv('DYNAMIC_ALLOCATION', 'False').lower() == 'true'
DYNAMIC_TOTAL_BUDGET = float(os.getenv('DYNAMIC_TOTAL_BUDGET', str(TOTAL_BUDGET)))


# 💡 [제거] 다중 컨테이너용 딜레이 로직 제거


# 💡 [V17.20] 다중 서버 데이터 공유용 중앙 Redis 초기화 및 안전 모드(Safe Mode)
USE_REDIS_CACHE = os.getenv('USE_REDIS_CACHE', 'False').lower() == 'true'
REDIS_HOST = os.getenv('REDIS_HOST', 'aegis_redis')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', None)

redis_client = None
SAFE_MODE_DELAY = 0.0 # Redis 장애 시 API 폭주를 막기 위한 강제 지연 시간

if USE_REDIS_CACHE:
    try:
        import redis
        redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, db=0)
        redis_client.ping()
        print(f"🟢 [시스템] 중앙 Redis 캐시 연결 성공! ({REDIS_HOST}:{REDIS_PORT})")
    except Exception as e:
        print(f"⚠️ [시스템] Redis 연결 실패! 안전 모드(Safe Mode) 발동. API 호출을 지연시킵니다: {e}")
        USE_REDIS_CACHE = False
        SAFE_MODE_DELAY = 1.5 # 다중 봇 동시 API 호출 WAF 방어용 1.5초 강제 지연

# 💡 [V17.26] get_tickers 캐싱 패치 추가 (API 병목 주범 제거)
_original_get_tickers = pyupbit.get_tickers

def _safe_get_tickers(fiat="KRW", verbose=False):
    cache_key = f"tickers_{fiat}_{verbose}"
    if USE_REDIS_CACHE:
        try:
            cached = redis_client.get(cache_key)
            if cached: return json.loads(cached.decode('utf-8'))
        except: pass

    retries = 3
    for i in range(retries):
        try:
            if SAFE_MODE_DELAY > 0: time.sleep(SAFE_MODE_DELAY)
            res = _original_get_tickers(fiat=fiat, verbose=verbose)
            if res is not None:
                if USE_REDIS_CACHE:
                    try: redis_client.setex(cache_key, 3600, json.dumps(res)) # 종목 목록은 1시간 캐싱
                    except: pass
                return res
        except Exception as e:
            time.sleep(2)
    return []
pyupbit.get_tickers = _safe_get_tickers

# 💡 [V17.18] Monkey Patching (API 호출 초과 시 오토 힐링 추가)
_original_get_current_price = pyupbit.get_current_price

def _safe_get_current_price(ticker, limit_info=False, verbose=False):
    # 1. 중앙 Redis 캐시 확인 (과도한 1초 이내 동일 호출 방어)
    if isinstance(ticker, list):
        # 리스트일 경우 정렬하여 고유한 단일 캐시 키 생성 (예: price_KRW-A,KRW-B)
        cache_key = "price_" + ",".join(sorted(ticker))
    else:
        cache_key = f"price_{ticker}"
        
    if USE_REDIS_CACHE:
        try:
            cached = redis_client.get(cache_key)
            if cached: return json.loads(cached.decode('utf-8'))
            
            # 💡 [핵심 방어] Cache Stampede 방지 분산 락(Mutex)
            lock_key = f"lock_{cache_key}"
            acquired = redis_client.set(lock_key, "1", nx=True, ex=3) # 3초간 접근 잠금
            if not acquired:
                time.sleep(0.3) # 대표 봇이 가져올 때까지 0.3초 대기
                cached = redis_client.get(cache_key)
                if cached: return json.loads(cached.decode('utf-8'))
        except: pass

    retries = 3
    for i in range(retries):
        try:
            if SAFE_MODE_DELAY > 0: time.sleep(SAFE_MODE_DELAY)
            res = _original_get_current_price(ticker, limit_info, verbose)
            if res is not None: 
                # 2. Redis에 저장 (2초 TTL 적용)
                if USE_REDIS_CACHE:
                    try: redis_client.setex(cache_key, 2, json.dumps(res))
                    except: pass
                return res
        except Exception as e:
            err_msg = str(e)
            if "Too Many Requests" in err_msg or "429" in err_msg:
                print(f"⚠️ [API 과부하] 호출 제한 도달. 2초 대기 후 재시도... ({i+1}/{retries})")
                time.sleep(2) # 숨 고르기
            elif "string indices must be integers" in err_msg or err_msg == "0" or "list index" in err_msg:
                print(f"⚠️ [API 차단 방어] 업비트 방화벽(WAF) 차단 응답 감지. 3초 대기... ({i+1}/{retries})")
                time.sleep(3)
            else:
                print(f"⚠️ [네트워크 에러] 시세 조회 지연 - 사유: {err_msg[:50]}... ({i+1}/{retries})")
                time.sleep(1)
    return {} if isinstance(ticker, list) else None

pyupbit.get_current_price = _safe_get_current_price

# 💡 [추가] 단일 잔고 조회(get_balance) 연결 타임아웃 오토 힐링 패치
_original_get_balance = pyupbit.Upbit.get_balance

def _safe_get_balance(self, *args, **kwargs):
    retries = 3
    for i in range(retries):
        try:
            res = _original_get_balance(self, *args, **kwargs)
            if res is not None:
                return res
        except Exception as e:
            err_msg = str(e)
            if "Too Many" in err_msg or "429" in err_msg or err_msg == "0" or "string indices" in err_msg:
                print(f"⚠️ [API 차단 방어] 단일 잔고 조회 WAF 차단. 2초 대기... ({i+1}/{retries})")
                time.sleep(2)
            else:
                print(f"⚠️ [네트워크 에러] 단일 잔고 조회 지연 - 사유: {err_msg[:50]}... ({i+1}/{retries})")
                time.sleep(1)
    return 0

pyupbit.Upbit.get_balance = _safe_get_balance

# 💡 [추가] 잔고 조회(get_balances) 연결 타임아웃 오토 힐링 패치
_original_get_balances = pyupbit.Upbit.get_balances

def _safe_get_balances(self, *args, **kwargs):
    retries = 3
    for i in range(retries):
        try:
            res = _original_get_balances(self, *args, **kwargs)
            if res is not None:
                return res
        except Exception as e:
            err_msg = str(e)
            if "Too Many Requests" in err_msg or "429" in err_msg or err_msg == "0" or "string indices" in err_msg:
                print(f"⚠️ [API 차단 방어] 전체 잔고 조회 WAF 차단. 2초 대기... ({i+1}/{retries})")
                time.sleep(2)
            else:
                print(f"⚠️ [네트워크 에러] 전체 잔고 조회 지연 - 사유: {err_msg[:50]}... ({i+1}/{retries})")
                time.sleep(1)
    return []

pyupbit.Upbit.get_balances = _safe_get_balances

# 💡 [추가] get_ohlcv 무한 호출 방지 (일봉 데이터 캐싱 패치)
_original_get_ohlcv = pyupbit.get_ohlcv
_ohlcv_cache = {}

def _safe_get_ohlcv(ticker, interval="day", count=200, to=None, period=0.1):
    now = datetime.now()
    if to is None:
        cache_key = f"{ticker}_{interval}_{count}"
        
        cache_duration = 0
        if interval == "day": cache_duration = 3600
        elif interval == "minute60": cache_duration = 1800 # 💡 [추가] 60분봉도 30분 동안 캐싱
        
        if cache_duration > 0:
            if USE_REDIS_CACHE:
                try:
                    cached = redis_client.get(f"ohlcv_{cache_key}")
                    if cached: return pickle.loads(cached)
                    
                    # 💡 [핵심 방어] OHLCV Cache Stampede 방지 분산 락(Mutex)
                    lock_key = f"lock_ohlcv_{cache_key}"
                    acquired = redis_client.set(lock_key, "1", nx=True, ex=10) # 10초간 접근 잠금
                    if not acquired:
                        time.sleep(0.5) # 대표 봇이 가져올 때까지 0.5초 대기
                        cached = redis_client.get(f"ohlcv_{cache_key}")
                        if cached: return pickle.loads(cached)
                except: pass
            else:
                # Redis가 꺼져있거나 연결 실패 시 기존 로컬 캐시 사용
                if cache_key in _ohlcv_cache:
                    cached_time, cached_df = _ohlcv_cache[cache_key]
                    if (now - cached_time).total_seconds() < cache_duration: 
                        return cached_df

    time.sleep(0.1 + SAFE_MODE_DELAY) # 기본 API 속도 조절 + 안전 모드 지연
    for i in range(3):
        try:
            df = _original_get_ohlcv(ticker, interval=interval, count=count, to=to, period=period)
            if df is not None and not (isinstance(df, list) and len(df) == 0):
                if cache_duration > 0:
                    if USE_REDIS_CACHE:
                        try: redis_client.setex(f"ohlcv_{cache_key}", cache_duration, pickle.dumps(df))
                        except: pass
                    else:
                        _ohlcv_cache[cache_key] = (now, df)
                return df
        except Exception as e:
            err_msg = str(e)
            if "Too Many" in err_msg or "429" in err_msg or "string" in err_msg or err_msg == "0":
                time.sleep(2)
            else:
                time.sleep(0.5)
    return None

pyupbit.get_ohlcv = _safe_get_ohlcv

# 사용자 정의 모듈 임포트
from config import *
import db_manager
import analyzer
import worker
import telegram_handler 

# 💡 [핵심] 모듈화된 최신 엔진 클래스 임포트
from engines.base_engine import BaseEngine
from engines.core_engine import CoreEngine
from engines.hunter_engine import HunterEngine
from engines.grid_engine import GridEngine
from engines.scalp_engine import ScalpEngine
from engines.classic_grid_engine import ClassicGridEngine

# -------------------------------------------------------------
# 🧠 [컨트롤 타워] 시장 상황(Regime) 기반 예산 동적 분배기
# -------------------------------------------------------------
def apply_dynamic_allocation(regime, notify=False):
    """시장의 파도에 따라 밸브를 열고 닫아 엔진별 예산을 재조정합니다."""
    global TOTAL_BUDGET, DYNAMIC_TOTAL_BUDGET
    if not DYNAMIC_ALLOCATION or DYNAMIC_TOTAL_BUDGET <= 0:
        return

    # 시장 국면별 5대 엔진의 이상적인 가중치, 전체 예산 사용률(usage), 💡 1회 투자 총알 크기 비율(unit_multiplier)
    allocation_map = {
        "SUPER_BULL": {"CORE": 0.4, "SCALP": 0.3, "GRID": 0.2, "CLASSIC_GRID": 0.1, "HUNTER": 0.0, "usage": 1.0, "unit_multiplier": 1.2},
        "NORMAL":     {"GRID": 0.4, "SCALP": 0.3, "CORE": 0.1, "HUNTER": 0.1, "CLASSIC_GRID": 0.1, "usage": 1.0, "unit_multiplier": 1.0},
        "CAUTION":    {"HUNTER": 0.4, "CLASSIC_GRID": 0.3, "GRID": 0.2, "SCALP": 0.1, "CORE": 0.0, "usage": 0.6, "unit_multiplier": 0.7},
        "ICE_AGE":    {"HUNTER": 0.5, "CLASSIC_GRID": 0.5, "GRID": 0.0, "SCALP": 0.0, "CORE": 0.0, "usage": 0.3, "unit_multiplier": 0.5}
    }
    
    weights = allocation_map.get(regime, allocation_map["NORMAL"])
    unit_mult = weights.get("unit_multiplier", 1.0)
    
    # 💡 [핵심] worker.py에 선언된 전역 변수를 실시간 업데이트하여 총알(Unit Size) 크기 스케일링
    worker.DYNAMIC_UNIT_MULTIPLIER = unit_mult
    
    # 사용자가 켠 엔진들 사이에서만 가중치를 100%로 꽉 차게 재조정(정규화)
    active_weight_sum = sum(weights.get(e, 0) for e in ACTIVE_ENGINES)
    if active_weight_sum == 0: return
        
    # 리스크 관리를 위해 빙하기에는 전체 예산의 일부(usage)만 사용
    actual_budget = DYNAMIC_TOTAL_BUDGET * weights.get("usage", 1.0)
    TOTAL_BUDGET = actual_budget
    
    noti_msg = f"🧠 [지능형 컨트롤 타워] 시장 국면 조정 발동!\n"
    noti_msg += f"🌍 변경된 시장: {REGIME_SETTINGS.get(regime, {}).get('desc', regime)}\n"
    noti_msg += f"💵 가용 예산: {actual_budget:,.0f}원 (총 풀의 {weights.get('usage', 1.0)*100:.0f}%)\n"
    noti_msg += f"💉 1회 매수 크기 스케일링: {unit_mult*100:.0f}%\n"
    noti_msg += "📊 [엔진별 예산 분배]\n"
    
    print(f"\n🧠 [컨트롤 타워] 시장 국면({regime}) 기반 예산 재조정 발동!")
    print(f"💵 가용 예산: {actual_budget:,.0f}원 (총 풀의 {weights.get('usage', 1.0)*100:.0f}%)")
    print(f"💉 투자 단위(Unit Size) 스케일링: {unit_mult*100:.0f}% 적용")

    for eng in ACTIVE_ENGINES:
        norm_w = weights.get(eng, 0) / active_weight_sum
        new_budget = actual_budget * norm_w
        ENGINE_BUDGETS[eng] = new_budget
        if eng in active_engines:
            active_engines[eng].MAX_BUDGET = new_budget
        icon = "🟢" if new_budget > 0 else "🔴"
        print(f"  - {icon} {eng:12}: {new_budget:>10,.0f}원 ({norm_w*100:>4.1f}%)")
        noti_msg += f"  - {icon} {eng}: {new_budget:,.0f}원 ({norm_w*100:.1f}%)\n"
        
    if notify:
        send_telegram(noti_msg)
        
    return noti_msg

# --- [전역 변수 초기화] ---
upbit = pyupbit.Upbit(UPBIT_ACCESS, UPBIT_SECRET)
SEED_MONEY = 0
bot_positions = {}
active_engines = {} # 💡 [버그 수정] 컨트롤 타워 초기 호출을 위한 전역 변수 선언 위치 이동
current_regime = "NORMAL"
core_targets, hunter_targets = {}, {}
top_grid_candidates = []
last_grid_eval_time = None
next_day_core_targets, next_day_hunter_targets = {}, {}
last_target_fetch_time = None
budget_lock_notified = {'SCALP': False, 'GRID': False, 'CLASSIC_GRID': False}
last_panic_check_time = datetime.now()
is_panic_state = False
last_regime_check_time = None

last_grid_eval_time = datetime.now() - timedelta(hours=2) # 💡 즉시 실행되도록 초기값 조정

# 💡 구동 시 즉시 현재 시장 상황을 스캔하고 컨트롤 타워 가동
current_regime = analyzer.get_market_regime("NORMAL")
init_allocation_msg = apply_dynamic_allocation(current_regime, notify=False)

print(f"====================================================")
print(f"🏆 [시스템] Aegis-Elite V17.21 통합 컨트롤 타워 가동 (활성: {', '.join(ACTIVE_ENGINES)})")
print(f"🧠 [지능형 동적 분배 모드]: {'활성화 ✅' if DYNAMIC_ALLOCATION else '비활성화 (고정 예산) ❌'}")
for engine in ACTIVE_ENGINES:
    symbol = "🏹" if engine == 'HUNTER' else "�️" if engine == 'CLASSIC_GRID' else "🛡️" if engine == 'CORE' else "⚡" if engine == 'SCALP' else "🎰" if engine == 'GRID' else "🤖"
    if engine == 'GRID':
        print(f"{symbol} GRID 슬롯: {GRID_TOTAL_SLOTS} | 다중슬롯: {USE_MULTI_SLOT} (Max {MAX_SLOTS_PER_COIN})")
    elif engine == 'SCALP':
        print(f"{symbol} SCALP 슬롯: {SCALP_TOTAL_SLOTS} | 다중슬롯: {SCALP_USE_MULTI_SLOT} (Max {SCALP_MAX_SLOTS_PER_COIN})")
    elif engine == 'CLASSIC_GRID':
        print(f"{symbol} CLASSIC_GRID 슬롯: {CG_TOTAL_SLOTS} ")
    else:
        print(f"{symbol} {engine} 타겟 슬롯: {TARGET_SLOTS}")
    # 💡 [추가] 엔진별 할당 예산 출력
    print(f"  - 💰 할당 예산: {ENGINE_BUDGETS.get(engine, 0):,.0f}원")

boot_msg = f"🚀 [{db_manager.ACCOUNT_ID} 통합 엔진 시동 완료]\n"
boot_msg += f"✅ 활성 엔진: {', '.join(ACTIVE_ENGINES)}\n"
if DYNAMIC_ALLOCATION:
    boot_msg += f"⚙️ 운용 모드: 지능형 가변 모드 (Dynamic)\n"
    boot_msg += f"💰 설정된 총 풀: {DYNAMIC_TOTAL_BUDGET:,.0f}원\n\n"
    boot_msg += init_allocation_msg if init_allocation_msg else ""
else:
    boot_msg += f"⚙️ 운용 모드: 고정 예산 모드 (Static)\n"
    boot_msg += f"💰 총 할당 예산: {TOTAL_BUDGET:,.0f}원\n"
    for eng in ACTIVE_ENGINES:
        boot_msg += f"  - {eng}: {ENGINE_BUDGETS.get(eng, 0):,.0f}원\n"

send_telegram(boot_msg)
print(f"💰 총 할당 예산: {TOTAL_BUDGET:,.0f}원")
print(f"====================================================\n")

# -------------------------------------------------------------
# 💡 [핵심] 구동할 엔진 객체 생성 및 스레드 락 초기화
# -------------------------------------------------------------
bot_positions_lock = threading.Lock()

for engine in ACTIVE_ENGINES:
    if engine == 'CORE': active_engines['CORE'] = CoreEngine(upbit, bot_positions, bot_positions_lock)
    elif engine == 'HUNTER': active_engines['HUNTER'] = HunterEngine(upbit, bot_positions, bot_positions_lock)
    elif engine == 'GRID': active_engines['GRID'] = GridEngine(upbit, bot_positions, bot_positions_lock)
    elif engine == 'SCALP': active_engines['SCALP'] = ScalpEngine(upbit, bot_positions, bot_positions_lock)
    elif engine == 'CLASSIC_GRID': active_engines['CLASSIC_GRID'] = ClassicGridEngine(upbit, bot_positions, bot_positions_lock)

    # 💡 [추가] 봇 구동 시 활성화된 엔진의 과거 일시 정지(Pause) 기록을 무시하고 무조건 가동(Resume) 상태로 초기화
    db_manager.set_engine_pause_state(engine, False)


def background_target_fetcher():
    global core_targets, hunter_targets, current_regime
    if current_regime == "ICE_AGE":
        print("💤 [동면] 시장 빙하기로 인해 타겟 탐색 스킵.")
        return

    print("🕵️‍♂️ 4H 레이더 가동 (CORE/HUNTER 스캔 중)...")
    temp_core, temp_hunter_candidates = {}, []
    
    # 1. CORE 타겟 스캔 (돌파 매매용)
    for ticker in CORE_UNIVERSE:
        time.sleep(0.3)  # 🚀 [API 차단 방지] 0.15초 -> 0.3초 완화
        df = pyupbit.get_ohlcv(ticker, interval="day", count=20)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 6: continue
        df['noise'] = 1 - abs(df['open'] - df['close']) / (df['high'] - df['low'])
        temp_core[ticker] = {
            'open': df.iloc[-1]['open'], 
            'range': analyzer.get_atr(df, 5), 
            'k': max(0.4, min(0.7, df['noise'].mean()))
        }
        
    # 2. HUNTER 타겟 스캔 (낙폭과대 매매용)
    try:
        tickers = pyupbit.get_tickers(fiat="KRW")
        for t in tickers:
            if t in CORE_UNIVERSE: continue 
            time.sleep(0.3)  # 🚀 [API 차단 방지] 0.1초 -> 0.3초 완화
            df = pyupbit.get_ohlcv(t, interval="day", count=6)
            if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 6: continue
            temp_hunter_candidates.append({'ticker': t, 'value': df.iloc[-2]['value'], 'open': df.iloc[-1]['open'], 'range': analyzer.get_atr(df, 5)})
        
        if temp_hunter_candidates:
            top3 = sorted(temp_hunter_candidates, key=lambda x: x['value'], reverse=True)[:3]
            hunter_targets = {item['ticker']: item for item in top3}
    except: pass
    
    core_targets = temp_core
    print("✅ [레이더] CORE/HUNTER 타겟 갱신 완료.")

# -------------------------------------------------------------
# 🕵️‍♂️ 그리드 전용: 종목 발굴 및 리밸런싱 로직
# -------------------------------------------------------------
def evaluate_grid_candidates():
    global top_grid_candidates
    try:
        print("🔍 [그리드 레이더] 최적 사냥터 스캔 중...")
        # 마켓 전체 스캔을 대상으로 할 경우 사용
        # all_tickers = pyupbit.get_tickers(fiat="KRW")
        # config.py에 정의된 GRID_POOL 목록만 가져와서 스캔 대상으로 삼습니다.
        all_tickers = GRID_POOL

        scores = []
        
        for t in all_tickers:
            #이더리움 단독 고정 슬롯 사용시만 필요
            #if t == "KRW-ETH": continue. 
            score = analyzer.get_grid_suitability_score(t)
            if score > 0:
                scores.append({'ticker': t, 'score': score})
            time.sleep(0.3) # 💡 [API 차단 방지] 다중 컨테이너 환경 고려 0.3초(약 3.3회/초)로 추가 완화
            
        sorted_scores = sorted(scores, key=lambda x: x['score'], reverse=True)
        # 💡 [버그 수정] 후보군 파이가 너무 적어 GRID/CG가 매수를 못하는 문제 해결 (상위 10개로 넉넉하게 추출)
        top_grid_candidates = [item['ticker'] for item in sorted_scores[:10]]
        # 💡 [수정] 통합 환경에 맞게 특정 엔진 구동 여부에 따라 발송
        if 'GRID' in ACTIVE_ENGINES:
            msg = f"🔍 [그리드 레이더] 신규 타겟 선정 완료\n- 후보: {', '.join(top_grid_candidates[:5])}..."
            send_telegram(msg)
    except Exception as e:
        print(f"❌ 후보 스캔 오류: {e}")

# -------------------------------------------------------------
# 🔄 메인 제어 루프
# -------------------------------------------------------------
bot_positions = db_manager.recover_bot_positions(upbit, ACTIVE_ENGINES)
for k, v in bot_positions.items():
    if 'buy_level' not in v:
        v['buy_level'] = 1
    # 💡 [필수 복구] 헌터 엔진 구조적 손절가 세팅
    if v['engine'] == 'HUNTER' and 'struct_stop' not in v: 
        v['struct_stop'] = analyzer.get_structural_stop(v['ticker'])

# 텔레그램 봇 백그라운드 가동 (이 한 줄 필수 추가!)
# -------------------------------------------------------------
# 🔄 메신저 당번 지정 (Conflict 에러 방지)
# -------------------------------------------------------------
# 환경 변수에서 ENABLE_TELEGRAM_COMMANDS를 읽어옵니다. (기본값은 False)
ENABLE_TELEGRAM_COMMANDS = os.getenv('ENABLE_TELEGRAM_COMMANDS', 'False').lower() == 'true'

if ENABLE_TELEGRAM_COMMANDS:
    # 💡 도커 컴포즈에서 True로 설정된 단 하나의 엔진만 이 코드를 실행합니다.
    # 💡 [V17.25] 텔레그램에서도 지능형 컨트롤 타워 정보(가변 모드 여부, 스케일링 비율)를 참조할 수 있도록 람다를 추가로 넘겨줍니다.
    telegram_handler.start_telegram_listener(bot_positions, bot_positions_lock, lambda: TOTAL_BUDGET, lambda: current_regime, lambda: (DYNAMIC_ALLOCATION, worker.DYNAMIC_UNIT_MULTIPLIER))
    print(f"🤖 [공통] 텔레그램 명령 리스너 가동 시작!")
else:
    print(f"🔇 [공통] 텔레그램 명령 수신을 스킵합니다.")

# 자동 일일 보고를 위한 변수
last_daily_report_day = None

# 💡 [추가] 연속 에러 알림 스팸 방지용 카운터
consecutive_errors = 0

last_daily_report_hour = -1  # 💡 추가: 마지막으로 보고서를 보낸 시간 기록
# 💡 [추가] 무한 루프 시작 '바로 위'에 카운터 변수를 하나 만들어 줍니다.
pause_log_counter = 0

while True:
    try:
        now = datetime.now()
        
        # 💡 매일 아침 8시 0분에 한 번만 자동 보고서 발송
        if now.hour == 8 and now.minute == 0 and last_daily_report_day != now.day:
            rows = db_manager.get_today_performance(1)
            report_msg = f"🌅 [아침 브리핑] 어제 총 결산\n\n"
            if not rows:
                report_msg += "어제는 완료된 매매가 없었습니다."
            else:
                total_krw = 0
                for r in rows:
                    report_msg += f"- {r['engine']}: {r['total_profit']:+,.0f}원\n"
                    total_krw += r['total_profit']
                report_msg += f"──────────────\n💵 총 실현 손익: {total_krw:+,.0f}원"
            
            send_telegram(report_msg)
            
            # 💡 [추가] 90일 지난 오래된 매매 로그 자동 삭제 (디스크 용량 관리)
            db_manager.cleanup_old_trade_logs(90)
            
            last_daily_report_day = now.day # 발송 완료 기록

        # 💡 [수정] 정기 보고서 발송 (8, 13, 18, 23시) - GRID에서만 보내기 (중복발송))
        report_hours = [13, 18, 23]
        if now.hour in report_hours and now.minute == 0 and last_daily_report_hour != now.hour:
            rows = db_manager.get_today_performance(0)
            # db_manager.ACCOUNT_ID를 사용하여 어떤 계정의 보고서인지 명시합니다.
            report_msg = f"📊 [{db_manager.ACCOUNT_ID}] 정기 수익 보고 ({now.hour}시)\n\n"
            
            if not rows:
                report_msg += "현재까지 완료된 매매 내역이 없습니다."
            else:
                total_krw = 0
                for r in rows:
                    # 수익금(total_profit)과 수익률(avg_rate)을 함께 표시합니다.
                    win_rate = r.get('win_rate', 0)
                    report_msg += f"- {r['engine']}: {r['total_profit']:+,.0f}원 (승률: {win_rate:.0f}%)\n"
                    total_krw += r['total_profit']
                report_msg += f"──────────────\n💵 당일 총 합계: {total_krw:+,.0f}원"
                
            # 💡 [자동 복리 재투자] 누적 수익을 반영하여 엔진별 예산 한도를 동적으로 상향
            if AUTO_COMPOUND:
                total_profit = db_manager.get_total_realized_profit()
                if total_profit > 0 and TOTAL_BUDGET > 0:
                    reinvest_pool = total_profit * COMPOUND_RATE
                    
                    if DYNAMIC_ALLOCATION:
                        base_pool = float(os.getenv('DYNAMIC_TOTAL_BUDGET', str(TOTAL_BUDGET)))
                        DYNAMIC_TOTAL_BUDGET = base_pool + reinvest_pool
                        report_msg += f"\n\n🔄 [지능형 복리] 누적 수익 반영\n- 컨트롤 타워 풀 예산 {reinvest_pool:,.0f}원 증가!"
                        alloc_info = apply_dynamic_allocation(current_regime, notify=False)
                        if alloc_info: report_msg += f"\n\n{alloc_info}"
                    else:
                        report_msg += f"\n\n🔄 [스노우볼 복리] 누적 수익 반영\n- 총 잉여 예산 {reinvest_pool:,.0f}원 추가 확보!"
                        for eng_name, eng_obj in active_engines.items():
                            base_budget = ENGINE_BUDGETS.get(eng_name, 0)
                            weight = base_budget / TOTAL_BUDGET 
                            eng_obj.MAX_BUDGET = base_budget + (reinvest_pool * weight)
            
            send_telegram(report_msg)
            last_daily_report_hour = now.hour # 💡 해당 시간 발송 완료 기록

        # 💡 [수정] 단일 컨테이너 환경이므로 분산 로직 제거
        if now.minute % 15 == 0 and (last_regime_check_time is None or (now - last_regime_check_time).total_seconds() > 60):
            new_regime = analyzer.get_market_regime(current_regime)
            if new_regime != current_regime:
                current_regime = new_regime
                apply_dynamic_allocation(current_regime, notify=True) # 💡 [컨트롤 타워] 시장 변화 감지 시 텔레그램 즉시 알림 발송!
            last_regime_check_time = now

        # 💡 [추가] 4시간마다 CORE/HUNTER 타겟 스캔 (50분 언저리에 실행하여 API 몰림 방지)
        # 💡 [버그 수정] 봇을 켰을 때(초기화 시) 즉시 1회 스캔하도록 'last_target_fetch_time is None' 조건 강화
        if any(e in ['CORE', 'HUNTER'] for e in ACTIVE_ENGINES):
            if last_target_fetch_time is None or (now.hour % 4 == 0 and now.minute == 50 and now >= last_target_fetch_time + timedelta(hours=3)):
                last_target_fetch_time = now 
                threading.Thread(target=background_target_fetcher).start()

        # 💡 [버그 수정] 메인 루프 병목(Freezing) 방지를 위해 그리드 스캐너에도 스레드(Thread) 적용 복구
        # 💡 [개선] 스캔 주기를 6시간 -> 1시간으로 단축하여 시장 변화에 빠르게 대응
        if any(e in ['GRID', 'SCALP', 'CLASSIC_GRID'] for e in ACTIVE_ENGINES):
            if last_grid_eval_time is None or now >= last_grid_eval_time + timedelta(hours=1):
                last_grid_eval_time = now
                threading.Thread(target=evaluate_grid_candidates).start()

        # 💡 [수정] 패닉 체크를 매 루프마다 하지 않고 컨테이너별로 10초에 한 번만 수행하도록 완화하여 API 폭주 방지
        if last_panic_check_time is None or (now - last_panic_check_time).total_seconds() >= 10:
            is_panic_state = analyzer.check_panic_fall()
            last_panic_check_time = now

        if is_panic_state:
            print("🚨 [패닉장 감지] 모든 신규 진입이 일시 중단됩니다. (익절/손절은 정상 가동)")

        # 💡 [핵심 최적화] 메인 루프에서 전체 잔고를 단 1번만 조회하여 모든 엔진에 공유 (API 병목 완화)
        balances = upbit.get_balances()
        safe_balances = {b['currency']: float(b['balance']) for b in balances} if isinstance(balances, list) else {}
        
        # 💡 단일 컨테이너 통합 루프 실행
        for engine_name, engine_obj in active_engines.items():
            if db_manager.is_engine_paused(engine_name):
                if pause_log_counter % 60 == 0:  
                    print(f"⏸️ [{engine_name}] 엔진 매매 루프 일시 정지 중...")
                continue
                
            if engine_name == 'CORE': engine_obj.run(now, current_regime, core_targets, is_panic_state, safe_balances)
            elif engine_name == 'HUNTER': engine_obj.run(now, current_regime, hunter_targets, is_panic_state, safe_balances)
            elif engine_name in ['GRID', 'SCALP', 'CLASSIC_GRID']: engine_obj.run(now, current_regime, top_grid_candidates, is_panic_state, safe_balances)
            
            time.sleep(0.5) # 엔진 간 루프 간격 (WAF 방어)
        
        pause_log_counter += 1

        consecutive_errors = 0
        time.sleep(3.0)

    except Exception as e:
        print(f"🚨 [메인 루프] 통합 실행 중 에러 발생: {e}")
        traceback.print_exc()
        
        # 💡 [추가] 에러 발생 시 텔레그램 긴급 노티 발송 및 스팸 방지
        consecutive_errors += 1
        
        # 연속 3회까지만 텔레그램을 발송하고, 이후는 콘솔에만 기록하여 스팸을 방지합니다.
        if consecutive_errors <= 3:
            error_msg = (
                f"🚨 [통합 봇 긴급 오류]\n"
                f"시스템 루프에서 에러가 발생했습니다.\n\n"
                f"원인: {str(e)[:150]}" # 텔레그램 메시지 길이 제한 방지를 위해 150자로 자름
            )
            
            if consecutive_errors == 3:
                error_msg += "\n\n⚠️ 동일 오류가 지속 반복되어 알림을 일시 중단합니다. 서버를 즉시 확인해 주세요!"
                
            try:
                send_telegram(error_msg)
            except:
                pass
                
        # 에러 발생 시 오토 힐링 및 과부하 방지를 위해 대기 시간을 기존 5초에서 10초로 연장
        time.sleep(10)
