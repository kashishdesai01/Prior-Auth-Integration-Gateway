ALTER TABLE audit ADD COLUMN actor_id text;
-- Nullable for historical records and old writers during an additive rollout.
-- The new application requires actor_id; a later contract migration may enforce it.
ALTER TABLE requests ADD COLUMN submission_payload jsonb;
CREATE TABLE payer_observations (
 id uuid PRIMARY KEY, request_id uuid NOT NULL REFERENCES requests,
 operation_id uuid NOT NULL REFERENCES operations, claim_token uuid NOT NULL,
 source text NOT NULL CHECK(source IN ('SUBMIT','POLL')),
 state text NOT NULL CHECK(state IN ('APPLIED','STALE','CONFLICT','IGNORED')),
 observation jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(operation_id,claim_token)
);
CREATE INDEX payer_observations_request ON payer_observations(request_id,created_at);
CREATE TRIGGER immutable_payer_observations BEFORE UPDATE OR DELETE ON payer_observations FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
