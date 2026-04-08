import pymysql
import pyupbit
from config import DB_CONF

def log_shadow_trade(market, reason, target_p, skip_p):
    """💡 섀도우 로깅: 매수 포기 순간을 기록합니다."""
    try:
        conn = pymysql.connect(**DB_CONF)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO shadow_logs (market, reason, target_price, skip_price) VALUES (%s, %s, %s, %s)",
                        (market, reason, target_p, skip_p))
        conn.commit(); conn.close()
    except: pass

def update_shadow_followup():
    """💡 24시간 후 가격 추적 업데이트"""
    try:
        conn = pymysql.connect(**DB_CONF, cursorclass=pymysql.cursors.DictCursor)
        with conn.cursor() as cur:
            cur.execute("SELECT id, market FROM shadow_logs WHERE after_24h_price IS NULL AND created_at < NOW() - INTERVAL 24 HOUR")
            rows = cur.fetchall()
            for row in rows:
                curr_p = pyupbit.get_current_price(row['market'])
                if curr_p:
                    cur.execute("UPDATE shadow_logs SET after_24h_price = %s WHERE id = %s", (curr_p, row['id']))
        conn.commit(); conn.close()
    except: pass

def generate_performance_report(days):
    """💡 성과 분석 및 제언 레포트 생성"""
    try:
        conn = pymysql.connect(**DB_CONF, cursorclass=pymysql.cursors.DictCursor)
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT reason, COUNT(*) as total,
                       SUM(CASE WHEN after_24h_price < skip_price THEN 1 ELSE 0 END) as good_skips,
                       SUM(CASE WHEN after_24h_price > skip_price THEN 1 ELSE 0 END) as missed_opps
                FROM shadow_logs 
                WHERE created_at > NOW() - INTERVAL %s DAY AND after_24h_price IS NULL = FALSE
                GROUP BY reason
            """, (days,))
            stats = cur.fetchall()
        conn.close()
        
        if not stats or all(s['total'] == 0 for s in stats): 
            return f"- {days}일간 분석할 데이터가 없습니다.\n"
        
        report = f"📈 [{days}일간 필터 성능]\n"
        for s in stats:
            if s['total'] == 0: continue
            accuracy = (s['good_skips'] / s['total']) * 100
            report += f"🔍 {s['reason']}: 방어 {accuracy:.1f}% ({s['good_skips']}/{s['total']})\n"
            if s['reason'] == 'VOLUME_LOW' and accuracy < 40: report += " ⚠️ 제언: 거래량 필터 완화 필요\n"
            if s['reason'] == 'ORDERBOOK_IMBALANCE' and accuracy < 40: report += " ⚠️ 제언: 호가창 불균형 필터 완화 필요\n"
        return report
    except: return "분석 오류\n"

def cleanup_old_shadow_logs(retention_days=30):
    """💡 DB 최적화: 설정된 기간이 지난 섀도우 로그 자동 삭제"""
    try:
        conn = pymysql.connect(**DB_CONF)
        with conn.cursor() as cur:
            # 삭제 대상 개수 확인
            cur.execute("SELECT COUNT(*) FROM shadow_logs WHERE created_at < NOW() - INTERVAL %s DAY", (retention_days,))
            count = cur.fetchone()[0]
            
            if count > 0:
                cur.execute("DELETE FROM shadow_logs WHERE created_at < NOW() - INTERVAL %s DAY", (retention_days,))
                conn.commit()
                print(f"🧹 [DB 최적화] {retention_days}일 경과한 섀도우 로그 {count}건을 삭제했습니다.")
        conn.close()
    except Exception as e:
        print(f"DB 최적화 오류: {e}")

