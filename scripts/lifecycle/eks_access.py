"""Pure Kubernetes RBAC manifests for the expiry executor; no clients or apply."""

from .core import Refused

WRITERS = ["research-api", "research-web", "research-worker", "langfuse-web", "langfuse-worker"]
BACKUP_PODS = ["observability-clickhouse-0", "observability-valkey-0"]


def expiry_rbac(environment):
    """Return a JSON-compatible List for an operator to review and apply privately.

    EKS maps the expiry executor to this group. An access entry authenticates the
    role; this namespace RoleBinding supplies only the backup/drain permissions.
    """
    if environment not in ("staging", "production"):
        raise Refused("Only staging/production accepted")
    name = "deepresearch-" + environment + "-expiry"
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": name, "namespace": "research"},
                "rules": [
                    {"apiGroups": ["apps"], "resources": ["deployments"],
                     "resourceNames": list(WRITERS), "verbs": ["get"]},
                    {"apiGroups": ["apps"], "resources": ["deployments/scale"],
                     "resourceNames": list(WRITERS), "verbs": ["get", "patch", "update"]},
                    # Quiescence checks must see every writer pod in this namespace.
                    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                    # kubectl cp uses exec; no exec access to application pods.
                    {"apiGroups": [""], "resources": ["pods/exec"],
                     "resourceNames": list(BACKUP_PODS), "verbs": ["get", "create"]},
                    {"apiGroups": ["networking.k8s.io"], "resources": ["ingresses"],
                     "resourceNames": ["research", "research-console"],
                     "verbs": ["get", "list", "watch", "delete"]},
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": name, "namespace": "research"},
                "subjects": [{"kind": "Group", "name": "deepresearch:" + environment + ":expiry",
                              "apiGroup": "rbac.authorization.k8s.io"}],
                "roleRef": {"kind": "Role", "name": name,
                            "apiGroup": "rbac.authorization.k8s.io"},
            },
        ],
    }
