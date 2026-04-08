import time
import pandas as pd
import numpy as np
import pyupbit
from config import VOLUME_SPIKE_RATIO
import pandas_ta_classic as ta # ADX 계산을 위해 pandas_ta 사용 권장 (없으면 pip install pandas_ta)

def get_adx(ticker):
    """💡 ADX(평균 방향성 지수) 계산 - 추세의 강도 측정"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=50)
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        return adx_df['ADX_14'].iloc[-1]
    except: return 0

def check_volume_spike(ticker):
    """💡 거래량 스파이크 확인 (최근 20봉 평균 대비 1.5배 이상)"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute15", count=21)
        avg_vol = df['volume'].iloc[:-1].mean()
        curr_vol = df['volume'].iloc[-1]
        return curr_vol >= (avg_vol * 1.5)
    except: return False

def is_pin_bar(ticker):
    """💡 아래꼬리(Pin Bar) 확인 - 매수 세력의 지지 확인"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute15", count=1)
        o, h, l, c = df.iloc[-1][['open', 'high', 'low', 'close']]
        body = abs(c - o)
        lower_tail = min(o, c) - l
        # 몸통보다 아래꼬리가 2배 이상 길 때 하단 지지로 판단
        return lower_tail > (body * 2) and lower_tail > (h - l) * 0.5
    except: return False

def calc_rsi(series, period=14):
    delta = series.diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=period-1, adjust=False).mean()
    ema_down = down.ewm(com=period-1, adjust=False).mean()
    return 100 - (100 / (1 + (ema_up / ema_down)))

def get_atr(df, period=5):
    try:
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
        return tr.rolling(window=period).mean().iloc[-1]
    except: return df['high'].iloc[-2] - df['low'].iloc[-2]


def check_panic_fall():
    """🚨 [DEFCON-1] 비트코인이 15분 내에 3.5% 이상 수직 낙하하는지 감지"""
    try:
        # 5분봉 3개를 가져와서 고점 대비 낙폭 계산
        df = pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=3)
        if df is None or len(df) < 3: return False
        
        highest = df['high'].max()
        current = df['close'].iloc[-1]
        fall_rate = (current - highest) / highest
        
        return fall_rate <= -0.035 # -3.5% 이하일 때 True
    except: return False


def get_grid_step(ticker):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=20)
        if not isinstance(df, pd.DataFrame) or df.empty: return 0 # 💡 API 방어막
        return get_atr(df, 14) * 0.5
    except: return 0

def get_ema200(ticker):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=210)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 200: return 0 # 💡 API 방어막
        return df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
    except: return 0

def check_btc_flash_crash():
    try:
        df = pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=4)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 4: return False # 💡 API 방어막
        return ((df['close'].iloc[-1] - df['high'].max()) / df['high'].max()) <= -0.015 
    except: return False

def check_orderbook_imbalance(ticker):
    try:
        ob = pyupbit.get_orderbook(ticker)
        if isinstance(ob, list) and len(ob) > 0: ob = ob[0]
        if not isinstance(ob, dict) or 'orderbook_units' not in ob: return False
        total_bid, total_ask = sum([u['bid_size'] for u in ob['orderbook_units']]), sum([u['ask_size'] for u in ob['orderbook_units']])
        return total_bid > (total_ask * 0.3)
    except: return False

def check_volume_spike(ticker):
    try: 
        df = pyupbit.get_ohlcv(ticker, interval="minute1", count=31)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 2: return False # 💡 API 방어막
        return df['volume'].iloc[-1] >= (df['volume'].iloc[:-1].mean() * VOLUME_SPIKE_RATIO)
    except: return False

def get_market_regime(current_regime):
    try:
        tickers = pyupbit.get_tickers(fiat="KRW")
        if not isinstance(tickers, list): return current_regime 
        risk_score = 0
        btc_df, eth_df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=6), pyupbit.get_ohlcv("KRW-ETH", interval="day", count=6)
        
        if isinstance(btc_df, pd.DataFrame) and len(btc_df) == 6 and pyupbit.get_current_price("KRW-BTC") < btc_df['close'].rolling(5).mean().iloc[-1]: risk_score += 25
        if isinstance(eth_df, pd.DataFrame) and len(eth_df) == 6 and pyupbit.get_current_price("KRW-ETH") < eth_df['close'].rolling(5).mean().iloc[-1]: risk_score += 25
        
        uptrend_count = sum(1 for t in tickers[:30] if pyupbit.get_current_price(t) >= (pyupbit.get_ohlcv(t, interval="day", count=21) if isinstance(pyupbit.get_ohlcv(t, interval="day", count=21), pd.DataFrame) else pd.DataFrame([{'close':float('inf')}]))['close'].mean())
        breadth = (uptrend_count / 30) * 100
        if breadth < 40: risk_score += 30
        elif breadth > 70: risk_score -= 10
        
        if current_regime == "SUPER_BULL" and risk_score <= 25: return "SUPER_BULL" 
        if current_regime == "NORMAL" and 10 <= risk_score <= 60: return "NORMAL"   
        if current_regime == "CAUTION" and 40 <= risk_score <= 85: return "CAUTION" 
        if risk_score <= 15: return "SUPER_BULL"
        elif risk_score <= 50: return "NORMAL"
        elif risk_score <= 80: return "CAUTION"
        else: return "ICE_AGE"
    except: return current_regime 

def check_core_momentum(ticker):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=40)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 40: return False # 💡 API 방어막
        macd = df['close'].ewm(span=12, adjust=False).mean() - df['close'].ewm(span=26, adjust=False).mean()
        hist = macd - macd.ewm(span=9, adjust=False).mean()
        obv = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
        return hist.iloc[-1] > 0 and obv.iloc[-1] > obv.rolling(10).mean().iloc[-1]
    except: return False

def check_keltner_breakout(ticker):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=25)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 25: return False # 💡 API 방어막
        tr = pd.concat([df['high'] - df['low'], (df['high'] - df['close'].shift(1)).abs(), (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
        return df['close'].iloc[-1] > (df['close'].ewm(span=20, adjust=False).mean() + (tr.ewm(span=20, adjust=False).mean() * 1.5)).iloc[-1]
    except: return False

def get_chandelier_exit(ticker, pos_peak_price, current_regime):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=20)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 20: return pos_peak_price * 0.95 # 💡 API 방어막
        multiplier = 3.0 if current_regime == "SUPER_BULL" else (1.5 if current_regime == "CAUTION" else 2.5)
        return pos_peak_price - (get_atr(df, 14) * multiplier)
    except: return pos_peak_price * 0.95

def check_hunter_dip_buy(ticker):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute5", count=150) 
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 144: return False # 💡 API 방어막
        df_session = df.tail(144)
        
        q = df_session['volume']
        p = (df_session['high'] + df_session['low'] + df_session['close']) / 3
        current_vwap = ((p * q).cumsum() / q.cumsum()).iloc[-1]
        
        curr_price = df_session['close'].iloc[-1]
        rsi = calc_rsi(df_session['close'], 14)
        
        if not (current_vwap * 0.975 <= curr_price <= current_vwap * 1.025): return False
        if not (rsi.iloc[-2] < 40 and rsi.iloc[-1] > rsi.iloc[-2]): return False
        if df_session['volume'].iloc[-1] <= df_session['volume'].iloc[-2]: return False
        
        return True
    except: return False

def get_structural_stop(ticker):
    try:
        df = pyupbit.get_ohlcv(ticker, interval="minute5", count=4)
        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < 4: return 0 # 💡 API 방어막
        return df['low'].iloc[-4:-1].min()
    except: return 0

def get_grid_suitability_score(ticker):
    """💡 그리드 적합 점수 계산 (높을수록 좋음)
       공식: (1 / ADX) * ATR_Percent
       즉, 추세는 낮고(횡보) 변동성은 큰 종목을 찾습니다.
    """
    try:
        # 최근 30시간의 데이터를 분석
        df = pyupbit.get_ohlcv(ticker, interval="minute60", count=30)
        if not isinstance(df, pd.DataFrame) or df.empty: return 0
        
        # 1. ADX 대용 지표 (추세 강도) 계산
        # 최근 고가/저가 채널 폭이 좁을수록 횡보로 간주
        high_low_range = (df['high'].max() - df['low'].min()) / df['close'].iloc[-1]
        
        # 2. ATR (변동성) 계산
        tr = pd.concat([df['high'] - df['low'], 
                        (df['high'] - df['close'].shift(1)).abs(), 
                        (df['low'] - df['close'].shift(1)).abs()], axis=1).max(axis=1)
        atr_pct = (tr.rolling(window=14).mean().iloc[-1] / df['close'].iloc[-1]) * 100
        
        # 3. 횡보 점수 (박스권 안에서 움직임이 활발한 종목 선정)
        score = (1 / (high_low_range + 0.01)) * atr_pct
        return score
    except: return 0
