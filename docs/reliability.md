# Reliability contracts

## Admission and identity

New requests pass FHIR and destination wire-contract validation before admission. Existing immutable receipts replay before today's admission policy is applied. A transaction takes advisory locks in deterministic order for the tenant's idempotency key and business identifier, then writes the request, replay response, initial history, operation, and outbox row. HTTP 202 follows commit. Concurrent duplicate requests either receive the same stored response or conflict on changed content.

Business identity is `(tenant, identifier.system, identifier.value)`. The gateway retains idempotency and business identities indefinitely in this release. There is no automatic identity cleanup that could reopen a duplicate window. Canonical hashing sorts object keys but intentionally preserves array order. A differently ordered Bundle is not promised semantic equivalence.

## Ownership and side effects

`operations` is the source of truth. A claim atomically increments its durable budget and installs a random lease token. HTTP work runs without database locks. Completion locks the request first, then the operation, checks the token and a fresh database clock, and commits result, history, follow-up operations, and outbox rows together. All durable request changes after admission pass through `Store._change`.

The lease is 60 seconds by default. HTTP connect/read limits and bounded response streams constrain normal handler duration; the configuration checks the auth/401 retry budget against the lease. A paused process can still outlive its lease. A successor may then make another external call, but stale completion cannot write local state. Safety at the payer depends on its retained idempotency identity, not on the lease itself.

The synthetic payers retain submission identities in PostgreSQL and reject changed payloads. Recovery always uses the original request UUID. New requests also retain their exact admitted partner payload; pre-snapshot requests use the original v1 translation to preserve their wire identity across this upgrade. Real partners require a reviewed idempotency/lookup contract and retention window. An ambiguous partner without those capabilities would require an uncertain-outcome/manual reconciliation policy; it cannot inherit these mock guarantees automatically.

## Durable scheduling

Retry due times, attempts, generations, and poll schedules are stored in PostgreSQL. A relay publishes batches of at most ten to each destination, marking only successful entries published. Its database transaction stays open during a bounded SDK publish; a crash after publish may create duplicates. This is a deliberate low-volume polling-outbox tradeoff, not a high-throughput CDC design.

The sweeper reissues overdue operations with no recent or unpublished wakeup. Queue retention or a lost/deleted message cannot erase the operation. Failed publication and acknowledgment remain retriable. Business retry counts do not depend on SQS receive counts. An append-only operation event log retains claim and completion observations across explicit recovery cycles.

Malformed messages are immediately quarantined by digest and acknowledged after commit. A malformed envelope does not necessarily identify a request, so it cannot safely mark one failed. SQS DLQs catch transport failures that prevent acknowledgment. Operators inspect and redrive those queues using AWS tooling; durable operation state determines whether an old message can do work.

Submission and webhook workers are separate processes/services. A slow provider receiver does not occupy the payer worker. This release does not provide per-payer circuit breakers or fairness queues; long payer outages can still delay other submissions within the single submission worker. Scaling and per-payer isolation require measured workload evidence and a deliberate concurrency policy.

## Decisions and callbacks

Delivery state and payer decision are separate. The decision is `UNKNOWN`, `PENDED`, `APPROVED`, or `DENIED`. Exhaustion generally changes delivery state to `NEEDS_REVIEW`; it does not imply payer denial. A permanent first-attempt rejection can produce `FAILED` when there was no prior ambiguous attempt.

Callbacks require a payer-specific HMAC, bounded timestamp, UUID correlation, event ID, and sequence. Inbox admission and application commit together. Duplicate IDs with altered content conflict. Unknown correlations remain durable for reconciliation. Lower sequences are stale. Changed terminal decisions or approval references are retained as conflicts without replacing the accepted decision. HTTP results are retained in a separate append-only journal under the completing worker's lease, including contradictory responses arriving after a terminal callback. Duplicate payer-reference ownership is serialized and retained as a conflict rather than reported as database unavailability.

Both synthetic protocols define one monotonic sequence per authorization. This is a partner contract, not a property inferred from SQS ordering or wall-clock timestamps. Arbitrary real partners may not support it.

`EXPIRED` means the gateway's local SLA elapsed; it is not a clinical decision. A later authenticated terminal payer decision can still reconcile the request. Newer pending observations within the SLA can reactivate a stopped poll with a fresh bounded budget and outbox generation; equal/older observations cannot reset that budget or replace an active lease. Pending observations after the SLA preserve expiry and do not restart polling. Local cancellation is permitted only before any submission claim. There is no false promise of remote cancellation once a dispatch could have occurred.

## Webhooks and retention

Every relevant transition creates one immutable event payload per active subscription, a delivery row, and a durable operation. Receiver retries may repeat the external POST; the event ID remains stable, including explicit redelivery. Receivers must deduplicate event IDs and avoid applying an older request version over a newer one. The sample receiver records event identities durably in SQLite; it does not model a production provider's clinical state.

Destinations match one operator-provisioned exact URL. This bounded policy avoids accepting arbitrary user-controlled destinations. The URL and its DNS/egress policy are trusted deployment configuration. It does not claim arbitrary-destination DNS-rebinding protection.

History, HTTP observations, operation events, and audit records have mutation-blocking triggers. A database owner can disable triggers; these are application integrity controls, not protection against a privileged database administrator. No hash-chain immutability claim is made.

This release retains records and payloads without automatic cleanup. Real data requires explicit retention, deletion, backups, and identity-fence tombstone policies before introducing cleanup.

Operator subscription, cancellation, recovery, and redelivery audit records carry the authenticated client subject supplied by the API. Historical records predating migration 3 retain a null actor; no identity is fabricated.
