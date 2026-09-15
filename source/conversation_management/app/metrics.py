from prometheus_client import Counter, Gauge, Histogram

CHAT_REQUESTS = Counter(
    "conversation_chat_requests_total", "Chat message requests", ["operation", "status"]
)
TASKS_ACTIVE = Gauge("conversation_generation_tasks_active", "Active generation tasks")
AGENT_TTFT_SECONDS = Histogram(
    "conversation_agent_ttft_seconds",
    "Time from worker task start to first answer delta",
    buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 3, 5, 10, 20, 60),
)
AGENT_STREAM_SECONDS = Histogram(
    "conversation_agent_stream_seconds",
    "Total Dify agent stream duration",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
)
DIFY_EVENTS = Counter("conversation_dify_events_total", "Dify stream events", ["event"])

OUTBOX_EVENTS = Counter(
    "conversation_outbox_events_total",
    "Transactional outbox delivery results",
    ["event_type", "result"],
)
OUTBOX_PUBLISH_SECONDS = Histogram(
    "conversation_outbox_publish_seconds",
    "Transactional outbox publish latency",
    ["event_type"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
