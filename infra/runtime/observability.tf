resource "aws_iam_role" "observability" {
  name               = "${local.name}-observability"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}
resource "aws_iam_role_policy" "observability" {
  role = aws_iam_role.observability.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "arn:aws:s3:::${var.foundation.buckets.traces}/*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = "arn:aws:s3:::${var.foundation.buckets.traces}" }
  ] })
}
resource "aws_eks_pod_identity_association" "observability" {
  cluster_name    = aws_eks_cluster.main.name
  namespace       = "research"
  service_account = "research-observability"
  role_arn        = aws_iam_role.observability.arn
}
resource "aws_iam_role" "ebs_csi" {
  name               = "${local.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}
resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
resource "aws_eks_addon" "ebs_csi" {
  cluster_name = aws_eks_cluster.main.name
  addon_name   = "aws-ebs-csi-driver"
  pod_identity_association {
    role_arn        = aws_iam_role.ebs_csi.arn
    service_account = "ebs-csi-controller-sa"
  }
  depends_on = [aws_eks_addon.pod_identity, aws_iam_role_policy_attachment.ebs_csi, aws_eks_node_group.cpu]
}
