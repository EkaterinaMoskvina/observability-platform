"""
Сервис-генератор телеметрии: метрики + логи + трейсы.
Имитирует API-сервис, который обрабатывает запросы и вызывает order-service.
"""
import time
import random
import requests

from opentelemetry import metrics, _logs, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

OTEL_ENDPOINT = "http://otel-collector:4317"

resource = Resource.create({
    "service.name": "api-gateway",
    "service.version": "1.0.0",
    "tenant_id": "client_001",
    "deployment.environment": "lab",
})

# --- Трейсы ---
trace_exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("api-gateway")

# --- Метрики ---
metric_exporter = OTLPMetricExporter(endpoint=OTEL_ENDPOINT, insecure=True)
reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)
meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("api-gateway")

request_counter = meter.create_counter("http_requests_total", description="Total HTTP requests")
request_duration = meter.create_histogram("http_request_duration_ms", unit="ms", description="Request duration")
cpu_gauge = meter.create_gauge("cpu_usage_sim", unit="1", description="Simulated CPU usage")
error_counter = meter.create_counter("http_errors_total", description="Total HTTP errors")

# --- Логи ---
log_exporter = OTLPLogExporter(endpoint=OTEL_ENDPOINT, insecure=True)
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
_logs.set_logger_provider(logger_provider)
logger = logger_provider.get_logger("api-gateway")

ENDPOINTS = ["/api/users", "/api/orders", "/api/products", "/api/health"]

print("=== api-gateway запущен. Генерация телеметрии... ===")

while True:
    endpoint = random.choice(ENDPOINTS)
    status_code = random.choices([200, 201, 400, 500], weights=[70, 10, 10, 10])[0]

    # Создаём корневой span — каждый "запрос" это трейс
    with tracer.start_as_current_span(
        f"HTTP GET {endpoint}",
        attributes={
            "http.method": "GET",
            "http.url": endpoint,
            "http.status_code": status_code,
        }
    ) as span:
        duration = random.uniform(5, 500)

        # Вложенный span — имитация обращения к БД
        with tracer.start_as_current_span("db.query", attributes={"db.system": "clickhouse"}):
            time.sleep(random.uniform(0.01, 0.05))

        # Если endpoint /api/orders — вызываем order-service (distributed trace)
        if endpoint == "/api/orders":
            with tracer.start_as_current_span("call order-service"):
                try:
                    # Прокидываем trace context через HTTP заголовки
                    headers = {}
                    TraceContextTextMapPropagator().inject(headers)
                    requests.get("http://order-service:8080/process", headers=headers, timeout=2)
                except Exception:
                    span.set_attribute("order_service.available", False)

        # Метрики
        request_counter.add(1, {"endpoint": endpoint, "status": str(status_code)})
        request_duration.record(duration, {"endpoint": endpoint})

        if status_code >= 400:
            error_counter.add(1, {"endpoint": endpoint, "status": str(status_code)})
            span.set_status(trace.StatusCode.ERROR, f"HTTP {status_code}")
            logger.emit(LogRecord(
                body=f"Ошибка {status_code} на {endpoint}, duration={duration:.0f}ms",
                severity_number=SeverityNumber.ERROR,
                attributes={"endpoint": endpoint, "status_code": status_code},
            ))
        else:
            logger.emit(LogRecord(
                body=f"OK {status_code} на {endpoint}, duration={duration:.0f}ms",
                severity_number=SeverityNumber.INFO,
                attributes={"endpoint": endpoint, "status_code": status_code},
            ))

    # Системная метрика
    cpu_gauge.set(random.randint(10, 90))

    time.sleep(random.uniform(0.5, 2))
