resource "aws_s3_bucket" "data" {
  for_each      = toset(["documents", "traces"])
  bucket        = "${local.name}-${each.key}-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "data" {
  for_each                = aws_s3_bucket.data
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "tls" {
  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.data]
}

resource "aws_sqs_queue" "dead" {
  name                      = "${local.name}-dead"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "research" {
  name                       = "${local.name}-research"
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 960
  receive_wait_time_seconds  = 20
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dead.arn, maxReceiveCount = 3 })
}

resource "aws_ecr_repository" "images" {
  for_each             = toset(["backend", "web"])
  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "images" {
  for_each   = aws_ecr_repository.images
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_security_group" "database" {
  name_prefix = "${local.name}-db-"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "database" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_db_subnet_group" "database" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "main" {
  identifier              = local.name
  engine                  = "postgres"
  engine_version          = "16"
  instance_class          = var.database_instance_class
  allocated_storage       = 20
  max_allocated_storage   = 40
  storage_encrypted       = true
  username                = "researchadmin"
  password                = random_password.database["master"].result
  db_name                 = "postgres"
  db_subnet_group_name    = aws_db_subnet_group.database.name
  vpc_security_group_ids  = [aws_security_group.database.id]
  publicly_accessible     = false
  multi_az                = false
  backup_retention_period = 1
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true
}
