#!/usr/bin/env python3
"""
Python Agent для сбора метрик с локального компьютера
- Системные метрики (CPU, RAM, Disk, Network)
- PostgreSQL метрики (connections, queries, size)
- Claude Code токены (если доступно)
- Кастомные метрики

Отправляет в OTel Collector → Prometheus → Grafana
"""

import os
import sys
import time
import json
import socket
import logging
import platform
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Iterable, Optional, Dict, Any
from dataclasses import dataclass

import psutil

# OpenTelemetry
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Observation, CallbackOptions

# PostgreSQL
try:
    import psycopg2
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False
    print("⚠️  psycopg2 not installed. PostgreSQL metrics disabled.")

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

@dataclass
class AgentConfig:
    # OTel Collector
    collector_endpoint: str = os.getenv("OTEL_COLLECTOR_ENDPOINT", "localhost:4317")
    service_name: str = os.getenv("OTEL_SERVICE_NAME", f"workstation-{socket.gethostname()}")

    # Интервалы сбора
    collection_interval_sec: int = int(os.getenv("COLLECTION_INTERVAL", "15"))

    # PostgreSQL
    pg_host: str = os.getenv("POSTGRES_HOST", "localhost")
    pg_port: int = int(os.getenv("POSTGRES_PORT", "5432"))
    pg_user: str = os.getenv("POSTGRES_USER", "postgres")
    pg_password: str = os.getenv("POSTGRES_PASSWORD", "")
    pg_database: str = os.getenv("POSTGRES_DB", "postgres")

    # Claude Code
    claude_config_path: str = os.getenv(
        "CLAUDE_CONFIG_PATH",
        str(Path.home() / ".claude" / "config.json")
    )
    claude_usage_path: str = os.getenv(
        "CLAUDE_USAGE_PATH",
        str(Path.home() / ".claude" / "usage.json")
    )


config = AgentConfig()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# OPENTELEMETRY SETUP
# ============================================================

def setup_otel() -> metrics.Meter:
    """Инициализация OpenTelemetry SDK"""

    resource = Resource.create({
        SERVICE_NAME: config.service_name,
        "host.name": socket.gethostname(),
        "host.arch": platform.machine(),
        "os.type": platform.system().lower(),
        "os.version": platform.release(),
        "agent.type": "python-workstation-agent",
        "agent.version": "2.0.0",
    })

    exporter = OTLPMetricExporter(
        endpoint=config.collector_endpoint,
        insecure=True
    )

    reader = PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=config.collection_interval_sec * 1000
    )

    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)

    logger.info(f"✅ OTel initialized: {config.service_name} -> {config.collector_endpoint}")

    return metrics.get_meter("workstation-agent", "2.0.0")


# ============================================================
# SYSTEM METRICS COLLECTOR
# ============================================================

