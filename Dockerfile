# 1. 뼈대: 가볍고 빠른 파이썬 3.11 슬림 버전 사용
FROM python:3.11-slim

# 2. 타임존 설정: 퀀트 봇의 생명줄인 한국 시간(KST) 강제 고정
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 3. 작업 공간 생성 및 이동
WORKDIR /app

# 4. 쇼핑 리스트(requirements.txt) 복사 및 패키지 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 봇 소스코드 전체를 도커 안으로 복사
COPY . .

# 6. 실행 명령: 출력 버퍼를 꺼서(-u) 로그가 터미널에 실시간으로 보이게 설정
CMD ["python", "-u", "main.py"]