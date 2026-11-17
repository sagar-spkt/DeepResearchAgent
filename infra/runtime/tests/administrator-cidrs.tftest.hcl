mock_provider "aws" {
  # Keep mocked policy JSON syntactically valid; these tests exercise endpoint
  # CIDR admission, not IAM document generation or AWS authorization.
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
}

variables {
  account_id             = "123456789012"
  environment            = "staging"
  session_id             = "test-1"
  availability_zones     = ["us-east-1a", "us-east-1b"]
  administrator_cidrs    = ["198.51.100.10/32", "203.0.113.20/32"]
  operator_principal_arn = "arn:aws:iam::123456789012:user/test-operator"
  kubernetes_version     = "1.37"
  foundation = {
    buckets           = { documents = "synthetic-documents", traces = "synthetic-traces", backups = "synthetic-backups" }
    queue_url         = "https://sqs.us-east-1.amazonaws.com/123456789012/test"
    queue_arn         = "arn:aws:sqs:us-east-1:123456789012:test"
    alert_topic_arn   = "arn:aws:sns:us-east-1:123456789012:test"
    alert_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-4111-8111-111111111111"
    secrets           = {}
  }
}

run "allow_distinct_nat_and_operator_hosts" {
  command = plan
  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].public_access_cidrs == toset(["198.51.100.10/32", "203.0.113.20/32"])
    error_message = "The reviewed NAT/operator host addresses must reach the public API unchanged."
  }
  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].endpoint_public_access && aws_eks_cluster.main.vpc_config[0].endpoint_private_access
    error_message = "Host restrictions must not disable existing private endpoint access."
  }
}

run "closed_rehearsal_can_avoid_gpu_capacity" {
  command = plan
  variables { gpu_desired_size = 0 }
  assert {
    condition     = aws_eks_node_group.gpu.scaling_config[0].desired_size == 0
    error_message = "A closed, no-model rehearsal must be able to leave the GPU node group at zero desired nodes."
  }
}

run "public_staging_keeps_gpu_default" {
  command = plan
  assert {
    condition     = aws_eks_node_group.gpu.scaling_config[0].desired_size == 1
    error_message = "Public vLLM staging must retain one GPU node by default."
  }
}

run "reject_more_than_one_gpu_node" {
  command = plan
  variables { gpu_desired_size = 2 }
  expect_failures = [var.gpu_desired_size]
}

run "reject_fractional_gpu_node_count" {
  command = plan
  variables { gpu_desired_size = 0.5 }
  expect_failures = [var.gpu_desired_size]
}

run "reject_unrestricted_internet" {
  command = plan
  variables { administrator_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.administrator_cidrs]
}
run "reject_network_range" {
  command = plan
  variables { administrator_cidrs = ["198.51.100.0/24"] }
  expect_failures = [var.administrator_cidrs]
}
run "reject_empty_list" {
  command = plan
  variables { administrator_cidrs = [] }
  expect_failures = [var.administrator_cidrs]
}
run "reject_duplicates" {
  command = plan
  variables { administrator_cidrs = ["198.51.100.10/32", "198.51.100.10/32"] }
  expect_failures = [var.administrator_cidrs]
}
run "reject_malformed_address" {
  command = plan
  variables { administrator_cidrs = ["999.1.1.1/32"] }
  expect_failures = [var.administrator_cidrs]
}
run "reject_ipv6" {
  command = plan
  variables { administrator_cidrs = ["2001:db8::1/128"] }
  expect_failures = [var.administrator_cidrs]
}
run "reject_noncanonical_host" {
  command = plan
  variables { administrator_cidrs = ["198.051.100.10/32"] }
  expect_failures = [var.administrator_cidrs]
}
