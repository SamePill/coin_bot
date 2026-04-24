#!/bin/bash

# ==========================================================
# [Aegis-Elite V17.19] 단일 컨테이너 통합 관리 시스템
# ==========================================================

# --- [0. .env 환경 변수 로드] ---
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
else
    echo "----------------------------------------------------"
    echo "!!! 오류: .env 파일이 존재하지 않습니다."
    echo "----------------------------------------------------"
    exit 1
fi

# Git 인증 URL
REPO_URL="https://${GH_USER}:${GH_TOKEN}@github.com/${GH_USER}/${GH_REPO}.git"


# --- [2. 안내 문구 함수] ---
show_usage() {
    echo ""
    echo "===================================================="
    echo " 🛠️   Aegis V17 통합 관리 명령 가이드"
    echo "----------------------------------------------------"
    echo " 1. 배포/업데이트"
    echo "    ./manage.sh update      : 소스 최신화 및 통합 봇 재기동"
    echo ""
    echo " 2. 모니터링"
    echo "    ./manage.sh report      : 💰 통합 수익 및 지갑 상태 조회"
    echo "    ./manage.sh logs        : 봇 전체 실시간 로그 확인"
    echo ""
    echo " 3. 시스템 제어 (엔진 개별 제어는 텔레그램 /pause, /resume 사용)"
    echo "    ./manage.sh start       : 통합 봇 시작"
    echo "    ./manage.sh stop        : 통합 봇 전체 종료 (DB 포함)"
    echo "    ./manage.sh restart     : 통합 봇 재시작"
    echo ""
    echo " 4. 기타 도구"
    echo "    ./manage.sh status      : 컨테이너 실행 상태 확인"
    echo "    ./manage.sh db          : MariaDB 직접 접속"
    echo "===================================================="
}

# --- [3. 명령어 처리] ---
case "$1" in
    update)
        echo "🚀 [STEP 1] 소스코드 최신화 ($BRANCH)..."
        git remote set-url origin "$REPO_URL"
        git fetch origin "$BRANCH"
        git reset --hard "origin/$BRANCH"

        echo "🐳 [STEP 2] 통합 엔진($ENABLED_ENGINES) 배포 시작..."
        # 단일 컨테이너 모드로 전체 빌드 및 실행
        docker-compose up -d --build --remove-orphans

        echo "🧹 [STEP 3] 시스템 정리..."
        docker image prune -f
        echo "✅ 배포 완료!"
        show_usage
        ;;

    report)
        # 어떤 엔진 컨테이너가 켜져있든 하나를 골라 내부의 cli_tool 실행
        ACTIVE_ONE=$(docker ps --format '{{.Names}}' | grep aegis | head -n 1)
        if [ -z "$ACTIVE_ONE" ]; then
            echo "❌ 실행 중인 엔진이 없습니다."
        else
            docker exec -it $ACTIVE_ONE python cli_tool.py
        fi
        ;;

    logs)
        docker-compose logs -f --tail 50
        ;;

    start)
        docker-compose up -d
        echo "▶️ 통합 봇 컨테이너를 기동했습니다."
        ;;

    stop)
        docker-compose down
        echo "🛑 전체 시스템이 종료되었습니다."
        ;;

    restart)
        docker-compose restart
        echo "🔄 재시작 완료."
        ;;

    status)
        docker-compose ps
        show_usage
        ;;

    db)
        echo "🗄️  DB 접속 (exit로 종료)"
        docker exec -it aegis_db mariadb -u root -p"${DB_PASSWORD}" "${DB_NAME}"
        ;;

    *)
        show_usage
        exit 1
esac
