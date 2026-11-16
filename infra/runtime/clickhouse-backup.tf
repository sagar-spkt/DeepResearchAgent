# ClickHouse alone can read/write the current session's backup objects.
resource "aws_iam_role" "clickhouse_backup" {
  name               = "${local.name}-clickhouse-backup"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}

resource "aws_iam_role_policy" "clickhouse_backup" {
  role = aws_iam_role.clickhouse_backup.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
      Resource = "arn:aws:s3:::${var.foundation.buckets.backups}/sessions/${var.environment}/${var.session_id}/clickhouse/*"
    },
    {
      Effect   = "Allow"
      Action   = ["s3:ListBucket"]
      Resource = "arn:aws:s3:::${var.foundation.buckets.backups}"
      Condition = { StringLike = {
        "s3:prefix" = ["sessions/${var.environment}/${var.session_id}/clickhouse/", "sessions/${var.environment}/${var.session_id}/clickhouse/*"]
      } }
    }
  ] })
}

resource "aws_eks_pod_identity_association" "clickhouse_backup" {
  cluster_name    = aws_eks_cluster.main.name
  namespace       = "research"
  service_account = "research-clickhouse-backup"
  role_arn        = aws_iam_role.clickhouse_backup.arn
}
