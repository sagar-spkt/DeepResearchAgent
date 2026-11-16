# ADR-0006: Environment-scoped SNS alert topic

- Status: accepted
- Date: 2026-11-15

## Context

Expiry alarms require an SNS topic ARN, but the foundation did not create or expose one. A read-only inventory of the authorized `research-operator` profile found no SNS topic in `us-east-1`. An alarm action without a topic cannot publish an alert.

## Decision

Create one retained SNS standard topic per environment in the foundation Terraform state and encrypt it with a dedicated symmetric customer-managed KMS key with automatic rotation. Expose both ARNs in the foundation output; pass the topic ARN to the separate expiry stack and the topic/key ARNs into lifecycle configuration. The SNS topic policy preserves account-owner management and permits `sns:Publish` from the CloudWatch service only when the source account matches and the source ARN matches one of this environment's two expiry alarms. The KMS key policy preserves account-owner key management and grants CloudWatch and SNS `kms:GenerateDataKey*` and `kms:Decrypt` only for those alarms and this exact topic's SNS encryption context. Lifecycle inventory checks the exact topic and key ARNs, key state, rotation, tags, and topic-to-key binding without enumerating account-wide SNS topics; if SNS reports a key alias, lifecycle resolves it and requires it to target the configured key ARN. Keep endpoint subscriptions outside Terraform so recipient addresses are not persisted in source/state and an unconfirmed email subscription is not represented as successfully managed. An operator must add and confirm the intended endpoint before relying on delivery.

Include the topic in retained-resource inventory and explicit purge planning. Do not apply this change as part of source implementation.

## Consequences

- Each environment has a stable notification destination and the expiry stack receives the topic ARN through an explicit input.
- The customer-managed key costs $1/month per environment before rotation and request charges; future rotation costs are included as a sensitivity in the cost estimate.
- A topic with no confirmed subscription receives publishes but does not notify an operator; live delivery remains an acceptance test.
- Old local lifecycle configs and saved plans without the new alert ARNs fail closed and must be regenerated after foundation apply.
- Foundation plans made before this change are stale and must be regenerated and reviewed before any future apply.

## References

- [Terraform AWS provider 6.20.0 `aws_sns_topic`](https://registry.terraform.io/providers/hashicorp/aws/6.20.0/docs/resources/sns_topic)
- [Amazon CloudWatch alarm notifications](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Notify_Users_Alarm_Changes.html)
- [SNS KMS key management, service compatibility and costs](https://docs.aws.amazon.com/sns/latest/dg/sns-key-management.html)
- [AWS KMS pricing](https://aws.amazon.com/kms/pricing/)
- [Terraform AWS provider 6.20.0 `aws_sns_topic_subscription`](https://registry.terraform.io/providers/hashicorp/aws/6.20.0/docs/resources/sns_topic_subscription)
