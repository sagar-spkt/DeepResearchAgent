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

resource "aws_eks_access_entry" "administrator" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = var.administrator_role_arn
}

resource "aws_eks_access_policy_association" "administrator" {

  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_eks_access_entry.administrator.principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
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

  depends_on = [aws_iam_role_policy_attachment.node]

}

resource "aws_eks_node_group" "gpu" {

  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "gpu"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.public[*].id
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

  depends_on = [aws_iam_role_policy_attachment.node]

}

resource "aws_eks_addon" "vpc_cni" {

  cluster_name = aws_eks_cluster.main.name
  addon_name   = "vpc-cni"
  configuration_values = jsonencode({
    enableNetworkPolicy = "true"
    }
  )

}

resource "aws_eks_addon" "pod_identity" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "eks-pod-identity-agent"
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

