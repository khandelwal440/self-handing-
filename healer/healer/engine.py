"""Turns Alertmanager notifications into guarded Kubernetes remediations.

Every firing alert carrying a `remediation` label goes through the same gauntlet:
  plan (resolve target + policy checks) -> cooldown -> action budget -> dry-run -> execute.
The engine never talks to the cluster directly; it calls a `kube` object (see kube.py),
which keeps it fully unit-testable.
"""

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import metrics

log = logging.getLogger(__name__)

OPT_IN = "healer.io/enabled"
MAX_REPLICAS = "healer.io/max-replicas"

_FRACTION = re.compile(r"\.(\d+)")


@dataclass
class Outcome:
    alertname: str
    action: str
    target: str
    result: str
    detail: str = ""


@dataclass
class Plan:
    target: str
    cooldown_key: str
    event_ref: tuple  # (kind, namespace, name) the Kubernetes Event is attached to
    run: Callable[[], str]


class PolicyViolation(Exception):
    """The alert maps to a known action, but policy forbids running it on this target."""


def parse_ts(value):
    """Parse Alertmanager timestamps, which carry nanoseconds Python's fromisoformat rejects."""
    if not value or value.startswith("0001-"):
        return None
    value = value.replace("Z", "+00:00")
    value = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    return datetime.fromisoformat(value)


def _require(labels, *names):
    missing = [n for n in names if not labels.get(n)]
    if missing:
        raise PolicyViolation(f"alert is missing label(s): {', '.join(missing)}")
    return tuple(labels[n] for n in names)


def _target_hint(labels):
    if labels.get("node"):
        return labels["node"]
    name = labels.get("pod") or labels.get("deployment") or "-"
    return f"{labels.get('namespace', '-')}/{name}"


