terraform {
  required_version = ">= 1.5, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  # Configure an encrypted, versioned remote backend during initialization.
  # Credentials and secret values must never be committed or passed as TF vars.
}

provider "aws" { region = var.region }

variable "region" { default = "us-west-2" }
variable "name" { default = "pagw-dev" }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "task_subnet_ids" { type = list(string) }
variable "database_security_group_id" { type = string }
variable "certificate_arn" { type = string }
variable "image_digest" {
  type        = string
  description = "Immutable sha256 digest of the image pushed to the created ECR repository"
  validation {
    condition     = can(regex("^sha256:[a-f0-9]{64}$", var.image_digest))
    error_message = "Supply an immutable image digest."
  }
}
variable "cpu_architecture" {
  type    = string
  default = "ARM64"
  validation {
    condition     = contains(["ARM64", "X86_64"], var.cpu_architecture)
    error_message = "Select ARM64 or X86_64 and build a matching Linux image."
  }
}
variable "service_secrets" {
  type        = map(object({ arn = string, kms_key_arn = string }))
  description = "Separate Secrets Manager JSON secret and CMK ARN per service; fields documented in deployment.md"
  validation {
    condition     = toset(keys(var.service_secrets)) == toset(["api", "relay", "worker", "webhooks", "scheduler"])
    error_message = "Supply exactly one secret configuration for each deployed service."
  }
  validation {
    condition     = length(distinct([for secret in values(var.service_secrets) : secret.arn])) == 5
    error_message = "Services must use distinct Secrets Manager secret ARNs."
  }
}
variable "issuer" { type = string }
variable "jwks_url" { type = string }
variable "payer_base_url" { type = string }
variable "callback_base_url" { type = string }
variable "receiver_url" { type = string }
variable "client_registry" { type = map(object({ tenant = string, scopes = list(string) })) }
variable "assign_public_ip" {
  default     = false
  description = "Private tasks require configured NAT or VPC endpoints plus partner egress"
}
locals {
  services = toset(["api", "relay", "worker", "webhooks", "scheduler"])
  environment = {
    PAGW_MODE              = "production", PAGW_BROKER = "sqs", PAGW_AWS_REGION = var.region,
    PAGW_ISSUER            = var.issuer, PAGW_JWKS_URL = var.jwks_url,
    PAGW_PAYER_BASE_URL    = var.payer_base_url, PAGW_CALLBACK_BASE_URL = var.callback_base_url,
    PAGW_RECEIVER_URL      = var.receiver_url, PAGW_CLIENT_REGISTRY = jsonencode(var.client_registry),
    PAGW_SUBMIT_QUEUE_URL  = aws_sqs_queue.work["submit"].url,
    PAGW_WEBHOOK_QUEUE_URL = aws_sqs_queue.work["webhook"].url
  }
  service_secret_fields = {
    api       = ["database_url", "callback_secret", "encryption_key"]
    worker    = ["database_url", "payer_client_secret", "client_private_key"]
    webhooks  = ["database_url", "encryption_key"]
    relay     = ["database_url"]
    scheduler = ["database_url"]
  }
}

resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_sqs_queue" "dead" {
  for_each                  = toset(["submit", "webhook"])
  name                      = "${var.name}-${each.key}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}
resource "aws_sqs_queue" "work" {
  for_each                   = toset(["submit", "webhook"])
  name                       = "${var.name}-${each.key}"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 1209600
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dead[each.key].arn, maxReceiveCount = 6 })
}
resource "aws_sqs_queue_redrive_allow_policy" "dead" {
  for_each             = aws_sqs_queue.dead
  queue_url            = each.value.id
  redrive_allow_policy = jsonencode({ redrivePermission = "byQueue", sourceQueueArns = [aws_sqs_queue.work[each.key].arn] })
}
resource "aws_sqs_queue_policy" "tls" {
  for_each  = merge({ for k, q in aws_sqs_queue.work : "work-${k}" => q }, { for k, q in aws_sqs_queue.dead : "dead-${k}" => q })
  queue_url = each.value.id
  policy    = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Principal = "*", Action = "sqs:*", Resource = each.value.arn, Condition = { Bool = { "aws:SecureTransport" = "false" } } }] })
}
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name}"
  retention_in_days = 14
}
resource "aws_ecs_cluster" "app" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}
data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "execution" {
  for_each           = local.services
  name               = "${var.name}-${each.key}-execution"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}