class SystemMetricsCollector:
    """Сбор системных метрик"""

    def cpu_utilization(self, options: CallbackOptions) -> Iterable[Observation]:
        """CPU по ядрам и total"""
        per_cpu = psutil.cpu_percent(percpu=True)
        for i, pct in enumerate(per_cpu):
            yield Observation(pct / 100.0, {"cpu": str(i)})
        yield Observation(psutil.cpu_percent() / 100.0, {"cpu": "total"})

    def cpu_frequency(self, options: CallbackOptions) -> Iterable[Observation]:
        freq = psutil.cpu_freq()
        if freq:
            yield Observation(freq.current, {"type": "current"})
            yield Observation(freq.max, {"type": "max"})

    def load_average(self, options: CallbackOptions) -> Iterable[Observation]:
        if hasattr(psutil, 'getloadavg'):
            load1, load5, load15 = psutil.getloadavg()
            yield Observation(load1, {"period": "1m"})
            yield Observation(load5, {"period": "5m"})
            yield Observation(load15, {"period": "15m"})

    def memory_utilization(self, options: CallbackOptions) -> Iterable[Observation]:
        mem = psutil.virtual_memory()
        yield Observation(mem.percent / 100.0, {"state": "used"})
        yield Observation(mem.available / mem.total, {"state": "available"})

    def memory_bytes(self, options: CallbackOptions) -> Iterable[Observation]:
        mem = psutil.virtual_memory()
        yield Observation(mem.total, {"state": "total"})
        yield Observation(mem.used, {"state": "used"})
        yield Observation(mem.available, {"state": "available"})
        yield Observation(mem.cached if hasattr(mem, 'cached') else 0, {"state": "cached"})

    def swap_utilization(self, options: CallbackOptions) -> Iterable[Observation]:
        swap = psutil.swap_memory()
        if swap.total > 0:
            yield Observation(swap.percent / 100.0, {})

    def disk_utilization(self, options: CallbackOptions) -> Iterable[Observation]:
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                yield Observation(
                    usage.percent / 100.0,
                    {"mountpoint": part.mountpoint, "device": part.device}
                )
            except (PermissionError, OSError):
                continue

    def disk_io(self, options: CallbackOptions) -> Iterable[Observation]:
        io = psutil.disk_io_counters()
        if io:
            yield Observation(io.read_bytes, {"direction": "read"})
            yield Observation(io.write_bytes, {"direction": "write"})

    def network_io(self, options: CallbackOptions) -> Iterable[Observation]:
        for iface, stats in psutil.net_io_counters(pernic=True).items():
            if iface.startswith(('lo', 'docker', 'br-', 'veth', 'vmnet')):
                continue
            yield Observation(stats.bytes_sent, {"interface": iface, "direction": "sent"})
            yield Observation(stats.bytes_recv, {"interface": iface, "direction": "recv"})

    def network_connections(self, options: CallbackOptions) -> Iterable[Observation]:
        try:
            conns = psutil.net_connections(kind='inet')
            states = {}
            for conn in conns:
                state = conn.status
                states[state] = states.get(state, 0) + 1
            for state, count in states.items():
                yield Observation(count, {"state": state})
        except (psutil.AccessDenied, PermissionError):
            pass

    def process_count(self, options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(len(psutil.pids()), {})

    def top_processes_cpu(self, options: CallbackOptions) -> Iterable[Observation]:
        procs = []
        for p in psutil.process_iter(['name', 'cpu_percent']):
            try:
                procs.append((p.info['name'], p.info['cpu_percent'] or 0))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: x[1], reverse=True)
        for name, cpu in procs[:5]:
            if cpu > 0:
                yield Observation(cpu, {"process": name[:50]})

    def top_processes_memory(self, options: CallbackOptions) -> Iterable[Observation]:
        procs = []
        for p in psutil.process_iter(['name', 'memory_percent']):
            try:
                procs.append((p.info['name'], p.info['memory_percent'] or 0))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: x[1], reverse=True)
        for name, mem in procs[:5]:
            if mem > 0:
                yield Observation(mem, {"process": name[:50]})

    def battery_percent(self, options: CallbackOptions) -> Iterable[Observation]:
        battery = psutil.sensors_battery()
        if battery:
            yield Observation(
                battery.percent / 100.0,
                {"plugged": str(battery.power_plugged)}
            )


# ============================================================
# POSTGRESQL METRICS COLLECTOR
# ============================================================

