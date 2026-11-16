"""Expiry namespace authorization boundaries; this is not a Kubernetes authorizer."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lifecycle.core import Refused
from lifecycle.eks_access import expiry_rbac


def permits(role, namespace, group, resource, verb, name=None):
    """Evaluate only the concrete resource rules emitted by expiry_rbac."""
    if namespace != role["metadata"]["namespace"]:
        return False
    for rule in role["rules"]:
        if group not in rule["apiGroups"] or resource not in rule["resources"] or verb not in rule["verbs"]:
            continue
        if "resourceNames" not in rule or name in rule["resourceNames"]:
            return True
    return False


class ExpiryAccessTests(unittest.TestCase):
    def setUp(self):
        self.role, self.binding = expiry_rbac("staging")["items"]

    def test_current_backup_drain_operations_are_authorized(self):
        for deployment in ["research-api", "research-web", "research-worker", "langfuse-web", "langfuse-worker"]:
            self.assertTrue(permits(self.role, "research", "apps", "deployments", "get", deployment))
            for verb in ["get", "patch", "update"]:
                self.assertTrue(permits(self.role, "research", "apps", "deployments/scale", verb, deployment))
        self.assertTrue(permits(self.role, "research", "", "pods", "list"))
        for pod in ["observability-clickhouse-0", "observability-valkey-0"]:
            for verb in ["get", "create"]:
                self.assertTrue(permits(self.role, "research", "", "pods/exec", verb, pod))
        for ingress in ["research", "research-console"]:
            for verb in ["get", "list", "watch", "delete"]:
                self.assertTrue(permits(self.role, "research", "networking.k8s.io", "ingresses", verb, ingress))

    def test_other_workloads_and_administration_are_denied(self):
        for args in [
            ("other", "apps", "deployments/scale", "patch", "research-api"),
            ("research", "apps", "deployments/scale", "patch", "vllm"),
            ("research", "apps", "deployments", "delete", "research-api"),
            ("research", "", "pods/exec", "create", "research-api-123"),
            ("research", "", "pods", "create", "arbitrary"),
            ("research", "", "secrets", "get", "research-observability"),
            ("research", "rbac.authorization.k8s.io", "rolebindings", "create", "other"),
            ("research", "networking.k8s.io", "ingresses", "delete", "other"),
            ("research", "networking.k8s.io", "ingresses", "list", "other"),
            ("research", "networking.k8s.io", "ingresses", "list", None),
            ("research", "networking.k8s.io", "ingresses", "deletecollection", None),
        ]:
            with self.subTest(request=args):
                self.assertFalse(permits(self.role, *args))

    def test_group_and_role_binding_are_environment_specific_and_namespaced(self):
        for environment in ["staging", "production"]:
            role, binding = expiry_rbac(environment)["items"]
            self.assertEqual("Role", role["kind"])
            self.assertEqual("RoleBinding", binding["kind"])
            self.assertEqual("research", binding["metadata"]["namespace"])
            self.assertEqual(role["metadata"]["name"], binding["roleRef"]["name"])
            self.assertEqual("Role", binding["roleRef"]["kind"])
            self.assertEqual([{"kind": "Group", "name": "deepresearch:" + environment + ":expiry", "apiGroup": "rbac.authorization.k8s.io"}], binding["subjects"])
            for rule in role["rules"]:
                for key in ["apiGroups", "resources", "verbs", "resourceNames"]:
                    self.assertNotIn("*", rule.get(key, []))

    def test_invalid_environment_fails_before_manifest_generation(self):
        for environment in ["", "prod", "staging:*", None, {}, "../../production"]:
            with self.subTest(environment=environment), self.assertRaises(Refused):
                expiry_rbac(environment)
