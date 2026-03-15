#!/bin/bash
# ============================================
# Запуск Python Agent для сбора метрик
# ============================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}   📊 Workstation Metrics Agent${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Проверка Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python 3 not found. Please install Python 3.8+${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo -e "${GREEN}✓ Python ${PYTHON_VERSION} found${NC}"

# Создаём виртуальное окружение если нет
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

# Активируем виртуальное окружение
source venv/bin/activate

# Устанавливаем зависимости
echo -e "${YELLOW}Installing dependencies...${NC}"
pip install --quiet --upgrade pip
pip install --quiet -r agent/requirements.txt

# Конфигурация
export OTEL_COLLECTOR_ENDPOINT="${OTEL_COLLECTOR_ENDPOINT:-localhost:4317}"
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-workstation-$(hostname)}"
export COLLECTION_INTERVAL="${COLLECTION_INTERVAL:-15}"

# PostgreSQL
export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export POSTGRES_USER="${POSTGRES_USER:-postgres}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
export POSTGRES_DB="${POSTGRES_DB:-postgres}"

echo ""
echo -e "${BLUE}Configuration:${NC}"
echo -e "  OTel Collector: ${GREEN}${OTEL_COLLECTOR_ENDPOINT}${NC}"
echo -e "  Service Name:   ${GREEN}${OTEL_SERVICE_NAME}${NC}"
echo -e "  Interval:       ${GREEN}${COLLECTION_INTERVAL}s${NC}"
echo -e "  PostgreSQL:     ${GREEN}${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}${NC}"
echo ""

# Запуск агента
echo -e "${GREEN}🚀 Starting agent...${NC}"
echo ""

python3 agent/workstation_agent.py
