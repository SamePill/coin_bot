# 서버정보
1번 인스턴스
/dockerspace/coin_bot/manage.sh 
/dockerspace/coin_bot/.env

위 2개의 파일을 통해 깃에서 소스코드를 다운받아 도커로 실행함.

--도커와 디비 연결을 위한 방화벽 열기
# 1. 3306 포트 허용 규칙 추가
sudo firewall-cmd --permanent --add-port=3306/tcp

# 2. 방화벽 설정 리로드 (즉시 적용)
sudo firewall-cmd --reload

# 3. 규칙이 잘 들어갔는지 확인 (ports 항목에 3306/tcp가 있어야 함)
sudo firewall-cmd --list-all


-- 기존 계정의 접속 허용 범위를 전체(%)로 확장하거나 새로 생성
CREATE USER 'coin_user'@'%' IDENTIFIED BY '패스워드..1!!!';
GRANT ALL PRIVILEGES ON coin_bot_db.* TO 'coin_user'@'%';
FLUSH PRIVILEGES;