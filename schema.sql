-- Fernhollow Credit Union demo schema.
-- core.*  = the fake core banking system (system of record, written only by core.py)
-- app.*   = the agent side: conversations, actions, approvals, cost ledger, eval runs
DROP SCHEMA IF EXISTS core CASCADE;
DROP SCHEMA IF EXISTS app CASCADE;
CREATE SCHEMA core;
CREATE SCHEMA app;

-- Append-only guard: audit tables reject UPDATE and DELETE. TRUNCATE is not blocked: the eval runner
-- and the canary reset the fake core (core.*) with it, and app.* is left alone by those resets.
CREATE FUNCTION app.forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% on %.% is not allowed: append-only table',
    TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME;
END $$;

CREATE TABLE core.member (
  member_id   text PRIMARY KEY,
  full_name   text NOT NULL,
  phone_last4 char(4) NOT NULL,
  address     text NOT NULL
);

CREATE TABLE core.membership (
  membership_no text PRIMARY KEY,
  label         text NOT NULL
);

CREATE TABLE core.membership_holder (
  membership_no text NOT NULL REFERENCES core.membership,
  member_id     text NOT NULL REFERENCES core.member,
  role          text NOT NULL CHECK (role IN ('primary', 'joint')),
  PRIMARY KEY (membership_no, member_id)
);

CREATE TABLE core.account (
  account_id    text PRIMARY KEY,
  membership_no text NOT NULL REFERENCES core.membership,
  kind          text NOT NULL CHECK (kind IN ('checking', 'savings')),
  balance_cents bigint NOT NULL CHECK (balance_cents >= 0)
);

CREATE TABLE core.card (
  card_id    text PRIMARY KEY,
  account_id text NOT NULL REFERENCES core.account,
  last4      char(4) NOT NULL,
  status     text NOT NULL CHECK (status IN ('active', 'frozen'))
);

CREATE TABLE core.loan (
  loan_id           text PRIMARY KEY,
  membership_no     text NOT NULL REFERENCES core.membership,
  kind              text NOT NULL,
  principal_cents   bigint NOT NULL CHECK (principal_cents >= 0),
  payment_due_cents bigint NOT NULL,
  due_date          date NOT NULL
);

CREATE TABLE core.txn (
  txn_id       bigserial PRIMARY KEY,
  account_id   text NOT NULL REFERENCES core.account,
  posted_on    date NOT NULL,
  amount_cents bigint NOT NULL,
  description  text NOT NULL
);

