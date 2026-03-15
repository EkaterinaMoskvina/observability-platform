# Local Observability Stack

Стек для мониторинга локального хоста с Data Quality проверками.

**Метрики мониторинга:**

- системные ресурсы (CPU/RAM/Disk/Network),

- Docker-контейнеры,

- Claude Code (токены, сессии, статус процесса)

**Дополнительно:** метрики проходят через DQ-пайплайн в OTel Collector — невалидные данные фильтруются, на аномалии навешиваются флаги `dq.alert`.

---

## Архитектура

```
┌──────────────────────────────────────────────────────────────────┐
│                        HOST MACHINE                              │
│                                                                  │
│  ┌─────────────────────┐                                         │
│  │   Python Agent      │  psutil + subprocess                    │
│  │  workstation_agent  │  собирает метрики каждые 15s            │
│  └──────────┬──────────┘                                         │
│             │ OTLP gRPC :4317                                    │
└─────────────┼────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────────────────────────────┐
│                     DOCKER NETWORK: observability                │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                  OTel Collector                           │   │
│  │  1. memory_limiter      → защита от OOM (256 MiB)        │   │
│  │  2. schema_validation   → отбрасывает метрики без unit   │   │
│  │  3. range_validation    → проверяет диапазоны 0–1        │   │
│  │  4. transform/dq_checks → навешивает dq.alert на аномалии│   │
│  │  5. resource            → добавляет метаданные collector  │   │
│  │  6. attributes/cleanup  → удаляет dq.timestamp           │   │
│  │  7. batch               → группирует перед отправкой     │   │
│  └────────────────┬──────────────────────────────────────────┘  │
│                   │ fan-out                                      │
│          ┌────────┴────────┐                                     │
│          │                 │                                     │
│          ▼                 ▼                                     │
│  ┌───────────────┐  ┌──────────────┐                            │
│  │  Prometheus   │  │  ClickHouse  │                            │
│  │  remote write │  │  tcp://9000  │                            │
│  │  retention 30d│  │  TTL 30 days │                            │
│  └───────┬───────┘  └──────┬───────┘                            │
│          │                 │                                     │
│          └────────┬────────┘                                     │
│                   │                                              │
│                   ▼                                              │
│           ┌───────────────┐                                      │
│           │    Grafana    │  datasources: Prometheus + ClickHouse│
│           │   :3000       │  dashboards: auto-provisioned        │
│           └───────────────┘                                      │
└──────────────────────────────────────────────────────────────────┘
```

---

## Компоненты

### Python Agent (`agent/workstation_agent.py`)

Процесс на хост-машине (вне Docker). Использует OTel SDK для отправки метрик по OTLP gRPC.

| Класс | Источник данных | Что собирает |
|---|---|---|
| `SystemMetricsCollector` | `psutil` | CPU, RAM, Disk, Network, процессы, батарея |
| `ClaudeCodeMetricsCollector` | `~/.claude/projects/*.jsonl` | input/output/cache токены, сессии, статус процесса |
| `DockerMetricsCollector` | `docker stats` subprocess | container count, CPU, memory |

Конфигурация через env-переменные, дефолты — в `AgentConfig`:
```python
collector_endpoint: str = "localhost:4317"
collection_interval_sec: int = 15
service_name: str = f"workstation-{hostname}"
```

Resource attributes, которые агент добавляет к каждой метрике:
```
host.name, host.arch, os.type, os.version, agent.type, agent.version
```

### OTel Collector (`collector/otel-collector-config.yaml`)

Image: `otel/opentelemetry-collector-contrib:0.96.0`

**Receivers:** OTLP gRPC `:4317`, OTLP HTTP `:4318`

**DQ-пайплайн процессоров** (порядок важен):

| Шаг | Процессор | Действие |
|---|---|---|
| 1 | `memory_limiter` | Сброс данных при > 256 MiB, spike limit 64 MiB |
| 2 | `filter/schema_validation` | Дропает datapoint где `metric.unit == ""` |
| 3 | `filter/range_validation` | Дропает utilization/charge вне `[0, 1]`, отрицательные I/O counters |
| 4 | `transform/dq_checks` | Ставит `attributes["dq.alert"]` для аномалий, `dq.validated=true` |
| 5 | `resource` | Добавляет `collector.version`, `environment=development` |
| 6 | `attributes/cleanup` | Удаляет `dq.timestamp` (внутренний атрибут) |
| 7 | `batch` | batch_size=512, timeout=5s |

