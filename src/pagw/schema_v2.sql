CREATE TABLE operation_events (
 id bigserial PRIMARY KEY, operation_id uuid NOT NULL REFERENCES operations,
 claim_token uuid NOT NULL, kind text NOT NULL, outcome text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX operation_events_operation ON operation_events(operation_id,created_at);
CREATE TRIGGER immutable_operation_events BEFORE UPDATE OR DELETE ON operation_events FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
