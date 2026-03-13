```mermaid
flowchart TB
    subgraph Sources["Источники данных"]
        direction LR
        VMs["Виртуальные машины<br/>50,000 instances"]
        K8s["Managed Kubernetes"]
        PG["Managed PostgreSQL<br/>5,000 instances"]
    end

    subgraph Collectors["OTel Collectors (Agent Layer)"]
        direction LR
        AgentVM["OTel Agent<br/>метрики + логи"]
        AgentK8s["OTel DaemonSet<br/>kubeletstats + k8s_cluster"]
        AgentPG["OTel Sidecar<br/>postgresql receiver"]
    end

    subgraph Gateway["OTel Gateway (Processing Layer)"]
        direction TB
        GW1["Gateway Pod 1"]
        GW2["Gateway Pod 2"]
        GW3["Gateway Pod 3"]
        LB["Load Balancer<br/>OTLP endpoint"]

        LB --> GW1
        LB --> GW2
        LB --> GW3
    end

    subgraph Processing["Gateway Pipeline"]
        direction LR
        Recv["OTLP<br/>Receiver"]
        MemLim["Memory<br/>Limiter"]
        Transform["Transform<br/>+ Validate"]
        Batch["Batch<br/>Processor"]
        Export["ClickHouse<br/>Exporter"]

        Recv --> MemLim --> Transform --> Batch --> Export
    end

    subgraph Buffer["Buffer Layer"]
        Kafka["Apache Kafka<br/>3 brokers"]
    end

    subgraph StorageLayer["Хранилище"]
        direction TB

        subgraph ClickHouseCluster["ClickHouse Cluster (Event Data)"]
            direction LR
            subgraph Shard1["Шард 1"]
                CH1R1["Реплика 1"]
                CH1R2["Реплика 2"]
            end

            subgraph Shard2["Шард 2"]
                CH2R1["Реплика 1"]
                CH2R2["Реплика 2"]
            end

            subgraph Shard3["Шард 3"]
                CH3R1["Реплика 1"]
                CH3R2["Реплика 2"]
            end
        end

        Keeper["ClickHouse<br/>3 ноды"]

        subgraph MongoCluster["MongoDB"]
            direction LR
            Mongo1["Primary узел (Основной)"]
            Mongo2["Secondary 1 (Вторичный)"]
            Mongo3["Secondary 2 (Вторичный)"]
        end
    end

    subgraph Tables["Data Separation"]
        direction TB

        subgraph CHData["ClickHouse Tables"]
            TLogs["otel_logs"]
            TMetrics["otel_metrics_*"]
            TViews["Materialized Views"]
        end

        subgraph MongoData["MongoDB Collections"]
            Dashboards["дашборды"]
            Alerts["алерты"]
            Users["пользователи"]
            SavedQueries["сохраненные_запросы"]
        end
    end

    subgraph Query["Слой SQL запросов"]
        direction LR
        QProxy["Query Proxy<br/>Изоляция пользователей"]
    end

    subgraph Visualization["Визуализация"]
        direction LR
        HyperDX["HyperDX UI<br/>логи"]
        Cabinet["Личный кабинет<br/>Встроенная панель"]
    end

    subgraph Consumers["Конечные пользователи"]
        direction LR
        Customer["Клиенты<br/>Видят только свою информацию"]
        Engineer["Инженеры<br/>Видят все данные"]
    end

    %% Connections
    VMs --> AgentVM
    K8s --> AgentK8s
    PG --> AgentPG

    AgentVM -->|"OTLP/gRPC"| LB
    AgentK8s -->|"OTLP/gRPC"| LB
    AgentPG -->|"OTLP/gRPC"| LB

    GW1 --> Processing
    GW2 --> Processing
    GW3 --> Processing

    Export -->|"Основной путь записи"| ClickHouseCluster
    Export -.->|"Резервный путь"| Kafka
    Kafka -.->|"Consumer"| ClickHouseCluster

    CH1R1 <-.-> Keeper
    CH2R1 <-.-> Keeper
    CH3R1 <-.-> Keeper

    Mongo1 <-.-> Mongo2
    Mongo1 <-.-> Mongo3

    ClickHouseCluster --> CHData
    MongoCluster --> MongoData

    CHData --> QProxy
    MongoData --> HyperDX

    QProxy --> HyperDX
    QProxy --> Cabinet

    Cabinet --> Customer
    HyperDX --> Engineer

    %% Styles
    classDef sources fill:#E0F2FE,stroke:#0284C7,stroke-width:2px,color:#0C4A6E,rx:8,ry:8
    classDef collectors fill:#CCFBF1,stroke:#0D9488,stroke-width:2px,color:#115E59,rx:8,ry:8
    classDef gateway fill:#F3E8FF,stroke:#7E22CE,stroke-width:2px,color:#581C87,rx:8,ry:8
    classDef clickhouse fill:#FFEDD5,stroke:#EA580C,stroke-width:2px,color:#9A3412,rx:8,ry:8
    classDef mongodb fill:#DCFCE7,stroke:#16A34A,stroke-width:2px,color:#14532D,rx:8,ry:8
    classDef kafka fill:#F1F5F9,stroke:#475569,stroke-width:2px,color:#1E293B,rx:8,ry:8
    classDef query fill:#E0E7FF,stroke:#4338CA,stroke-width:2px,color:#312E81,rx:8,ry:8
    classDef viz fill:#FCE7F3,stroke:#DB2777,stroke-width:2px,color:#831843,rx:8,ry:8
    classDef users fill:#FEF3C7,stroke:#D97706,stroke-width:2px,color:#78350F,rx:8,ry:8

    class VMs,K8s,PG sources
    class AgentVM,AgentK8s,AgentPG collectors
    class GW1,GW2,GW3,LB,Recv,MemLim,Transform,Batch,Export gateway
    class CH1R1,CH1R2,CH2R1,CH2R2,CH3R1,CH3R2,Keeper,TLogs,TMetrics,TTraces,TViews clickhouse
    class Mongo1,Mongo2,Mongo3,Dashboards,Alerts,Users,SavedQueries mongodb
    class Kafka kafka
    class QProxy query
    class HyperDX,Cabinet viz
    class Customer,Engineer users

    style Sources fill:#FDF6E3,stroke:#BAE6FD,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style Collectors fill:#FDF6E3,stroke:#99F6E4,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style Gateway fill:#FDF6E3,stroke:#E9D5FF,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style Processing fill:#FDF6E3,stroke:#E9D5FF,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style Buffer fill:#FDF6E3,stroke:#CBD5E1,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style StorageLayer fill:#FDF6E3,stroke:#CBD5E1,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style Tables fill:#FDF6E3,stroke:#CBD5E1,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style Query fill:#FDF6E3,stroke:#C7D2FE,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style Visualization fill:#FDF6E3,stroke:#FBCFE8,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    style Consumers fill:#FDF6E3,stroke:#FDE68A,stroke-width:2px,stroke-dasharray: 5 5,color:#1F2937
    linkStyle default stroke:#333,stroke-width:2px;
```