**Exporters:**
- `prometheusremotewrite` → `http://prometheus:9090/api/v1/write`
- `clickhouse` → `tcp://clickhouse:9000`, база `otel`, TTL 720h
- `file/dlq` → `/var/log/otel/dead_letter.json` (ротация 50 MiB, 7 дней)
- `debug` (sampling 1/100)

### Prometheus (`prometheus/prometheus.yaml`)

Image: `prom/prometheus:v2.50.1`, retention 30 дней.

Принимает метрики через Remote Write API (`--web.enable-remote-write-receiver`).

Дополнительно скрапит:
- `localhost:9090` — собственные метрики Prometheus
- `otel-collector:8888` — внутренние метрики OTel Collector
- `clickhouse:9363` — встроенный Prometheus endpoint ClickHouse

Правила алертов подключаются из `prometheus/alerts/*.yaml`.

### ClickHouse (`docker-compose.yaml`)

Image: `clickhouse/clickhouse-server:24.3-alpine`

Схема создаётся автоматически OTel Collector (ClickHouse exporter). Таблицы:
- `otel.otel_metrics_gauge`
- `otel.otel_metrics_sum`
- `otel.otel_metrics_histogram`

Встроенный Prometheus endpoint: `:9363/metrics` (метрики `ClickHouseMetrics_*`, `ClickHouseAsyncMetrics_*`, `ClickHouseProfileEvents_*`).

### Grafana (`grafana/`)

Image: `grafana/grafana:10.4.1`

Datasources и dashboards провизионируются автоматически из `grafana/provisioning/`. Плагины: `grafana-clickhouse-datasource`, `grafana-clock-panel`, `grafana-piechart-panel`.

Дашборды:
- `workstation.json` — метрики рабочей станции (Prometheus datasource)
- `comparison.json` — сравнение Prometheus vs ClickHouse (нефункциональные требования: RAM, CPU, latency)
- `clickhouse_metrics.json` — OTel данные + системные таблицы ClickHouse (ClickHouse datasource)

---

## Каталог метрик

### Системные метрики

| Метрика | Тип | Unit | Атрибуты | Описание |
|---|---|---|---|---|
| `system.cpu.utilization` | Gauge | `1` | `cpu` (0..N, total) | CPU utilization 0–1 |
| `system.cpu.frequency` | Gauge | `MHz` | `type` (current, max) | Частота CPU |
| `system.cpu.load_average` | Gauge | `1` | `period` (1m, 5m, 15m) | LA системы |
| `system.memory.utilization` | Gauge | `1` | `state` (used, available) | RAM 0–1 |
| `system.memory.usage` | Gauge | `By` | `state` (total, used, available, cached) | RAM в байтах |
| `system.swap.utilization` | Gauge | `1` | — | Swap 0–1 |
| `system.filesystem.utilization` | Gauge | `1` | `mountpoint`, `device` | Диск 0–1 по разделам |
| `system.disk.io` | Counter | `By` | `direction` (read, write) | Кумулятивный I/O |
| `system.network.io` | Counter | `By` | `interface`, `direction` (sent, recv) | Сетевой I/O (lo, docker*, br-* исключены) |
| `system.network.connections` | Gauge | `{connections}` | `state` (ESTABLISHED, etc.) | TCP/UDP соединения |
| `system.processes.count` | Gauge | `{processes}` | — | Всего процессов |
| `system.processes.top_cpu` | Gauge | `%` | `process` | Top-5 по CPU |
| `system.processes.top_memory` | Gauge | `%` | `process` | Top-5 по RAM |
| `system.battery.charge` | Gauge | `1` | `plugged` (True/False) | Заряд 0–1 |

### Claude Code метрики

