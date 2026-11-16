resource "aws_db_instance" "main" {

  identifier                  = local.name
  engine                      = "postgres"
  engine_version              = "16"
  instance_class              = var.database_instance_class
  allocated_storage           = 20
  max_allocated_storage       = 40
  storage_encrypted           = true
  username                    = var.database_snapshot_identifier == null ? "researchadmin" : null
  manage_master_user_password = var.database_snapshot_identifier == null ? true : null
  snapshot_identifier         = var.database_snapshot_identifier
  db_name                     = var.database_snapshot_identifier == null ? "research" : null
  db_subnet_group_name        = aws_db_subnet_group.database.name
  vpc_security_group_ids      = [aws_security_group.database.id]
  publicly_accessible         = false
  multi_az                    = false
  backup_retention_period     = 7
  skip_final_snapshot         = false
  final_snapshot_identifier   = "${local.name}-${var.session_id}-final"
  deletion_protection         = true
  auto_minor_version_upgrade  = true

}

output "runtime" {

  value = {

    cluster_name         = aws_eks_cluster.main.name
    database_endpoint    = aws_db_instance.main.address
    database_identifier  = aws_db_instance.main.identifier
    node_role_arn        = aws_iam_role.node.arn
    application_role_arn = aws_iam_role.application.arn

  }


}

output "cluster_admin_role_arn" {
  value = aws_iam_role.cluster_admin.arn
}

