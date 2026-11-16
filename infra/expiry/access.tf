# EKS access entries authenticate the executor; namespace RBAC authorizes it.
# The operator applies expiry_rbac(environment) before enabling the schedule.
resource "aws_eks_access_entry" "executor" {
  cluster_name      = "deepresearch-${var.environment}"
  principal_arn     = aws_iam_role.executor.arn
  type              = "STANDARD"
  kubernetes_groups = ["deepresearch:${var.environment}:expiry"]
}

resource "aws_iam_role_policy" "describe_cluster" {
  role = aws_iam_role.executor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["eks:DescribeCluster"]
      Resource = "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/deepresearch-${var.environment}"
    }]
  })
}
