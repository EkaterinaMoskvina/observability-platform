"""
order-service — принимает запросы от api-gateway.
Демонстрирует propagation трейсов между сервисами.
"""
import time
import random
from http.server import HTTPServer, BaseHTTPRequestHandler

from opentelemetry import trace, _logs
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

OTEL_ENDPOINT = "http://otel-collector:4317"

resource = Resource.create({
    "service.name": "order-service",
    "service.version": "1.0.0",
    "deployment.environment": "lab",
})

# --- Трейсы ---
trace_exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("order-service")

# --- Логи ---
log_exporter = OTLPLogExporter(endpoint=OTEL_ENDPOINT, insecure=True)
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
_logs.set_logger_provider(logger_provider)
logger = logger_provider.get_logger("order-service")

propagator = TraceContextTextMapPropagator()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Извлекаем trace context из входящих заголовков
        ctx = propagator.extract(dict(self.headers))

        with tracer.start_as_current_span("process_order", context=ctx) as span:
            # Имитация работы: валидация
            with tracer.start_as_current_span("validate_order"):
                time.sleep(random.uniform(0.01, 0.03))

            # Имитация работы: запись в БД
            with tracer.start_as_current_span("db.insert", attributes={"db.system": "clickhouse"}):
                time.sleep(random.uniform(0.02, 0.08))

            # Иногда ошибки
            if random.random() < 0.15:
                span.set_status(trace.StatusCode.ERROR, "order processing failed")
                logger.emit(LogRecord(
                    body="Ошибка обработки заказа",
                    severity_number=SeverityNumber.ERROR,
                ))
                self.send_response(500)
            else:
                logger.emit(LogRecord(
                    body="Заказ обработан успешно",
                    severity_number=SeverityNumber.INFO,
                ))
                self.send_response(200)

            self.end_headers()

    def log_message(self, format, *args):
        pass  # подавляем стандартные логи HTTP сервера


if __name__ == "__main__":
    print("=== order-service запущен на :8080 ===")
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
