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
from datetime import datetime
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
# CLAUDE CODE METRICS COLLECTOR
# ============================================================

class ClaudeCodeMetricsCollector:
    """
    Сбор метрик использования Claude Code

    Claude Code хранит сессии в ~/.claude/projects/<project>/<session>.jsonl
    Каждая строка — JSON с полем message.usage для assistant-сообщений:
      {input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}
    """

    def __init__(self):
        self.projects_path = Path.home() / ".claude" / "projects"
        self._cache = {}  # date -> {input, output, cache_create, cache_read, sessions}
        self._last_scan = None

    def _scan_usage(self):
        """Сканирование JSONL-файлов сессий Claude Code"""
        now = datetime.now()

        # Не сканируем чаще раза в минуту
        if self._last_scan and (now - self._last_scan).seconds < 60:
            return

        self._last_scan = now
        today = now.strftime("%Y-%m-%d")

        if not self.projects_path.exists():
            return

        totals = {'input': 0, 'output': 0, 'cache_create': 0, 'cache_read': 0}
        sessions = set()

        for project_dir in self.projects_path.iterdir():
            if not project_dir.is_dir():
                continue

            for session_file in project_dir.glob("*.jsonl"):
                try:
                    stat = session_file.stat()
                    file_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
                    if file_date != today:
                        continue

                    sessions.add(session_file.stem)

                    with open(session_file, 'r') as f:
                        for line in f:
                            try:
                                d = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            msg = d.get('message', {})
                            if not isinstance(msg, dict):
                                continue

                            usage = msg.get('usage')
                            if not usage:
                                continue

                            totals['input'] += usage.get('input_tokens', 0)
                            totals['output'] += usage.get('output_tokens', 0)
                            totals['cache_create'] += usage.get('cache_creation_input_tokens', 0)
                            totals['cache_read'] += usage.get('cache_read_input_tokens', 0)

                except (IOError, OSError):
                    continue

        self._cache[today] = {**totals, 'sessions': len(sessions)}

    def tokens_today(self, options: CallbackOptions) -> Iterable[Observation]:
        """Общее количество токенов за сегодня (input + output + cache)"""
        self._scan_usage()
        today = datetime.now().strftime("%Y-%m-%d")
        data = self._cache.get(today, {})

        total = (data.get('input', 0) + data.get('output', 0)
                 + data.get('cache_create', 0) + data.get('cache_read', 0))
        yield Observation(total, {"date": today})

    def input_tokens_today(self, options: CallbackOptions) -> Iterable[Observation]:
        """Input токены за сегодня"""
        self._scan_usage()
        today = datetime.now().strftime("%Y-%m-%d")
        data = self._cache.get(today, {})
        yield Observation(data.get('input', 0), {"date": today, "type": "input"})

    def output_tokens_today(self, options: CallbackOptions) -> Iterable[Observation]:
        """Output токены за сегодня"""
        self._scan_usage()
        today = datetime.now().strftime("%Y-%m-%d")
        data = self._cache.get(today, {})
        yield Observation(data.get('output', 0), {"date": today, "type": "output"})

    def cache_read_tokens_today(self, options: CallbackOptions) -> Iterable[Observation]:
        """Cache read токены за сегодня"""
        self._scan_usage()
        today = datetime.now().strftime("%Y-%m-%d")
        data = self._cache.get(today, {})
        yield Observation(data.get('cache_read', 0), {"date": today, "type": "cache_read"})

    def cache_create_tokens_today(self, options: CallbackOptions) -> Iterable[Observation]:
        """Cache creation токены за сегодня"""
        self._scan_usage()
        today = datetime.now().strftime("%Y-%m-%d")
        data = self._cache.get(today, {})
        yield Observation(data.get('cache_create', 0), {"date": today, "type": "cache_create"})

    def sessions_today(self, options: CallbackOptions) -> Iterable[Observation]:
        """Количество сессий за сегодня"""
        self._scan_usage()
        today = datetime.now().strftime("%Y-%m-%d")
        data = self._cache.get(today, {})
        yield Observation(data.get('sessions', 0), {"date": today})

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

    # Claude Code metrics
    claude = ClaudeCodeMetricsCollector()

    meter.create_observable_gauge(
        "claude.tokens.today",
        callbacks=[claude.tokens_today],
        description="Claude Code total tokens today (input+output+cache)",
        unit="{tokens}"
    )
    meter.create_observable_gauge(
        "claude.tokens.input",
        callbacks=[claude.input_tokens_today],
        description="Claude Code input tokens today",
        unit="{tokens}"
    )
    meter.create_observable_gauge(
        "claude.tokens.output",
        callbacks=[claude.output_tokens_today],
        description="Claude Code output tokens today",
        unit="{tokens}"
    )
    meter.create_observable_gauge(
        "claude.tokens.cache_read",
        callbacks=[claude.cache_read_tokens_today],
        description="Claude Code cache read tokens today",
        unit="{tokens}"
    )
    meter.create_observable_gauge(
        "claude.tokens.cache_create",
        callbacks=[claude.cache_create_tokens_today],
        description="Claude Code cache creation tokens today",
        unit="{tokens}"
    )
    meter.create_observable_gauge(
        "claude.sessions.today",
        callbacks=[claude.sessions_today],
        description="Claude Code sessions today",
        unit="{sessions}"
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
