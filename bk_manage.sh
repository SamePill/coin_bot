#!/bin/bash

# ==========================================================
# [Aegis-Elite V17.17] 엔진별 선택적 제어 통합 관리 시스템
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

# --- [1. 엔진 프로필 설정] ---
# .env의 ENABLED_ENGINES(예: core,hunter)를 읽어 도커 옵션 생성
IFS=',' read -ra ENGINES <<< "$ENABLED_ENGINES"
PROFILE_OPTS=""
for engine in "${ENGINES[@]}"; do
    PROFILE_OPTS="$PROFILE_OPTS --profile $engine"
done

# Git 인증 URL
REPO_URL="https://${GH_USER}:${GH_TOKEN}@github.com/${GH_USER}/${GH_REPO}.git"

# --- [2. 안내 문구 함수] ---
show_usage() {
    echo ""
    echo "===================================================="
    echo " 🛠️  Aegis V17 관리 명령 가이드"
    echo "----------------------------------------------------"
    echo " 1. 배포/업데이트"
    echo "    ./manage.sh update      : 활성 엔진($ENABLED_ENGINES) 빌드 및 재기동"
    echo ""
    echo " 2. 모니터링"
    echo "    ./manage.sh report      : 💰 통합 수익 및 지갑 상태 조회"
    echo "    ./manage.sh logs        : 전체 엔진 실시간 로그"
    echo "    ./manage.sh logs [엔진] : 특정 엔진(core|hunter|grid) 로그"
    echo ""
    echo " 3. 개별 엔진 제어"
    echo "    ./manage.sh start [엔진]  : 특정 엔진 즉시 가동"
    echo "    ./manage.sh stop [엔진]   : 특정 엔진 즉시 중지"
    echo "    ./manage.sh restart [엔진]: 특정 엔진만 재시작"
    echo ""
    echo " 4. 시스템 관리"
    echo "    ./manage.sh status      : 컨테이너 실행 상태 확인"
    echo "    ./manage.sh db          : MariaDB 직접 접속"
    echo "    ./manage.sh stop all    : 모든 컨테이너(DB 포함) 종료"
    echo "===================================================="
}

# --- [3. 명령어 처리] ---
case "$1" in
    update)
        echo "🚀 [STEP 1] 소스코드 최신화 ($BRANCH)..."
        git remote set-url origin "$REPO_URL"
        git fetch origin "$BRANCH"
        git reset --hard "origin/$BRANCH"

        echo "🐳 [STEP 2] 활성 엔진($ENABLED_ENGINES) 배포 시작..."
        # --remove-orphans: 설정에서 빠진 엔진은 자동으로 중지/삭제
        docker compose $PROFILE_OPTS up -d --build --remove-orphans
        
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
        if [ -n "$2" ]; then
            docker logs -f --tail 50 "aegis_$2"
        else
            docker compose $PROFILE_OPTS logs -f --tail 50
        fi
        ;;

    start)
        if [ -n "$2" ]; then
            docker compose --profile "$2" up -d
            echo "▶️ $2 엔진을 기동했습니다."
        else
            echo "❌ 사용법: ./manage.sh start {core|hunter|grid}"
        fi
        ;;

    stop)
        if [ "$2" == "all" ]; then
            docker compose $PROFILE_OPTS down
            echo "🛑 전체 시스템이 종료되었습니다."
        elif [ -n "$2" ]; then
            docker compose --profile "$2" stop
            echo "⏹️ $2 엔진을 중지했습니다."
        else
            echo "❌ 사용법: ./manage.sh stop {core|hunter|grid|all}"
        fi
        ;;

    restart)
        if [ -n "$2" ]; then
            docker compose restart "aegis_$2"
        else
            docker compose $PROFILE_OPTS restart
        fi
        echo "🔄 재시작 완료."
        ;;

    status)
        docker compose $PROFILE_OPTS ps
        show_usage
        ;;

    db)
        echo "🗄️ DB 접속 (exit로 종료)"
        docker exec -it aegis_db mariadb -u root -p"${DB_PASSWORD}" "${DB_NAME}"
        ;;

    *)
        show_usage
        exit 1
esac