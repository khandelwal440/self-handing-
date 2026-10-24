"""Thin wrapper over the Kubernetes API: the only module that talks to the cluster."""

import logging
from datetime import datetime, timezone

from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.client.rest import ApiException

log = logging.getLogger(__name__)

REVISION = "deployment.kubernetes.io/revision"
ROLLED_BACK_FROM = "healer.io/rolled-back-from"
CORDONED_BY_HEALER = "healer.io/cordoned-by-healer"
CONTROL_PLANE = "node-role.kubernetes.io/control-plane"


class NoPreviousRevision(Exception):
    pass


def _owner(metadata, kind):
    return next((o for o in metadata.owner_references or [] if o.kind == kind), None)


def _revision(obj):
    return int((obj.metadata.annotations or {}).get(REVISION, "0"))


class Kube:
    def __init__(self, core, apps, autoscaling):
        self.core = core
        self.apps = apps
        self.autoscaling = autoscaling

    @classmethod
    def connect(cls):
        try:
            kube_config.load_incluster_config()
        except kube_config.ConfigException:
            kube_config.load_kube_config()
        return cls(client.CoreV1Api(), client.AppsV1Api(), client.AutoscalingV2Api())

    def deployment_for_pod(self, namespace, pod):
        try:
            pod_obj = self.core.read_namespaced_pod(pod, namespace)
            rs_ref = _owner(pod_obj.metadata, "ReplicaSet")
            if rs_ref is None:
                return None
            rs = self.apps.read_namespaced_replica_set(rs_ref.name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        dep_ref = _owner(rs.metadata, "Deployment")
        return dep_ref.name if dep_ref else None

    def deployment_annotations(self, namespace, name):
        try:
            deployment = self.apps.read_namespaced_deployment(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return dict(deployment.metadata.annotations or {})

    def delete_pod(self, namespace, pod):
        self.core.delete_namespaced_pod(pod, namespace)

    def rollback_deployment(self, namespace, name):
        """Equivalent of `kubectl rollout undo`: restore the newest older ReplicaSet's pod template."""
        deployment = self.apps.read_namespaced_deployment(name, namespace)
        current = _revision(deployment)
        owned = [
            rs
            for rs in self.apps.list_namespaced_replica_set(namespace).items
            if any(o.uid == deployment.metadata.uid for o in rs.metadata.owner_references or [])
        ]
        older = [rs for rs in owned if _revision(rs) < current]
        if not older:
            raise NoPreviousRevision(f"{namespace}/{name} has no revision older than {current}")
        previous = max(older, key=_revision)

        template = previous.spec.template
        template.metadata.labels.pop("pod-template-hash", None)
        deployment.spec.template = template
        annotations = deployment.metadata.annotations or {}
        annotations[ROLLED_BACK_FROM] = str(current)
        deployment.metadata.annotations = annotations
        # A full replace, not a patch: a strategic-merge patch would keep env vars the bad release added.
        self.apps.replace_namespaced_deployment(name, namespace, deployment)
        return current, _revision(previous)

    def get_replicas(self, namespace, name):
        return self.apps.read_namespaced_deployment_scale(name, namespace).spec.replicas

    def set_replicas(self, namespace, name, replicas):
        self.apps.patch_namespaced_deployment_scale(name, namespace, {"spec": {"replicas": replicas}})

    def has_hpa(self, namespace, name):
        hpas = self.autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace).items
        return any(h.spec.scale_target_ref.kind == "Deployment" and h.spec.scale_target_ref.name == name for h in hpas)

    def is_control_plane(self, node):
        return CONTROL_PLANE in (self.core.read_node(node).metadata.labels or {})

    def cordoned_nodes(self):
        return [n.metadata.name for n in self.core.list_node().items if n.spec.unschedulable]

    def cordon(self, node):
        self.core.patch_node(
            node,
            {"spec": {"unschedulable": True}, "metadata": {"annotations": {CORDONED_BY_HEALER: "true"}}},
        )

    def uncordon_if_ours(self, node):
        annotations = self.core.read_node(node).metadata.annotations or {}
        if annotations.get(CORDONED_BY_HEALER) != "true":
            return False
        self.core.patch_node(
            node,
            {"spec": {"unschedulable": False}, "metadata": {"annotations": {CORDONED_BY_HEALER: None}}},
        )
        return True

    def evict_pods(self, node):
        """Evict everything except DaemonSet and static pods; the Eviction API still honours PDBs."""
        pods = self.core.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node}").items
        evicted = 0
        for pod in pods:
            meta = pod.metadata
            if _owner(meta, "DaemonSet") or "kubernetes.io/config.mirror" in (meta.annotations or {}):
                continue
            body = client.V1Eviction(metadata=client.V1ObjectMeta(name=meta.name, namespace=meta.namespace))
            try:
                self.core.create_namespaced_pod_eviction(meta.name, meta.namespace, body)
                evicted += 1
            except ApiException as exc:
                if exc.status == 429:
                    log.warning("eviction of %s/%s blocked by a PodDisruptionBudget", meta.namespace, meta.name)
                elif exc.status != 404:
                    raise
        return evicted

    def record_event(self, kind, namespace, name, reason, message):
        """Best effort: shows up in `kubectl describe` and `kubectl get events`."""
        now = datetime.now(timezone.utc)
        event = client.CoreV1Event(
            metadata=client.V1ObjectMeta(generate_name=f"{name}.healer.", namespace=namespace),
            involved_object=client.V1ObjectReference(
                kind=kind, name=name, namespace=None if kind == "Node" else namespace
            ),
            reason=reason,
            message=message,
            type="Normal",
            count=1,
            first_timestamp=now,
            last_timestamp=now,
            source=client.V1EventSource(component="healer"),
        )
        try:
            self.core.create_namespaced_event(namespace, event)
        except ApiException:
            log.warning("could not record event for %s %s/%s", kind, namespace, name, exc_info=True)
