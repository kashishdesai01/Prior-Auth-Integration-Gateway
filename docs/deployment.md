# Deployment

## What is provisioned

The Terraform module deploys immutable images to ECR/Fargate, creates SQS queues with 14-day retention and DLQs, separates submission/webhook worker roles, provisions an HTTPS ALB, security-group rules, CloudWatch logs, and DLQ alarms. It uses an existing VPC, subnets, PostgreSQL deployment, certificate, and service-specific Secrets Manager secrets. It does not create those prerequisites or pretend to be an applied cloud deployment.

The task CPU architecture defaults to `ARM64`, matching the locally verified Linux image. Build/push for `linux/arm64`, or set `cpu_architecture = "X86_64"` and supply a corresponding `linux/amd64` image. An arbitrary image digest does not by itself establish architecture compatibility.

Private task subnets require working egress to the issuer, partners, AWS APIs, registry, and logs through reviewed NAT/endpoints. Public-IP tasks are an explicit optional input, not the default. Configure DNS matching the certificate and `callback_base_url`; the raw ALB hostname output alone will not match a custom ACM certificate.

## Runtime secrets and service roles

Supply `service_secrets`, a map from each deployed service name to `{ arn, kms_key_arn }`. Use a distinct Secrets Manager JSON secret ARN for each service. Each ECS execution role can read only its service's secret and decrypt its configured CMK; application task roles retain their separate queue permissions. Secret JSON documents must contain only the fields needed by that service:

| Service / `PAGW_SERVICE_ROLE` | Secret JSON fields |
|---|---|
| `api` | `database_url`, `callback_secret`, `encryption_key` |
| `worker` | `database_url`, `payer_client_secret`, `client_private_key` |
| `webhooks` | `database_url`, `encryption_key` |
| `relay` | `database_url` |
| `scheduler` | `database_url` |

The API and webhook worker use the same encryption key value, distributed through their own restricted secrets. `client_private_key` contains the legacy partner's PEM signing key. Production subscriber signing secrets are generated per subscription; the local receiver's `webhook_secret` is not injected into production services. Keep secret values out of Terraform variables and plans.

`database_url` must use verified TLS with the appropriate root certificate and a role appropriate to the service. Callback and partner client secrets must be at least 32 characters. The API requires a `client_registry` mapping verified token subjects to tenants and allowed scopes. Access tokens use RS256; verify the real issuer's identity contract before deployment. Production commands must match their configured service role; combined workers and the local `all` role are rejected.

Supply the five restricted secrets before applying the service configuration.

## Migration and rollout

1. Configure encrypted/versioned remote Terraform state and locking with your supported Terraform version. Do not store state in source control.
2. Initialize and validate Terraform. Provision ECR in a reviewed bootstrap step or use your existing image-build process; services require a pushed immutable image digest. There is no executable placeholder image.
3. Build/scan/push the image. Review the Terraform plan with real network IDs, TLS endpoints, service secret ARNs, IAM boundaries, and expected recurring cost. Budget alarms are monitoring, not a spending cap.
4. Run `python -m pagw.cli migrate` as a one-off controlled task using a migration-only credential. Set `PAGW_SERVICE_ROLE=migrate`, provide its migration-only database credential, and verify all three migrations succeed before service rollout. Schema migration and application roles must be separate.
5. Apply the reviewed service deployment and verify ALB readiness, issuer validation, queue processing, an authenticated synthetic submission, webhook delivery, and alarm routing.
6. Preserve the previous image digest for service rollback. Database rollback is not automatic. The three migrations are additive; future migrations require explicit compatibility/rollback review.

The repository includes CI, not an automatically authorized AWS apply workflow. Remote CI, cloud plan/apply, real issuer/payer compatibility, backup restoration, notification delivery, and cloud costs must be verified in the target environment before making those claims.

## Database permissions

Migrations require schema ownership. The runtime role should have SELECT/INSERT/UPDATE only where needed, DELETE only on local queue/replay-cache tables if used, INSERT/SELECT on append-only tables, and sequence access. It must not own tables, disable triggers, create schema objects, or truncate state. Local tests use an isolated owner role to reset `pagw_test`; that is not a production privilege model.

## Schema and worker upgrades

Migration 3 adds HTTP observation history, a nullable audit actor, and a nullable submission payload. Historical actor identities remain unknown; old instances may still write null actors during a rolling deployment. The new API always supplies the verified actor. Enforcing a database NOT NULL constraint requires a later contract migration after old writers are retired.

New admissions store the exact partner JSON payload transactionally and reuse it for every retry/recovery. Existing rows without a snapshot retain the original v1 beta wire mapping, without revalidating stored inputs against new admission rules. Stored idempotency receipts also replay before new admission validation. Do not remove this compatibility path while any pre-snapshot work remains recoverable. For this upgrade, pause admissions while services roll and drain all old submission workers before enabling new API admissions. Deploy and verify the snapshot-aware workers before reopening ingress; old workers cannot consume new snapshot-version requests safely. This upgrade does not claim an automatic zero-downtime rollout; rolling back to an older binary while new-version work is pending requires an explicit compatibility review.

The runtime role needs SELECT/INSERT on `payer_observations`, sequence access where applicable, and access to the new request/audit columns. It must not modify or delete observation history. Verify migration from an existing database as well as a clean installation.
