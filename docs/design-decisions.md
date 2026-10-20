# Design decisions

## PostgreSQL owns progress

Requests, identities, operations, inbox observations, retry schedules, and histories share a transactional database. This makes the admission and completion invariants concrete and independently inspectable. Queues provide replaceable delivery hints. It avoids making queue visibility, retention, or receive counts the authority for business progress.

The tradeoff is a deliberate dependency on PostgreSQL availability and disciplined transaction/lock ordering. Local development uses a simple connection-per-transaction approach; production connection limits/pooling require measured workload configuration.

## Standard SQS and durable operation claims

Ordering belongs to the partner event sequence and database state, not queue ordering. Standard SQS duplicates are expected. A claim token plus fresh database-time check fences local results; the payer's retained submission identity fences its own synthetic authorization creation. Neither mechanism guarantees exactly-once network delivery.

The relay holds a small locked batch while publishing through a bounded SDK client. It favors a simple auditable design over CDC complexity at this project's scale. Partial batch success is explicitly tested.

## Separate delivery and decision

An operational timeout and a clinical denial mean different things. Keeping the two states separate lets the gateway stop automatic retries while still accepting late authenticated payer observations. Conflicts are retained instead of silently changing a terminal clinical decision. Cancellation is intentionally limited to before the first dispatch claim.

## Bounded interoperability

The gateway uses the official R4 JSON schema and explicit reference checks. The supported Claim subset is narrower than all valid R4 resources: one focal coverage, coded diagnoses, flat service lines, positive quantities, and CPT/HCPCS namespaces. Unsupported resource, insurance, claim, and service fields are rejected rather than accepted and silently lost in the legacy mapper. Unknown modifiers are rejected recursively. Inputs with metadata that cannot be embedded under R4 contained-resource rules are rejected without stripping security labels. The actual legacy wire model is checked before admission. No PAS-profile or terminology-conformance claim is made.

The legacy wire contract contains separate member, provider, diagnosis, priority, and service data. It does not contain the source Bundle. Mapping tests verify identity, line relationships, code systems, and quantities/units across this boundary.

## Fixed webhook destinations

An exact configured destination is a practical security boundary for a bounded integration service. Arbitrary customer webhook URLs would need connection-time IP validation, egress controls, subscription verification, and operational policies. Those capabilities are deliberately not implied by the current allowlist.

## Small operator interface

A dependency-free HTML/JavaScript console exposes the important behavior: durable acceptance, progress, attempts, conflicts, recovery, and the FHIR response. Request data is rendered using text nodes, tokens remain in tab memory, and production hides local demo controls. The interface supports mobile stacking and a desktop split view. The backend reliability contracts remain testable without the UI.