-- Every write the core accepted, keyed by the caller's idempotency key.
-- A replay with the same key and request returns the stored response;
-- the same key with a different request is refused.
CREATE TABLE core.operation (
  idempotency_key text PRIMARY KEY,
  operation       text NOT NULL,
  request         jsonb NOT NULL,
  response        jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER operation_append_only BEFORE UPDATE OR DELETE ON core.operation
  FOR EACH ROW EXECUTE FUNCTION app.forbid_change();

CREATE TABLE app.conversation (
  conversation_id uuid PRIMARY KEY,
  channel         text NOT NULL DEFAULT 'chat',
  model           text,
  started_at      timestamptz NOT NULL DEFAULT now()
);

-- The transcript: every message sent to or received from the model, in order.
CREATE TABLE app.turn (
  conversation_id uuid NOT NULL REFERENCES app.conversation,
  seq             integer NOT NULL,
  role            text NOT NULL CHECK (role IN ('member', 'assistant', 'tool_results', 'runtime')),
  content         jsonb NOT NULL,
  at              timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (conversation_id, seq)
);

-- The policy: which tool calls may run, and which need a person's approval.
-- A call matches the first row for its tool whose max_amount_cents covers the amount
-- (NULL = any amount, or a call without an amount). No matching row: refused.
CREATE TABLE app.policy_rule (
  rule_id          text PRIMARY KEY,
  tool             text NOT NULL,
  max_amount_cents bigint,
  policy_class     text NOT NULL CHECK (policy_class IN ('read', 'low', 'gated')),
  why              text NOT NULL
);
INSERT INTO app.policy_rule VALUES
  ('read.verify',       'verify_member',    NULL,   'read',  'identity check'),
  ('read.memberships',  'list_memberships', NULL,   'read',  'the verified member''s own data'),
  ('read.balance',      'get_balance',      NULL,   'read',  'the verified member''s own data'),
  ('read.transactions', 'get_transactions', NULL,   'read',  'the verified member''s own data'),
  ('read.handoff',      'handoff',          NULL,   'read',  'always allowed to reach a person'),
  ('low.freeze',        'freeze_card',      NULL,   'low',   'protective and reversible'),
  ('low.loan',          'pay_loan',         25000,  'low',   'small payment from own account'),
  ('gated.loan',        'pay_loan',         500000, 'gated', 'larger payment: a person approves'),
  ('low.transfer',      'transfer',         25000,  'low',   'small move between own accounts'),
  ('gated.transfer',    'transfer',         500000, 'gated', 'larger move: a person approves'),
  ('gated.address',     'update_address',   NULL,   'gated', 'address change is a common first step in account takeover');

-- One row per tool call the agent asked for. Its status is derived from
-- action_event, never stored, so the history cannot be rewritten.
CREATE TABLE app.action (
  action_id       uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES app.conversation,
  tool            text NOT NULL,
  args            jsonb NOT NULL,
  policy_class    text NOT NULL CHECK (policy_class IN ('read', 'low', 'gated', 'denied')),
  policy_rule_id  text REFERENCES app.policy_rule,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.action_event (
  event_id  bigserial PRIMARY KEY,
  action_id uuid NOT NULL REFERENCES app.action,
  kind      text NOT NULL CHECK (kind IN ('requested', 'approved', 'rejected', 'expired',
                                        'executed', 'failed', 'refused')),
  detail    jsonb NOT NULL DEFAULT '{}',
  at        timestamptz NOT NULL DEFAULT now()
);

-- At most one decision per action; it approves the exact payload by hash.
CREATE TABLE app.approval (
  approval_id    bigserial PRIMARY KEY,
  action_id      uuid NOT NULL UNIQUE REFERENCES app.action,
  decision       text NOT NULL CHECK (decision IN ('approve', 'reject')),
  approver       text NOT NULL,
  payload_sha256 text NOT NULL,
  reason         text,
  decided_at     timestamptz NOT NULL DEFAULT now()
);

-- Said versus did: each completed action the agent claimed, and whether the core shows it.
CREATE TABLE app.claim_check (
  check_id        bigserial PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES app.conversation,
  seq             integer NOT NULL,
  tool            text NOT NULL,
  claim           text NOT NULL,
  verified        boolean NOT NULL,
  evidence        jsonb NOT NULL,
  checked_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.ledger (
  ledger_id          bigserial PRIMARY KEY,
  conversation_id    uuid REFERENCES app.conversation,
  model              text NOT NULL,
  input_tokens       integer NOT NULL,
  cache_read_tokens  integer NOT NULL DEFAULT 0,
  cache_write_tokens integer NOT NULL DEFAULT 0,
  output_tokens      integer NOT NULL,
  cost_usd           numeric(12, 6) NOT NULL,
  latency_ms         integer,          -- wall time of the model call; NULL on rows recorded before latency was tracked
  at                 timestamptz NOT NULL DEFAULT now()
);

-- The canary: one scripted conversation per tick through the real runtime and core.
CREATE TABLE app.canary_tick (
  tick_id         bigserial PRIMARY KEY,
  conversation_id uuid REFERENCES app.conversation,
  ok              boolean NOT NULL,
  failures        jsonb NOT NULL DEFAULT '[]',
  alerted         boolean NOT NULL DEFAULT false,
  code_version    text,             -- git commit the tick ran on, "-dirty" when the tree had changes
  at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.eval_run (
  eval_run_id uuid PRIMARY KEY,
  model       text NOT NULL,
  set_sha256  text NOT NULL,
  results     jsonb NOT NULL,
  started_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER turn_append_only BEFORE UPDATE OR DELETE ON app.turn
  FOR EACH ROW EXECUTE FUNCTION app.forbid_change();
CREATE TRIGGER action_append_only BEFORE UPDATE OR DELETE ON app.action
  FOR EACH ROW EXECUTE FUNCTION app.forbid_change();
CREATE TRIGGER action_event_append_only BEFORE UPDATE OR DELETE ON app.action_event
  FOR EACH ROW EXECUTE FUNCTION app.forbid_change();
CREATE TRIGGER approval_append_only BEFORE UPDATE OR DELETE ON app.approval
  FOR EACH ROW EXECUTE FUNCTION app.forbid_change();
CREATE TRIGGER claim_check_append_only BEFORE UPDATE OR DELETE ON app.claim_check
  FOR EACH ROW EXECUTE FUNCTION app.forbid_change();
CREATE TRIGGER canary_tick_append_only BEFORE UPDATE OR DELETE ON app.canary_tick
  FOR EACH ROW EXECUTE FUNCTION app.forbid_change();
CREATE TRIGGER ledger_append_only BEFORE UPDATE OR DELETE ON app.ledger
  FOR EACH ROW EXECUTE FUNCTION app.forbid_change();