class PostgreSQLMetricsCollector:
    """Сбор метрик PostgreSQL"""

    def __init__(self):
        self.conn = None
        self._connect()

    def _connect(self):
        """Подключение к PostgreSQL"""
        if not HAS_PSYCOPG2:
            return

        try:
            self.conn = psycopg2.connect(
                host=config.pg_host,
                port=config.pg_port,
                user=config.pg_user,
                password=config.pg_password,
                database=config.pg_database,
                connect_timeout=5
            )
            self.conn.autocommit = True
            logger.info(f"✅ Connected to PostgreSQL: {config.pg_host}:{config.pg_port}")
        except Exception as e:
            logger.warning(f"⚠️  PostgreSQL connection failed: {e}")
            self.conn = None

    def _query(self, sql: str) -> list:
        """Выполнение запроса"""
        if not self.conn:
            self._connect()
        if not self.conn:
            return []

        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchall()
        except Exception as e:
            logger.error(f"PostgreSQL query error: {e}")
            self.conn = None
            return []

    def connection_count(self, options: CallbackOptions) -> Iterable[Observation]:
        """Количество подключений"""
        rows = self._query("""
            SELECT state, count(*)
            FROM pg_stat_activity
            WHERE state IS NOT NULL
            GROUP BY state
        """)
        for state, count in rows:
            yield Observation(count, {"state": state})

    def database_size(self, options: CallbackOptions) -> Iterable[Observation]:
        """Размер баз данных"""
        rows = self._query("""
            SELECT datname, pg_database_size(datname) as size
            FROM pg_database
            WHERE datistemplate = false
        """)
        for dbname, size in rows:
            yield Observation(size, {"database": dbname})

    def table_count(self, options: CallbackOptions) -> Iterable[Observation]:
        """Количество таблиц"""
        rows = self._query("""
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
        if rows:
            yield Observation(rows[0][0], {})

    def transactions(self, options: CallbackOptions) -> Iterable[Observation]:
        """Транзакции: commits и rollbacks"""
        rows = self._query("""
            SELECT datname, xact_commit, xact_rollback
            FROM pg_stat_database
            WHERE datname = current_database()
        """)
        for dbname, commits, rollbacks in rows:
            yield Observation(commits, {"database": dbname, "type": "commit"})
            yield Observation(rollbacks, {"database": dbname, "type": "rollback"})

    def cache_hit_ratio(self, options: CallbackOptions) -> Iterable[Observation]:
        """Cache hit ratio"""
        rows = self._query("""
            SELECT
                CASE WHEN blks_hit + blks_read = 0 THEN 0
                     ELSE blks_hit::float / (blks_hit + blks_read)
                END as ratio
            FROM pg_stat_database
            WHERE datname = current_database()
        """)
        if rows and rows[0][0] is not None:
            yield Observation(rows[0][0], {})

    def deadlocks(self, options: CallbackOptions) -> Iterable[Observation]:
        """Количество deadlocks"""
        rows = self._query("""
            SELECT deadlocks FROM pg_stat_database
            WHERE datname = current_database()
        """)
        if rows:
            yield Observation(rows[0][0], {})

    def slow_queries(self, options: CallbackOptions) -> Iterable[Observation]:
        """Количество медленных запросов (> 1 сек)"""
        rows = self._query("""
            SELECT count(*) FROM pg_stat_activity
            WHERE state = 'active'
            AND now() - query_start > interval '1 second'
        """)
        if rows:
            yield Observation(rows[0][0], {})

    def replication_lag(self, options: CallbackOptions) -> Iterable[Observation]:
        """Лаг репликации (если есть)"""
        rows = self._query("""
            SELECT
                client_addr,
                EXTRACT(EPOCH FROM (now() - sent_lsn::text::pg_lsn - replay_lsn::text::pg_lsn))
            FROM pg_stat_replication
        """)
        for addr, lag in rows:
            if lag is not None:
                yield Observation(lag, {"replica": str(addr)})


# ============================================================
# CLAUDE CODE METRICS COLLECTOR
# ============================================================

class ClaudeCodeMetricsCollector:
    """
    Сбор метрик использования Claude Code

    Claude Code хранит данные в:
    - ~/.claude/config.json — конфигурация
    - ~/.claude/projects/ — проекты и история
    - ~/.claude.json — глобальные настройки

    Токены можно отслеживать через:
    1. Логи Claude Code (если включены)
    2. API usage (если доступен)
    3. Парсинг истории сессий
    """

    def __init__(self):
        self.config_path = Path(config.claude_config_path)
        self.usage_path = Path(config.claude_usage_path)
        self.projects_path = Path.home() / ".claude" / "projects"
        self.daily_tokens = {}
        self._last_scan = None

    def _scan_usage(self):
        """Сканирование использования токенов"""
        now = datetime.now()

        # Не сканируем чаще раза в минуту
        if self._last_scan and (now - self._last_scan).seconds < 60:
            return

        self._last_scan = now
        today = now.strftime("%Y-%m-%d")

        # Пробуем разные источники данных

        # 1. Проверяем usage.json (если Claude Code его создаёт)
        if self.usage_path.exists():
            try:
                with open(self.usage_path, 'r') as f:
                    usage_data = json.load(f)
                    if 'daily_tokens' in usage_data:
                        self.daily_tokens = usage_data['daily_tokens']
                        return
            except (json.JSONDecodeError, IOError) as e:
                logger.debug(f"Could not read usage.json: {e}")

        # 2. Сканируем проекты Claude Code
        if self.projects_path.exists():
            total_tokens = 0
            sessions_today = 0

            for project_dir in self.projects_path.iterdir():
                if not project_dir.is_dir():
                    continue

                # Проверяем файлы сессий
                for session_file in project_dir.glob("*.json"):
                    try:
                        stat = session_file.stat()
                        file_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")

                        if file_date == today:
                            sessions_today += 1

                            # Пробуем извлечь информацию о токенах
                            with open(session_file, 'r') as f:
                                session_data = json.load(f)

                                # Структура зависит от версии Claude Code
                                if isinstance(session_data, dict):
                                    # Пробуем разные поля
                                    tokens = (
                                        session_data.get('total_tokens', 0) or
                                        session_data.get('tokens_used', 0) or
                                        session_data.get('usage', {}).get('total_tokens', 0)
                                    )
                                    total_tokens += tokens

                                elif isinstance(session_data, list):
                                    # Если это список сообщений
                                    for msg in session_data:
                                        if isinstance(msg, dict):
                                            tokens = msg.get('tokens', 0) or msg.get('usage', {}).get('total_tokens', 0)
                                            total_tokens += tokens

                    except (json.JSONDecodeError, IOError, KeyError):
                        continue

            self.daily_tokens[today] = {
                'tokens': total_tokens,
                'sessions': sessions_today
            }

        # 3. Проверяем логи Claude Code (альтернативный путь)
        log_paths = [
            Path.home() / ".claude" / "logs",
            Path.home() / "Library" / "Logs" / "Claude",  # macOS
            Path.home() / ".local" / "share" / "claude" / "logs",  # Linux
        ]

        for log_path in log_paths:
            if log_path.exists():
                self._parse_logs(log_path, today)
                break

    def _parse_logs(self, log_path: Path, today: str):
        """Парсинг логов Claude Code"""
        total_tokens = 0

        for log_file in log_path.glob("*.log"):
            try:
                stat = log_file.stat()
                file_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")

                if file_date != today:
                    continue

                with open(log_file, 'r') as f:
                    for line in f:
                        # Ищем строки с информацией о токенах
                        if 'tokens' in line.lower():
                            # Пробуем извлечь число
                            import re
                            matches = re.findall(r'tokens["\s:]+(\d+)', line, re.IGNORECASE)
                            for match in matches:
                                total_tokens += int(match)

            except (IOError, ValueError):
                continue

        if total_tokens > 0:
            if today not in self.daily_tokens:
                self.daily_tokens[today] = {'tokens': 0, 'sessions': 0}
            self.daily_tokens[today]['tokens'] = max(
                self.daily_tokens[today]['tokens'],
                total_tokens
            )

    def tokens_today(self, options: CallbackOptions) -> Iterable[Observation]:
        """Токены за сегодня"""
        self._scan_usage()
        today = datetime.now().strftime("%Y-%m-%d")

        if today in self.daily_tokens:
            tokens = self.daily_tokens[today].get('tokens', 0)
            yield Observation(tokens, {"date": today})
        else:
            yield Observation(0, {"date": today})

    def sessions_today(self, options: CallbackOptions) -> Iterable[Observation]:
        """Сессии за сегодня"""
        self._scan_usage()
        today = datetime.now().strftime("%Y-%m-%d")

        if today in self.daily_tokens:
            sessions = self.daily_tokens[today].get('sessions', 0)
            yield Observation(sessions, {"date": today})
        else:
            yield Observation(0, {"date": today})

    def tokens_weekly(self, options: CallbackOptions) -> Iterable[Observation]:
        """Токены за последние 7 дней"""
        self._scan_usage()

        total = 0
        now = datetime.now()
        for i in range(7):
            date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            if date in self.daily_tokens:
                total += self.daily_tokens[date].get('tokens', 0)

        yield Observation(total, {"period": "7d"})

    def is_running(self, options: CallbackOptions) -> Iterable[Observation]:
        """Проверка, запущен ли Claude Code"""
        running = 0
        for p in psutil.process_iter(['name', 'cmdline']):
            try:
                name = p.info['name'].lower()
                cmdline = ' '.join(p.info['cmdline'] or []).lower()

                if 'claude' in name or 'claude' in cmdline:
                    running = 1
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        yield Observation(running, {})


# ============================================================
# DOCKER METRICS (bonus)
# ============================================================

class DockerMetricsCollector:
    """Метрики Docker контейнеров"""

    def __init__(self):
        self.docker_available = self._check_docker()

    def _check_docker(self) -> bool:
        try:
            result = subprocess.run(
                ['docker', 'ps', '-q'],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except:
            return False

    def _get_stats(self) -> list:
        if not self.docker_available:
            return []

        try:
            result = subprocess.run(
                ['docker', 'stats', '--no-stream', '--format',
                 '{{.Name}}\t{{.CPUPerc}}\t{{.MemPerc}}\t{{.MemUsage}}'],
                capture_output=True,
                text=True,
                timeout=10
            )

            stats = []
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('\t')
                if len(parts) >= 3:
                    name = parts[0]
                    cpu = float(parts[1].replace('%', '')) / 100
                    mem = float(parts[2].replace('%', '')) / 100
                    stats.append({'name': name, 'cpu': cpu, 'mem': mem})

            return stats
        except:
            return []

    def container_count(self, options: CallbackOptions) -> Iterable[Observation]:
        if not self.docker_available:
            return

        try:
            result = subprocess.run(
                ['docker', 'ps', '-q'],
                capture_output=True,
                text=True,
                timeout=5
            )
            count = len(result.stdout.strip().split('\n')) if result.stdout.strip() else 0
            yield Observation(count, {})
        except:
            pass

    def container_cpu(self, options: CallbackOptions) -> Iterable[Observation]:
        for stat in self._get_stats():
            yield Observation(stat['cpu'], {"container": stat['name']})

    def container_memory(self, options: CallbackOptions) -> Iterable[Observation]:
        for stat in self._get_stats():
            yield Observation(stat['mem'], {"container": stat['name']})


# ============================================================
# REGISTER ALL METRICS
# ============================================================

def register_metrics(meter: metrics.Meter):
    """Регистрация всех метрик"""

    # System metrics
    system = SystemMetricsCollector()

    meter.create_observable_gauge(
        "system.cpu.utilization",
        callbacks=[system.cpu_utilization],
        description="CPU utilization (0-1)",
        unit="1"
    )
    meter.create_observable_gauge(
        "system.cpu.frequency",
        callbacks=[system.cpu_frequency],
        description="CPU frequency in MHz",
        unit="MHz"
    )
    meter.create_observable_gauge(
        "system.cpu.load_average",
        callbacks=[system.load_average],
        description="System load average",
        unit="1"
    )
    meter.create_observable_gauge(
        "system.memory.utilization",
        callbacks=[system.memory_utilization],
        description="Memory utilization (0-1)",
        unit="1"
    )
    meter.create_observable_gauge(
        "system.memory.usage",
        callbacks=[system.memory_bytes],
        description="Memory usage in bytes",
        unit="By"
    )
    meter.create_observable_gauge(
        "system.swap.utilization",
        callbacks=[system.swap_utilization],
        description="Swap utilization (0-1)",
        unit="1"
    )
    meter.create_observable_gauge(
        "system.filesystem.utilization",
        callbacks=[system.disk_utilization],
        description="Filesystem utilization (0-1)",
        unit="1"
    )
    meter.create_observable_counter(
        "system.disk.io",
        callbacks=[system.disk_io],
        description="Disk I/O in bytes",
        unit="By"
    )
    meter.create_observable_counter(
        "system.network.io",
        callbacks=[system.network_io],
        description="Network I/O in bytes",
        unit="By"
    )
    meter.create_observable_gauge(
        "system.network.connections",
        callbacks=[system.network_connections],
        description="Network connections by state",
        unit="{connections}"
    )
    meter.create_observable_gauge(
        "system.processes.count",
        callbacks=[system.process_count],
        description="Total process count",
        unit="{processes}"
    )
    meter.create_observable_gauge(
        "system.processes.top_cpu",
        callbacks=[system.top_processes_cpu],
        description="Top processes by CPU",
        unit="%"
    )
    meter.create_observable_gauge(
        "system.processes.top_memory",
        callbacks=[system.top_processes_memory],
        description="Top processes by memory",
        unit="%"
    )
    meter.create_observable_gauge(
        "system.battery.charge",
        callbacks=[system.battery_percent],
        description="Battery charge (0-1)",
        unit="1"
    )

    logger.info("✅ System metrics registered")

    # PostgreSQL metrics
    if HAS_PSYCOPG2:
        pg = PostgreSQLMetricsCollector()

        meter.create_observable_gauge(
            "postgres.connections",
            callbacks=[pg.connection_count],
            description="PostgreSQL connections by state",
            unit="{connections}"
        )
        meter.create_observable_gauge(
            "postgres.database.size",
            callbacks=[pg.database_size],
            description="Database size in bytes",
            unit="By"
        )
        meter.create_observable_gauge(
            "postgres.tables.count",
            callbacks=[pg.table_count],
            description="Number of tables",
            unit="{tables}"
        )
        meter.create_observable_counter(
            "postgres.transactions",
            callbacks=[pg.transactions],
            description="Transaction count",
            unit="{transactions}"
        )
        meter.create_observable_gauge(
            "postgres.cache.hit_ratio",
            callbacks=[pg.cache_hit_ratio],
            description="Cache hit ratio (0-1)",
            unit="1"
        )
        meter.create_observable_counter(
            "postgres.deadlocks",
            callbacks=[pg.deadlocks],
            description="Deadlock count",
            unit="{deadlocks}"
        )
        meter.create_observable_gauge(
            "postgres.slow_queries",
            callbacks=[pg.slow_queries],
            description="Slow queries (>1s)",
            unit="{queries}"
        )

        logger.info("✅ PostgreSQL metrics registered")

    # Claude Code metrics
    claude = ClaudeCodeMetricsCollector()

    meter.create_observable_gauge(
        "claude.tokens.today",
        callbacks=[claude.tokens_today],
        description="Claude Code tokens used today",
        unit="{tokens}"
    )
    meter.create_observable_gauge(
        "claude.sessions.today",
        callbacks=[claude.sessions_today],
        description="Claude Code sessions today",
        unit="{sessions}"
    )
    meter.create_observable_gauge(
        "claude.tokens.weekly",
        callbacks=[claude.tokens_weekly],
        description="Claude Code tokens last 7 days",
        unit="{tokens}"
    )
    meter.create_observable_gauge(
        "claude.is_running",
        callbacks=[claude.is_running],
        description="Claude Code process running (0/1)",
        unit="1"
    )

    logger.info("✅ Claude Code metrics registered")

    # Docker metrics
    docker = DockerMetricsCollector()
    if docker.docker_available:
        meter.create_observable_gauge(
            "docker.containers.count",
            callbacks=[docker.container_count],
            description="Running container count",
            unit="{containers}"
        )
        meter.create_observable_gauge(
            "docker.container.cpu",
            callbacks=[docker.container_cpu],
            description="Container CPU utilization",
            unit="1"
        )
        meter.create_observable_gauge(
            "docker.container.memory",
            callbacks=[docker.container_memory],
            description="Container memory utilization",
            unit="1"
        )

        logger.info("✅ Docker metrics registered")


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("🚀 Starting Workstation Metrics Agent")
    logger.info(f"   Host: {socket.gethostname()}")
    logger.info(f"   OS: {platform.system()} {platform.release()}")
    logger.info(f"   Collector: {config.collector_endpoint}")
    logger.info(f"   Interval: {config.collection_interval_sec}s")

    # Инициализация OTel
    meter = setup_otel()

    # Регистрация метрик
    register_metrics(meter)

    # Держим процесс живым
    logger.info("📊 Agent running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("👋 Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
