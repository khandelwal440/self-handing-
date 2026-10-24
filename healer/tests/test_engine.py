from datetime import timedelta

import pytest
from prometheus_client import REGISTRY

from healer.config import Config
from healer.engine import Engine, parse_ts


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeKube:
    def __init__(self):
        self.pods = {("shop", "shop-api-1"): "shop-api", ("shop", "shop-api-2"): "shop-api"}
        self.deployments = {
            ("shop", "shop-api"): {"healer.io/enabled": "true", "healer.io/max-replicas": "5"},
            ("shop", "legacy"): {},
        }
        self.replicas = {("shop", "shop-api"): 3}
        self.hpas = set()
        self.control_plane = {"cp"}
        self.cordoned = {}  # node -> cordoned by healer?
        self.calls = []
        self.events = []
        self.fail_with = None

    def deployment_for_pod(self, namespace, pod):
        return self.pods.get((namespace, pod))

    def deployment_annotations(self, namespace, name):
        found = self.deployments.get((namespace, name))
        return None if found is None else dict(found)

    def delete_pod(self, namespace, pod):
        if self.fail_with:
            raise self.fail_with
        self.calls.append(("delete-pod", namespace, pod))

    def rollback_deployment(self, namespace, name):
        self.calls.append(("rollback", namespace, name))
        return 4, 3

    def get_replicas(self, namespace, name):
        return self.replicas[(namespace, name)]

    def set_replicas(self, namespace, name, replicas):
        self.calls.append(("scale", namespace, name, replicas))
        self.replicas[(namespace, name)] = replicas

    def has_hpa(self, namespace, name):
        return (namespace, name) in self.hpas

    def is_control_plane(self, node):
        return node in self.control_plane

    def cordoned_nodes(self):
        return list(self.cordoned)

    def cordon(self, node):
        self.calls.append(("cordon", node))
        self.cordoned[node] = True

    def evict_pods(self, node):
        return 2

    def uncordon_if_ours(self, node):
        if self.cordoned.pop(node, False):
            self.calls.append(("uncordon", node))
            return True
        return False

    def record_event(self, kind, namespace, name, reason, message):
        self.events.append((kind, namespace, name, reason, message))


def alert(remediation, status="firing", fingerprint="fp1", alertname="TestAlert", **labels):
    return {
        "status": status,
        "fingerprint": fingerprint,
        "labels": {"alertname": alertname, "remediation": remediation, **labels},
        "startsAt": "2026-01-01T10:00:00.000000000Z",
        "endsAt": "2026-01-01T10:03:00Z" if status == "resolved" else "0001-01-01T00:00:00Z",
    }


def send(engine, *alerts):
    return engine.handle({"alerts": list(alerts)})


@pytest.fixture
def kube():
    return FakeKube()


@pytest.fixture
def clock():
    return FakeClock()


def make_engine(kube, clock, **overrides):
    return Engine(kube, Config(**overrides), clock=clock)


def test_delete_pod_for_opted_in_deployment(kube, clock):
    [outcome] = send(make_engine(kube, clock), alert("delete-pod", namespace="shop", pod="shop-api-1"))
    assert outcome.result == "success"
    assert kube.calls == [("delete-pod", "shop", "shop-api-1")]
    assert kube.events[0][:3] == ("Deployment", "shop", "shop-api")


def test_alerts_without_remediation_label_are_ignored(kube, clock):
    plain = {"status": "firing", "labels": {"alertname": "Watchdog"}}
    assert send(make_engine(kube, clock), plain) == []
    assert kube.calls == []


def test_deployment_must_opt_in(kube, clock):
    kube.pods[("shop", "legacy-1")] = "legacy"
    [outcome] = send(make_engine(kube, clock), alert("delete-pod", namespace="shop", pod="legacy-1"))
    assert outcome.result == "skipped_policy"
    assert "opted in" in outcome.detail
    assert kube.calls == []


def test_namespace_must_be_allow_listed(kube, clock):
    [outcome] = send(make_engine(kube, clock), alert("delete-pod", namespace="kube-system", pod="coredns-1"))
    assert outcome.result == "skipped_policy"
    assert kube.calls == []


def test_missing_labels_are_a_policy_skip(kube, clock):
    [outcome] = send(make_engine(kube, clock), alert("rollback", namespace="shop"))
    assert outcome.result == "skipped_policy"
    assert "deployment" in outcome.detail


def test_pod_cooldown_is_per_deployment(kube, clock):
    engine = make_engine(kube, clock)
    send(engine, alert("delete-pod", namespace="shop", pod="shop-api-1"))
    [second] = send(engine, alert("delete-pod", fingerprint="fp2", namespace="shop", pod="shop-api-2"))
    assert second.result == "skipped_cooldown"

    clock.now += 121
    [third] = send(engine, alert("delete-pod", fingerprint="fp2", namespace="shop", pod="shop-api-2"))
    assert third.result == "success"


