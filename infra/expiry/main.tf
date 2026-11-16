terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.20.0" }
  }
  backend "s3" {}
}
provider "aws" { region = var.region }
variable "region" { type = string }
variable "environment" {
  type = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Only staging/production."
  }
}
variable "session_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,15}$", var.session_id))
    error_message = "Session ID must be at most16 safe characters."
  }
}
variable "expire_at_utc" { type = string }
variable "source_bucket" { type = string }
variable "source_key" { type = string }
variable "source_version_id" { type = string }
variable "source_kms_key_arn" { type = string }
variable "runner_image_digest" {
  type = string
  validation {
    condition     = can(regex("^.+@sha256:[a-f0-9]{64}$", var.runner_image_digest))
    error_message = "Runner image must be immutable."
  }
}
variable "runner_repository_arn" { type = string }
variable "approved_intent_sha256" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.approved_intent_sha256))
    error_message = "An explicitly reviewed expiry intent hash is required."
  }
}
variable "executor_policy_json" {
  type        = string
  description = "Reviewed environment/resource-scoped AWS actions for the exact runtime plan, backups and read-only verification. No default broad policy."
}
variable "management_vpc_id" { type = string }
variable "runtime_vpc_id" { type = string }
variable "management_subnet_ids" { type = list(string) }
variable "management_security_group_ids" { type = list(string) }
variable "alert_topic_arn" { type = string }
data "aws_caller_identity" "current" {}
locals { name = "deepresearch-${var.environment}-expiry-${var.session_id}" }
resource "aws_cloudwatch_log_group" "executor" {
  name              = "/aws/codebuild/${local.name}"
  retention_in_days = 7
}
resource "aws_iam_role" "executor" {
  name               = local.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "codebuild.amazonaws.com" }, Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }, ArnEquals = { "aws:SourceArn" = "arn:aws:codebuild:${var.region}:${data.aws_caller_identity.current.account_id}:project/${local.name}" } } }] })
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role_policy" "executor" {
  role   = aws_iam_role.executor.id
  policy = var.executor_policy_json
}
resource "aws_iam_role_policy" "artifact_and_logs" {
  role = aws_iam_role.executor.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = "arn:aws:s3:::${var.source_bucket}/${var.source_key}", Condition = { StringEquals = { "s3:VersionId" = var.source_version_id } } },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.source_kms_key_arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.executor.arn}:*" },
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"], Resource = var.runner_repository_arn }
  ] })
}
resource "aws_codebuild_project" "executor" {
  name           = local.name
  service_role   = aws_iam_role.executor.arn
  build_timeout  = 90
  source_version = var.source_version_id
  artifacts { type = "NO_ARTIFACTS" }
  source {
    type     = "S3"
    location = "${var.source_bucket}/${var.source_key}"
    buildspec = jsonencode({ version = 0.2, phases = {
      pre_build = { commands = [
        "terraform -chdir=infra/runtime init -backend-config=artifacts/runtime.backend.hcl -lockfile=readonly",
        "aws eks update-kubeconfig --name \"$CLUSTER_NAME\" --region \"$AWS_REGION\""
      ] },
      build = { commands = ["python3 scripts/platform_lifecycle.py expiry --plan artifacts/expiry-intent.json --approved-plan-hash \"$APPROVED_INTENT_SHA\" --workspace \"$CODEBUILD_SRC_DIR\" --execute --output artifacts/expiry-result.json"] }
    } })
  }
  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = var.runner_image_digest
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "SERVICE_ROLE"
    privileged_mode             = false
    environment_variable {
      name  = "APPROVED_INTENT_SHA"
      value = var.approved_intent_sha256
    }
    environment_variable {
      name  = "CLUSTER_NAME"
      value = "deepresearch-${var.environment}"
    }
  }
  vpc_config {
    vpc_id             = var.management_vpc_id
    subnets            = var.management_subnet_ids
    security_group_ids = var.management_security_group_ids
  }
  logs_config {
    cloudwatch_logs { group_name = aws_cloudwatch_log_group.executor.name }
  }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = var.management_vpc_id != var.runtime_vpc_id
      error_message = "Executor cannot run in the VPC it destroys."
    }
  }
}
resource "aws_iam_role" "state_machine" {
  name               = "${local.name}-states"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "states.amazonaws.com" }, Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } } }] })
}
resource "aws_iam_role_policy" "state_machine" {
  role = aws_iam_role.state_machine.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["codebuild:StartBuild"], Resource = aws_codebuild_project.executor.arn },
    { Effect = "Allow", Action = ["codebuild:StopBuild", "codebuild:BatchGetBuilds"], Resource = "arn:aws:codebuild:${var.region}:${data.aws_caller_identity.current.account_id}:build/${local.name}:*" },
    { Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = "arn:aws:events:${var.region}:${data.aws_caller_identity.current.account_id}:rule/StepFunctionsGetEventForCodeBuildStartBuildRule" }
  ] })
}
resource "aws_sfn_state_machine" "expiry" {
  name     = local.name
  role_arn = aws_iam_role.state_machine.arn
  definition = jsonencode({ StartAt = "BackupAndShutdown", States = {
    BackupAndShutdown = { Type = "Task", Resource = "arn:aws:states:::codebuild:startBuild.sync", Parameters = { ProjectName = aws_codebuild_project.executor.name }, TimeoutSeconds = 5400, End = true }
  } })
}
resource "aws_sqs_queue" "dead" {
  name                      = "${local.name}-scheduler-dead"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
}
resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "scheduler.amazonaws.com" }, Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } } }] })
}
resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["states:StartExecution"], Resource = aws_sfn_state_machine.expiry.arn },
    { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.dead.arn }
  ] })
}
resource "aws_scheduler_schedule" "expiry" {
  depends_on                   = [aws_eks_access_entry.executor, aws_iam_role_policy.describe_cluster]
  name                         = local.name
  schedule_expression          = "at(${var.expire_at_utc})"
  schedule_expression_timezone = "UTC"
  action_after_completion      = "DELETE"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_sfn_state_machine.expiry.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ session_id = var.session_id, environment = var.environment, approved_intent_sha256 = var.approved_intent_sha256 })
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 1
    }
    dead_letter_config { arn = aws_sqs_queue.dead.arn }
  }
}
resource "aws_cloudwatch_metric_alarm" "expiry_failure" {
  alarm_name          = "${local.name}-failed"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.expiry.arn }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  alarm_actions       = [var.alert_topic_arn]
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "scheduler_dead_letter" {
  # SQS backlog metrics are approximate; require two samples to avoid paging on a stale single datapoint.
  alarm_name          = "${local.name}-scheduler-dlq-visible"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dead.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  alarm_actions       = [var.alert_topic_arn]
  treat_missing_data  = "notBreaching"
}
