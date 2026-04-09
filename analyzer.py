import time
import pandas as pd
import numpy as np
import pyupbit
from config import VOLUME_SPIKE_RATIO
# ARM 서버 호환성을 위해 classic 버전 사용
import pandas_ta_classic as ta 

# -------------------------------------------------------------
# 🛡️ 공용 기술 지표 함수
# -------------------------------------------------------------

def get_adx(ticker):
    """💡 ADX(평균 방향성 지수) 계산 - 추세의 강도 측정 (25 이상 시 추세 발생)"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=50)
        if df is None or df.empty: return 0
        # pandas_ta_classic을 이용한 표준 ADX 계산
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        return adx_df['ADX_14'].iloc[-1]
    except: return 0

def calc_rsi(series, period=14):
    """💡 RSI 계산 (상대강도지수)"""
    delta = series.diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=period-1, adjust=False).mean()
    ema_down = down.ewm(com=period-1, adjust=False).mean()
    return 100 - (100 / (1 + (ema_up / ema_down)))

def get_atr(df, period=5):
    """💡 ATR(평균 실제 범위) 계산 - 변동성 측정"""
    try:
        tr = pd.concat([
            df['high'] - df['low'], 
            (df['high'] - df['close'].shift(1)).abs(), 
            (df['low'] - df['close'].shift(1)).abs()
        ], axis=1).max(axis=1)
        return tr.rolling(window=period).mean().iloc[-1]
    except: 
        return df['high'].iloc[-2] - df['low'].iloc[-2]

def get_ema200(ticker):
    """💡 EMA 200(장기 이평선) - 장기 추세 필터"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=210)
        if df is None or len(df) < 200: return 0
        return df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
    except: return 0

# -------------------------------------------------------------
# 🚨 시장 위험 감지 (Panic/Crash)
# -------------------------------------------------------------

def check_panic_fall():
    """🚨 [DEFCON-1] 비트코인 급락 감지 (15분 내 -3.5% 하락 시 전량 매도 신호)"""
    try:
        df = pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=3)
        if df is None or len(df) < 3: return False
        highest = df['high'].max()
        current = df['close'].iloc[-1]
        fall_rate = (current - highest) / highest
        return fall_rate <= -0.035
    except: return False

def check_btc_flash_crash():
    """🚨 비트코인 단기 플래시 크래시 감지 (-1.5% 급락 시 매수 금지)"""
    try:
        df = pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=4)
        if df is None or len(df) < 4: return False
        return ((df['close'].iloc[-1] - df['high'].max()) / df['high'].max()) <= -0.015 
    except: return False

# -------------------------------------------------------------
# 🛡️ CORE 엔진 전용 필터
# -------------------------------------------------------------

def check_keltner_breakout(ticker):
    """🛡️ 켈트너 채널 상단 돌파 확인"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=25)
        if df is None or len(df) < 25: return False
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
        # 중심선(EMA 20) + (ATR 20 * 1.5) 돌파 시 매수
        upper_band = df['close'].ewm(span=20, adjust=False).mean() + (tr.ewm(span=20, adjust=False).mean() * 1.5)
        return df['close'].iloc[-1] > upper_band.iloc[-1]
    except: return False

def check_volume_spike(ticker):
    """🛡️ 거래량 스파이크 감지 (평균 대비 설정값 이상)"""
    try: 
        df = pyupbit.get_ohlcv(ticker, interval="minute1", count=31)
        if df is None or len(df) < 2: return False
        return df['volume'].iloc[-1] >= (df['volume'].iloc[:-1].mean() * VOLUME_SPIKE_RATIO)
    except: return False

def get_chandelier_exit(ticker, pos_peak_price, current_regime):
    """🛡️ 샹들리에 청산가 계산 (추세 추종 익절 라인)"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=20)
        if df is None or len(df) < 20: return pos_peak_price * 0.95
        # 시장 상황(Regime)에 따라 변동성 허용 폭 조절
        multiplier = 3.0 if current_regime == "SUPER_BULL" else (1.5 if current_regime == "CAUTION" else 2.5)
        return pos_peak_price - (get_atr(df, 14) * multiplier)
    except: return pos_peak_price * 0.95

# -------------------------------------------------------------
# 🏹 HUNTER 엔진 전용 필터
# -------------------------------------------------------------