def test_budget_opens_the_circuit_then_recovers(kube, clock):
    engine = make_engine(kube, clock, budget_actions=2, budget_window_seconds=600, cooldown_seconds=0)
    results = [send(engine, alert("delete-pod", namespace="shop", pod="shop-api-1"))[0].result for _ in range(3)]
    assert results == ["success", "success", "skipped_budget"]
    assert REGISTRY.get_sample_value("healer_circuit_open") == 1

    clock.now += 601
    [outcome] = send(engine, alert("delete-pod", namespace="shop", pod="shop-api-1"))
    assert outcome.result == "success"
    assert REGISTRY.get_sample_value("healer_circuit_open") == 0


def test_dry_run_never_touches_the_cluster(kube, clock):
    [outcome] = send(make_engine(kube, clock, dry_run=True), alert("rollback", namespace="shop", deployment="shop-api"))
    assert outcome.result == "dry_run"
    assert kube.calls == []


def test_rollback_has_a_longer_cooldown(kube, clock):
    engine = make_engine(kube, clock)
    first = send(engine, alert("rollback", namespace="shop", deployment="shop-api"))[0]
    assert first.result == "success"
    assert first.detail == "rolled back from revision 4 to 3"

    clock.now += 300  # past the normal cooldown, inside the rollback one
    [again] = send(engine, alert("rollback", namespace="shop", deployment="shop-api"))
    assert again.result == "skipped_cooldown"
    assert kube.calls == [("rollback", "shop", "shop-api")]


def test_scale_up_is_capped_by_the_max_replicas_annotation(kube, clock):
    kube.replicas[("shop", "shop-api")] = 4
    engine = make_engine(kube, clock)
    [outcome] = send(engine, alert("scale-up", namespace="shop", deployment="shop-api"))
    assert outcome.detail == "scaled 4 -> 5 replicas"

    clock.now += 121
    [capped] = send(engine, alert("scale-up", namespace="shop", deployment="shop-api"))
    assert capped.result == "skipped_policy"
    assert "max replicas" in capped.detail


def test_scale_up_does_not_fight_an_hpa(kube, clock):
    kube.hpas.add(("shop", "shop-api"))
    [outcome] = send(make_engine(kube, clock), alert("scale-up", namespace="shop", deployment="shop-api"))
    assert outcome.result == "skipped_policy"
    assert kube.calls == []


def test_control_plane_is_never_drained(kube, clock):
    [outcome] = send(make_engine(kube, clock), alert("cordon-drain", node="cp"))
    assert outcome.result == "skipped_policy"
    assert kube.calls == []


def test_only_one_node_cordoned_at_a_time(kube, clock):
    engine = make_engine(kube, clock)
    send(engine, alert("cordon-drain", node="worker1"))
    [second] = send(engine, alert("cordon-drain", fingerprint="fp2", node="worker2"))
    assert second.result == "skipped_policy"
    assert ("cordon", "worker2") not in kube.calls


def test_resolved_node_alert_uncordons_and_records_recovery(kube, clock):
    engine = make_engine(kube, clock)
    before = REGISTRY.get_sample_value("healer_recovery_seconds_sum", {"alertname": "NodeDown"}) or 0

    send(engine, alert("cordon-drain", alertname="NodeDown", node="worker1"))
    [resolved] = send(engine, alert("cordon-drain", status="resolved", alertname="NodeDown", node="worker1"))

    assert resolved.result == "resolved"
    assert "uncordoned worker1" in resolved.detail
    assert kube.calls[-1] == ("uncordon", "worker1")
    after = REGISTRY.get_sample_value("healer_recovery_seconds_sum", {"alertname": "NodeDown"})
    assert after - before == pytest.approx(180)


def test_resolved_alert_we_never_acted_on_is_silent(kube, clock):
    assert send(make_engine(kube, clock), alert("delete-pod", status="resolved", namespace="shop", pod="x")) == []


def test_api_failure_is_reported_not_raised(kube, clock):
    kube.fail_with = RuntimeError("apiserver unavailable")
    [outcome] = send(make_engine(kube, clock), alert("delete-pod", namespace="shop", pod="shop-api-1"))
    assert outcome.result == "failed"
    assert "apiserver unavailable" in outcome.detail


def test_unknown_action(kube, clock):
    [outcome] = send(make_engine(kube, clock), alert("reboot-the-internet", namespace="shop"))
    assert outcome.result == "unknown_action"


def test_parse_ts_handles_nanoseconds_and_zero_time():
    a = parse_ts("2026-01-01T10:00:00.123456789Z")
    b = parse_ts("2026-01-01T10:00:01Z")
    assert b - a == timedelta(microseconds=876544)
    assert parse_ts("0001-01-01T00:00:00Z") is None
