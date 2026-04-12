from abc import ABC, abstractmethod

class BaseEngine(ABC):
    """
    모든 매매 엔진의 뼈대가 되는 추상 베이스 클래스입니다.
    """
    def __init__(self, upbit, bot_positions, bot_positions_lock):
        self.upbit = upbit
        self.bot_positions = bot_positions
        self.bot_positions_lock = bot_positions_lock

    @abstractmethod
    def run(self, now, *args, **kwargs):
        """
        각 엔진은 이 메서드를 오버라이드하여 핵심 매매 로직을 구현해야 합니다.
        """
        pass

    def get_safe_balances(self):
        """업비트 잔고를 안전하게 딕셔너리 형태로 가져옵니다."""
        balances = self.upbit.get_balances()
        if isinstance(balances, list):
            return {b['currency']: float(b['balance']) for b in balances}
        return {}