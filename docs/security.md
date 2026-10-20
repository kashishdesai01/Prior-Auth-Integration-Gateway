# Security boundary

Only synthetic data is permitted. The gateway is not HIPAA-certified or assessed for real PHI. FHIR schema validation is not a clinical, terminology, PAS-profile, or regulatory assessment.

## Authentication and tenancy

The resource server validates RS256 access tokens against a configured issuer, audience, expiry, subject, and cached fixed JWKS URL. Unknown key refresh is rate bounded. Arbitrary token `jku` URLs are never followed. Tenant identity comes from a server-side client registry, not a caller-provided tenant header. Granted token scopes are intersected with that registry. Resource reads, cancellation, recovery, webhook access, and metrics are tenant-scoped.

Local mock client credentials use one-time RS384 private-key JWT assertions with audience and expiry validation and a PostgreSQL-backed issuer/JTI uniqueness fence. These are controlled fixtures sharing generated local key material. They are not a full SMART authorization server. Production requires an explicit `PAGW_CLIENT_REGISTRY` and trusted real issuer/JWKS endpoints. Mock servers and demo token/fixture endpoints refuse production mode.

## Data and network

The API limits inbound bodies to 1 MiB and bounds bundle/service counts. Errors use fixed diagnostic codes and locations without echoing input. Application logs accept an explicit field allowlist; access logging and partner HTTP debug logging are disabled. Traces contain operation metadata and propagated context, not resource payloads. This is tested scrubbing, not an assurance about every third-party collector or future code change.

Subscription secrets are encrypted with Fernet using an externally supplied key. Signed callback keys are derived separately for each configured synthetic payer. Signatures cover timestamp and raw body. Provider webhook timestamps are refreshed per attempt; stable event IDs provide deduplication. Outbound HTTP disables redirects and environment proxy discovery, bounds successful response reads, rejects compression, and closes webhook responses without consuming their body.

Operator audit actors come from the verified client subject; request headers cannot override them. Production roles require only their service-specific credentials, distributed through separate Secrets Manager secrets and execution roles.

Production requires TLS URLs and real SQS. Configure verified PostgreSQL TLS in the database DSN, private networking, least-privilege application and migration roles, secret rotation, egress restrictions, authentication throttling/WAF rules, and authenticated telemetry collection. The Terraform module provides HTTPS ingress and distinct service queue and secret-execution roles; it does not provision the organization's complete security controls.

## Explicit operational limits

Local demo login deliberately grants a synthetic operator identity without a password and must remain loopback/private. Production disables it. Do not expose local mode through a public tunnel.

The sample receiver uses SQLite only for durable synthetic event deduplication. Gateway and mock-payer authority use PostgreSQL. No real provider callback processing, BAA, penetration test, formal threat-model signoff, multi-region availability, or production authorization-server integration has been verified.
