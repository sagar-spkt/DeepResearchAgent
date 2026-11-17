data "aws_iam_policy_document" "eks_trust" {

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }

  }


}

resource "aws_iam_role" "cluster" {
  name               = "${local.name}-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_trust.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "main" {

  name     = local.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version
  upgrade_policy {
    support_type = "STANDARD"
  }
  # All default networking add-ons are managed and version-pinned below.
  bootstrap_self_managed_addons = false
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  vpc_config {

    subnet_ids              = aws_subnet.public[*].id
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.administrator_cidrs

  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]
  depends_on                = [aws_iam_role_policy_attachment.cluster]

}

resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${local.name}/cluster"
  retention_in_days = 7
}

data "aws_iam_policy_document" "cluster_admin_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [var.operator_principal_arn]
    }
  }
}

resource "aws_iam_role" "cluster_admin" {
  name                 = "${local.name}-cluster-admin"
  description          = "Short-lived EKS administrator access for the authorized project operator."
  assume_role_policy   = data.aws_iam_policy_document.cluster_admin_trust.json
  max_session_duration = 3600
}

resource "aws_eks_access_entry" "administrator" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_iam_role.cluster_admin.arn
}

resource "aws_eks_access_policy_association" "administrator" {

  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_eks_access_entry.administrator.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }


}

resource "aws_eks_access_entry" "github_actions" {
  count         = var.environment == "staging" ? 1 : 0
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = "arn:aws:iam::${var.account_id}:role/deepresearch-staging-github-oidc"
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "github_actions" {
  count         = var.environment == "staging" ? 1 : 0
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_eks_access_entry.github_actions[count.index].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
  access_scope {
    type       = "namespace"
    namespaces = ["research"]
  }
}

data "aws_iam_policy_document" "node_trust" {

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }

  }


}

resource "aws_iam_role" "node" {
  name               = "${local.name}-node"
  assume_role_policy = data.aws_iam_policy_document.node_trust.json
}

resource "aws_iam_role_policy_attachment" "node" {

  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly", "AmazonEKS_CNI_Policy"])
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"

}

resource "aws_eks_node_group" "cpu" {

  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "cpu"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.public[*].id
  instance_types  = ["m6i.2xlarge"]
  capacity_type   = "ON_DEMAND"
  ami_type        = "AL2023_x86_64_STANDARD"
  scaling_config {
    min_size     = 1
    max_size     = 1
    desired_size = 1
  }

  labels = {
    workload = "cpu"
  }

  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.vpc_cni]

}

resource "aws_eks_node_group" "gpu" {

  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "gpu-flex"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = concat(aws_subnet.public[*].id, [aws_subnet.gpu.id, aws_subnet.gpu_fallback.id])
  instance_types  = ["g6e.xlarge"]
  capacity_type   = "ON_DEMAND"
  ami_type        = "AL2023_x86_64_NVIDIA"
  disk_size       = 100
  scaling_config {
    min_size     = 0
    max_size     = 1
    desired_size = 1
  }

  labels = {
    workload = "gpu"
  }

  taint {
    key    = "nvidia.com/gpu"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
    aws_eks_addon.coredns,
    aws_eks_addon.pod_identity,
    aws_route_table_association.gpu,
    aws_route_table_association.gpu_fallback,
  ]

}

resource "aws_eks_addon" "vpc_cni" {

  cluster_name = aws_eks_cluster.main.name
  addon_name   = "vpc-cni"
  # Verified compatible with Kubernetes 1.37 in us-east-1 on 2026-11-14.
  addon_version = "v1.23.3-eksbuild.1"
  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
    }
  )

}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "kube-proxy"
  # Verified compatible with Kubernetes 1.37 in us-east-1 on 2026-11-14.
  addon_version = "v1.37.0-eksbuild.39"
  depends_on    = [aws_eks_node_group.cpu]
}

resource "aws_eks_addon" "coredns" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "coredns"
  # Verified compatible with Kubernetes 1.37 in us-east-1 on 2026-11-14.
  addon_version = "v1.14.7-eksbuild.35"
  depends_on    = [aws_eks_node_group.cpu]
}

resource "aws_eks_addon" "pod_identity" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "eks-pod-identity-agent"
  # Verified compatible with Kubernetes 1.37 in us-east-1 on 2026-11-14.
  addon_version = "v1.4.1-eksbuild.3"
  depends_on    = [aws_eks_node_group.cpu]
}

data "aws_iam_policy_document" "pod_trust" {

  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }

  }


}

resource "aws_iam_role" "application" {
  name               = "${local.name}-application"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}

data "aws_iam_policy_document" "load_balancer_controller_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks-cluster-name"
      values   = [local.name]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-namespace"
      values   = ["kube-system"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = ["aws-load-balancer-controller"]
    }
  }
}

resource "aws_iam_role" "load_balancer_controller" {
  name               = "${local.name}-load-balancer-controller"
  assume_role_policy = data.aws_iam_policy_document.load_balancer_controller_trust.json
  description        = "Pod Identity role for the staging AWS Load Balancer Controller."
}

resource "aws_iam_policy" "load_balancer_controller" {
  name        = "${local.name}-load-balancer-controller"
  description = "AWS Load Balancer Controller v2.14.1 policy for the staging cluster."
  policy      = jsonencode(jsondecode(file("${path.module}/aws-load-balancer-controller-policy.json")))
}

resource "aws_iam_role_policy_attachment" "load_balancer_controller" {
  role       = aws_iam_role.load_balancer_controller.name
  policy_arn = aws_iam_policy.load_balancer_controller.arn
}

resource "aws_iam_role_policy" "application" {

  role = aws_iam_role.application.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [
      {
        Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = ["arn:aws:s3:::${var.foundation.buckets.documents}/*"]
      }
      ,
      {
        Effect = "Allow", Action = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], Resource = var.foundation.queue_arn
      }

    ]
    }
  )

}

resource "aws_eks_pod_identity_association" "application" {

  cluster_name    = aws_eks_cluster.main.name
  namespace       = "research"
  service_account = "research-application"
  role_arn        = aws_iam_role.application.arn

}

resource "aws_eks_pod_identity_association" "load_balancer_controller" {
  cluster_name    = aws_eks_cluster.main.name
  namespace       = "kube-system"
  service_account = "aws-load-balancer-controller"
  role_arn        = aws_iam_role.load_balancer_controller.arn

  depends_on = [aws_eks_addon.pod_identity, aws_iam_role_policy_attachment.load_balancer_controller]
}