class Engine:
    def __init__(self, kube, config, clock=time.monotonic):
        self.kube = kube
        self.config = config
        self.clock = clock
        self._lock = threading.Lock()
        self._last_run = {}
        self._history = deque()
        self._acted = {}
        self._planners = {
            "delete-pod": self._plan_delete_pod,
            "rollback": self._plan_rollback,
            "scale-up": self._plan_scale_up,
            "cordon-drain": self._plan_cordon_drain,
        }

    def handle(self, payload):
        # One remediation at a time: concurrent fixes on the same cluster make failures unreadable.
        with self._lock:
            outcomes = [self._handle_alert(alert) for alert in payload.get("alerts", [])]
        return [o for o in outcomes if o is not None]

    def _handle_alert(self, alert):
        labels = alert.get("labels", {})
        alertname = labels.get("alertname", "unknown")
        status = alert.get("status", "firing")
        metrics.ALERTS_RECEIVED.labels(alertname, status).inc()

        action = labels.get("remediation")
        if not action:
            return None
        if status == "resolved":
            return self._on_resolved(alert, alertname, action)

        planner = self._planners.get(action)
        if planner is None:
            return self._record(Outcome(alertname, action, _target_hint(labels), "unknown_action"))
        try:
            plan = planner(labels)
        except PolicyViolation as exc:
            return self._record(Outcome(alertname, action, _target_hint(labels), "skipped_policy", str(exc)))
        except Exception as exc:  # noqa: BLE001 - an API error while planning is reported, never fatal
            log.exception("planning %s for %s failed", action, _target_hint(labels))
            return self._record(Outcome(alertname, action, _target_hint(labels), "failed", str(exc)))

        now = self.clock()
        cooldown = self.config.cooldown_for(action)
        key = (action, plan.cooldown_key)
        last = self._last_run.get(key)
        if last is not None and now - last < cooldown:
            left = int(cooldown - (now - last))
            return self._record(Outcome(alertname, action, plan.target, "skipped_cooldown", f"{left}s left"))
        if self._budget_exhausted(now):
            detail = "action budget exhausted, circuit open: needs a human"
            return self._record(Outcome(alertname, action, plan.target, "skipped_budget", detail))

        self._last_run[key] = now
        if self.config.dry_run:
            return self._record(Outcome(alertname, action, plan.target, "dry_run"))

        # Failed attempts count against the budget too: repeated failure is itself a signal.
        self._history.append(now)
        try:
            detail = plan.run()
        except Exception as exc:  # noqa: BLE001
            log.exception("remediation %s on %s failed", action, plan.target)
            return self._record(Outcome(alertname, action, plan.target, "failed", str(exc)))

        self._acted[alert.get("fingerprint")] = action
        kind, namespace, name = plan.event_ref
        self.kube.record_event(kind, namespace, name, "SelfHealed", f"{alertname}: {detail}")
        return self._record(Outcome(alertname, action, plan.target, "success", detail))

    def _budget_exhausted(self, now):
        window_start = now - self.config.budget_window_seconds
        while self._history and self._history[0] <= window_start:
            self._history.popleft()
        exhausted = len(self._history) >= self.config.budget_actions
        metrics.CIRCUIT_OPEN.set(1 if exhausted else 0)
        return exhausted

    def _on_resolved(self, alert, alertname, action):
        labels = alert.get("labels", {})
        details = []
        # Uncordon on any resolve, not only ones we remember acting on: the node annotation
        # proves ownership, and this keeps working across healer restarts.
        if action == "cordon-drain" and labels.get("node") and not self.config.dry_run:
            try:
                if self.kube.uncordon_if_ours(labels["node"]):
                    details.append(f"uncordoned {labels['node']}")
            except Exception:  # noqa: BLE001
                log.exception("uncordoning %s failed", labels["node"])
        if self._acted.pop(alert.get("fingerprint"), None) is not None:
            started, ended = parse_ts(alert.get("startsAt")), parse_ts(alert.get("endsAt"))
            if started and ended:
                seconds = (ended - started).total_seconds()
                metrics.RECOVERY_SECONDS.labels(alertname).observe(seconds)
                details.append(f"recovered in {seconds:.0f}s")
        if not details:
            return None
        outcome = Outcome(alertname, action, _target_hint(labels), "resolved", ", ".join(details))
        log.info("%s %s on %s -> resolved (%s)", alertname, action, outcome.target, outcome.detail)
        return outcome

    def _record(self, outcome):
        metrics.REMEDIATIONS.labels(outcome.action, outcome.result).inc()
        log.info(
            "%s %s on %s -> %s%s",
            outcome.alertname,
            outcome.action,
            outcome.target,
            outcome.result,
            f" ({outcome.detail})" if outcome.detail else "",
        )
        return outcome

    def _check_namespace(self, namespace):
        if namespace not in self.config.allowed_namespaces:
            raise PolicyViolation(f"namespace {namespace} is not in the allow-list")

    def _opted_in_annotations(self, namespace, deployment):
        annotations = self.kube.deployment_annotations(namespace, deployment)
        if annotations is None:
            raise PolicyViolation(f"deployment {namespace}/{deployment} not found")
        if annotations.get(OPT_IN) != "true":
            raise PolicyViolation(f"deployment {namespace}/{deployment} has not opted in ({OPT_IN})")
        return annotations

    def _plan_delete_pod(self, labels):
        namespace, pod = _require(labels, "namespace", "pod")
        self._check_namespace(namespace)
        deployment = self.kube.deployment_for_pod(namespace, pod)
        if deployment is None:
            raise PolicyViolation(f"{namespace}/{pod} is gone or not owned by a Deployment")
        self._opted_in_annotations(namespace, deployment)

        def run():
            self.kube.delete_pod(namespace, pod)
            return f"deleted pod {pod}"

        # Cooldown per Deployment, not per pod: a fleet-wide fault must not make us delete every pod at once.
        return Plan(f"{namespace}/{pod}", f"{namespace}/{deployment}", ("Deployment", namespace, deployment), run)

    def _plan_rollback(self, labels):
        namespace, deployment = _require(labels, "namespace", "deployment")
        self._check_namespace(namespace)
        self._opted_in_annotations(namespace, deployment)

        def run():
            bad, good = self.kube.rollback_deployment(namespace, deployment)
            return f"rolled back from revision {bad} to {good}"

        target = f"{namespace}/{deployment}"
        return Plan(target, target, ("Deployment", namespace, deployment), run)

    def _plan_scale_up(self, labels):
        namespace, deployment = _require(labels, "namespace", "deployment")
        self._check_namespace(namespace)
        annotations = self._opted_in_annotations(namespace, deployment)
        if self.kube.has_hpa(namespace, deployment):
            raise PolicyViolation("deployment is managed by an HPA; not fighting it")
        maximum = int(annotations.get(MAX_REPLICAS, self.config.default_max_replicas))
        current = self.kube.get_replicas(namespace, deployment)
        if current >= maximum:
            raise PolicyViolation(f"already at max replicas ({maximum})")
        desired = min(current + self.config.scale_step, maximum)

        def run():
            self.kube.set_replicas(namespace, deployment, desired)
            return f"scaled {current} -> {desired} replicas"

        target = f"{namespace}/{deployment}"
        return Plan(target, target, ("Deployment", namespace, deployment), run)

    def _plan_cordon_drain(self, labels):
        (node,) = _require(labels, "node")
        if not self.config.node_remediation:
            raise PolicyViolation("node remediation is disabled")
        if self.kube.is_control_plane(node):
            raise PolicyViolation("control-plane nodes are protected")
        cordoned = self.kube.cordoned_nodes()
        if node not in cordoned and len(cordoned) >= self.config.max_cordoned_nodes:
            raise PolicyViolation(
                f"{len(cordoned)} node(s) already cordoned, limit is {self.config.max_cordoned_nodes}"
            )

        def run():
            self.kube.cordon(node)
            evicted = self.kube.evict_pods(node)
            return f"cordoned {node} and evicted {evicted} pod(s)"

        return Plan(node, node, ("Node", "default", node), run)