def check_hunter_dip_buy(ticker):
    """🏹 과매도 구간(RSI) 및 VWAP 지지 확인"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute5", count=150) 
        if df is None or len(df) < 144: return False
        df_session = df.tail(144) # 최근 12시간
        
        # VWAP 근사치 계산
        q = df_session['volume']
        p = (df_session['high'] + df_session['low'] + df_session['close']) / 3
        current_vwap = ((p * q).cumsum() / q.cumsum()).iloc[-1]
        
        curr_price = df_session['close'].iloc[-1]
        rsi = calc_rsi(df_session['close'], 14)
        
        # VWAP 근처에서 RSI 반등 및 거래량 증가 확인
        cond1 = (current_vwap * 0.975 <= curr_price <= current_vwap * 1.025)
        cond2 = (rsi.iloc[-2] < 40 and rsi.iloc[-1] > rsi.iloc[-2])
        cond3 = df_session['volume'].iloc[-1] > df_session['volume'].iloc[-2]
        
        return cond1 and cond2 and cond3
    except: return False

def is_pin_bar(ticker):
    """🏹 아래꼬리 핀바 확인 (바닥 지지력 확인)"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute15", count=1)
        o, h, l, c = df.iloc[-1][['open', 'high', 'low', 'close']]
        body = abs(c - o)
        lower_tail = min(o, c) - l
        # 몸통 대비 아래꼬리가 2배 이상 길고 캔들 전체의 50% 이상일 때
        return lower_tail > (body * 2) and lower_tail > (h - l) * 0.5
    except: return False

def get_structural_stop(ticker):
    """🏹 직전 저점 기반 구조적 손절가 산출"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute5", count=4)
        if df is None or len(df) < 4: return 0
        return df['low'].iloc[-4:-1].min()
    except: return 0

# -------------------------------------------------------------
# 🕸️ GRID 엔진 전용 필터
# -------------------------------------------------------------

def get_grid_suitability_score(ticker):
    """🕸️ 그리드 적합 점수 (횡보성↑ 변동성↑)"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=30)
        if df is None or df.empty: return 0
        
        # 박스권 높이 (낮을수록 횡보)
        high_low_range = (df['high'].max() - df['low'].min()) / df['close'].iloc[-1]
        # ATR % (높을수록 횡보 안에서 움직임 활발)
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
        atr_pct = (tr.rolling(window=14).mean().iloc[-1] / df['close'].iloc[-1]) * 100
        
        # 공식: 변동성을 박스권 높이로 나눔 (좁은 박스 안에서 요동치는 종목)
        score = (1 / (high_low_range + 0.01)) * atr_pct
        return score
    except: return 0

def get_grid_step(ticker):
    """🕸️ 그리드 간격(Step) 계산"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=20)
        if df is None: return 0
        return get_atr(df, 14) * 0.5
    except: return 0

# -------------------------------------------------------------
# 🌍 시장 레지메 (Market Regime)
# -------------------------------------------------------------

def get_market_regime(current_regime):
    """🌍 전체 시장 상황 판단 (SUPER_BULL ~ ICE_AGE)"""
    try:
        tickers = pyupbit.get_tickers(fiat="KRW")
        risk_score = 0
        
        # 비트코인/이더리움 이평선 정배열 확인
        btc_df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=6)
        eth_df = pyupbit.get_ohlcv("KRW-ETH", interval="day", count=6)
        
        if btc_df is not None and pyupbit.get_current_price("KRW-BTC") < btc_df['close'].rolling(5).mean().iloc[-1]: risk_score += 25
        if eth_df is not None and pyupbit.get_current_price("KRW-ETH") < eth_df['close'].rolling(5).mean().iloc[-1]: risk_score += 25
        
        # 상위 30종목 정배열 비율 (마켓 브레스)
        uptrend_count = 0
        for t in tickers[:30]:
            df = pyupbit.get_ohlcv(t, interval="day", count=21)
            if df is not None and pyupbit.get_current_price(t) >= df['close'].mean():
                uptrend_count += 1
        
        breadth = (uptrend_count / 30) * 100
        if breadth < 40: risk_score += 30
        elif breadth > 70: risk_score -= 10
        
        # 리스크 점수 기반 레지메 결정
        if risk_score <= 15: return "SUPER_BULL"
        elif risk_score <= 50: return "NORMAL"
        elif risk_score <= 80: return "CAUTION"
        else: return "ICE_AGE"
    except: return current_regime