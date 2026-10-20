# Operations runbook

## Find a request

Use the console timeline or tenant-scoped `GET /api/requests/{id}`. Delivery state is operational; the payer decision is separate. Inspect operation events, callback/HTTP observations, and delivered webhook versions before taking recovery action. Never assume a network timeout means the payer did not accept a request.

## NEEDS_REVIEW

1. Inspect the request's fixed error code, operation kind, attempts, and retained callback/HTTP conflicts.
2. Check partner authentication, availability, and its authorization lookup by the original submission identity. Resolve the underlying problem first.
3. An operator may call `POST /api/requests/{id}/recover` with an `ops` scope. The same payer idempotency key and admitted wire payload are reused and an actor-attributed audit record is appended. Confirm the new operation and final decision. Never manually change the request UUID or cached identity fences.

Local SLA expiry is not a clinical denial. Late authoritative decisions remain reconcilable. A contradictory terminal observation is retained as `CONFLICT`; there is no automated override of a prior clinical decision in this release.

## Overdue operations / outbox publication

Inspect `pagw_due_operation_lag_seconds`, relay logs, and SQS connectivity. `pagw_durable_operations{state="READY"}` shows outstanding work; `RUNNING` work may recover after its lease expires. Restarting relay, scheduler, or worker is safe with respect to committed local state. Acknowledgment failures can generate duplicate wakeups.

If no work progresses, check database connectivity, service credentials, queue URLs/region, and worker process health. The outbox relay handles partial SDK batch success entry by entry. Do not mark all rows published manually after a partially failed batch.

## Dead provider webhooks

Inspect the tenant-scoped `/webhooks/deliveries`. Correct receiver availability or signature configuration, then call `POST /webhooks/deliveries/{id}/redeliver` with a `webhooks` scope. The event ID and payload are preserved. A provider receiver must deduplicate repeated event IDs and use request versions when maintaining state.

## Transport DLQ / quarantine

Malformed message envelopes are quarantined by digest without storing their body. Inspect recent deploy/schema changes and queue producers. SQS redrive is an operational action using the AWS console/CLI after correcting the cause. Redriving an old wakeup cannot bypass a completed/dead operation's database state. Redrive alone does not authorize a new submission identity.

The Terraform DLQ alarms require environment-specific notification routing. Configure and test alarm actions before describing them as operational alerting.

## Database recovery

Preserve the database as the authority. Rebuilding queues is supported; deleting database identity fences is not. Before real deployment, test backups and restoration of requests, operation leases, inbox, idempotency, subscription secrets, and audit records together. Restore the matching encryption key. No backup/restore RTO or RPO has been measured here.

For native development, `make down` stops registered processes and preserves the database; `make up` restarts them. Use `scripts/dev.py up --sqs` when the emulator configuration was bootstrapped. Logs are in `.runtime/`. Compose logs use `docker compose logs --tail=100 SERVICE`.