| Метрика | Тип | Unit | Атрибуты | Источник данных |
|---|---|---|---|---|
| `claude.tokens.today` | Gauge | `{tokens}` | `date` | сумма всех типов токенов за сегодня |
| `claude.tokens.input` | Gauge | `{tokens}` | `date`, `type=input` | input-токены (текст запроса, контекст) |
| `claude.tokens.output` | Gauge | `{tokens}` | `date`, `type=output` | output-токены (ответ Claude) |
| `claude.tokens.cache_read` | Gauge | `{tokens}` | `date`, `type=cache_read` | токены, прочитанные из кэша (дешевле input) |
| `claude.tokens.cache_create` | Gauge | `{tokens}` | `date`, `type=cache_create` | токены, записанные в кэш (дороже input, окупается при повторных вызовах) |
| `claude.sessions.today` | Gauge | `{sessions}` | `date` | количество JSONL-файлов сессий, изменённых сегодня |
| `claude.is_running` | Gauge | `1` | — | `psutil.process_iter` (поиск процесса "claude") |

Агент сканирует `~/.claude/projects/<project>/<session>.jsonl` не чаще 1 раза в минуту. Данные о токенах извлекаются из поля `message.usage` в assistant-сообщениях JSONL-файлов.

### Docker метрики

| Метрика | Тип | Unit | Атрибуты | Источник |
|---|---|---|---|---|
| `docker.containers.count` | Gauge | `{containers}` | — | `docker ps -q` |
| `docker.container.cpu` | Gauge | `1` | `container` | `docker stats --no-stream` |
| `docker.container.memory` | Gauge | `1` | `container` | `docker stats --no-stream` |

Метрики Docker регистрируются только если `docker ps` отрабатывает без ошибок.

---

## DQ-флаги аномалий

OTel Collector добавляет атрибут `dq.alert` к datapoint при нарушении порогов:

| Флаг | Условие | Метрика |
|---|---|---|
| `high_cpu` | CPU utilization > 90% | `system.cpu.utilization` |
| `low_memory` | available memory < 10% | `system.memory.utilization{state=available}` |
| `disk_full` | filesystem utilization > 90% | `system.filesystem.utilization` |
| `low_battery` | battery < 20% | `system.battery.charge` |

Все прошедшие валидацию datapoints получают `dq.validated=true`.

Дропнутые (невалидные) данные пишутся в Dead Letter Queue:
```bash
docker exec otel-collector cat /var/log/otel/dead_letter.json
```

---

## Алерты Prometheus

Файл: `prometheus/alerts/alerts.yaml`

| Alert | Severity | Условие | For |
|---|---|---|---|
| `HighCPUUsage` | warning | `system_cpu_utilization{cpu="total"} > 0.9` | 5m |
| `LowMemoryAvailable` | critical | `system_memory_utilization{state="available"} < 0.1` | 5m |
| `DiskSpaceLow` | warning | `system_filesystem_utilization > 0.9` | 10m |
| `LowBattery` | warning | `system_battery_charge < 0.2` (not plugged) | 1m |
| `ClaudeHighTokenUsage` | info | `claude_tokens_today > 100000` | — |
| `ContainerHighCPU` | warning | `docker_container_cpu > 0.8` | 5m |
| `ContainerHighMemory` | warning | `docker_container_memory > 0.9` | 5m |

---

## Конфигурация

### `.env`

```env
# OTel Collector endpoint (с точки зрения агента на хосте)
OTEL_COLLECTOR_ENDPOINT=localhost:4317
OTEL_SERVICE_NAME=workstation

# Интервал сбора (секунды)
COLLECTION_INTERVAL=15
```

Все переменные опциональны — у каждой есть дефолт.

### Пути к данным Claude Code

Агент читает JSONL-файлы сессий из `~/.claude/projects/`. Путь определяется автоматически через `Path.home() / ".claude" / "projects"`.

---

## Развёртывание

### Требования

- Docker + Docker Compose
- Python 3.8+

### 1. Запуск инфраструктуры

```bash
cd local-observability-stack
docker-compose up -d
```

Проверка:
```bash
docker-compose ps
# Все сервисы: clickhouse, otel-collector, prometheus, grafana
```

### 2. Запуск агента

```bash
chmod +x start-agent.sh
./start-agent.sh
```

Скрипт создаёт `venv`, устанавливает зависимости и запускает агент. Агент работает на хосте (не в контейнере), чтобы получить доступ к системным ресурсам.

