CREATE TABLE requests (
 id uuid PRIMARY KEY, tenant text NOT NULL, business_system text NOT NULL,
 business_value text NOT NULL, digest text NOT NULL, bundle jsonb NOT NULL,
 payer text NOT NULL CHECK(payer IN ('alpha','beta')),
 delivery_state text NOT NULL DEFAULT 'RECEIVED' CHECK(delivery_state IN
 ('RECEIVED','RETRYING','SUBMITTED','NEEDS_REVIEW','FAILED','EXPIRED','CANCELLED')),
 decision text NOT NULL DEFAULT 'UNKNOWN' CHECK(decision IN ('UNKNOWN','PENDED','APPROVED','DENIED')),
 payer_ref text, preauth_ref text, last_sequence bigint NOT NULL DEFAULT -1,
 version int NOT NULL DEFAULT 0, last_error text,
 expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(tenant,business_system,business_value), UNIQUE(payer,payer_ref)
);
CREATE INDEX requests_tenant_created ON requests(tenant,created_at DESC);
CREATE TABLE idempotency (
 tenant text NOT NULL, key text NOT NULL, digest text NOT NULL, request_id uuid NOT NULL REFERENCES requests,
 response jsonb NOT NULL, response_code int NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant,key)
);
CREATE TABLE history (
 id bigserial PRIMARY KEY, request_id uuid NOT NULL REFERENCES requests, version int NOT NULL,
 kind text NOT NULL, delivery_state text NOT NULL, decision text NOT NULL, reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(request_id,version)
);
CREATE TABLE subscriptions (
 id uuid PRIMARY KEY, tenant text NOT NULL, url text NOT NULL, secret bytea NOT NULL,
 enabled boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(tenant,url)
);
CREATE TABLE deliveries (
 id uuid PRIMARY KEY, subscription_id uuid NOT NULL REFERENCES subscriptions,
 request_id uuid NOT NULL REFERENCES requests, version int NOT NULL, event_id uuid NOT NULL UNIQUE,
 payload jsonb NOT NULL, state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','DELIVERED','DEAD')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), delivered_at timestamptz,
 UNIQUE(subscription_id,request_id,version)
);
CREATE TABLE operations (
 id uuid PRIMARY KEY, request_id uuid REFERENCES requests, delivery_id uuid UNIQUE REFERENCES deliveries,
 kind text NOT NULL CHECK(kind IN ('SUBMIT','POLL','WEBHOOK')),
 state text NOT NULL DEFAULT 'READY' CHECK(state IN ('READY','RUNNING','DONE','DEAD')),
 attempts int NOT NULL DEFAULT 0, generation int NOT NULL DEFAULT 0,
 due_at timestamptz NOT NULL DEFAULT clock_timestamp(), lease_token uuid, lease_until timestamptz,
 last_error text, CHECK((kind='WEBHOOK' AND delivery_id IS NOT NULL AND request_id IS NULL)
 OR (kind IN ('SUBMIT','POLL') AND request_id IS NOT NULL AND delivery_id IS NULL)),
 CHECK((state='RUNNING' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
 OR (state<>'RUNNING' AND lease_token IS NULL AND lease_until IS NULL))
);
CREATE UNIQUE INDEX operation_per_request ON operations(request_id,kind) WHERE request_id IS NOT NULL;
CREATE INDEX operations_due ON operations(due_at) WHERE state IN ('READY','RUNNING');
CREATE TABLE outbox (
 id uuid PRIMARY KEY, operation_id uuid NOT NULL REFERENCES operations, generation int NOT NULL,
 queue text NOT NULL CHECK(queue IN ('submit','webhook')), traceparent text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), published_at timestamptz,
 UNIQUE(operation_id,generation)
);
CREATE INDEX outbox_unpublished ON outbox(created_at) WHERE published_at IS NULL;
CREATE TABLE local_messages (
 id uuid PRIMARY KEY, body jsonb NOT NULL, queue text NOT NULL, receipt uuid,
 available_at timestamptz NOT NULL DEFAULT clock_timestamp(), receives int NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX local_messages_available ON local_messages(available_at);
CREATE TABLE quarantine (
 id uuid PRIMARY KEY, queue text NOT NULL, body_hash text NOT NULL, reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE inbox (
 payer text NOT NULL, event_id text NOT NULL, digest text NOT NULL, event jsonb NOT NULL,
 state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','APPLIED','STALE','CONFLICT','UNMATCHED')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(payer,event_id)
);
CREATE TABLE audit (
 id bigserial PRIMARY KEY, tenant text NOT NULL, action text NOT NULL, resource_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append-only table'; END $$;
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON history FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER immutable_audit BEFORE UPDATE OR DELETE ON audit FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TABLE assertion_jtis (issuer text NOT NULL, jti text NOT NULL, expires_at timestamptz NOT NULL, PRIMARY KEY(issuer,jti));
CREATE TABLE mock_authorizations (
 id uuid PRIMARY KEY, payer text NOT NULL, submission_key text NOT NULL, digest text NOT NULL,
 correlation_id uuid NOT NULL, bundle jsonb NOT NULL, code text NOT NULL, decision text NOT NULL, sequence bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), ready_at timestamptz NOT NULL,
 callback_sent boolean NOT NULL DEFAULT false, UNIQUE(payer,submission_key)
);
CREATE TABLE mock_modes (payer text PRIMARY KEY, mode text NOT NULL DEFAULT 'normal', remaining int NOT NULL DEFAULT 0);
INSERT INTO mock_modes(payer) VALUES ('alpha'),('beta');
