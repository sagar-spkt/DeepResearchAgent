resource "aws_vpc" "main" {

  cidr_block           = "10.60.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "public" {

  count                   = 2
  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.availability_zones[count.index]
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  map_public_ip_on_launch = true
  tags = {
    "kubernetes.io/role/elb" = "1"
  }


}

resource "aws_subnet" "gpu" {
  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.gpu_availability_zone
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, 2)
  map_public_ip_on_launch = true
  tags = {
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_route_table_association" "gpu" {
  subnet_id      = aws_subnet.gpu.id
  route_table_id = aws_route_table.public.id
}

resource "aws_subnet" "gpu_fallback" {
  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.gpu_fallback_availability_zone
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, 3)
  map_public_ip_on_launch = true
  tags = {
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_route_table_association" "gpu_fallback" {
  subnet_id      = aws_subnet.gpu_fallback.id
  route_table_id = aws_route_table.public.id
}

resource "aws_subnet" "private" {

  count                   = 2
  vpc_id                  = aws_vpc.main.id
  availability_zone       = var.availability_zones[count.index]
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 10)
  map_public_ip_on_launch = false

}

resource "aws_route_table" "public" {

  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }


}

resource "aws_route_table_association" "public" {

  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id

}

resource "aws_security_group" "database" {

  name_prefix = "${local.name}-db-"
  vpc_id      = aws_vpc.main.id

}

resource "aws_vpc_security_group_ingress_rule" "database" {

  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
  tags                         = { Component = "database-ingress" }
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"

}

resource "aws_db_subnet_group" "database" {

  name       = local.name
  subnet_ids = aws_subnet.private[*].id

}

