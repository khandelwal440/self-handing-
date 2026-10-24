from prometheus_client import Counter, Gauge, Histogram

ALERTS_RECEIVED = Counter(
    "healer_alerts_received",
    "Alert notifications received from Alertmanager.",
    ["alertname", "status"],
)
REMEDIATIONS = Counter(
    "healer_remediations",
    "Remediation attempts, by action and result.",
    ["action", "result"],
)
RECOVERY_SECONDS = Histogram(
    "healer_recovery_seconds",
    "Time from an alert starting to it resolving, for alerts the healer acted on (MTTR).",
    ["alertname"],
    buckets=(30, 60, 90, 120, 180, 240, 300, 420, 600, 900),
)
CIRCUIT_OPEN = Gauge(
    "healer_circuit_open",
    "1 when the action budget is exhausted and the healer has stopped acting.",
)