Вручную:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r agent/requirements.txt
python3 agent/workstation_agent.py
```

### 3. Дашборды

| Сервис | URL | Credentials |
|---|---|---|
| Grafana | http://localhost:3000 | admin / admin |
| Prometheus | http://localhost:9090 | — |
| ClickHouse HTTP | http://localhost:8123 | — |
| OTel health check | http://localhost:13133 | — |
| OTel zPages | http://localhost:55780/debug/tracez | — |
| OTel internal metrics | http://localhost:8888/metrics | — |

---

## Порты

| Порт | Сервис | Протокол |
|---|---|---|
| 3000 | Grafana | HTTP |
| 4317 | OTel Collector OTLP | gRPC |
| 4318 | OTel Collector OTLP | HTTP |
| 8123 | ClickHouse | HTTP API |
| 8888 | OTel Collector metrics | HTTP |
| 9000 | ClickHouse | Native TCP |
| 9090 | Prometheus | HTTP |
| 9363 | ClickHouse Prometheus endpoint | HTTP |
| 13133 | OTel Collector health | HTTP |
| 55780 | OTel Collector zPages (→55679 внутри) | HTTP |

---

## PromQL — примеры запросов

```promql
# CPU всех ядер
system_cpu_utilization{cpu!="total"}

# Среднее CPU за 5 минут
avg_over_time(system_cpu_utilization{cpu="total"}[5m])

# Свободная память в гигабайтах
system_memory_usage{state="available"} / 1024 / 1024 / 1024

# Метрики с DQ-флагом аномалии (если dq.alert пробрасывается в метку)
{dq_alert!=""}

# Токены Claude за сегодня (общее)
claude_tokens_today

# Разбивка токенов по типам
claude_tokens_input
claude_tokens_output
claude_tokens_cache_read
claude_tokens_cache_create

# Top-5 контейнеров по CPU
topk(5, docker_container_cpu)

# Disk utilization по разделам выше 80%
system_filesystem_utilization > 0.8
```

---

## Troubleshooting

### Агент не подключается к Collector

```bash
# Проверь health check
curl http://localhost:13133/health

# Посмотри логи Collector
docker logs otel-collector

# Проверь, что порт доступен с хоста
nc -zv localhost 4317
```

### Метрики не появляются в Prometheus

```bash
# Remote write receiver включён?
curl http://localhost:9090/api/v1/status/config | grep remote_write

# Внутренние метрики Collector (processed vs dropped)
curl http://localhost:8888/metrics | grep otelcol_processor

# Посмотри pipeline в zPages
open http://localhost:55780/debug/pipelinez
```

### Dead Letter Queue растёт

```bash
# Посмотри что попадает в DLQ
docker exec otel-collector cat /var/log/otel/dead_letter.json | head -50

# Размер DLQ файла
docker exec otel-collector ls -lh /var/log/otel/
```

### ClickHouse не стартует

```bash
docker logs clickhouse

# Проверь ulimits (нужно 262144)
docker inspect clickhouse | grep -A5 Ulimits
```

---

## Структура файлов

```
local-observability-stack/
├── docker-compose.yaml              # Инфраструктура: ClickHouse, OTel Collector, Prometheus, Grafana
├── start-agent.sh                   # Скрипт запуска Python Agent (создаёт venv, устанавливает deps)
├── .env                             # Конфигурация (env-переменные для агента)
├── README.md                        # Эта документация
│
├── agent/
│   ├── workstation_agent.py         # Python Agent (OTel SDK + psutil)
│   └── requirements.txt             # opentelemetry-sdk, psutil, python-dotenv
│
├── collector/
│   └── otel-collector-config.yaml   # OTel Collector: receivers, DQ processors, exporters
│
├── prometheus/
│   ├── prometheus.yaml              # Scrape configs + remote write receiver
│   └── alerts/
│       └── alerts.yaml              # Правила алертов (system, claude, docker)
│
├── clickhouse/
│   └── config/
│       └── prometheus.xml           # Включает Prometheus endpoint :9363
│
└── grafana/
    ├── provisioning/
    │   ├── datasources/             # Prometheus + ClickHouse datasources
    │   └── dashboards/              # Dashboard provider config
    └── dashboards/
        ├── workstation.json         # Дашборд мониторинга рабочей станции (Prometheus)
        ├── comparison.json          # Сравнение Prometheus vs ClickHouse (NFR)
        └── clickhouse_metrics.json  # OTel данные + system tables ClickHouse
```