## ОБЪЯСНЕНИЕ ЖИЗНЕННОГО ЦИКЛА ДАННЫХ ТЕЛЕМЕТРИИ

### Шаг 1. Уровень сбора данных

На ресурсах (Virtual Machines, узлы Managed Kubernetes, инстансы Managed PostgreSQL) развернуты агенты **OTel Collector**.

Их основная функция — непрерывный парсинг логов и получение метрик. Агент осуществляет первичную компоновку данных и их передачу по протоколу OTLP (OpenTelemetry Protocol) на уровень централизованной обработки через защищенное gRPC-соединение.

###  Шаг 2. Уровень агрегации и обработки

Для обеспечения высокой доступности и обработки интенсивного входящего потока данных применяется кластерный **OTel Gateway**.

Входящий трафик распределяется через балансировщик нагрузки (**Load Balancer**) между подами шлюза. Внутри каждого шлюза настроен конвейер обработки данных, состоящий из следующих компонентов:

- **Memory Limiter Processor**: реализует механизм backpressure, предотвращая падение узлов шлюза от OOM во время пиковых нагрузок.
[Memory Limiter Processor](https://github.com/open-telemetry/opentelemetry-collector/tree/main/processor/memorylimiterprocessor)

- **Transform Processor**: выполняет бизнес-логику обогащения данных. Используя язык OTTL (OpenTelemetry Transformation Language), процессор принудительно инжектирует атрибут tenant_id в метаданные телеметрии для обеспечения строгой мультитенантности.
[Transform Processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/transformprocessor)

- **Filter Processor**: выполняет бизнес-логику оптимизации затрат. Процессор оценивает поле SeverityNumber лог-записей и отбрасывает события уровней INFO, DEBUG и TRACE, пропуская в хранилище только события (WARN, ERROR).
[Filter Processor](https://opentelemetry.io/docs/specs/otel/logs/data-model/#field-severitynumber)

- **Batch Processor**: Выполняет агрегацию непрерывного потока мелких событий в батчи, что предотвращает избыточную фрагментацию данных на диске Clickhouse (проблема *Too many parts*).
[Batch Processor](https://github.com/open-telemetry/opentelemetry-collector/tree/main/processor/batchprocessor)

- **ClickHouse Exporter**: специализированный компонент OpenTelemetry Collector, обеспечивающий финальную конвертацию и передачу телеметрических данных в целевое хранилище.
[OpenTelemetry ClickHouse Exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/clickhouseexporter)

###  Шаг 3. Уровень буферизации

Для обеспечения надежности и реализации стратегии **Zero Data Loss** (нулевая потеря данных) внедрен промежуточный слой буферизации на базе **Apache Kafka**.

 Слой работает в рамках двухконтурной схемы:

1.  **Основной контур:** в штатном режиме сформированные батчи передаются из OTel Gateway напрямую в ClickHouse Cluster для обеспечения минимальной задержки отображения данных.

2.  **Контур отказоустойчивости:** в случае временной недоступности БД, сетевых сбоев, ClickHouse Exporter автоматически перенаправляет поток данных в топики Kafka.

3. **Механизм восстановления**: после восстановления БД данные асинхронно вычитываются из Kafka через Kafka Table Engine.


### Шаг 4. Уровень хранения данных
Архитектура хранилища предусматривает разделение событийных данных и состояния приложения:

- **ClickHouse (хранение данных)** для хранения метрик и логов. Для обеспечения масштабируемости и отказоустойчивости применяется шардирование и репликация данных. Кроме таблиц, создаются материализованные представления для автоматического вычисления статистических показателей в момент записи данных.

- **MongoDB (хранение информации о состоянии приложения)** для хранения метаданных: профилей пользователей, политик доступа, конфигураций дашбордов и правил алертинга.

Для отказоустойчивости используется схема Replica Set. В случае выхода из строя Primary-узла, система проводит автоматические выборы нового лидера, что гарантирует доступность Личного кабинета 24/7.


### Шаг 5. Уровень доступа к данным и безопасности

Прямой доступ к аналитической БД ClickHouse из внешних сетей строго запрещен. Для изоляции данных между клиентами применяется паттерн **Backend API (или Query Proxy)**:

**Backend API личного кабинета (Query Proxy)**: собственный микросервис облачного провайдера. Выступает единой точкой входа для запросов от клиентских веб-интерфейсов. При получении запроса на построение графика, микросервис валидирует JWT-токен пользователя, определяет его принадлежность к конкретному пользователю и принудительно инжектирует в формируемый SQL-запрос фильтр WHERE tenant_id = '<ID_клиента>'.

### Шаг 6. Уровень визуализации
Взаимодействие с данными разделено на два потока в зависимости от роли пользователя:

**Клиенты:** используют встроенный интерфейс Личного кабинета. Бэкенд кабинета самостоятельно запрашивает агрегированные метрики из ClickHouse по API, применяя жесткую фильтрацию по tenant_id, и отрисовывает базовые графики ресурсов.

**Инженеры поддержки**: Используют HyperDX UI для прямого доступа к глобальной базе телеметрии ClickHouse.