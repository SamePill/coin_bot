import os
import requests
from dotenv import load_dotenv

load_dotenv()

# API & DB 키 설정
UPBIT_ACCESS = os.getenv("UPBIT_ACCESS_KEY")
UPBIT_SECRET = os.getenv("UPBIT_SECRET_KEY")
TEL_TOKEN = os.getenv("TELEGRAM_TOKEN")
TEL_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DB_CONF = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "db": os.getenv("DB_NAME")
}

# 💡 [V17.5 바벨 전략] 포트폴리오 슬롯 재분배 (2 : 1 : 1)
MAX_BOT_BUDGET = 250000      
CORE_SLOTS, HUNTER_SLOTS, GRID_SLOTS = 1, 1, 2               
TOTAL_SLOTS = CORE_SLOTS + HUNTER_SLOTS + GRID_SLOTS

# [config.py 내부에 추가/수정]
# GRID_TICKER = "KRW-ETH" # (기존 단일 코인 방식 - 삭제 또는 주석)
# 스마트 그리드 유동적 사냥터 풀 (V17.14)
#GRID_POOL = ["KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-DOGE", "KRW-LINK"]
# 💡 스마트 그리드 유동적 사냥터 풀 (V17.17 확장판 - 총 15종목)
GRID_POOL = [
    "KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-XRP", "KRW-DOGE", # 기존 및 대장주
    "KRW-LINK", "KRW-NEAR", "KRW-SUI", "KRW-AVAX", "KRW-APT", # 고성능 메인넷 및 오라클
    "KRW-STX", "KRW-ARB", "KRW-OP",                           # 인프라 & L2
    "KRW-ADA", "KRW-SHIB"                                     # 클래식 & 밈 볼륨
]

VOLUME_SPIKE_RATIO = 3.0       

CORE_UNIVERSE = [
    "KRW-BTC", "KRW-SOL", "KRW-XRP", "KRW-ADA", "KRW-AVAX", 
    "KRW-LINK", "KRW-DOT", "KRW-DOGE", "KRW-MATIC", "KRW-STX", 
    "KRW-NEAR", "KRW-ARB"
]

CORE_TP_TRIGGER, CORE_SL_TRIGGER, CORE_HARD_STOP, SCALE_OUT_RATIO = 0.035, 0.030, 0.050, 0.5
HUNTER_TRAIL_START, HUNTER_TRAIL_DROP, HUNTER_SL = 0.020, 0.015, 0.020 

REGIME_SETTINGS = {
    "SUPER_BULL": {"ratio": 1.0, "desc": "☀️ 슈퍼 불장"},
    "NORMAL":     {"ratio": 1.0, "desc": "🌤️ 일반 상승장"},
    "CAUTION":    {"ratio": 0.5, "desc": "🌧️ 하락 경계장"},
    "ICE_AGE":    {"ratio": 0.0, "desc": "⛈️ 빙하기"}
}


# 💡 [추가] 텔레그램 매매 알림 On/Off 스위치 (.env 파일에서 ENABLE_TRADE_NOTI=False 로 끌 수 있음)
ENABLE_TRADE_NOTI = os.getenv("ENABLE_TRADE_NOTI", "True").lower() == "true"

def send_telegram(message):
    try: requests.get(f"https://api.telegram.org/bot{TEL_TOKEN}/sendMessage", params={"chat_id": TEL_CHAT_ID, "text": message})
    except: pass
