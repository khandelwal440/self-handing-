import os
from dataclasses import dataclass


def _env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    allowed_namespaces: frozenset = frozenset({"shop"})
    dry_run: bool = False
    cooldown_seconds: int = 120
    rollback_cooldown_seconds: int = 600
    budget_actions: int = 10
    budget_window_seconds: int = 600
    node_remediation: bool = True
    max_cordoned_nodes: int = 1
    scale_step: int = 2
    default_max_replicas: int = 10

    def cooldown_for(self, action):
        # A rollback flips the Deployment to another revision. Acting again while the old alert
        # is still draining out of the rate() window would flip it straight back to the bad one.
        return self.rollback_cooldown_seconds if action == "rollback" else self.cooldown_seconds

    @classmethod
    def from_env(cls):
        namespaces = os.getenv("ALLOWED_NAMESPACES", "shop").split(",")
        return cls(
            allowed_namespaces=frozenset(n.strip() for n in namespaces if n.strip()),
            dry_run=_env_bool("DRY_RUN", False),
            cooldown_seconds=int(os.getenv("COOLDOWN_SECONDS", "120")),
            rollback_cooldown_seconds=int(os.getenv("ROLLBACK_COOLDOWN_SECONDS", "600")),
            budget_actions=int(os.getenv("BUDGET_ACTIONS", "10")),
            budget_window_seconds=int(os.getenv("BUDGET_WINDOW_SECONDS", "600")),
            node_remediation=_env_bool("NODE_REMEDIATION", True),
            max_cordoned_nodes=int(os.getenv("MAX_CORDONED_NODES", "1")),
            scale_step=int(os.getenv("SCALE_STEP", "2")),
            default_max_replicas=int(os.getenv("DEFAULT_MAX_REPLICAS", "10")),
        )