resource "aws_iam_role_policy" "execution" {
  for_each = local.services
  role     = aws_iam_role.execution[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = aws_ecr_repository.app.arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.app.arn}:*" },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.service_secrets[each.key].arn },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.service_secrets[each.key].kms_key_arn }
  ] })
}
resource "aws_iam_role" "task" {
  for_each           = local.services
  name               = "${var.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}
resource "aws_iam_role_policy" "relay" {
  role   = aws_iam_role.task["relay"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = [for q in aws_sqs_queue.work : q.arn] }] })
}
resource "aws_iam_role_policy" "worker" {
  for_each = toset(["worker", "webhooks"])
  role     = aws_iam_role.task[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage"], Resource = aws_sqs_queue.work[each.key == "worker" ? "submit" : "webhook"].arn }] })
}
resource "aws_security_group" "alb" {
  name   = "${var.name}-alb"
  vpc_id = var.vpc_id
}
resource "aws_security_group" "tasks" {
  name   = "${var.name}-tasks"
  vpc_id = var.vpc_id
}
resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "api" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "database" {
  security_group_id            = var.database_security_group_id
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_egress_rule" "alb" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 8080
  to_port                      = 8080
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_egress_rule" "database" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = var.database_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}
resource "aws_lb" "app" {
  name                       = var.name
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = var.public_subnet_ids
  drop_invalid_header_fields = true
  enable_deletion_protection = true
}
resource "aws_lb_target_group" "api" {
  name        = var.name
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"
  health_check {
    path    = "/readyz"
    matcher = "200"
  }
}
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.app.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
resource "aws_ecs_task_definition" "app" {
  for_each                 = local.services
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution[each.key].arn
  task_role_arn            = aws_iam_role.task[each.key].arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }
  container_definitions = jsonencode([{
    name             = each.key, image = "${aws_ecr_repository.app.repository_url}@${var.image_digest}", essential = true,
    command          = each.key == "api" ? ["api", "--host", "0.0.0.0", "--port", "8080"] : each.key == "worker" ? ["worker", "--queue", "submit"] : each.key == "webhooks" ? ["worker", "--queue", "webhook"] : [each.key],
    user             = "10001", readonlyRootFilesystem = true,
    linuxParameters  = { capabilities = { drop = ["ALL"] } },
    portMappings     = each.key == "api" ? [{ containerPort = 8080, protocol = "tcp" }] : [],
    environment      = [for k, v in merge(local.environment, { PAGW_SERVICE_ROLE = each.key }) : { name = k, value = v }],
    secrets          = [for k in local.service_secret_fields[each.key] : { name = "PAGW_${upper(k)}", valueFrom = "${var.service_secrets[each.key].arn}:${k}::" }],
    logConfiguration = { logDriver = "awslogs", options = { awslogs-group = aws_cloudwatch_log_group.app.name, awslogs-region = var.region, awslogs-stream-prefix = each.key } }
  }])
}
resource "aws_ecs_service" "app" {
  for_each                          = local.services
  name                              = each.key
  cluster                           = aws_ecs_cluster.app.id
  task_definition                   = aws_ecs_task_definition.app[each.key].arn
  desired_count                     = 1
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = each.key == "api" ? 30 : null
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = var.task_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = var.assign_public_ip
  }
  dynamic "load_balancer" {
    for_each = each.key == "api" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.api.arn
      container_name   = "api"
      container_port   = 8080
    }
  }
  depends_on = [aws_lb_listener.https, aws_iam_role_policy.execution]
}
resource "aws_cloudwatch_metric_alarm" "dlq" {
  for_each            = aws_sqs_queue.dead
  alarm_name          = "${var.name}-${each.key}-dlq"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = each.value.name }
}
output "ecr_repository" { value = aws_ecr_repository.app.repository_url }
output "https_endpoint" { value = "https://${aws_lb.app.dns_name}" }
output "queues" { value = { for k, q in aws_sqs_queue.work : k => q.url } }
