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

# --- [1.5 도커 컴포즈 env_file 유효성 검사 함수] ---
check_env_files() {
    if [ -f "docker-compose.yml" ]; then
        # 주석(#) 처리되지 않은 활성 컨테이너의 env_file 항목만 추출
        ENV_FILES=$(grep -v '^[[:space:]]*#' docker-compose.yml | grep 'env_file:' | awk '{print $2}')
        for env_file in $ENV_FILES; do
            if [ ! -f "$env_file" ]; then
                echo "----------------------------------------------------"
                echo "❌ 오류: docker-compose.yml에 지정된 환경변수 파일($env_file)이 존재하지 않습니다!"
                echo "👉 봇을 기동하기 전에 해당 파일을 생성해 주세요. (예: cp .env $env_file)"
                echo "----------------------------------------------------"
                exit 1
            fi
        done
    fi
}

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
    echo "    ./manage.sh logs [이름] : 실시간 로그 확인 (전체 또는 특정 봇)"
    echo "      * 예: ./manage.sh logs (전체) / ./manage.sh logs bot_account_1 (특정 봇)"
    echo ""
    echo " 3. 시스템 제어 (엔진 개별 제어는 텔레그램 /pause, /resume 사용)"
    echo "    ./manage.sh start [봇이름]   : 시스템 기동 (특정 봇만 기동 가능)"
    echo "    ./manage.sh stop [봇이름]    : 시스템 종료 (특정 봇만 종료 가능)"
    echo "    ./manage.sh restart [봇이름] : 봇 재시작 (환경변수 수정 후 적용 시 사용)"
    echo "      * 예: ./manage.sh restart bot_account_1"
    echo ""
    echo " 4. 기타 도구"
    echo "    ./manage.sh status      : 컨테이너 실행 상태 확인"
    echo "    ./manage.sh db          : MariaDB 직접 접속"
    echo "    ./manage.sh redis       : Redis 캐시 직접 접속"
    echo ""
    echo " 5. 도움말"
    echo "    ./manage.sh help        : 이 도움말 출력"
    echo "===================================================="
}

# --- [3. 명령어 처리] ---
case "$1" in
    update)
        echo "🚀 [STEP 1] 소스코드 최신화 ($BRANCH)..."
        git remote set-url origin "$REPO_URL"
        git fetch origin "$BRANCH"
        git reset --hard "origin/$BRANCH"

        echo "🔄 [STEP 1.5] 관리 스크립트(manage.sh) 갱신 및 실행 권한 부여..."
        if [ -f "bk_manage.sh" ]; then
            cp -f bk_manage.sh manage.sh
            chmod +x manage.sh
        fi

        echo "🚀 [STEP 2] 통합 엔진 배포 시작..."
        check_env_files
        # 단일 컨테이너 모드로 전체 빌드 및 실행
        docker-compose up -d --build --remove-orphans

        echo "🧹 [STEP 3] 시스템 정리..."
        docker image prune -f
        echo "✅ 배포 완료!"
        show_usage
        ;;

    report)
        # 💡 실행 중인 모든 봇 컨테이너를 찾아 각각 리포트를 출력합니다.
        ACTIVE_BOTS=$(docker ps --format '{{.Names}}' | grep aegis_bot)
        if [ -z "$ACTIVE_BOTS" ]; then
            echo "❌ 실행 중인 엔진이 없습니다."
        else
            for bot in $ACTIVE_BOTS; do
                echo "📊 [$bot] 계정 리포트 조회..."
                docker exec -it $bot python cli_tool.py
                echo ""
            done
        fi
        ;;

    logs)
        # 💡 특정 봇 로그만 볼 수 있도록 개선 (예: ./manage.sh logs bot_account_1)
        if [ -z "$2" ]; then
            echo "📋 전체 시스템의 실시간 로그를 출력합니다. (종료: Ctrl+C)"
            docker-compose logs -f --tail 50
        else
            echo "📋 [$2] 컨테이너의 실시간 로그를 출력합니다. (종료: Ctrl+C)"
            docker-compose logs -f --tail 50 "$2"
        fi
        ;;

    start)
        if [ -z "$2" ]; then
            check_env_files
            docker-compose up -d
            echo "▶️ 통합 봇 전체 시스템을 기동했습니다."
        else
            # 특정 봇 지정 시 특정 봇의 env_file 존재 여부는 docker-compose 자체가 에러를 뱉어주므로
            # 전체 통합 검사를 한 번 수행하는 것으로 충분합니다.
            check_env_files
            docker-compose up -d "$2"
            echo "▶️ [$2] 컨테이너를 기동했습니다."
        fi
        ;;

    stop)
        if [ -z "$2" ]; then
            docker-compose down
            echo "🛑 전체 시스템이 종료되었습니다."
        else
            docker-compose stop "$2"
            echo "🛑 [$2] 컨테이너가 종료되었습니다."
        fi
        ;;

    restart)
        if [ -z "$2" ]; then
            check_env_files
            docker-compose restart
            echo "🔄 전체 시스템 재시작 완료."
        else
            check_env_files
            # 💡 특정 컨테이너 환경변수 변경을 적용하기 위해 up -d 명령을 사용합니다.
            docker-compose up -d "$2"
            echo "🔄 [$2] 재시작 및 환경변수 반영 완료."
        fi
        ;;

    status)
        docker-compose ps
        show_usage
        ;;

    db)
        echo "🗄️  DB 접속 (exit로 종료)"
        docker exec -it aegis_db mariadb -u root -p"${DB_PASSWORD}" "${DB_NAME}"
        ;;

    redis)
        echo "🔴 Redis 캐시 접속 (exit로 종료)"
        docker exec -it aegis_redis redis-cli
        ;;

    help)
        show_usage
        ;;

    *)
        show_usage
        exit 1
esac
