-- A REAL schema-49 store, captured once from the M4 tree.  DO NOT
-- REGENERATE FROM A CURRENT TREE.
--
-- Why this file exists.  The migration tests must run against a store
-- the pre-M5 schema code actually built, not one the current tree
-- constructed: a current store already carries the widened CHECK, so
-- _rebuild_events_table finds nothing to do and every test against it
-- passes by never running the copy at all (design 11.21).
--
-- Why a dump and not `git archive`.  The first version of these tests
-- archived commit 18f2c8f in setUpClass.  That commit is a local
-- intermediate on a branch that squashes to a single commit before it
-- leaves this machine, so the sha becomes unreachable and the test
-- fails for everyone thereafter.  It also needs full history (CI often
-- clones shallow) and a .git directory (a source tarball has none).
--
-- Why TWO PASSES.  sqlite3.iterdump writes a virtual table through
-- sqlite_master under PRAGMA writable_schema=ON and then INSERTs into
-- it in the same script, before the connection has reloaded the schema,
-- so a single-pass restore dies with `no such table: memory_fts`.
-- Everything above the PASS 2 marker restores on one connection; the
-- caller then REOPENS and runs the rest.  Pass 2 is ordinary DDL --
-- CREATE VIRTUAL TABLE, an external-content rebuild, then the triggers
-- -- so no writable_schema hackery survives in this file at all.
--
-- FTS IS KEPT ON PURPOSE (boss ruling).  The defect this fixture
-- exists to catch is that ALTER TABLE ... RENAME re-parses EVERY
-- trigger in the schema, and a trigger nobody listed broke the rename.
-- FTS5 brings exactly such triggers.  A fixture that drops a whole
-- class of real triggers cannot reproduce the class of failure it was
-- built for, and 'the migration does not touch FTS' is precisely what
-- would have been said about ladder_promotions_require_spine_event.
--
-- HOW IT WAS CAPTURED, so it can be regenerated deliberately:
--   1. git archive 18f2c8f  (the M4 commit, schema 49 / spine 48)
--   2. write the sidecar <db>.memory-spine.key = 5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a
--      (32 bytes of 0x5a: no checked-in secret, and the keyed digests
--       below verify under a known constant)
--   3. with THAT tree on sys.path: Memory(db), two remember_claim
--      calls, one conversation, two add_message calls, close
--   4. sqlite3 iterdump, FTS statements lifted into pass 2
--
-- Store shape (E-12 derives its baseline from these rather than
-- from a second checked-in file): 4 conversations written round-robin so
-- message ids interleave (N-1), 492 messages, 4 claim rows, 1 fact proposal(s)
-- holding a message back from compaction, verified and unverified
-- memories, and one claim created then erased so a tombstone and its
-- redactions are on the chain.
--
-- Captured state: user_version 49, 13 spine events, 24 triggers
-- including 6 FTS triggers and 2 virtual tables; events CHECK WITHOUT
-- 'transcript.compacted'.  tests/test_memory_compaction.py asserts every
-- one of those on load, so this fixture cannot silently drift.
PRAGMA user_version = 49;
BEGIN TRANSACTION;
CREATE TABLE activity_log (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                category TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL,
                task_id INTEGER, details_json TEXT NOT NULL DEFAULT '{}'
            );
CREATE TABLE agent_projects (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                name TEXT NOT NULL,
                relative_path TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))
            );
INSERT INTO "agent_projects" VALUES(1,'2026-09-05T14:42:19.192308+00:00','2026-09-05T14:42:19.192308+00:00','Default workspace','.',1);
CREATE TABLE approvals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
                reason TEXT NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('pending', 'approved', 'denied', 'consumed', 'expired')),
                expires_at TEXT, decided_at TEXT, task_id INTEGER,
                scope TEXT NOT NULL
            );
CREATE TABLE approved_subjects (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                subject TEXT NOT NULL UNIQUE, notes TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))
            );
CREATE TABLE conversation_goals (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                    ('active', 'incomplete', 'complete', 'cancelled', 'superseded')),
                family TEXT NOT NULL,
                goal_text TEXT NOT NULL,
                context_json TEXT NOT NULL DEFAULT '[]',
                last_result_summary TEXT,
                retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0, 1)),
                resume_count INTEGER NOT NULL DEFAULT 0 CHECK(resume_count >= 0), contract_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );
CREATE TABLE conversations (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL
            , project_id INTEGER NOT NULL DEFAULT 1);
INSERT INTO "conversations" VALUES(1,'2026-09-05T14:42:19.280826+00:00','legacy 0',1);
INSERT INTO "conversations" VALUES(2,'2026-09-05T14:42:19.282501+00:00','legacy 1',1);
INSERT INTO "conversations" VALUES(3,'2026-09-05T14:42:19.283006+00:00','legacy 2',1);
INSERT INTO "conversations" VALUES(4,'2026-09-05T14:42:19.283503+00:00','legacy 3',1);
CREATE TABLE evaluation_cases (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE,
                prompt TEXT NOT NULL,
                expected_contains_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))
            );
CREATE TABLE goals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('goal', 'project')),
                title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed', 'cancelled')),
                priority INTEGER NOT NULL DEFAULT 50
            );
CREATE TABLE initiative_events (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                signal_key TEXT NOT NULL UNIQUE, signal_kind TEXT NOT NULL,
                tier INTEGER NOT NULL CHECK(tier IN (0, 1)),
                domain_id INTEGER, project_id INTEGER NOT NULL,
                summary TEXT NOT NULL, evidence_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('observed', 'queued', 'running', 'done', 'failed', 'blocked')),
                task_id INTEGER, completed_at TEXT, result_summary TEXT
            );
CREATE TABLE journal_entries (
                id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                task_id INTEGER, FOREIGN KEY(goal_id) REFERENCES goals(id)
            );
CREATE TABLE ladder_id_sequence (
    id INTEGER PRIMARY KEY CHECK(id=1),
    next_id INTEGER NOT NULL CHECK(next_id > 0)
);
INSERT INTO "ladder_id_sequence" VALUES(1,1);
CREATE TABLE ladder_promotions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    family TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN
        ('staged','approved','unapproved_legacy','rolled_back','withdrawn','discarded')),
    stage_reason TEXT,
    lesson_ids_json TEXT NOT NULL,
    proof_json TEXT NOT NULL,
    proof_sha256 TEXT NOT NULL
        CHECK(length(proof_sha256)=64 AND proof_sha256 NOT GLOB '*[^0-9a-f]*'),
    reuse_count INTEGER NOT NULL CHECK(reuse_count >= 0),
    context_count INTEGER NOT NULL CHECK(context_count >= 0),
    epoch_id INTEGER,
    gate_json TEXT NOT NULL,
    staged_sha256 TEXT NOT NULL
        CHECK(length(staged_sha256)=64 AND staged_sha256 NOT GLOB '*[^0-9a-f]*'),
    approval_token TEXT NOT NULL
        CHECK(length(approval_token) BETWEEN 16 AND 43
              AND approval_token NOT GLOB '*[^A-Za-z0-9_-]*'),
    approved_sha256 TEXT,
    approved_at TEXT,
    prior_sha256 TEXT,
    prior_document BLOB CHECK(prior_document IS NULL OR length(prior_document) <= 32768),
    prior_document_pruned INTEGER NOT NULL DEFAULT 0
        CHECK(prior_document_pruned IN (0,1)),
    spine_event_id INTEGER NOT NULL UNIQUE,
    FOREIGN KEY(project_id) REFERENCES agent_projects(id),
    FOREIGN KEY(epoch_id) REFERENCES memory_calibration_ledger(id),
    FOREIGN KEY(spine_event_id) REFERENCES memory_spine_events(id),
    CHECK(stage NOT IN ('staged','discarded') OR approved_sha256 IS NULL),
    CHECK(stage NOT IN ('staged','discarded') OR approved_at IS NULL),
    CHECK(stage NOT IN ('approved','unapproved_legacy') OR approved_sha256 IS NOT NULL),
    CHECK(stage NOT IN ('approved','unapproved_legacy') OR approved_at IS NOT NULL)
);
CREATE TABLE learning_runs (
                id INTEGER PRIMARY KEY,
                topic_id INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(topic_id, scheduled_for),
                FOREIGN KEY(topic_id) REFERENCES learning_topics(id),
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
CREATE TABLE learning_topics (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, topic TEXT NOT NULL UNIQUE,
                interval_hours INTEGER NOT NULL, next_run TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
CREATE TABLE "lesson_applications" (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                prediction_id INTEGER NOT NULL,
                memory_id INTEGER NOT NULL,
                family TEXT NOT NULL CHECK(family IN ('code_build','code_fix','code_refactor','code_test','conversation','deep_research','desktop_file_ops','external_publish','file_ops','learning_brief','security_analysis')),
                rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 10),
                resolved_at TEXT,
                successful INTEGER CHECK(successful IN (0, 1)), tool_name TEXT,
                UNIQUE(prediction_id, memory_id),
                CHECK((resolved_at IS NULL AND successful IS NULL) OR
                      (resolved_at IS NOT NULL AND successful IN (0, 1))),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
            );
CREATE TABLE lesson_controls (
                memory_id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                valid_until TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN
                    ('active','contradicted','superseded','quarantined')),
                superseded_by INTEGER,
                recorded_at TEXT NOT NULL,
                control_sha256 TEXT NOT NULL CHECK(
                    length(control_sha256)=64 AND
                    control_sha256 NOT GLOB '*[^0-9a-f]*'),
                FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES agent_projects(id),
                FOREIGN KEY(superseded_by) REFERENCES memories(id),
                CHECK((lifecycle_status IN ('contradicted','superseded') AND
                       superseded_by IS NOT NULL) OR
                      (lifecycle_status IN ('active','quarantined') AND
                       superseded_by IS NULL))
            );
CREATE TABLE lesson_provenance (
                prediction_id INTEGER PRIMARY KEY,
                memory_id INTEGER NOT NULL,
                reflection_id INTEGER NOT NULL UNIQUE,
                verified_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                provenance_sha256 TEXT,
                FOREIGN KEY(memory_id) REFERENCES memories(id),
                FOREIGN KEY(reflection_id) REFERENCES reflections(id),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
            );
CREATE TABLE long_horizon_checkpoints (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            previous_sha256 TEXT,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(plan_id, sequence),
            UNIQUE(plan_id, stage_id),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES long_horizon_stages(id) ON DELETE CASCADE
        );
CREATE TABLE long_horizon_final_verifications (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            verifier_id TEXT NOT NULL,
            authority_id TEXT NOT NULL,
            verifier_runtime_sha256 TEXT NOT NULL CHECK(length(verifier_runtime_sha256)=64),
            passed INTEGER NOT NULL CHECK(passed IN (0,1)),
            evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
            signature_sha256 TEXT NOT NULL CHECK(length(signature_sha256)=128),
            verification_sha256 TEXT NOT NULL CHECK(length(verification_sha256)=64),
            checkpoint_head_sha256 TEXT NOT NULL CHECK(length(checkpoint_head_sha256)=64),
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(plan_id, verifier_id, verification_sha256),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE
        );
CREATE TABLE long_horizon_mutation_receipts (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>=1),
            reconciliation_round INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_round>=0),
            event_type TEXT NOT NULL CHECK(event_type IN ('intent','authorization','effect_permit','result','reconciliation')),
            outcome TEXT,
            effect_key TEXT NOT NULL CHECK(length(effect_key)=64),
            actor_id TEXT NOT NULL,
            evidence_sha256 TEXT,
            authority_id TEXT,
            runtime_sha256 TEXT,
            signature_sha256 TEXT,
            previous_sha256 TEXT,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(stage_id, generation, event_type, reconciliation_round),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES long_horizon_stages(id) ON DELETE CASCADE
        );
CREATE TABLE long_horizon_plans (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            clock_floor_at TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('active','paused','cancelled','failed','quarantined','complete')),
            manifest_json TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL UNIQUE CHECK(length(manifest_sha256)=64),
            manifest_mac_sha256 TEXT NOT NULL CHECK(length(manifest_mac_sha256)=64),
            stage_count INTEGER NOT NULL CHECK(stage_count BETWEEN 5 AND 64),
            next_stage_ordinal INTEGER NOT NULL DEFAULT 1,
            checkpoint_head_sha256 TEXT,
            retry_head_sha256 TEXT,
            usage_head_sha256 TEXT,
            final_verification_id INTEGER,
            quarantine_reason TEXT,
            pause_reason_sha256 TEXT,
            cancelled_reason_sha256 TEXT,
            used_elapsed_seconds INTEGER NOT NULL DEFAULT 0 CHECK(used_elapsed_seconds>=0),
            used_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_tool_calls>=0),
            used_model_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_model_calls>=0),
            used_prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_prompt_tokens>=0),
            used_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_completion_tokens>=0),
            used_retries INTEGER NOT NULL DEFAULT 0 CHECK(used_retries>=0),
            state_mac_sha256 TEXT,
            FOREIGN KEY(project_id) REFERENCES agent_projects(id),
            FOREIGN KEY(conversation_id) REFERENCES conversations(id),
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
CREATE TABLE long_horizon_retry_receipts (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number>=1),
            reason TEXT NOT NULL CHECK(reason IN
                ('lease_expired','pause_reclaim','mutation_not_applied','reconciliation_not_applied')),
            previous_sha256 TEXT,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(stage_id, attempt_number),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES long_horizon_stages(id) ON DELETE CASCADE
        );
CREATE TABLE long_horizon_stages (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            stage_key TEXT NOT NULL,
            stage_type TEXT NOT NULL,
            mutation_kind TEXT NOT NULL,
            stage_json TEXT NOT NULL,
            stage_sha256 TEXT NOT NULL CHECK(length(stage_sha256)=64),
            stage_mac_sha256 TEXT NOT NULL CHECK(length(stage_mac_sha256)=64),
            status TEXT NOT NULL CHECK(status IN
                ('pending','claimed','awaiting_reconciliation','complete','failed','cancelled','quarantined')),
            claim_owner TEXT,
            lease_token_sha256 TEXT,
            lease_expires_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
            idempotency_key TEXT NOT NULL UNIQUE CHECK(length(idempotency_key)=64),
            effect_key TEXT NOT NULL UNIQUE CHECK(length(effect_key)=64),
            executor_id TEXT,
            outcome_sha256 TEXT,
            artifact_sha256 TEXT,
            checkpoint_id INTEGER,
            active_reservation_id INTEGER,
            authorization_expires_at TEXT,
            authorization_consumed_at TEXT,
            mutation_state TEXT NOT NULL DEFAULT 'none',
            used_elapsed_seconds INTEGER NOT NULL DEFAULT 0 CHECK(used_elapsed_seconds>=0),
            used_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_tool_calls>=0),
            used_model_calls INTEGER NOT NULL DEFAULT 0 CHECK(used_model_calls>=0),
            used_prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_prompt_tokens>=0),
            used_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK(used_completion_tokens>=0),
            state_mac_sha256 TEXT,
            UNIQUE(plan_id, ordinal),
            UNIQUE(plan_id, stage_key),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE
        );
CREATE TABLE long_horizon_usage_reservations (
            id INTEGER PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            stage_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number>=1),
            usage_json TEXT NOT NULL,
            previous_sha256 TEXT,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(receipt_sha256)=64),
            receipt_mac_sha256 TEXT NOT NULL CHECK(length(receipt_mac_sha256)=64),
            UNIQUE(stage_id, attempt_number),
            FOREIGN KEY(plan_id) REFERENCES long_horizon_plans(id) ON DELETE CASCADE,
            FOREIGN KEY(stage_id) REFERENCES long_horizon_stages(id) ON DELETE CASCADE
        );
CREATE TABLE memories (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                content TEXT NOT NULL, source TEXT, family TEXT, outcome_status TEXT, reflection_id INTEGER, spine_event_id INTEGER, UNIQUE(kind, content)
            );
INSERT INTO "memories" VALUES(1,'2026-09-05T14:42:19.684298+00:00','claim','[jarvis project claim v1]{"created_at":"2026-09-05T14:42:19.684298+00:00","predicate":"maintainer","schema":"jarvis.project-claim-memory.v1","scope":"project:1","subject":"Kestrel relay","value":"Dana Okonkwo"}','operator:explicit operator project fact',NULL,NULL,NULL,3);
INSERT INTO "memories" VALUES(2,'2026-09-05T14:42:19.686821+00:00','claim','[jarvis project claim v1]{"created_at":"2026-09-05T14:42:19.686821+00:00","predicate":"listen port","schema":"jarvis.project-claim-memory.v1","scope":"project:1","subject":"Kestrel relay","value":"8443"}','operator:explicit operator project fact',NULL,NULL,NULL,4);
INSERT INTO "memories" VALUES(3,'2026-09-05T14:42:19.688251+00:00','claim','[jarvis project claim v1]{"created_at":"2026-09-05T14:42:19.688251+00:00","predicate":"datacenter","schema":"jarvis.project-claim-memory.v1","scope":"project:1","subject":"Harrier box","value":"Fenwick"}','operator:explicit operator project fact',NULL,NULL,NULL,5);
INSERT INTO "memories" VALUES(4,'2026-09-05T14:42:19.689697+00:00','claim','[jarvis project claim v1]{"created_at":"2026-09-05T14:42:19.689697+00:00","predicate":"listen port","schema":"jarvis.project-claim-memory.v1","scope":"project:1","subject":"Kestrel relay","value":"9443"}','operator:explicit operator project fact',NULL,NULL,NULL,6);
INSERT INTO "memories" VALUES(5,'2026-09-05T14:42:19.691049+00:00','fact','An aside 0 about the relay fleet.','operator',NULL,NULL,NULL,8);
INSERT INTO "memories" VALUES(6,'2026-09-05T14:42:19.692005+00:00','fact','An aside 1 about the relay fleet.','operator',NULL,NULL,NULL,9);
INSERT INTO "memories" VALUES(7,'2026-09-05T14:42:19.692720+00:00','fact','An aside 2 about the relay fleet.','operator',NULL,NULL,NULL,10);
INSERT INTO "memories" VALUES(8,'2026-09-05T14:42:19.693400+00:00','fact','An unverified aside about the fleet.',NULL,NULL,NULL,NULL,11);
CREATE TABLE memory_calibration_ledger (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    family TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK(epoch > 0),
    n INTEGER NOT NULL CHECK(n > 0),
    successes INTEGER NOT NULL CHECK(successes >= 0 AND successes <= n),
    mean_predicted REAL NOT NULL CHECK(mean_predicted BETWEEN 0.0 AND 1.0),
    brier REAL NOT NULL CHECK(brier BETWEEN 0.0 AND 1.0),
    calibration_error REAL NOT NULL CHECK(calibration_error BETWEEN 0.0 AND 1.0),
    evidence_applicable INTEGER NOT NULL CHECK(evidence_applicable >= 0),
    evidence_successes INTEGER NOT NULL CHECK(evidence_successes >= 0),
    applied_n INTEGER NOT NULL CHECK(applied_n >= 0),
    applied_successes INTEGER NOT NULL CHECK(applied_successes >= 0),
    unapplied_n INTEGER NOT NULL CHECK(unapplied_n >= 0),
    unapplied_successes INTEGER NOT NULL CHECK(unapplied_successes >= 0),
    refused_stagings INTEGER NOT NULL DEFAULT 0 CHECK(refused_stagings >= 0),
    refused_approvals INTEGER NOT NULL DEFAULT 0 CHECK(refused_approvals >= 0),
    withdrawals INTEGER NOT NULL DEFAULT 0 CHECK(withdrawals >= 0),
    screened_components INTEGER NOT NULL DEFAULT 0 CHECK(screened_components >= 0),
    unverified_at_seal INTEGER NOT NULL DEFAULT 0 CHECK(unverified_at_seal >= 0),
    first_prediction_id INTEGER NOT NULL,
    last_prediction_id INTEGER NOT NULL
        CHECK(last_prediction_id >= first_prediction_id),
    -- The exact prediction ids the epoch covers, ascending, canonical JSON.
    -- The [first, last] pair above is the reported range and the index key,
    -- but it does NOT determine the covered set: a prediction held open while
    -- the block around it is cut sits inside that range, so a range test
    -- would leave it permanently uncoverable while competence() kept counting
    -- it (design 10.7 item 8, the S-2 defect one level down).  The spine
    -- payload stays digest-only: coverage_digest binds this set, and the ids
    -- themselves live only here.
    covered_ids_json TEXT NOT NULL,
    coverage_digest TEXT NOT NULL
        CHECK(length(coverage_digest)=64 AND coverage_digest NOT GLOB '*[^0-9a-f]*'),
    spine_event_id INTEGER NOT NULL UNIQUE,
    UNIQUE(family, epoch),
    FOREIGN KEY(spine_event_id) REFERENCES memory_spine_events(id)
);
CREATE TABLE memory_claim_clock_statistics (
                claim_id INTEGER PRIMARY KEY,
                reads INTEGER NOT NULL DEFAULT 0 CHECK(reads >= 0),
                stale_reads INTEGER NOT NULL DEFAULT 0 CHECK(stale_reads >= 0),
                last_effective_confidence REAL NOT NULL
                    CHECK(last_effective_confidence BETWEEN 0 AND 1),
                last_clock_status TEXT NOT NULL,
                last_read_at TEXT NOT NULL,
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
            );
CREATE TABLE memory_claim_events (
                id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('active', 'disputed', 'superseded')),
                reason TEXT NOT NULL, related_claim_id INTEGER, spine_event_id INTEGER,
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id),
                FOREIGN KEY(related_claim_id) REFERENCES memory_claims(id)
            );
INSERT INTO "memory_claim_events" VALUES(1,1,'2026-09-05T14:42:19.684298+00:00','active','new strongest claim',NULL,3);
INSERT INTO "memory_claim_events" VALUES(2,2,'2026-09-05T14:42:19.686821+00:00','active','new strongest claim',NULL,4);
INSERT INTO "memory_claim_events" VALUES(3,3,'2026-09-05T14:42:19.688251+00:00','active','new strongest claim',NULL,5);
INSERT INTO "memory_claim_events" VALUES(4,4,'2026-09-05T14:42:19.689697+00:00','active','new strongest claim',2,6);
INSERT INTO "memory_claim_events" VALUES(5,2,'2026-09-05T14:42:19.689697+00:00','superseded','replaced by newer authoritative claim',4,7);
CREATE TABLE memory_claim_evidence (
                id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, source TEXT NOT NULL,
                authority TEXT NOT NULL CHECK(authority IN
                    ('external', 'learned', 'verified', 'operator')),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                evidence_sha256 TEXT NOT NULL,
                UNIQUE(claim_id, evidence_sha256),
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
            );
INSERT INTO "memory_claim_evidence" VALUES(1,1,'2026-09-05T14:42:19.684298+00:00','explicit operator project fact','operator',1.0,'8c5ba1e32d037f5bab230f9d5a6cfaa6fc9df3d359fb6317f46cbf7a75fc296d');
INSERT INTO "memory_claim_evidence" VALUES(2,2,'2026-09-05T14:42:19.686821+00:00','explicit operator project fact','operator',1.0,'f16d7d97f0ae74a58dc6d96933eedbabb161fad71782ed97a36e139a4e68ca78');
INSERT INTO "memory_claim_evidence" VALUES(3,3,'2026-09-05T14:42:19.688251+00:00','explicit operator project fact','operator',1.0,'060ee4a47ca23594cbd00c3510089ec92743157a8209a6936068bd17d561df5b');
INSERT INTO "memory_claim_evidence" VALUES(4,4,'2026-09-05T14:42:19.689697+00:00','explicit operator project fact','operator',1.0,'6b0c5c0cc510481c664eead1362d152377f53777e4ac9821e7fb9e83c7e990d4');
CREATE TABLE memory_claim_observations (
                id INTEGER PRIMARY KEY,
                claim_id INTEGER NOT NULL,
                claim_key TEXT NOT NULL,
                predicate TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                value_sha256 TEXT NOT NULL,
                source_key TEXT NOT NULL,
                authority TEXT NOT NULL CHECK(authority IN
                    ('external', 'learned', 'verified', 'operator')),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
            );
CREATE TABLE memory_claim_sequence (
            id INTEGER PRIMARY KEY CHECK(id=1),
            next_id INTEGER NOT NULL CHECK(next_id > 0)
        );
INSERT INTO "memory_claim_sequence" VALUES(1,6);
CREATE TABLE memory_claim_volatility (
                predicate TEXT PRIMARY KEY,
                hazard_per_day REAL NOT NULL CHECK(hazard_per_day >= 0),
                pair_count INTEGER NOT NULL CHECK(pair_count >= 0),
                vocabulary_size INTEGER NOT NULL CHECK(vocabulary_size >= 2),
                fitted_at TEXT NOT NULL
            );
CREATE TABLE memory_claims (
                id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                claim_key TEXT NOT NULL, subject TEXT NOT NULL,
                predicate TEXT NOT NULL, value TEXT NOT NULL,
                value_sha256 TEXT NOT NULL, source TEXT NOT NULL,
                authority TEXT NOT NULL CHECK(authority IN
                    ('external', 'learned', 'verified', 'operator')),
                confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                status TEXT NOT NULL CHECK(status IN
                    ('active', 'disputed', 'superseded')),
                valid_from TEXT NOT NULL, valid_until TEXT,
                supersedes_id INTEGER, scope TEXT NOT NULL DEFAULT 'global', spine_event_id INTEGER,
                FOREIGN KEY(memory_id) REFERENCES memories(id),
                FOREIGN KEY(supersedes_id) REFERENCES memory_claims(id)
            );
INSERT INTO "memory_claims" VALUES(1,1,'2026-09-05T14:42:19.684298+00:00','2026-09-05T14:42:19.684298+00:00','835ea40342c39d96f961278b839872745dd00f511d41cda197802ca134720e47','Kestrel relay','maintainer','Dana Okonkwo','8ea6c125d6ca33ab510e38616a6f60e4a96ef41bdfdefb2b3f5fd959dac35ded','explicit operator project fact','operator',1.0,'active','2026-09-05T14:42:19.684298+00:00',NULL,NULL,'project:1',3);
INSERT INTO "memory_claims" VALUES(2,2,'2026-09-05T14:42:19.686821+00:00','2026-09-05T14:42:19.689697+00:00','4bdafaebe7348db1c7a8ccaa8b7d8848d1a833c3a5ebcb925e10bd88f54d0288','Kestrel relay','listen port','8443','e998d857e5637aa94c02419c98be82013f1571b2f4be936b9c79eae57c17dd2f','explicit operator project fact','operator',1.0,'superseded','2026-09-05T14:42:19.686821+00:00','2026-09-05T14:42:19.689697+00:00',NULL,'project:1',4);
INSERT INTO "memory_claims" VALUES(3,3,'2026-09-05T14:42:19.688251+00:00','2026-09-05T14:42:19.688251+00:00','ee769d381e301e90b27786c7ec725cabcff4f5a1cffaeb7822b5b8ab267cd166','Harrier box','datacenter','Fenwick','e5585ed39e668d3542d7a887f97d9ad95ece956668676e07a0097c4eb7584cf3','explicit operator project fact','operator',1.0,'active','2026-09-05T14:42:19.688251+00:00',NULL,NULL,'project:1',5);
INSERT INTO "memory_claims" VALUES(4,4,'2026-09-05T14:42:19.689697+00:00','2026-09-05T14:42:19.689697+00:00','4bdafaebe7348db1c7a8ccaa8b7d8848d1a833c3a5ebcb925e10bd88f54d0288','Kestrel relay','listen port','9443','136e34f998a68d208fc88ee13b8a12395b0451c97087f14023a91c941786e041','explicit operator project fact','operator',1.0,'active','2026-09-05T14:42:19.689697+00:00',NULL,2,'project:1',6);
CREATE TABLE memory_embedding_leases (
                memory_id INTEGER NOT NULL, model TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, lease_owner TEXT,
                lease_expires_at TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(memory_id, model),
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            );
CREATE TABLE memory_embeddings (
                memory_id INTEGER NOT NULL, model TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
                content_sha256 TEXT NOT NULL, embedding_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, embedding_blob BLOB, vector_norm REAL,
                PRIMARY KEY(memory_id, model),
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            );
CREATE TABLE memory_fact_proposals (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                conversation_id INTEGER NOT NULL,
                assistant_message_id INTEGER NOT NULL UNIQUE,
                project_id INTEGER NOT NULL,
                command TEXT NOT NULL,
                command_sha256 TEXT NOT NULL CHECK(length(command_sha256)=64),
                assisted INTEGER NOT NULL CHECK(assisted IN (0, 1)),
                reply_asked_question INTEGER NOT NULL
                    CHECK(reply_asked_question IN (0, 1)),
                status TEXT NOT NULL CHECK(status IN
                    ('shown', 'confirmed', 'refused', 'expired')),
                resolved_at TEXT,
                claim_id INTEGER, command_salt TEXT, spine_event_id INTEGER,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                FOREIGN KEY(assistant_message_id) REFERENCES messages(id),
                FOREIGN KEY(claim_id) REFERENCES memory_claims(id)
            );
INSERT INTO "memory_fact_proposals" VALUES(1,'2026-09-05T14:42:19.485781+00:00',1,242,1,'Remember this project fact: {"subject":"Millrace weir","predicate":"gate count","value":"four"}','6a423767f325299cc264faea5a9a6d082d6d8b718234cab9bdf1b8b30cc6082d',0,0,'shown',NULL,NULL,'ccfa4e863848333710809abc3060e601',NULL);
CREATE TABLE memory_graph_edges (
    claim_id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    src_entity_id INTEGER NOT NULL,
    predicate_key TEXT NOT NULL,
    dst_entity_id INTEGER,
    value_kind TEXT NOT NULL CHECK(value_kind IN ('entity','literal')),
    status TEXT NOT NULL CHECK(status IN ('active','disputed','superseded')),
    authority TEXT NOT NULL CHECK(authority IN
        ('external','learned','verified','operator')),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    spine_event_id INTEGER NOT NULL,
    projected_at TEXT NOT NULL,
    CHECK((value_kind='entity') = (dst_entity_id IS NOT NULL)),
    FOREIGN KEY(claim_id) REFERENCES memory_claims(id),
    FOREIGN KEY(src_entity_id) REFERENCES memory_graph_entities(id),
    FOREIGN KEY(dst_entity_id) REFERENCES memory_graph_entities(id)
);
INSERT INTO "memory_graph_edges" VALUES(1,'project:1','835ea40342c39d96f961278b839872745dd00f511d41cda197802ca134720e47',1,'maintainer',2,'entity','active','operator',1.0,'2026-09-05T14:42:19.684298+00:00',NULL,3,'2026-09-05T14:42:19.684298+00:00');
INSERT INTO "memory_graph_edges" VALUES(2,'project:1','4bdafaebe7348db1c7a8ccaa8b7d8848d1a833c3a5ebcb925e10bd88f54d0288',1,'listen port',NULL,'literal','superseded','operator',1.0,'2026-09-05T14:42:19.686821+00:00','2026-09-05T14:42:19.689697+00:00',4,'2026-09-05T14:42:19.686821+00:00');
INSERT INTO "memory_graph_edges" VALUES(3,'project:1','ee769d381e301e90b27786c7ec725cabcff4f5a1cffaeb7822b5b8ab267cd166',3,'datacenter',4,'entity','active','operator',1.0,'2026-09-05T14:42:19.688251+00:00',NULL,5,'2026-09-05T14:42:19.688251+00:00');
INSERT INTO "memory_graph_edges" VALUES(4,'project:1','4bdafaebe7348db1c7a8ccaa8b7d8848d1a833c3a5ebcb925e10bd88f54d0288',1,'listen port',NULL,'literal','active','operator',1.0,'2026-09-05T14:42:19.689697+00:00',NULL,6,'2026-09-05T14:42:19.689697+00:00');
CREATE TABLE memory_graph_entities (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    label TEXT NOT NULL CHECK(length(label) <= 80),
    created_at TEXT NOT NULL,
    UNIQUE(scope, entity_key)
);
INSERT INTO "memory_graph_entities" VALUES(1,'project:1','kestrel relay','Kestrel relay','2026-09-05T14:42:19.684298+00:00');
INSERT INTO "memory_graph_entities" VALUES(2,'project:1','dana okonkwo','Dana Okonkwo','2026-09-05T14:42:19.684298+00:00');
INSERT INTO "memory_graph_entities" VALUES(3,'project:1','harrier box','Harrier box','2026-09-05T14:42:19.688251+00:00');
INSERT INTO "memory_graph_entities" VALUES(4,'project:1','fenwick','Fenwick','2026-09-05T14:42:19.688251+00:00');
CREATE TABLE memory_graph_entity_sequence (
    id INTEGER PRIMARY KEY CHECK(id=1),
    next_id INTEGER NOT NULL CHECK(next_id >= 1)
);
INSERT INTO "memory_graph_entity_sequence" VALUES(1,7);
CREATE TABLE memory_id_sequence (
            id INTEGER PRIMARY KEY CHECK(id=1),
            next_id INTEGER NOT NULL CHECK(next_id > 0)
        );
INSERT INTO "memory_id_sequence" VALUES(1,10);
CREATE TABLE memory_query_embeddings (
                query_sha256 TEXT NOT NULL, model TEXT NOT NULL,
                dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
                embedding_blob BLOB NOT NULL, vector_norm REAL NOT NULL,
                created_at TEXT NOT NULL, last_used_at TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0 CHECK(hit_count >= 0),
                PRIMARY KEY(query_sha256, model, dimensions)
            );
CREATE TABLE memory_retrievals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                prediction_id INTEGER NOT NULL, conversation_id INTEGER,
                family TEXT NOT NULL, query_sha256 TEXT NOT NULL,
                memory_id INTEGER NOT NULL, rank INTEGER NOT NULL,
                channel TEXT NOT NULL CHECK(channel IN ('lexical', 'semantic', 'hybrid')),
                resolved_at TEXT, successful INTEGER CHECK(successful IN (0, 1)),
                UNIQUE(prediction_id, memory_id),
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            );
CREATE TABLE memory_spine_events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'spine.genesis','claim.imported','claim.created','claim.reasserted',
        'claim.superseded','claim.disputed','claim.retracted','claim.tombstoned',
        'proposal.not_stored','proposal.confirmed','conversation.deleted',
        'projection.rebuilt','memory.imported','memory.created','memory.reasserted',
        'memory.updated','memory.deleted','lesson.created','lesson.applied',
        'ladder.calibration_sealed','ladder.candidate','ladder.staged',
        'ladder.approved','ladder.rolled_back','ladder.withdrawn',
        'ladder.grandfathered')),
    actor TEXT NOT NULL CHECK(actor IN
        ('operator','runtime','model','worker','companion','system')),
    source TEXT NOT NULL,
    scope TEXT NOT NULL,
    permission TEXT NOT NULL,
    conversation_id INTEGER,
    subject_kind TEXT CHECK(subject_kind IS NULL OR subject_kind IN
        ('claim','conversation','projection','proposal','memory',
         'ladder','calibration','lesson')),
    subject_id INTEGER,
    parent_event_id INTEGER,
    outcome TEXT NOT NULL CHECK(outcome IN ('applied','rejected','noop')),
    payload_json TEXT,
    payload_salt TEXT,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
    prev_sha256 TEXT NOT NULL CHECK(length(prev_sha256)=64),
    event_sha256 TEXT NOT NULL CHECK(length(event_sha256)=64),
    redacted_by_event_id INTEGER
);
INSERT INTO "memory_spine_events" VALUES(1,'2026-09-05T14:42:19.264740+00:00','spine.genesis','system','memory spine migration','global','migration',NULL,NULL,NULL,NULL,'applied','{"claims_backfilled":0,"key_fingerprint":"b90dae625d02b00658ac96a94d83ea31990ec4430d40a627654724cda3134ad2","schema_version":48}','c7c86a13060f1dc9fa2716e95d93571a','ae5446f85a38f1442133882a569d7bbf6b2e7d5b4bb303d2c1fc95e0ab543e27','0000000000000000000000000000000000000000000000000000000000000000','616a173e8abba658bb72272ce1bb54d76275d4d6806323359eaf42393fd4e315',NULL);
INSERT INTO "memory_spine_events" VALUES(2,'2026-09-05T14:42:19.271218+00:00','projection.rebuilt','system','graph migration 48','global','migration',NULL,'projection',NULL,NULL,'applied','{"at":"2026-09-05T14:42:19.271218+00:00","divergences_fixed":0,"entities":0,"excluded":{"excluded_predicate":0,"subject_private":0,"subject_too_long":0},"projection":"graph","removed_ids":[],"rows_after":0,"rows_before":0}','a9d091a99313fabd8801c670a4e81da4','275ede36d1a05257e0356ba04224efa9e87fa7c7612f15436143b551e17418ad','616a173e8abba658bb72272ce1bb54d76275d4d6806323359eaf42393fd4e315','4178baf977d6a7b96f49be97166b3b7a8dfdae2e32f9c51b30838bf211699094',NULL);
INSERT INTO "memory_spine_events" VALUES(3,'2026-09-05T14:42:19.684298+00:00','claim.created','operator','explicit operator project fact','project:1','operator:interactive',1,'claim',1,NULL,'applied','{"at":"2026-09-05T14:42:19.684298+00:00","authority":"operator","claim_key":"835ea40342c39d96f961278b839872745dd00f511d41cda197802ca134720e47","confidence":1.0,"predicate":"maintainer","source":"explicit operator project fact","status":"active","subject":"Kestrel relay","supersedes_id":null,"valid_from":"2026-09-05T14:42:19.684298+00:00","valid_until":null,"value":"Dana Okonkwo","value_sha256":"8ea6c125d6ca33ab510e38616a6f60e4a96ef41bdfdefb2b3f5fd959dac35ded"}','aed374d48e5a2741132d72388ec887a9','ee36d2578fe53b3cdc662b8f86bac5b4f0231c767b12b1aad40f5d601fd1fba5','4178baf977d6a7b96f49be97166b3b7a8dfdae2e32f9c51b30838bf211699094','e339e146dc00a674b94a42eb4e1d7d796e3d23dc7dc96df86f2703bb987c1d25',NULL);
INSERT INTO "memory_spine_events" VALUES(4,'2026-09-05T14:42:19.686821+00:00','claim.created','operator','explicit operator project fact','project:1','operator:interactive',1,'claim',2,NULL,'applied','{"at":"2026-09-05T14:42:19.686821+00:00","authority":"operator","claim_key":"4bdafaebe7348db1c7a8ccaa8b7d8848d1a833c3a5ebcb925e10bd88f54d0288","confidence":1.0,"predicate":"listen port","source":"explicit operator project fact","status":"active","subject":"Kestrel relay","supersedes_id":null,"valid_from":"2026-09-05T14:42:19.686821+00:00","valid_until":null,"value":"8443","value_sha256":"e998d857e5637aa94c02419c98be82013f1571b2f4be936b9c79eae57c17dd2f"}','ce674067d4e790f0c12697d8ae1fa233','1c79aea88382195c3169f7c9be79e8fa7d5cfe4dae53ae7a3f3ac8f7743d2e28','e339e146dc00a674b94a42eb4e1d7d796e3d23dc7dc96df86f2703bb987c1d25','f0ee268a74131b97d7fb6c14b05c6ac7854e0de304fb48309e50d444c16ebb07',NULL);
INSERT INTO "memory_spine_events" VALUES(5,'2026-09-05T14:42:19.688251+00:00','claim.created','operator','explicit operator project fact','project:1','operator:interactive',1,'claim',3,NULL,'applied','{"at":"2026-09-05T14:42:19.688251+00:00","authority":"operator","claim_key":"ee769d381e301e90b27786c7ec725cabcff4f5a1cffaeb7822b5b8ab267cd166","confidence":1.0,"predicate":"datacenter","source":"explicit operator project fact","status":"active","subject":"Harrier box","supersedes_id":null,"valid_from":"2026-09-05T14:42:19.688251+00:00","valid_until":null,"value":"Fenwick","value_sha256":"e5585ed39e668d3542d7a887f97d9ad95ece956668676e07a0097c4eb7584cf3"}','e580625ab97f8ae8ee049acb2ef26535','36014318702eda640282b9d8c3609a23a15b0861c043d90900ab3d9b05c6fc91','f0ee268a74131b97d7fb6c14b05c6ac7854e0de304fb48309e50d444c16ebb07','86df48e138a73a75bb612588d2089f480ffcfee75d4515042edd71b9f1f47c0a',NULL);
INSERT INTO "memory_spine_events" VALUES(6,'2026-09-05T14:42:19.689697+00:00','claim.created','operator','explicit operator project fact','project:1','operator:interactive',1,'claim',4,NULL,'applied','{"at":"2026-09-05T14:42:19.689697+00:00","authority":"operator","claim_key":"4bdafaebe7348db1c7a8ccaa8b7d8848d1a833c3a5ebcb925e10bd88f54d0288","confidence":1.0,"predicate":"listen port","source":"explicit operator project fact","status":"active","subject":"Kestrel relay","supersedes_id":2,"valid_from":"2026-09-05T14:42:19.689697+00:00","valid_until":null,"value":"9443","value_sha256":"136e34f998a68d208fc88ee13b8a12395b0451c97087f14023a91c941786e041"}','ae9f93ff72a54460825d94dc50288a2d','72733ce05e052cf9a39706d34cb216704d688166b285f15ba58d7d6022c93c27','86df48e138a73a75bb612588d2089f480ffcfee75d4515042edd71b9f1f47c0a','e6bda5e488609cccae16903a6fb623b2d2328488021d441d4ced1afeb6c461e8',NULL);
INSERT INTO "memory_spine_events" VALUES(7,'2026-09-05T14:42:19.689698+00:00','claim.superseded','operator','explicit operator project fact','project:1','operator:interactive',1,'claim',2,NULL,'applied','{"at":"2026-09-05T14:42:19.689697+00:00","authority":"operator","claim_id":2,"claim_key":"4bdafaebe7348db1c7a8ccaa8b7d8848d1a833c3a5ebcb925e10bd88f54d0288","confidence":1.0,"reason":"replaced by newer authoritative claim","related_claim_id":4,"source":"explicit operator project fact","status":"superseded","valid_until":"2026-09-05T14:42:19.689697+00:00"}','92593229466952dbcda76be8d33a5fa2','9dd0a4ebb0e704877009b2bb8958b06201b55563fa4e94b6c8ebd2f81c3ce46b','e6bda5e488609cccae16903a6fb623b2d2328488021d441d4ced1afeb6c461e8','6ebfe8e3862035f1463c5766588f748fb91ce75fc70acafea358621423bd2ba0',NULL);
INSERT INTO "memory_spine_events" VALUES(8,'2026-09-05T14:42:19.691049+00:00','memory.created','runtime','operator','global','runtime',NULL,'memory',5,NULL,'applied','{"content_digest":"dc3be73596d8301fa0dddb2dda4ef079ff17649045eaebc0a1cb7fd5441e5a1a","content_length":33,"eligible":true,"family":null,"kind":"fact","origin":"explicit_operator_memory","outcome_status":null,"reflection_id":null,"source":"operator"}','a260112efb9cfd5e523f0f390cb100a5','117bd1570508e3c5658e1e4c85d986af4310c0e08ce0693bd891429891a08538','6ebfe8e3862035f1463c5766588f748fb91ce75fc70acafea358621423bd2ba0','baf578b23ac77b76bb60a0466bf159d7275a133a1c369e13eb735e5fced94de7',NULL);
INSERT INTO "memory_spine_events" VALUES(9,'2026-09-05T14:42:19.692005+00:00','memory.created','runtime','operator','global','runtime',NULL,'memory',6,NULL,'applied','{"content_digest":"cc73e2868f3cbedb67438d203b0a62b957ddf2ab98629af8fac6f6de330d8e25","content_length":33,"eligible":true,"family":null,"kind":"fact","origin":"explicit_operator_memory","outcome_status":null,"reflection_id":null,"source":"operator"}','682351dacf0c273ef62d52bc90f6912f','9de52934cf39c863427b4fa67e8647db735222cdf8049deda4992f4d0c3e6b31','baf578b23ac77b76bb60a0466bf159d7275a133a1c369e13eb735e5fced94de7','358d34d48d2ef2f575c768c4eabc1712db6b731540cdbae3faeb1c788165b49e',NULL);
INSERT INTO "memory_spine_events" VALUES(10,'2026-09-05T14:42:19.692720+00:00','memory.created','runtime','operator','global','runtime',NULL,'memory',7,NULL,'applied','{"content_digest":"d9b7187a1f19e9703db58b8be65a776445b5c25d52767ad8c727528df4701051","content_length":33,"eligible":true,"family":null,"kind":"fact","origin":"explicit_operator_memory","outcome_status":null,"reflection_id":null,"source":"operator"}','667236bfd17dd4676a513da66c1dce72','d0aa7efc8c82757cc93e1e0faef560e31a7e1d71c9461ef710b89c3a358ee5a2','358d34d48d2ef2f575c768c4eabc1712db6b731540cdbae3faeb1c788165b49e','6a930a1f09496a17cf3adf4d1cc8ac19ab95f6183c50ba1c3d831f9baa38b34b',NULL);
INSERT INTO "memory_spine_events" VALUES(11,'2026-09-05T14:42:19.693400+00:00','memory.created','runtime','','global','runtime',NULL,'memory',8,NULL,'applied','{"content_digest":"4c5d77a6dae27915d14c99352d588a2ebfec5591806f58c76418151c873919f4","content_length":36,"eligible":false,"family":null,"kind":"fact","origin":"unverified","outcome_status":null,"reflection_id":null,"source":null}','0df47728667bfcb0b1bd04af25dfb4d7','c24b80e35b9a14f8eddd000d19a701a91431ed39335332e5b99ac121c7b0808a','6a930a1f09496a17cf3adf4d1cc8ac19ab95f6183c50ba1c3d831f9baa38b34b','9c50616ff37eaf4360ba4919a20ec36bf011f878a69e2f1b7d68a745ce427a45',NULL);
INSERT INTO "memory_spine_events" VALUES(12,'2026-09-05T14:42:19.694340+00:00','claim.created','operator','explicit operator project fact','project:1','operator:interactive',2,'claim',5,NULL,'applied',NULL,NULL,'959d9dcc363e9b84d142202b910dea4a79a7649f2ef3dc240716b92eb726db48','9c50616ff37eaf4360ba4919a20ec36bf011f878a69e2f1b7d68a745ce427a45','24e8b4a6088e5bb80d2a58f6f3f2d8a16d31ba3f52dd63af8c662be8a84de4c0',13);
INSERT INTO "memory_spine_events" VALUES(13,'2026-09-05T14:42:19.695454+00:00','claim.tombstoned','operator','explicit operator project fact erasure','project:1','operator:interactive',2,'claim',5,NULL,'applied','{"at":"2026-09-05T14:42:19.695454+00:00","claim_key":"871f399927386955026cb96739c23fdc65f471c7754a3937f326af445015e98c","redacted_event_ids":[12],"removed_claim_ids":[5],"removed_entity_ids":[5,6],"removed_memory_ids":[9],"transcript_copies":1}','9fe7b0686e06429a8639fb6c592dbd04','bd06db749fe08730acf42bf3c615e3bab70ee8b2675b8035b314f2a177b1c563','24e8b4a6088e5bb80d2a58f6f3f2d8a16d31ba3f52dd63af8c662be8a84de4c0','736177f980d0c46993b44d7a56dc3e004f29e2b6f0bb07146ac1ef82bdc67797',NULL);
CREATE TABLE memory_spine_head (
    id INTEGER PRIMARY KEY CHECK(id=1),
    last_event_id INTEGER NOT NULL,
    last_event_sha256 TEXT NOT NULL CHECK(length(last_event_sha256)=64),
    head_mac TEXT NOT NULL CHECK(length(head_mac)=64)
);
INSERT INTO "memory_spine_head" VALUES(1,13,'736177f980d0c46993b44d7a56dc3e004f29e2b6f0bb07146ac1ef82bdc67797','d2b9e5fec9045b009f17db3fe16263d30f8b0b72b38d1dae35b509ec6d83760a');
CREATE TABLE memory_statistics (
                memory_id INTEGER PRIMARY KEY,
                retrievals INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0,
                utility REAL NOT NULL DEFAULT 0.5 CHECK(utility BETWEEN 0 AND 1),
                last_retrieved_at TEXT, last_resolved_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            );
CREATE TABLE messages (
                id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );
INSERT INTO "messages" VALUES(1,1,'2026-09-05T14:42:19.284227+00:00','user','Legacy turn 0 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(2,1,'2026-09-05T14:42:19.285187+00:00','assistant','Legacy reply 0: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(3,2,'2026-09-05T14:42:19.285894+00:00','user','Legacy turn 0 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(4,2,'2026-09-05T14:42:19.286717+00:00','assistant','Legacy reply 0: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(5,3,'2026-09-05T14:42:19.287610+00:00','user','Legacy turn 0 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(6,3,'2026-09-05T14:42:19.288494+00:00','assistant','Legacy reply 0: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(7,4,'2026-09-05T14:42:19.289255+00:00','user','Legacy turn 0 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(8,4,'2026-09-05T14:42:19.290084+00:00','assistant','Legacy reply 0: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(9,1,'2026-09-05T14:42:19.290824+00:00','user','Legacy turn 1 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(10,1,'2026-09-05T14:42:19.291648+00:00','assistant','Legacy reply 1: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(11,2,'2026-09-05T14:42:19.292355+00:00','user','Legacy turn 1 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(12,2,'2026-09-05T14:42:19.293232+00:00','assistant','Legacy reply 1: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(13,3,'2026-09-05T14:42:19.294200+00:00','user','Legacy turn 1 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(14,3,'2026-09-05T14:42:19.295025+00:00','assistant','Legacy reply 1: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(15,4,'2026-09-05T14:42:19.295770+00:00','user','Legacy turn 1 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(16,4,'2026-09-05T14:42:19.296589+00:00','assistant','Legacy reply 1: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(17,1,'2026-09-05T14:42:19.297397+00:00','user','Legacy turn 2 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(18,1,'2026-09-05T14:42:19.298267+00:00','assistant','Legacy reply 2: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(19,2,'2026-09-05T14:42:19.299075+00:00','user','Legacy turn 2 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(20,2,'2026-09-05T14:42:19.299908+00:00','assistant','Legacy reply 2: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(21,3,'2026-09-05T14:42:19.300617+00:00','user','Legacy turn 2 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(22,3,'2026-09-05T14:42:19.301574+00:00','assistant','Legacy reply 2: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(23,4,'2026-09-05T14:42:19.302376+00:00','user','Legacy turn 2 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(24,4,'2026-09-05T14:42:19.303226+00:00','assistant','Legacy reply 2: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(25,1,'2026-09-05T14:42:19.304046+00:00','user','Legacy turn 3 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(26,1,'2026-09-05T14:42:19.304930+00:00','assistant','Legacy reply 3: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(27,2,'2026-09-05T14:42:19.305723+00:00','user','Legacy turn 3 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(28,2,'2026-09-05T14:42:19.306613+00:00','assistant','Legacy reply 3: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(29,3,'2026-09-05T14:42:19.307387+00:00','user','Legacy turn 3 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(30,3,'2026-09-05T14:42:19.308426+00:00','assistant','Legacy reply 3: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(31,4,'2026-09-05T14:42:19.309320+00:00','user','Legacy turn 3 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(32,4,'2026-09-05T14:42:19.310256+00:00','assistant','Legacy reply 3: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(33,1,'2026-09-05T14:42:19.311002+00:00','user','Legacy turn 4 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(34,1,'2026-09-05T14:42:19.311858+00:00','assistant','Legacy reply 4: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(35,2,'2026-09-05T14:42:19.312590+00:00','user','Legacy turn 4 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(36,2,'2026-09-05T14:42:19.313557+00:00','assistant','Legacy reply 4: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(37,3,'2026-09-05T14:42:19.314337+00:00','user','Legacy turn 4 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(38,3,'2026-09-05T14:42:19.315185+00:00','assistant','Legacy reply 4: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(39,4,'2026-09-05T14:42:19.315945+00:00','user','Legacy turn 4 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(40,4,'2026-09-05T14:42:19.316775+00:00','assistant','Legacy reply 4: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(41,1,'2026-09-05T14:42:19.317490+00:00','user','Legacy turn 5 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(42,1,'2026-09-05T14:42:19.318301+00:00','assistant','Legacy reply 5: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(43,2,'2026-09-05T14:42:19.319051+00:00','user','Legacy turn 5 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(44,2,'2026-09-05T14:42:19.319872+00:00','assistant','Legacy reply 5: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(45,3,'2026-09-05T14:42:19.320589+00:00','user','Legacy turn 5 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(46,3,'2026-09-05T14:42:19.321410+00:00','assistant','Legacy reply 5: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(47,4,'2026-09-05T14:42:19.322299+00:00','user','Legacy turn 5 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(48,4,'2026-09-05T14:42:19.323215+00:00','assistant','Legacy reply 5: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(49,1,'2026-09-05T14:42:19.323953+00:00','user','Legacy turn 6 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(50,1,'2026-09-05T14:42:19.324780+00:00','assistant','Legacy reply 6: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(51,2,'2026-09-05T14:42:19.325494+00:00','user','Legacy turn 6 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(52,2,'2026-09-05T14:42:19.326298+00:00','assistant','Legacy reply 6: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(53,3,'2026-09-05T14:42:19.327061+00:00','user','Legacy turn 6 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(54,3,'2026-09-05T14:42:19.327876+00:00','assistant','Legacy reply 6: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(55,4,'2026-09-05T14:42:19.328759+00:00','user','Legacy turn 6 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(56,4,'2026-09-05T14:42:19.329644+00:00','assistant','Legacy reply 6: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(57,1,'2026-09-05T14:42:19.330371+00:00','user','Legacy turn 7 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(58,1,'2026-09-05T14:42:19.331204+00:00','assistant','Legacy reply 7: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(59,2,'2026-09-05T14:42:19.331963+00:00','user','Legacy turn 7 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(60,2,'2026-09-05T14:42:19.332774+00:00','assistant','Legacy reply 7: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(61,3,'2026-09-05T14:42:19.333521+00:00','user','Legacy turn 7 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(62,3,'2026-09-05T14:42:19.334385+00:00','assistant','Legacy reply 7: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(63,4,'2026-09-05T14:42:19.335114+00:00','user','Legacy turn 7 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(64,4,'2026-09-05T14:42:19.336006+00:00','assistant','Legacy reply 7: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(65,1,'2026-09-05T14:42:19.336824+00:00','user','Legacy turn 8 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(66,1,'2026-09-05T14:42:19.337686+00:00','assistant','Legacy reply 8: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(67,2,'2026-09-05T14:42:19.338466+00:00','user','Legacy turn 8 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(68,2,'2026-09-05T14:42:19.339279+00:00','assistant','Legacy reply 8: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(69,3,'2026-09-05T14:42:19.340138+00:00','user','Legacy turn 8 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(70,3,'2026-09-05T14:42:19.341045+00:00','assistant','Legacy reply 8: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(71,4,'2026-09-05T14:42:19.341787+00:00','user','Legacy turn 8 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(72,4,'2026-09-05T14:42:19.342654+00:00','assistant','Legacy reply 8: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(73,1,'2026-09-05T14:42:19.343451+00:00','user','Legacy turn 9 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(74,1,'2026-09-05T14:42:19.344288+00:00','assistant','Legacy reply 9: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(75,2,'2026-09-05T14:42:19.345050+00:00','user','Legacy turn 9 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(76,2,'2026-09-05T14:42:19.345882+00:00','assistant','Legacy reply 9: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(77,3,'2026-09-05T14:42:19.346642+00:00','user','Legacy turn 9 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(78,3,'2026-09-05T14:42:19.347462+00:00','assistant','Legacy reply 9: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(79,4,'2026-09-05T14:42:19.348177+00:00','user','Legacy turn 9 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(80,4,'2026-09-05T14:42:19.349075+00:00','assistant','Legacy reply 9: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(81,1,'2026-09-05T14:42:19.349832+00:00','user','Legacy turn 10 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(82,1,'2026-09-05T14:42:19.350687+00:00','assistant','Legacy reply 10: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(83,2,'2026-09-05T14:42:19.351446+00:00','user','Legacy turn 10 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(84,2,'2026-09-05T14:42:19.352304+00:00','assistant','Legacy reply 10: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(85,3,'2026-09-05T14:42:19.353124+00:00','user','Legacy turn 10 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(86,3,'2026-09-05T14:42:19.354074+00:00','assistant','Legacy reply 10: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(87,4,'2026-09-05T14:42:19.355025+00:00','user','Legacy turn 10 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(88,4,'2026-09-05T14:42:19.355914+00:00','assistant','Legacy reply 10: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(89,1,'2026-09-05T14:42:19.356719+00:00','user','Legacy turn 11 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(90,1,'2026-09-05T14:42:19.357615+00:00','assistant','Legacy reply 11: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(91,2,'2026-09-05T14:42:19.358428+00:00','user','Legacy turn 11 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(92,2,'2026-09-05T14:42:19.359319+00:00','assistant','Legacy reply 11: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(93,3,'2026-09-05T14:42:19.360183+00:00','user','Legacy turn 11 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(94,3,'2026-09-05T14:42:19.361054+00:00','assistant','Legacy reply 11: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(95,4,'2026-09-05T14:42:19.361862+00:00','user','Legacy turn 11 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(96,4,'2026-09-05T14:42:19.362776+00:00','assistant','Legacy reply 11: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(97,1,'2026-09-05T14:42:19.363569+00:00','user','Legacy turn 12 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(98,1,'2026-09-05T14:42:19.364444+00:00','assistant','Legacy reply 12: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(99,2,'2026-09-05T14:42:19.365204+00:00','user','Legacy turn 12 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(100,2,'2026-09-05T14:42:19.366156+00:00','assistant','Legacy reply 12: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(101,3,'2026-09-05T14:42:19.366934+00:00','user','Legacy turn 12 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(102,3,'2026-09-05T14:42:19.367757+00:00','assistant','Legacy reply 12: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(103,4,'2026-09-05T14:42:19.368494+00:00','user','Legacy turn 12 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(104,4,'2026-09-05T14:42:19.369327+00:00','assistant','Legacy reply 12: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(105,1,'2026-09-05T14:42:19.370063+00:00','user','Legacy turn 13 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(106,1,'2026-09-05T14:42:19.370990+00:00','assistant','Legacy reply 13: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(107,2,'2026-09-05T14:42:19.371773+00:00','user','Legacy turn 13 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(108,2,'2026-09-05T14:42:19.372604+00:00','assistant','Legacy reply 13: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(109,3,'2026-09-05T14:42:19.373371+00:00','user','Legacy turn 13 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(110,3,'2026-09-05T14:42:19.374320+00:00','assistant','Legacy reply 13: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(111,4,'2026-09-05T14:42:19.375127+00:00','user','Legacy turn 13 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(112,4,'2026-09-05T14:42:19.375943+00:00','assistant','Legacy reply 13: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(113,1,'2026-09-05T14:42:19.376684+00:00','user','Legacy turn 14 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(114,1,'2026-09-05T14:42:19.377523+00:00','assistant','Legacy reply 14: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(115,2,'2026-09-05T14:42:19.378237+00:00','user','Legacy turn 14 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(116,2,'2026-09-05T14:42:19.379085+00:00','assistant','Legacy reply 14: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(117,3,'2026-09-05T14:42:19.379804+00:00','user','Legacy turn 14 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(118,3,'2026-09-05T14:42:19.380622+00:00','assistant','Legacy reply 14: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(119,4,'2026-09-05T14:42:19.381395+00:00','user','Legacy turn 14 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(120,4,'2026-09-05T14:42:19.382215+00:00','assistant','Legacy reply 14: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(121,1,'2026-09-05T14:42:19.382926+00:00','user','Legacy turn 15 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(122,1,'2026-09-05T14:42:19.383773+00:00','assistant','Legacy reply 15: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(123,2,'2026-09-05T14:42:19.384622+00:00','user','Legacy turn 15 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(124,2,'2026-09-05T14:42:19.385519+00:00','assistant','Legacy reply 15: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(125,3,'2026-09-05T14:42:19.386237+00:00','user','Legacy turn 15 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(126,3,'2026-09-05T14:42:19.387159+00:00','assistant','Legacy reply 15: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(127,4,'2026-09-05T14:42:19.387892+00:00','user','Legacy turn 15 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(128,4,'2026-09-05T14:42:19.388761+00:00','assistant','Legacy reply 15: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(129,1,'2026-09-05T14:42:19.389581+00:00','user','Legacy turn 16 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(130,1,'2026-09-05T14:42:19.390456+00:00','assistant','Legacy reply 16: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(131,2,'2026-09-05T14:42:19.391294+00:00','user','Legacy turn 16 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(132,2,'2026-09-05T14:42:19.392152+00:00','assistant','Legacy reply 16: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(133,3,'2026-09-05T14:42:19.392911+00:00','user','Legacy turn 16 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(134,3,'2026-09-05T14:42:19.393748+00:00','assistant','Legacy reply 16: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(135,4,'2026-09-05T14:42:19.394505+00:00','user','Legacy turn 16 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(136,4,'2026-09-05T14:42:19.395334+00:00','assistant','Legacy reply 16: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(137,1,'2026-09-05T14:42:19.396112+00:00','user','Legacy turn 17 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(138,1,'2026-09-05T14:42:19.396964+00:00','assistant','Legacy reply 17: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(139,2,'2026-09-05T14:42:19.397682+00:00','user','Legacy turn 17 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(140,2,'2026-09-05T14:42:19.398714+00:00','assistant','Legacy reply 17: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(141,3,'2026-09-05T14:42:19.399479+00:00','user','Legacy turn 17 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(142,3,'2026-09-05T14:42:19.400403+00:00','assistant','Legacy reply 17: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(143,4,'2026-09-05T14:42:19.401138+00:00','user','Legacy turn 17 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(144,4,'2026-09-05T14:42:19.402063+00:00','assistant','Legacy reply 17: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(145,1,'2026-09-05T14:42:19.402970+00:00','user','Legacy turn 18 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(146,1,'2026-09-05T14:42:19.403873+00:00','assistant','Legacy reply 18: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(147,2,'2026-09-05T14:42:19.404653+00:00','user','Legacy turn 18 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(148,2,'2026-09-05T14:42:19.405725+00:00','assistant','Legacy reply 18: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(149,3,'2026-09-05T14:42:19.407437+00:00','user','Legacy turn 18 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(150,3,'2026-09-05T14:42:19.408754+00:00','assistant','Legacy reply 18: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(151,4,'2026-09-05T14:42:19.409507+00:00','user','Legacy turn 18 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(152,4,'2026-09-05T14:42:19.410360+00:00','assistant','Legacy reply 18: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(153,1,'2026-09-05T14:42:19.411115+00:00','user','Legacy turn 19 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(154,1,'2026-09-05T14:42:19.411997+00:00','assistant','Legacy reply 19: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(155,2,'2026-09-05T14:42:19.412884+00:00','user','Legacy turn 19 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(156,2,'2026-09-05T14:42:19.413782+00:00','assistant','Legacy reply 19: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(157,3,'2026-09-05T14:42:19.414617+00:00','user','Legacy turn 19 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(158,3,'2026-09-05T14:42:19.415482+00:00','assistant','Legacy reply 19: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(159,4,'2026-09-05T14:42:19.416275+00:00','user','Legacy turn 19 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(160,4,'2026-09-05T14:42:19.417109+00:00','assistant','Legacy reply 19: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(161,1,'2026-09-05T14:42:19.417838+00:00','user','Legacy turn 20 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(162,1,'2026-09-05T14:42:19.418657+00:00','assistant','Legacy reply 20: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(163,2,'2026-09-05T14:42:19.419570+00:00','user','Legacy turn 20 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(164,2,'2026-09-05T14:42:19.420525+00:00','assistant','Legacy reply 20: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(165,3,'2026-09-05T14:42:19.421479+00:00','user','Legacy turn 20 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(166,3,'2026-09-05T14:42:19.422326+00:00','assistant','Legacy reply 20: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(167,4,'2026-09-05T14:42:19.423091+00:00','user','Legacy turn 20 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(168,4,'2026-09-05T14:42:19.423937+00:00','assistant','Legacy reply 20: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(169,1,'2026-09-05T14:42:19.424675+00:00','user','Legacy turn 21 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(170,1,'2026-09-05T14:42:19.425496+00:00','assistant','Legacy reply 21: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(171,2,'2026-09-05T14:42:19.426233+00:00','user','Legacy turn 21 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(172,2,'2026-09-05T14:42:19.427064+00:00','assistant','Legacy reply 21: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(173,3,'2026-09-05T14:42:19.427805+00:00','user','Legacy turn 21 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(174,3,'2026-09-05T14:42:19.428741+00:00','assistant','Legacy reply 21: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(175,4,'2026-09-05T14:42:19.429554+00:00','user','Legacy turn 21 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(176,4,'2026-09-05T14:42:19.430389+00:00','assistant','Legacy reply 21: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(177,1,'2026-09-05T14:42:19.431120+00:00','user','Legacy turn 22 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(178,1,'2026-09-05T14:42:19.431990+00:00','assistant','Legacy reply 22: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(179,2,'2026-09-05T14:42:19.432759+00:00','user','Legacy turn 22 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(180,2,'2026-09-05T14:42:19.433748+00:00','assistant','Legacy reply 22: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(181,3,'2026-09-05T14:42:19.434552+00:00','user','Legacy turn 22 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(182,3,'2026-09-05T14:42:19.435401+00:00','assistant','Legacy reply 22: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(183,4,'2026-09-05T14:42:19.436222+00:00','user','Legacy turn 22 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(184,4,'2026-09-05T14:42:19.437079+00:00','assistant','Legacy reply 22: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(185,1,'2026-09-05T14:42:19.437819+00:00','user','Legacy turn 23 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(186,1,'2026-09-05T14:42:19.438681+00:00','assistant','Legacy reply 23: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(187,2,'2026-09-05T14:42:19.439401+00:00','user','Legacy turn 23 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(188,2,'2026-09-05T14:42:19.440325+00:00','assistant','Legacy reply 23: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(189,3,'2026-09-05T14:42:19.441169+00:00','user','Legacy turn 23 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(190,3,'2026-09-05T14:42:19.442076+00:00','assistant','Legacy reply 23: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(191,4,'2026-09-05T14:42:19.442828+00:00','user','Legacy turn 23 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(192,4,'2026-09-05T14:42:19.443658+00:00','assistant','Legacy reply 23: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(193,1,'2026-09-05T14:42:19.444412+00:00','user','Legacy turn 24 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(194,1,'2026-09-05T14:42:19.445290+00:00','assistant','Legacy reply 24: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(195,2,'2026-09-05T14:42:19.446017+00:00','user','Legacy turn 24 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(196,2,'2026-09-05T14:42:19.446855+00:00','assistant','Legacy reply 24: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(197,3,'2026-09-05T14:42:19.447585+00:00','user','Legacy turn 24 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(198,3,'2026-09-05T14:42:19.448437+00:00','assistant','Legacy reply 24: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(199,4,'2026-09-05T14:42:19.449165+00:00','user','Legacy turn 24 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(200,4,'2026-09-05T14:42:19.449991+00:00','assistant','Legacy reply 24: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(201,1,'2026-09-05T14:42:19.450726+00:00','user','Legacy turn 25 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(202,1,'2026-09-05T14:42:19.451613+00:00','assistant','Legacy reply 25: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(203,2,'2026-09-05T14:42:19.452344+00:00','user','Legacy turn 25 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(204,2,'2026-09-05T14:42:19.453181+00:00','assistant','Legacy reply 25: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(205,3,'2026-09-05T14:42:19.453910+00:00','user','Legacy turn 25 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(206,3,'2026-09-05T14:42:19.454777+00:00','assistant','Legacy reply 25: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(207,4,'2026-09-05T14:42:19.455560+00:00','user','Legacy turn 25 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(208,4,'2026-09-05T14:42:19.456453+00:00','assistant','Legacy reply 25: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(209,1,'2026-09-05T14:42:19.457207+00:00','user','Legacy turn 26 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(210,1,'2026-09-05T14:42:19.458070+00:00','assistant','Legacy reply 26: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(211,2,'2026-09-05T14:42:19.458859+00:00','user','Legacy turn 26 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(212,2,'2026-09-05T14:42:19.459709+00:00','assistant','Legacy reply 26: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(213,3,'2026-09-05T14:42:19.460542+00:00','user','Legacy turn 26 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(214,3,'2026-09-05T14:42:19.461503+00:00','assistant','Legacy reply 26: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(215,4,'2026-09-05T14:42:19.462263+00:00','user','Legacy turn 26 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(216,4,'2026-09-05T14:42:19.463133+00:00','assistant','Legacy reply 26: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(217,1,'2026-09-05T14:42:19.463890+00:00','user','Legacy turn 27 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(218,1,'2026-09-05T14:42:19.464738+00:00','assistant','Legacy reply 27: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(219,2,'2026-09-05T14:42:19.465490+00:00','user','Legacy turn 27 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(220,2,'2026-09-05T14:42:19.466411+00:00','assistant','Legacy reply 27: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(221,3,'2026-09-05T14:42:19.467172+00:00','user','Legacy turn 27 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(222,3,'2026-09-05T14:42:19.468073+00:00','assistant','Legacy reply 27: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(223,4,'2026-09-05T14:42:19.468904+00:00','user','Legacy turn 27 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(224,4,'2026-09-05T14:42:19.469778+00:00','assistant','Legacy reply 27: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(225,1,'2026-09-05T14:42:19.470514+00:00','user','Legacy turn 28 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(226,1,'2026-09-05T14:42:19.471414+00:00','assistant','Legacy reply 28: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(227,2,'2026-09-05T14:42:19.472152+00:00','user','Legacy turn 28 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(228,2,'2026-09-05T14:42:19.472990+00:00','assistant','Legacy reply 28: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(229,3,'2026-09-05T14:42:19.473720+00:00','user','Legacy turn 28 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(230,3,'2026-09-05T14:42:19.474601+00:00','assistant','Legacy reply 28: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(231,4,'2026-09-05T14:42:19.475460+00:00','user','Legacy turn 28 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(232,4,'2026-09-05T14:42:19.476394+00:00','assistant','Legacy reply 28: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(233,1,'2026-09-05T14:42:19.477132+00:00','user','Legacy turn 29 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(234,1,'2026-09-05T14:42:19.477952+00:00','assistant','Legacy reply 29: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(235,2,'2026-09-05T14:42:19.478843+00:00','user','Legacy turn 29 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(236,2,'2026-09-05T14:42:19.479768+00:00','assistant','Legacy reply 29: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(237,3,'2026-09-05T14:42:19.480495+00:00','user','Legacy turn 29 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(238,3,'2026-09-05T14:42:19.481540+00:00','assistant','Legacy reply 29: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(239,4,'2026-09-05T14:42:19.482321+00:00','user','Legacy turn 29 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(240,4,'2026-09-05T14:42:19.483164+00:00','assistant','Legacy reply 29: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(241,1,'2026-09-05T14:42:19.483914+00:00','user','Legacy turn 30 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(242,1,'2026-09-05T14:42:19.484788+00:00','assistant','Legacy reply 30: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(243,2,'2026-09-05T14:42:19.486577+00:00','user','Legacy turn 30 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(244,2,'2026-09-05T14:42:19.487413+00:00','assistant','Legacy reply 30: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(245,3,'2026-09-05T14:42:19.488143+00:00','user','Legacy turn 30 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(246,3,'2026-09-05T14:42:19.489022+00:00','assistant','Legacy reply 30: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(247,4,'2026-09-05T14:42:19.489784+00:00','user','Legacy turn 30 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(248,4,'2026-09-05T14:42:19.490620+00:00','assistant','Legacy reply 30: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(249,1,'2026-09-05T14:42:19.491352+00:00','user','Legacy turn 31 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(250,1,'2026-09-05T14:42:19.492172+00:00','assistant','Legacy reply 31: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(251,2,'2026-09-05T14:42:19.492883+00:00','user','Legacy turn 31 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(252,2,'2026-09-05T14:42:19.493709+00:00','assistant','Legacy reply 31: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(253,3,'2026-09-05T14:42:19.494441+00:00','user','Legacy turn 31 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(254,3,'2026-09-05T14:42:19.495316+00:00','assistant','Legacy reply 31: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(255,4,'2026-09-05T14:42:19.496055+00:00','user','Legacy turn 31 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(256,4,'2026-09-05T14:42:19.496884+00:00','assistant','Legacy reply 31: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(257,1,'2026-09-05T14:42:19.497836+00:00','user','Legacy turn 32 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(258,1,'2026-09-05T14:42:19.498708+00:00','assistant','Legacy reply 32: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(259,2,'2026-09-05T14:42:19.499452+00:00','user','Legacy turn 32 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(260,2,'2026-09-05T14:42:19.500289+00:00','assistant','Legacy reply 32: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(261,3,'2026-09-05T14:42:19.501004+00:00','user','Legacy turn 32 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(262,3,'2026-09-05T14:42:19.501824+00:00','assistant','Legacy reply 32: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(263,4,'2026-09-05T14:42:19.502549+00:00','user','Legacy turn 32 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(264,4,'2026-09-05T14:42:19.503443+00:00','assistant','Legacy reply 32: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(265,1,'2026-09-05T14:42:19.504178+00:00','user','Legacy turn 33 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(266,1,'2026-09-05T14:42:19.504999+00:00','assistant','Legacy reply 33: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(267,2,'2026-09-05T14:42:19.505720+00:00','user','Legacy turn 33 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(268,2,'2026-09-05T14:42:19.506565+00:00','assistant','Legacy reply 33: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(269,3,'2026-09-05T14:42:19.507307+00:00','user','Legacy turn 33 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(270,3,'2026-09-05T14:42:19.508172+00:00','assistant','Legacy reply 33: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(271,4,'2026-09-05T14:42:19.509885+00:00','user','Legacy turn 33 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(272,4,'2026-09-05T14:42:19.511177+00:00','assistant','Legacy reply 33: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(273,1,'2026-09-05T14:42:19.511983+00:00','user','Legacy turn 34 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(274,1,'2026-09-05T14:42:19.512858+00:00','assistant','Legacy reply 34: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(275,2,'2026-09-05T14:42:19.513701+00:00','user','Legacy turn 34 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(276,2,'2026-09-05T14:42:19.514649+00:00','assistant','Legacy reply 34: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(277,3,'2026-09-05T14:42:19.515416+00:00','user','Legacy turn 34 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(278,3,'2026-09-05T14:42:19.516297+00:00','assistant','Legacy reply 34: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(279,4,'2026-09-05T14:42:19.517050+00:00','user','Legacy turn 34 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(280,4,'2026-09-05T14:42:19.518080+00:00','assistant','Legacy reply 34: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(281,1,'2026-09-05T14:42:19.518818+00:00','user','Legacy turn 35 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(282,1,'2026-09-05T14:42:19.519635+00:00','assistant','Legacy reply 35: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(283,2,'2026-09-05T14:42:19.520355+00:00','user','Legacy turn 35 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(284,2,'2026-09-05T14:42:19.521181+00:00','assistant','Legacy reply 35: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(285,3,'2026-09-05T14:42:19.521908+00:00','user','Legacy turn 35 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(286,3,'2026-09-05T14:42:19.522747+00:00','assistant','Legacy reply 35: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(287,4,'2026-09-05T14:42:19.523576+00:00','user','Legacy turn 35 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(288,4,'2026-09-05T14:42:19.524462+00:00','assistant','Legacy reply 35: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(289,1,'2026-09-05T14:42:19.525198+00:00','user','Legacy turn 36 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(290,1,'2026-09-05T14:42:19.526042+00:00','assistant','Legacy reply 36: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(291,2,'2026-09-05T14:42:19.526777+00:00','user','Legacy turn 36 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(292,2,'2026-09-05T14:42:19.527602+00:00','assistant','Legacy reply 36: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(293,3,'2026-09-05T14:42:19.528378+00:00','user','Legacy turn 36 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(294,3,'2026-09-05T14:42:19.529202+00:00','assistant','Legacy reply 36: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(295,4,'2026-09-05T14:42:19.529843+00:00','user','Legacy turn 36 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(296,4,'2026-09-05T14:42:19.530869+00:00','assistant','Legacy reply 36: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(297,1,'2026-09-05T14:42:19.531677+00:00','user','Legacy turn 37 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(298,1,'2026-09-05T14:42:19.532503+00:00','assistant','Legacy reply 37: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(299,2,'2026-09-05T14:42:19.533221+00:00','user','Legacy turn 37 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(300,2,'2026-09-05T14:42:19.534051+00:00','assistant','Legacy reply 37: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(301,3,'2026-09-05T14:42:19.534788+00:00','user','Legacy turn 37 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(302,3,'2026-09-05T14:42:19.535634+00:00','assistant','Legacy reply 37: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(303,4,'2026-09-05T14:42:19.536393+00:00','user','Legacy turn 37 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(304,4,'2026-09-05T14:42:19.537310+00:00','assistant','Legacy reply 37: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(305,1,'2026-09-05T14:42:19.538186+00:00','user','Legacy turn 38 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(306,1,'2026-09-05T14:42:19.539059+00:00','assistant','Legacy reply 38: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(307,2,'2026-09-05T14:42:19.539825+00:00','user','Legacy turn 38 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(308,2,'2026-09-05T14:42:19.540667+00:00','assistant','Legacy reply 38: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(309,3,'2026-09-05T14:42:19.541396+00:00','user','Legacy turn 38 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(310,3,'2026-09-05T14:42:19.542247+00:00','assistant','Legacy reply 38: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(311,4,'2026-09-05T14:42:19.542890+00:00','user','Legacy turn 38 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(312,4,'2026-09-05T14:42:19.543832+00:00','assistant','Legacy reply 38: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(313,1,'2026-09-05T14:42:19.544575+00:00','user','Legacy turn 39 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(314,1,'2026-09-05T14:42:19.545394+00:00','assistant','Legacy reply 39: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(315,2,'2026-09-05T14:42:19.546137+00:00','user','Legacy turn 39 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(316,2,'2026-09-05T14:42:19.547049+00:00','assistant','Legacy reply 39: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(317,3,'2026-09-05T14:42:19.547927+00:00','user','Legacy turn 39 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(318,3,'2026-09-05T14:42:19.548905+00:00','assistant','Legacy reply 39: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(319,4,'2026-09-05T14:42:19.549666+00:00','user','Legacy turn 39 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(320,4,'2026-09-05T14:42:19.550502+00:00','assistant','Legacy reply 39: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(321,1,'2026-09-05T14:42:19.551357+00:00','user','Legacy turn 40 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(322,1,'2026-09-05T14:42:19.552298+00:00','assistant','Legacy reply 40: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(323,2,'2026-09-05T14:42:19.553040+00:00','user','Legacy turn 40 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(324,2,'2026-09-05T14:42:19.553872+00:00','assistant','Legacy reply 40: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(325,3,'2026-09-05T14:42:19.554615+00:00','user','Legacy turn 40 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(326,3,'2026-09-05T14:42:19.555361+00:00','assistant','Legacy reply 40: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(327,4,'2026-09-05T14:42:19.556131+00:00','user','Legacy turn 40 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(328,4,'2026-09-05T14:42:19.556988+00:00','assistant','Legacy reply 40: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(329,1,'2026-09-05T14:42:19.557917+00:00','user','Legacy turn 41 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(330,1,'2026-09-05T14:42:19.558919+00:00','assistant','Legacy reply 41: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(331,2,'2026-09-05T14:42:19.559700+00:00','user','Legacy turn 41 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(332,2,'2026-09-05T14:42:19.560573+00:00','assistant','Legacy reply 41: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(333,3,'2026-09-05T14:42:19.561328+00:00','user','Legacy turn 41 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(334,3,'2026-09-05T14:42:19.562169+00:00','assistant','Legacy reply 41: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(335,4,'2026-09-05T14:42:19.562903+00:00','user','Legacy turn 41 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(336,4,'2026-09-05T14:42:19.563815+00:00','assistant','Legacy reply 41: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(337,1,'2026-09-05T14:42:19.564576+00:00','user','Legacy turn 42 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(338,1,'2026-09-05T14:42:19.565496+00:00','assistant','Legacy reply 42: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(339,2,'2026-09-05T14:42:19.566266+00:00','user','Legacy turn 42 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(340,2,'2026-09-05T14:42:19.567119+00:00','assistant','Legacy reply 42: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(341,3,'2026-09-05T14:42:19.567842+00:00','user','Legacy turn 42 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(342,3,'2026-09-05T14:42:19.568818+00:00','assistant','Legacy reply 42: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(343,4,'2026-09-05T14:42:19.569558+00:00','user','Legacy turn 42 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(344,4,'2026-09-05T14:42:19.570400+00:00','assistant','Legacy reply 42: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(345,1,'2026-09-05T14:42:19.571137+00:00','user','Legacy turn 43 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(346,1,'2026-09-05T14:42:19.571998+00:00','assistant','Legacy reply 43: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(347,2,'2026-09-05T14:42:19.572772+00:00','user','Legacy turn 43 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(348,2,'2026-09-05T14:42:19.573627+00:00','assistant','Legacy reply 43: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(349,3,'2026-09-05T14:42:19.574366+00:00','user','Legacy turn 43 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(350,3,'2026-09-05T14:42:19.575322+00:00','assistant','Legacy reply 43: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(351,4,'2026-09-05T14:42:19.576099+00:00','user','Legacy turn 43 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(352,4,'2026-09-05T14:42:19.576967+00:00','assistant','Legacy reply 43: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(353,1,'2026-09-05T14:42:19.577692+00:00','user','Legacy turn 44 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(354,1,'2026-09-05T14:42:19.578509+00:00','assistant','Legacy reply 44: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(355,2,'2026-09-05T14:42:19.579371+00:00','user','Legacy turn 44 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(356,2,'2026-09-05T14:42:19.580220+00:00','assistant','Legacy reply 44: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(357,3,'2026-09-05T14:42:19.581029+00:00','user','Legacy turn 44 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(358,3,'2026-09-05T14:42:19.581864+00:00','assistant','Legacy reply 44: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(359,4,'2026-09-05T14:42:19.582595+00:00','user','Legacy turn 44 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(360,4,'2026-09-05T14:42:19.583426+00:00','assistant','Legacy reply 44: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(361,1,'2026-09-05T14:42:19.584159+00:00','user','Legacy turn 45 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(362,1,'2026-09-05T14:42:19.585004+00:00','assistant','Legacy reply 45: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(363,2,'2026-09-05T14:42:19.585744+00:00','user','Legacy turn 45 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(364,2,'2026-09-05T14:42:19.586637+00:00','assistant','Legacy reply 45: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(365,3,'2026-09-05T14:42:19.587405+00:00','user','Legacy turn 45 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(366,3,'2026-09-05T14:42:19.588250+00:00','assistant','Legacy reply 45: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(367,4,'2026-09-05T14:42:19.589023+00:00','user','Legacy turn 45 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(368,4,'2026-09-05T14:42:19.589854+00:00','assistant','Legacy reply 45: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(369,1,'2026-09-05T14:42:19.590558+00:00','user','Legacy turn 46 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(370,1,'2026-09-05T14:42:19.591455+00:00','assistant','Legacy reply 46: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(371,2,'2026-09-05T14:42:19.592233+00:00','user','Legacy turn 46 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(372,2,'2026-09-05T14:42:19.593148+00:00','assistant','Legacy reply 46: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(373,3,'2026-09-05T14:42:19.593875+00:00','user','Legacy turn 46 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(374,3,'2026-09-05T14:42:19.594719+00:00','assistant','Legacy reply 46: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(375,4,'2026-09-05T14:42:19.595443+00:00','user','Legacy turn 46 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(376,4,'2026-09-05T14:42:19.596271+00:00','assistant','Legacy reply 46: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(377,1,'2026-09-05T14:42:19.596982+00:00','user','Legacy turn 47 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(378,1,'2026-09-05T14:42:19.597804+00:00','assistant','Legacy reply 47: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(379,2,'2026-09-05T14:42:19.598526+00:00','user','Legacy turn 47 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(380,2,'2026-09-05T14:42:19.599386+00:00','assistant','Legacy reply 47: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(381,3,'2026-09-05T14:42:19.600289+00:00','user','Legacy turn 47 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(382,3,'2026-09-05T14:42:19.601226+00:00','assistant','Legacy reply 47: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(383,4,'2026-09-05T14:42:19.601956+00:00','user','Legacy turn 47 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(384,4,'2026-09-05T14:42:19.602775+00:00','assistant','Legacy reply 47: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(385,1,'2026-09-05T14:42:19.603515+00:00','user','Legacy turn 48 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(386,1,'2026-09-05T14:42:19.604442+00:00','assistant','Legacy reply 48: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(387,2,'2026-09-05T14:42:19.605189+00:00','user','Legacy turn 48 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(388,2,'2026-09-05T14:42:19.606030+00:00','assistant','Legacy reply 48: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(389,3,'2026-09-05T14:42:19.606794+00:00','user','Legacy turn 48 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(390,3,'2026-09-05T14:42:19.607655+00:00','assistant','Legacy reply 48: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(391,4,'2026-09-05T14:42:19.608400+00:00','user','Legacy turn 48 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(392,4,'2026-09-05T14:42:19.609253+00:00','assistant','Legacy reply 48: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(393,1,'2026-09-05T14:42:19.609980+00:00','user','Legacy turn 49 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(394,1,'2026-09-05T14:42:19.610821+00:00','assistant','Legacy reply 49: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(395,2,'2026-09-05T14:42:19.611577+00:00','user','Legacy turn 49 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(396,2,'2026-09-05T14:42:19.612446+00:00','assistant','Legacy reply 49: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(397,3,'2026-09-05T14:42:19.613344+00:00','user','Legacy turn 49 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(398,3,'2026-09-05T14:42:19.614391+00:00','assistant','Legacy reply 49: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(399,4,'2026-09-05T14:42:19.615198+00:00','user','Legacy turn 49 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(400,4,'2026-09-05T14:42:19.616071+00:00','assistant','Legacy reply 49: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(401,1,'2026-09-05T14:42:19.616822+00:00','user','Legacy turn 50 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(402,1,'2026-09-05T14:42:19.617677+00:00','assistant','Legacy reply 50: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(403,2,'2026-09-05T14:42:19.619263+00:00','user','Legacy turn 50 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(404,2,'2026-09-05T14:42:19.620546+00:00','assistant','Legacy reply 50: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(405,3,'2026-09-05T14:42:19.621431+00:00','user','Legacy turn 50 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(406,3,'2026-09-05T14:42:19.622275+00:00','assistant','Legacy reply 50: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(407,4,'2026-09-05T14:42:19.623087+00:00','user','Legacy turn 50 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(408,4,'2026-09-05T14:42:19.624013+00:00','assistant','Legacy reply 50: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(409,1,'2026-09-05T14:42:19.624817+00:00','user','Legacy turn 51 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(410,1,'2026-09-05T14:42:19.625751+00:00','assistant','Legacy reply 51: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(411,2,'2026-09-05T14:42:19.626539+00:00','user','Legacy turn 51 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(412,2,'2026-09-05T14:42:19.627383+00:00','assistant','Legacy reply 51: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(413,3,'2026-09-05T14:42:19.628127+00:00','user','Legacy turn 51 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(414,3,'2026-09-05T14:42:19.628971+00:00','assistant','Legacy reply 51: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(415,4,'2026-09-05T14:42:19.629718+00:00','user','Legacy turn 51 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(416,4,'2026-09-05T14:42:19.630579+00:00','assistant','Legacy reply 51: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(417,1,'2026-09-05T14:42:19.631318+00:00','user','Legacy turn 52 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(418,1,'2026-09-05T14:42:19.632147+00:00','assistant','Legacy reply 52: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(419,2,'2026-09-05T14:42:19.632895+00:00','user','Legacy turn 52 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(420,2,'2026-09-05T14:42:19.633748+00:00','assistant','Legacy reply 52: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(421,3,'2026-09-05T14:42:19.634620+00:00','user','Legacy turn 52 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(422,3,'2026-09-05T14:42:19.635569+00:00','assistant','Legacy reply 52: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(423,4,'2026-09-05T14:42:19.636312+00:00','user','Legacy turn 52 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(424,4,'2026-09-05T14:42:19.637241+00:00','assistant','Legacy reply 52: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(425,1,'2026-09-05T14:42:19.637984+00:00','user','Legacy turn 53 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(426,1,'2026-09-05T14:42:19.638838+00:00','assistant','Legacy reply 53: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(427,2,'2026-09-05T14:42:19.639575+00:00','user','Legacy turn 53 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(428,2,'2026-09-05T14:42:19.640417+00:00','assistant','Legacy reply 53: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(429,3,'2026-09-05T14:42:19.641247+00:00','user','Legacy turn 53 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(430,3,'2026-09-05T14:42:19.642173+00:00','assistant','Legacy reply 53: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(431,4,'2026-09-05T14:42:19.642944+00:00','user','Legacy turn 53 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(432,4,'2026-09-05T14:42:19.643845+00:00','assistant','Legacy reply 53: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(433,1,'2026-09-05T14:42:19.644587+00:00','user','Legacy turn 54 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(434,1,'2026-09-05T14:42:19.645419+00:00','assistant','Legacy reply 54: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(435,2,'2026-09-05T14:42:19.646155+00:00','user','Legacy turn 54 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(436,2,'2026-09-05T14:42:19.646987+00:00','assistant','Legacy reply 54: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(437,3,'2026-09-05T14:42:19.647759+00:00','user','Legacy turn 54 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(438,3,'2026-09-05T14:42:19.648668+00:00','assistant','Legacy reply 54: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(439,4,'2026-09-05T14:42:19.649436+00:00','user','Legacy turn 54 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(440,4,'2026-09-05T14:42:19.650304+00:00','assistant','Legacy reply 54: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(441,1,'2026-09-05T14:42:19.651087+00:00','user','Legacy turn 55 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(442,1,'2026-09-05T14:42:19.651948+00:00','assistant','Legacy reply 55: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(443,2,'2026-09-05T14:42:19.652732+00:00','user','Legacy turn 55 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(444,2,'2026-09-05T14:42:19.653628+00:00','assistant','Legacy reply 55: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(445,3,'2026-09-05T14:42:19.654375+00:00','user','Legacy turn 55 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(446,3,'2026-09-05T14:42:19.655254+00:00','assistant','Legacy reply 55: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(447,4,'2026-09-05T14:42:19.656023+00:00','user','Legacy turn 55 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(448,4,'2026-09-05T14:42:19.656883+00:00','assistant','Legacy reply 55: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(449,1,'2026-09-05T14:42:19.657907+00:00','user','Legacy turn 56 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(450,1,'2026-09-05T14:42:19.658795+00:00','assistant','Legacy reply 56: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(451,2,'2026-09-05T14:42:19.659564+00:00','user','Legacy turn 56 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(452,2,'2026-09-05T14:42:19.660444+00:00','assistant','Legacy reply 56: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(453,3,'2026-09-05T14:42:19.661243+00:00','user','Legacy turn 56 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(454,3,'2026-09-05T14:42:19.662228+00:00','assistant','Legacy reply 56: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(455,4,'2026-09-05T14:42:19.663058+00:00','user','Legacy turn 56 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(456,4,'2026-09-05T14:42:19.663926+00:00','assistant','Legacy reply 56: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(457,1,'2026-09-05T14:42:19.664687+00:00','user','Legacy turn 57 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(458,1,'2026-09-05T14:42:19.665549+00:00','assistant','Legacy reply 57: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(459,2,'2026-09-05T14:42:19.666315+00:00','user','Legacy turn 57 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(460,2,'2026-09-05T14:42:19.667191+00:00','assistant','Legacy reply 57: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(461,3,'2026-09-05T14:42:19.667958+00:00','user','Legacy turn 57 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(462,3,'2026-09-05T14:42:19.668799+00:00','assistant','Legacy reply 57: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(463,4,'2026-09-05T14:42:19.669524+00:00','user','Legacy turn 57 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(464,4,'2026-09-05T14:42:19.670359+00:00','assistant','Legacy reply 57: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(465,1,'2026-09-05T14:42:19.671097+00:00','user','Legacy turn 58 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(466,1,'2026-09-05T14:42:19.671974+00:00','assistant','Legacy reply 58: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(467,2,'2026-09-05T14:42:19.672749+00:00','user','Legacy turn 58 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(468,2,'2026-09-05T14:42:19.673581+00:00','assistant','Legacy reply 58: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(469,3,'2026-09-05T14:42:19.674326+00:00','user','Legacy turn 58 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(470,3,'2026-09-05T14:42:19.675181+00:00','assistant','Legacy reply 58: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(471,4,'2026-09-05T14:42:19.675912+00:00','user','Legacy turn 58 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(472,4,'2026-09-05T14:42:19.676797+00:00','assistant','Legacy reply 58: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(473,1,'2026-09-05T14:42:19.677569+00:00','user','Legacy turn 59 in 0: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(474,1,'2026-09-05T14:42:19.678405+00:00','assistant','Legacy reply 59: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(475,2,'2026-09-05T14:42:19.679143+00:00','user','Legacy turn 59 in 1: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(476,2,'2026-09-05T14:42:19.679974+00:00','assistant','Legacy reply 59: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(477,3,'2026-09-05T14:42:19.680706+00:00','user','Legacy turn 59 in 2: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(478,3,'2026-09-05T14:42:19.681545+00:00','assistant','Legacy reply 59: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(479,4,'2026-09-05T14:42:19.682429+00:00','user','Legacy turn 59 in 3: what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? : what changed on the millrace weir? ');
INSERT INTO "messages" VALUES(480,4,'2026-09-05T14:42:19.683261+00:00','assistant','Legacy reply 59: the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. : the gate count and the listen port were both revised. ');
INSERT INTO "messages" VALUES(481,1,'2026-09-05T14:42:19.684298+00:00','user','Remember this project fact: {"subject": "Kestrel relay", "predicate": "maintainer", "value": "Dana Okonkwo"}');
INSERT INTO "messages" VALUES(482,1,'2026-09-05T14:42:19.684298+00:00','assistant','Stored project fact (claim record #1).');
INSERT INTO "messages" VALUES(483,1,'2026-09-05T14:42:19.686821+00:00','user','Remember this project fact: {"subject": "Kestrel relay", "predicate": "listen port", "value": "8443"}');
INSERT INTO "messages" VALUES(484,1,'2026-09-05T14:42:19.686821+00:00','assistant','Stored project fact (claim record #2).');
INSERT INTO "messages" VALUES(485,1,'2026-09-05T14:42:19.688251+00:00','user','Remember this project fact: {"subject": "Harrier box", "predicate": "datacenter", "value": "Fenwick"}');
INSERT INTO "messages" VALUES(486,1,'2026-09-05T14:42:19.688251+00:00','assistant','Stored project fact (claim record #3).');
INSERT INTO "messages" VALUES(487,1,'2026-09-05T14:42:19.689697+00:00','user','Remember this project fact: {"subject":"Kestrel relay","predicate":"listen port","value":"9443"}');
INSERT INTO "messages" VALUES(488,1,'2026-09-05T14:42:19.689697+00:00','assistant','Updated project fact (claim record #4). The prior value remains in this project''s version history.');
INSERT INTO "messages" VALUES(489,2,'2026-09-05T14:42:19.694340+00:00','user','Remember this project fact: {"subject":"Spent fact","predicate":"state","value":"gone"}');
INSERT INTO "messages" VALUES(490,2,'2026-09-05T14:42:19.694340+00:00','assistant','Stored project fact (claim record #5).');
INSERT INTO "messages" VALUES(491,2,'2026-09-05T14:42:19.695454+00:00','user','Erase this project fact: {"subject":"Spent fact","predicate":"state"}');
INSERT INTO "messages" VALUES(492,2,'2026-09-05T14:42:19.695454+00:00','assistant','Erased project fact (1 version removed; tombstone #13). 1 transcript copy remain until their conversations are deleted.');
CREATE TABLE model_call_budget_events (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                budget_scope TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reserved', 'completed')),
                estimated_prompt_tokens INTEGER NOT NULL
                    CHECK(estimated_prompt_tokens >= 0),
                prompt_tokens INTEGER CHECK(prompt_tokens IS NULL OR prompt_tokens >= 0),
                completion_tokens INTEGER
                    CHECK(completion_tokens IS NULL OR completion_tokens >= 0),
                success INTEGER CHECK(success IS NULL OR success IN (0, 1))
            );
CREATE TABLE model_call_metrics (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                profile TEXT NOT NULL,
                latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
                prompt_tokens INTEGER CHECK(prompt_tokens IS NULL OR prompt_tokens >= 0),
                completion_tokens INTEGER CHECK(completion_tokens IS NULL OR completion_tokens >= 0),
                success INTEGER NOT NULL CHECK(success IN (0, 1)),
                failure_kind TEXT
            , budget_scope TEXT);
CREATE TABLE ordinary_memory_provenance (
                memory_id INTEGER PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                origin TEXT NOT NULL,
                eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
                content_sha256 TEXT NOT NULL,
                provenance_sha256 TEXT NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memories(id)
            );
INSERT INTO "ordinary_memory_provenance" VALUES(5,'2026-09-05T14:42:19.691347+00:00','explicit_operator_memory',1,'b596d49e42ec6279bb6629127a27e410744af3cf188991dbd1c1c3a08f9d51fd','3eed9fa377d9d46e2f1dcd2dc0e690c61d1b34a9f6d208516e22b1ed86d3dc39');
INSERT INTO "ordinary_memory_provenance" VALUES(6,'2026-09-05T14:42:19.692198+00:00','explicit_operator_memory',1,'3bb8b8cce4db2020ee3eccb95fea01e9ff96ffc19649bae346216a45f97d0955','d4f6d58b51a612e16d152189c14411dfee8e68cbd82879d8f433ed2d207a3998');
INSERT INTO "ordinary_memory_provenance" VALUES(7,'2026-09-05T14:42:19.692899+00:00','explicit_operator_memory',1,'efb309f2ac67da60ef8fe1d6587cebb2698a5ebdebc27512e58dd8a5f33f1627','26658211f2b06ae68a8aaa19610b62ba9adfc5d6b245c3bb01d59c6be4280d1f');
INSERT INTO "ordinary_memory_provenance" VALUES(8,'2026-09-05T14:42:19.693575+00:00','unverified',0,'4e8e4787a8919247d287ae3d3934e38e6e1368901eb59b404483fe9320d9f6ac','32b44190713bda4f233c1b0b43cffccc6e20f17a36373b4ebaf3caf969374e39');
CREATE TABLE ordinary_memory_quality_assessments (
                    memory_id INTEGER PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    contract_version INTEGER NOT NULL CHECK(
                        contract_version=1
                    ),
                    recall_allowed INTEGER NOT NULL CHECK(recall_allowed IN (0,1)),
                    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
                    source_is_null INTEGER NOT NULL CHECK(source_is_null IN (0,1)),
                    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
                    provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256)=64),
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );
CREATE TABLE persistent_approval_grants (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                effect_fingerprint TEXT NOT NULL UNIQUE,
                action TEXT NOT NULL,
                resource TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_approval_id INTEGER,
                revoked_at TEXT, grant_kind TEXT NOT NULL DEFAULT 'always'
                   CHECK(grant_kind IN ('always', 'session')), scope TEXT, expires_at TEXT,
                FOREIGN KEY(source_approval_id) REFERENCES approvals(id)
            );
CREATE TABLE preferences (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE, value TEXT NOT NULL, source TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
            );
CREATE TABLE presence_jobs (
                job_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                conversation_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                model_override TEXT NOT NULL CHECK(model_override IN
                    ('auto', 'fast', 'reasoning', 'coding', 'deep')),
                status TEXT NOT NULL CHECK(status IN
                    ('queued', 'running', 'completed', 'failed',
                     'cancelled', 'interrupted')),
                lease_owner TEXT, started_at TEXT, finished_at TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0
                    CHECK(cancel_requested IN (0, 1)),
                last_error TEXT, metrics_json TEXT NOT NULL DEFAULT '{}', run_origin TEXT NOT NULL DEFAULT 'interactive' CHECK(run_origin IN ('interactive','companion_suggestion','companion_action')), replayable INTEGER NOT NULL DEFAULT 1 CHECK(replayable IN (0, 1)),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            );
CREATE TABLE presence_pairing_codes (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                label TEXT NOT NULL, code_salt BLOB NOT NULL,
                code_digest BLOB NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','consumed','revoked')),
                consumed_at TEXT
            );
CREATE TABLE presence_sessions (
                session_id TEXT PRIMARY KEY, session_digest TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL, revoked_at TEXT,
                label TEXT NOT NULL, pairing_code_id INTEGER NOT NULL,
                FOREIGN KEY(pairing_code_id) REFERENCES presence_pairing_codes(id)
            );
CREATE TABLE proactive_backlog (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('research', 'ideas', 'prototype')),
                subject_id INTEGER NOT NULL, goal_id INTEGER,
                instructions TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 50,
                interval_hours INTEGER NOT NULL DEFAULT 168, next_run TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                FOREIGN KEY(subject_id) REFERENCES approved_subjects(id),
                FOREIGN KEY(goal_id) REFERENCES goals(id)
            );
CREATE TABLE proactive_runs (
                id INTEGER PRIMARY KEY, backlog_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL,
                result_summary TEXT,
                FOREIGN KEY(backlog_id) REFERENCES proactive_backlog(id)
            );
CREATE TABLE recovery_attestations (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                runtime_sha256 TEXT NOT NULL, schema_version INTEGER NOT NULL,
                passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
                evidence_json TEXT NOT NULL
            );
CREATE TABLE reflections (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, task_id INTEGER,
                conversation_id INTEGER, status TEXT NOT NULL, summary TEXT NOT NULL,
                mistakes TEXT NOT NULL, improvements TEXT NOT NULL,
                tool_calls INTEGER NOT NULL DEFAULT 0
            , prediction_id INTEGER);
CREATE TABLE runtime_control (
                id INTEGER PRIMARY KEY CHECK(id=1),
                state TEXT NOT NULL CHECK(state IN ('running', 'paused', 'stopped')),
                updated_at TEXT NOT NULL, reason TEXT
            );
INSERT INTO "runtime_control" VALUES(1,'running','2026-09-05T14:42:19.191881+00:00',NULL);
CREATE TABLE scheduled_jobs (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL
                    CHECK(interval_minutes BETWEEN 1 AND 525600),
                next_run_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                last_run_at TEXT,
                last_task_id INTEGER,
                FOREIGN KEY(project_id) REFERENCES agent_projects(id),
                FOREIGN KEY(last_task_id) REFERENCES tasks(id)
            );
CREATE TABLE screen_companion_action_outcomes (
                feedback_id INTEGER PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN
                    ('complete','failed','incomplete')),
                evidence_kind TEXT NOT NULL CHECK(evidence_kind IN
                    ('cited_sources','failure_observed','process_evidence','tool_success')),
                prediction_id INTEGER NOT NULL UNIQUE,
                reusable INTEGER NOT NULL CHECK(reusable IN (0, 1)),
                CHECK((outcome='complete' AND reusable=1) OR
                      (outcome IN ('failed','incomplete') AND reusable=0)),
                FOREIGN KEY(feedback_id) REFERENCES screen_companion_feedback(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
            );
CREATE TABLE screen_companion_auto_receipts (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                day_key TEXT NOT NULL CHECK(
                    length(day_key)=10 AND
                    day_key GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
                context_sha256 TEXT NOT NULL CHECK(
                    length(context_sha256)=64 AND
                    context_sha256 NOT GLOB '*[^0-9a-f]*'),
                UNIQUE(day_key, context_sha256)
            );
CREATE TABLE screen_companion_conversations (
                conversation_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                    ON DELETE CASCADE
            );
CREATE TABLE screen_companion_feedback (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                suggestion_sha256 TEXT NOT NULL CHECK(
                    length(suggestion_sha256)=64 AND suggestion_sha256 NOT GLOB '*[^0-9a-f]*'),
                context_sha256 TEXT NOT NULL CHECK(
                    length(context_sha256)=64 AND context_sha256 NOT GLOB '*[^0-9a-f]*'),
                application_sha256 TEXT NOT NULL CHECK(
                    length(application_sha256)=64 AND application_sha256 NOT GLOB '*[^0-9a-f]*'),
                category TEXT NOT NULL CHECK(category IN
                    ('coding','general','navigation','organization','research','writing')),
                action_mode TEXT NOT NULL CHECK(action_mode IN
                    ('suggest','collaborate')),
                decision TEXT NOT NULL CHECK(decision IN ('accepted','dismissed')),
                action_job_sha256 TEXT UNIQUE CHECK(
                    action_job_sha256 IS NULL OR
                    (length(action_job_sha256)=64 AND action_job_sha256 NOT GLOB '*[^0-9a-f]*')),
                CHECK(
                    (decision='accepted' AND action_job_sha256 IS NOT NULL) OR
                    (decision='dismissed' AND action_job_sha256 IS NULL)
                ),
                UNIQUE(suggestion_sha256, context_sha256, application_sha256)
            );
CREATE TABLE screen_companion_receipts (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                rule_id INTEGER,
                application_sha256 TEXT NOT NULL,
                context_sha256 TEXT NOT NULL,
                action_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                job_id TEXT,
                FOREIGN KEY(rule_id) REFERENCES screen_companion_rules(id)
            );
CREATE TABLE screen_companion_rules (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                trigger_app TEXT NOT NULL,
                title_contains TEXT,
                action_prompt TEXT NOT NULL,
                action_mode TEXT NOT NULL CHECK(action_mode IN
                    ('suggest', 'collaborate')),
                cooldown_seconds INTEGER NOT NULL CHECK(
                    cooldown_seconds BETWEEN 30 AND 86400),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                last_triggered_at TEXT
            );
CREATE TABLE screen_companion_state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                mode TEXT NOT NULL CHECK(mode IN
                    ('disabled', 'observe', 'suggest', 'collaborate')),
                paused INTEGER NOT NULL CHECK(paused IN (0, 1)),
                auto_suggest INTEGER NOT NULL CHECK(auto_suggest IN (0, 1)),
                excluded_apps_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );
INSERT INTO "screen_companion_state" VALUES(1,'disabled',1,0,'[]','2026-09-05T14:42:19.210476+00:00');
CREATE TABLE self_repair_proposals (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                trigger_text TEXT NOT NULL, failing_tests_json TEXT NOT NULL,
                diff_text TEXT NOT NULL, diff_sha256 TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('proposed', 'voided')),
                void_reason TEXT, candidate_path TEXT NOT NULL
            );
CREATE TABLE self_snapshots (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, snapshot_json TEXT NOT NULL
            );
CREATE TABLE specialist_agents (
                agent_key TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
                model_profile TEXT NOT NULL, families_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ready', 'working')),
                active_task_id INTEGER, completed_tasks INTEGER NOT NULL DEFAULT 0,
                failed_tasks INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                last_started_at TEXT, last_reported_at TEXT
            );
INSERT INTO "specialist_agents" VALUES('coding','Forge','software implementation, debugging, refactoring, and verification only','coding','["code_build","code_fix","code_refactor","code_test"]','ready',NULL,0,0,'2026-09-05T14:42:19.193889+00:00','2026-09-05T14:42:19.193889+00:00',NULL,NULL);
INSERT INTO "specialist_agents" VALUES('research','Archivist','source-grounded public research and learning briefs only','reasoning','["deep_research","learning_brief"]','ready',NULL,0,0,'2026-09-05T14:42:19.193889+00:00','2026-09-05T14:42:19.193889+00:00',NULL,NULL);
INSERT INTO "specialist_agents" VALUES('cybersecurity','Sentinel','defensive cybersecurity analysis, hardening, and incident response only','deep','["security_analysis"]','ready',NULL,0,0,'2026-09-05T14:42:19.193889+00:00','2026-09-05T14:42:19.193889+00:00',NULL,NULL);
INSERT INTO "specialist_agents" VALUES('network','Relay','network architecture, diagnostics, and engineering analysis only','deep','["security_analysis"]','ready',NULL,0,0,'2026-09-05T14:42:19.193889+00:00','2026-09-05T14:42:19.193889+00:00',NULL,NULL);
INSERT INTO "specialist_agents" VALUES('operations','Steward','bounded local workspace file operations only','reasoning','["file_ops","desktop_file_ops"]','ready',NULL,0,0,'2026-09-05T14:42:19.193889+00:00','2026-09-05T14:42:19.193889+00:00',NULL,NULL);
CREATE TABLE strategy_transfer_applications (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    prediction_id INTEGER NOT NULL,
                    memory_id INTEGER NOT NULL,
                    project_id INTEGER NOT NULL,
                    strategy TEXT NOT NULL CHECK(strategy IN ('checkpoint_and_resume','compare_authoritative_sources','inspect_before_change','verify_output')),
                    source_family TEXT NOT NULL CHECK(source_family IN ('code_build','code_fix','code_refactor','code_test','conversation','deep_research','desktop_file_ops','external_publish','file_ops','learning_brief','security_analysis')),
                    target_family TEXT NOT NULL CHECK(target_family IN ('code_build','code_fix','code_refactor','code_test','conversation','deep_research','desktop_file_ops','external_publish','file_ops','learning_brief','security_analysis')),
                    mode TEXT NOT NULL CHECK(mode IN ('advise','observe','trial')),
                    applied INTEGER NOT NULL CHECK(applied IN (0, 1)),
                    rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 32),
                    source_observation_sha256 TEXT NOT NULL CHECK(
                        length(source_observation_sha256)=64 AND
                        source_observation_sha256 NOT GLOB '*[^0-9a-f]*'),
                    source_provenance_sha256 TEXT NOT NULL CHECK(
                        length(source_provenance_sha256)=64 AND
                        source_provenance_sha256 NOT GLOB '*[^0-9a-f]*'),
                    source_control_sha256 TEXT NOT NULL CHECK(
                        length(source_control_sha256)=64 AND
                        source_control_sha256 NOT GLOB '*[^0-9a-f]*'),
                    resolved_at TEXT,
                    successful INTEGER CHECK(successful IN (0, 1)),
                    application_sha256 TEXT NOT NULL CHECK(
                        length(application_sha256)=64 AND
                        application_sha256 NOT GLOB '*[^0-9a-f]*'),
                    UNIQUE(prediction_id, memory_id, strategy),
                    CHECK(source_family <> target_family),
                    CHECK(mode IN ('advise', 'trial') OR applied=0),
                    CHECK((resolved_at IS NULL AND successful IS NULL) OR
                          (resolved_at IS NOT NULL AND successful IN (0, 1))),
                    FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id) REFERENCES agent_projects(id)
                );
CREATE TABLE strategy_transfer_attestations (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('applied_ab','sealed_benchmark')),
                recorded_at TEXT NOT NULL,
                evaluator_version TEXT NOT NULL,
                evaluator_sha256 TEXT NOT NULL CHECK(
                    length(evaluator_sha256)=64 AND
                    evaluator_sha256 NOT GLOB '*[^0-9a-f]*'),
                config_sha256 TEXT NOT NULL CHECK(
                    length(config_sha256)=64 AND
                    config_sha256 NOT GLOB '*[^0-9a-f]*'),
                fixture_sha256 TEXT,
                assignment_manifest_sha256 TEXT,
                artifact_json TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL CHECK(
                    length(artifact_sha256)=64 AND
                    artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
                attestation_sha256 TEXT NOT NULL CHECK(
                    length(attestation_sha256)=64 AND
                    attestation_sha256 NOT GLOB '*[^0-9a-f]*'),
                UNIQUE(kind, artifact_sha256),
                UNIQUE(kind, attestation_sha256),
                CHECK((kind='sealed_benchmark' AND fixture_sha256 IS NOT NULL
                       AND assignment_manifest_sha256 IS NULL) OR
                      (kind='applied_ab' AND fixture_sha256 IS NULL
                       AND assignment_manifest_sha256 IS NOT NULL))
            );
CREATE TABLE strategy_transfer_trial_assignments (
                id INTEGER PRIMARY KEY,
                manifest_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                target_family TEXT NOT NULL CHECK(target_family IN ('code_build','code_fix','code_refactor','code_test','conversation','deep_research','desktop_file_ops','external_publish','file_ops','learning_brief','security_analysis')),
                family_sequence INTEGER NOT NULL CHECK(family_sequence>=0),
                block_index INTEGER NOT NULL CHECK(block_index>=0),
                block_slot INTEGER NOT NULL CHECK(block_slot BETWEEN 0 AND 3),
                arm TEXT NOT NULL CHECK(arm IN ('control','treatment')),
                strategies_json TEXT NOT NULL,
                selection_sha256 TEXT NOT NULL CHECK(
                    length(selection_sha256)=64 AND
                    selection_sha256 NOT GLOB '*[^0-9a-f]*'),
                assignment_sha256 TEXT NOT NULL UNIQUE CHECK(
                    length(assignment_sha256)=64 AND
                    assignment_sha256 NOT GLOB '*[^0-9a-f]*'),
                prompt_recorded_at TEXT,
                base_prompt_sha256 TEXT,
                final_prompt_sha256 TEXT,
                advice_applied INTEGER CHECK(advice_applied IN (0, 1)),
                prompt_receipt_sha256 TEXT,
                provider_dispatched_at TEXT,
                provider_dispatch_sha256 TEXT,
                status TEXT NOT NULL CHECK(status IN ('aborted','assigned','contaminated','resolved')),
                status_reason TEXT CHECK(
                    status_reason IS NULL OR
                    status_reason IN ('application_receipt_invalid','assignment_integrity','manifest_drift','operator_abort','prediction_outcome_invalid','prompt_receipt_invalid','prompt_receipt_missing','provider_dispatch_missing','quarantine_detected','runtime_drift')),
                resolved_at TEXT,
                successful INTEGER CHECK(successful IN (0, 1)),
                outcome_sha256 TEXT,
                UNIQUE(manifest_id, target_family, family_sequence),
                UNIQUE(manifest_id, target_family, block_index, block_slot),
                CHECK((prompt_recorded_at IS NULL AND base_prompt_sha256 IS NULL
                       AND final_prompt_sha256 IS NULL AND advice_applied IS NULL
                       AND prompt_receipt_sha256 IS NULL) OR
                      (prompt_recorded_at IS NOT NULL
                       AND length(base_prompt_sha256)=64
                       AND base_prompt_sha256 NOT GLOB '*[^0-9a-f]*'
                       AND length(final_prompt_sha256)=64
                       AND final_prompt_sha256 NOT GLOB '*[^0-9a-f]*'
                       AND advice_applied IN (0, 1)
                       AND length(prompt_receipt_sha256)=64
                       AND prompt_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
                CHECK((provider_dispatched_at IS NULL
                       AND provider_dispatch_sha256 IS NULL) OR
                      (provider_dispatched_at IS NOT NULL
                       AND length(provider_dispatch_sha256)=64
                       AND provider_dispatch_sha256 NOT GLOB '*[^0-9a-f]*')),
                CHECK((status='assigned' AND resolved_at IS NULL
                       AND successful IS NULL AND outcome_sha256 IS NULL
                       AND status_reason IS NULL) OR
                      (status='resolved' AND resolved_at IS NOT NULL
                       AND successful IN (0, 1) AND outcome_sha256 IS NOT NULL
                       AND status_reason IS NULL) OR
                      (status IN ('aborted', 'contaminated')
                       AND resolved_at IS NOT NULL AND successful IS NULL
                       AND outcome_sha256 IS NOT NULL
                       AND status_reason IS NOT NULL)),
                FOREIGN KEY(manifest_id)
                    REFERENCES strategy_transfer_trial_manifests(id),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id),
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            );
CREATE TABLE strategy_transfer_trial_manifests (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                target_families_json TEXT NOT NULL,
                family_caps_json TEXT NOT NULL,
                strategies_json TEXT NOT NULL,
                sample_cap INTEGER NOT NULL CHECK(
                    sample_cap BETWEEN 40 AND 200 AND sample_cap % 4 = 0),
                block_size INTEGER NOT NULL CHECK(block_size=4),
                seed TEXT NOT NULL CHECK(
                    length(seed)=64 AND seed NOT GLOB '*[^0-9a-f]*'),
                evaluator_version TEXT NOT NULL,
                evaluator_sha256 TEXT NOT NULL CHECK(
                    length(evaluator_sha256)=64 AND
                    evaluator_sha256 NOT GLOB '*[^0-9a-f]*'),
                fixture_sha256 TEXT NOT NULL CHECK(
                    length(fixture_sha256)=64 AND
                    fixture_sha256 NOT GLOB '*[^0-9a-f]*'),
                config_sha256 TEXT NOT NULL CHECK(
                    length(config_sha256)=64 AND
                    config_sha256 NOT GLOB '*[^0-9a-f]*'),
                runtime_sha256 TEXT NOT NULL CHECK(
                    length(runtime_sha256)=64 AND
                    runtime_sha256 NOT GLOB '*[^0-9a-f]*'),
                operator_confirmed INTEGER NOT NULL CHECK(operator_confirmed=1),
                status TEXT NOT NULL CHECK(status IN ('aborted','active','closed','promoted')),
                status_reason TEXT CHECK(
                    status_reason IS NULL OR status_reason IN ('cap_reached','drift_detected','expired','integrity_error','operator_abort','operator_promoted','quarantine_detected','trial_complete')),
                closed_at TEXT,
                promoted_at TEXT,
                manifest_sha256 TEXT NOT NULL UNIQUE CHECK(
                    length(manifest_sha256)=64 AND
                    manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
                state_sha256 TEXT NOT NULL CHECK(
                    length(state_sha256)=64 AND
                    state_sha256 NOT GLOB '*[^0-9a-f]*'),
                UNIQUE(project_id, seed),
                CHECK((status='active' AND closed_at IS NULL AND promoted_at IS NULL)
                   OR (status IN ('closed', 'aborted') AND closed_at IS NOT NULL
                       AND promoted_at IS NULL)
                   OR (status='promoted' AND closed_at IS NOT NULL
                       AND promoted_at IS NOT NULL)),
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            );
CREATE TABLE task_predictions (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                task_id INTEGER,
                conversation_id INTEGER,
                origin TEXT NOT NULL,
                family TEXT NOT NULL,
                profile TEXT NOT NULL,
                model TEXT NOT NULL,
                predicted_success REAL NOT NULL,
                predicted_steps INTEGER NOT NULL,
                predicted_verification TEXT NOT NULL,
                basis TEXT NOT NULL,
                resolved_at TEXT,
                actual_status TEXT,
                actual_steps INTEGER,
                evidence_ok INTEGER,
                failure_class TEXT
            , run_id_sha256 TEXT);
CREATE TABLE task_strategy_observations (
                prediction_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                project_id INTEGER NOT NULL,
                source_family TEXT NOT NULL CHECK(source_family IN ('code_build','code_fix','code_refactor','code_test','conversation','deep_research','desktop_file_ops','external_publish','file_ops','learning_brief','security_analysis')),
                evidence_json TEXT NOT NULL,
                strategies_json TEXT NOT NULL,
                observation_sha256 TEXT NOT NULL CHECK(
                    length(observation_sha256)=64 AND
                    observation_sha256 NOT GLOB '*[^0-9a-f]*'),
                FOREIGN KEY(prediction_id) REFERENCES task_predictions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES agent_projects(id)
            );
CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                status TEXT NOT NULL, prompt TEXT NOT NULL, result TEXT
            , available_at TEXT, lease_owner TEXT, lease_expires_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, last_error TEXT, idempotency_key TEXT, goal_id INTEGER, backlog_id INTEGER, awaiting_approval_id INTEGER, project_id INTEGER NOT NULL DEFAULT 1, requested_model TEXT, initiative_event_id INTEGER, specialist_key TEXT, delegated_by TEXT, parent_conversation_id INTEGER, model_budget_scope TEXT, initial_available_at TEXT, availability_mode TEXT NOT NULL
                   DEFAULT 'legacy_unknown'
                   CHECK(availability_mode IN
                       ('immediate','scheduled','legacy_unknown')));
CREATE TABLE training_examples (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                conversation_id INTEGER,
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                model TEXT NOT NULL,
                profile TEXT NOT NULL,
                task_kind TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                quality_score REAL NOT NULL CHECK(quality_score >= 0 AND quality_score <= 1),
                verified INTEGER NOT NULL CHECK(verified IN (0, 1)),
                split TEXT NOT NULL CHECK(split IN ('train', 'validation', 'test')),
                content_hash TEXT NOT NULL UNIQUE,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );
CREATE TABLE work_domains (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('research', 'workspace_project', 'maintenance')),
                project_id INTEGER NOT NULL, max_tasks_per_day INTEGER NOT NULL,
                standing_authorization INTEGER NOT NULL CHECK(standing_authorization IN (0, 1)),
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1))
            );
CREATE INDEX idx_messages_conversation ON messages(conversation_id, id);
CREATE INDEX idx_tasks_status ON tasks(status, id);
CREATE INDEX idx_tasks_claim ON tasks(status, available_at, id);
CREATE INDEX idx_tasks_lease ON tasks(status, lease_expires_at);
CREATE UNIQUE INDEX idx_tasks_idempotency ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_learning_runs_task ON learning_runs(task_id);
CREATE INDEX idx_training_export ON training_examples(verified, quality_score, split, id);
CREATE INDEX idx_activity_created ON activity_log(created_at, id);
CREATE INDEX idx_journal_goal ON journal_entries(goal_id, id);
CREATE INDEX idx_reflections_task ON reflections(task_id, id);
CREATE INDEX idx_backlog_due ON proactive_backlog(enabled, next_run, priority);
CREATE INDEX idx_proactive_runs_task ON proactive_runs(task_id);
CREATE INDEX idx_approvals_fingerprint ON approvals(fingerprint, status, id);
CREATE INDEX idx_approvals_scope ON approvals(scope, fingerprint, status, id);
CREATE INDEX idx_tasks_awaiting_approval ON tasks(awaiting_approval_id) WHERE awaiting_approval_id IS NOT NULL;
CREATE INDEX idx_predictions_family ON task_predictions(family, resolved_at);
CREATE INDEX idx_predictions_open ON task_predictions(id) WHERE resolved_at IS NULL;
CREATE INDEX idx_model_call_metrics_created ON model_call_metrics(created_at, id);
CREATE INDEX idx_conversations_project ON conversations(project_id, id);
CREATE INDEX idx_tasks_project ON tasks(project_id, status, id);
CREATE INDEX idx_memories_lessons ON memories(kind, family, outcome_status, id);
CREATE INDEX idx_recovery_attestations_created ON recovery_attestations(created_at, id);
CREATE INDEX idx_work_domains_project ON work_domains(project_id, enabled);
CREATE INDEX idx_initiative_events_created ON initiative_events(created_at, id);
CREATE INDEX idx_tasks_specialist ON tasks(specialist_key, status, id);
CREATE INDEX idx_tasks_parent_conversation ON tasks(parent_conversation_id, id);
CREATE INDEX idx_memory_embeddings_model ON memory_embeddings(model, memory_id);
CREATE INDEX idx_memory_retrievals_prediction ON memory_retrievals(prediction_id, rank);
CREATE INDEX idx_memory_retrievals_memory ON memory_retrievals(memory_id, resolved_at);
CREATE INDEX idx_memory_statistics_utility ON memory_statistics(utility DESC, resolved DESC);
CREATE INDEX idx_memory_claims_key ON memory_claims(claim_key, status, id);
CREATE INDEX idx_memory_claims_memory ON memory_claims(memory_id);
CREATE INDEX idx_memory_claim_events_claim ON memory_claim_events(claim_id, id);
CREATE INDEX idx_memory_claim_evidence_claim ON memory_claim_evidence(claim_id, id);
CREATE UNIQUE INDEX idx_presence_jobs_live_conversation
               ON presence_jobs(conversation_id)
               WHERE status IN ('queued', 'running');
CREATE INDEX idx_presence_jobs_status_created
               ON presence_jobs(status, created_at, job_id);
CREATE INDEX idx_presence_pairing_status_expiry
               ON presence_pairing_codes(status, expires_at, id);
CREATE INDEX idx_presence_sessions_live
               ON presence_sessions(revoked_at, expires_at, created_at);
CREATE INDEX idx_memory_embedding_leases_due
               ON memory_embedding_leases(model, lease_expires_at, memory_id);
CREATE INDEX idx_memory_query_embeddings_lru
               ON memory_query_embeddings(last_used_at, query_sha256);
CREATE INDEX idx_claim_observations_predicate
               ON memory_claim_observations(predicate, observed_at, id);
CREATE INDEX idx_claim_observations_claim
               ON memory_claim_observations(claim_key, observed_at, id);
CREATE INDEX idx_claim_observations_value
               ON memory_claim_observations(claim_id, observed_at, id);
CREATE INDEX idx_model_budget_scope ON model_call_budget_events(budget_scope, id);
CREATE INDEX idx_tasks_model_budget_scope ON tasks(model_budget_scope, id) WHERE model_budget_scope IS NOT NULL;
CREATE INDEX idx_persistent_approval_grants_live
               ON persistent_approval_grants(revoked_at, id);
CREATE INDEX idx_persistent_approval_session
               ON persistent_approval_grants(grant_kind, scope, expires_at, revoked_at);
CREATE INDEX idx_scheduled_jobs_due
               ON scheduled_jobs(enabled, next_run_at, project_id, id);
CREATE INDEX idx_conversation_goals_current
               ON conversation_goals(conversation_id, state, id);
CREATE UNIQUE INDEX idx_reflections_prediction ON reflections(prediction_id) WHERE prediction_id IS NOT NULL;
CREATE INDEX idx_lesson_provenance_memory ON lesson_provenance(memory_id);
CREATE INDEX idx_ordinary_memory_provenance_eligible ON ordinary_memory_provenance(eligible, memory_id);
CREATE INDEX idx_screen_companion_rules_enabled
               ON screen_companion_rules(enabled, trigger_app, id);
CREATE INDEX idx_screen_companion_feedback_aggregate
               ON screen_companion_feedback(category, action_mode, decision, id);
CREATE INDEX idx_screen_companion_outcomes_aggregate
               ON screen_companion_action_outcomes(outcome, evidence_kind, feedback_id);
CREATE UNIQUE INDEX idx_predictions_run_id ON task_predictions(run_id_sha256) WHERE run_id_sha256 IS NOT NULL;
CREATE INDEX idx_screen_companion_auto_recent
               ON screen_companion_auto_receipts(created_at DESC, id DESC);
CREATE INDEX idx_lesson_controls_scope
               ON lesson_controls(project_id, lifecycle_status, valid_until, memory_id);
CREATE INDEX idx_lesson_applications_prediction
               ON lesson_applications(prediction_id, rank);
CREATE INDEX idx_task_strategy_observations_scope
               ON task_strategy_observations(
                   project_id, source_family, prediction_id
               );
CREATE INDEX idx_strategy_transfer_attestations_compatibility
               ON strategy_transfer_attestations(
                   kind, evaluator_version, evaluator_sha256,
                   config_sha256, id
               );
CREATE INDEX idx_strategy_transfer_applications_prediction
                   ON strategy_transfer_applications(prediction_id, rank, id);
CREATE INDEX idx_strategy_transfer_applications_effectiveness
                   ON strategy_transfer_applications(
                       target_family, strategy, mode, applied, resolved_at,
                       prediction_id
                   );
CREATE INDEX idx_strategy_transfer_trial_manifest_scope
               ON strategy_transfer_trial_manifests(
                   project_id, status, expires_at, id
               );
CREATE INDEX idx_strategy_transfer_trial_assignment_manifest
               ON strategy_transfer_trial_assignments(
                   manifest_id, target_family, status, family_sequence
               );
CREATE INDEX idx_long_horizon_plan_status ON long_horizon_plans(status, project_id, id);
CREATE INDEX idx_long_horizon_stage_claim ON long_horizon_stages(plan_id, status, ordinal);
CREATE INDEX idx_long_horizon_lease ON long_horizon_stages(status, lease_expires_at);
CREATE INDEX idx_long_horizon_mutation_stage ON long_horizon_mutation_receipts(stage_id, id);
CREATE INDEX idx_long_horizon_retry_plan ON long_horizon_retry_receipts(plan_id, id);
CREATE INDEX idx_long_horizon_usage_plan ON long_horizon_usage_reservations(plan_id, id);
CREATE TRIGGER tasks_schedule_binding_immutable
               BEFORE UPDATE OF initial_available_at, availability_mode ON tasks
               WHEN OLD.initial_available_at IS NOT NEW.initial_available_at
                 OR OLD.availability_mode IS NOT NEW.availability_mode
               BEGIN
                   SELECT RAISE(ABORT, 'task scheduling intent is immutable');
               END;
CREATE INDEX idx_memory_claims_scope_key
               ON memory_claims(scope, claim_key, status, id);
CREATE TRIGGER memory_claim_scope_valid_insert
               BEFORE INSERT ON memory_claims
               WHEN NEW.scope <> 'global' AND (
                   length(NEW.scope) > 27
                   OR substr(NEW.scope, 1, 8) <> 'project:'
                   OR length(substr(NEW.scope, 9)) = 0
                   OR substr(NEW.scope, 9) GLOB '*[^0-9]*'
                   OR CAST(substr(NEW.scope, 9) AS INTEGER) <= 0
                   OR NEW.scope <> 'project:' || CAST(
                       CAST(substr(NEW.scope, 9) AS INTEGER) AS TEXT
                   )
               )
               BEGIN
                   SELECT RAISE(ABORT, 'invalid memory claim scope');
               END;
CREATE TRIGGER memory_claim_scope_immutable
               BEFORE UPDATE OF scope ON memory_claims
               WHEN NEW.scope <> OLD.scope
               BEGIN
                   SELECT RAISE(ABORT, 'memory claim scope is immutable');
               END;
CREATE TRIGGER ordinary_memory_quality_memory_changed
               AFTER UPDATE OF created_at,kind,content,source ON memories
               WHEN NEW.created_at IS NOT OLD.created_at
                 OR NEW.kind IS NOT OLD.kind
                 OR NEW.content IS NOT OLD.content
                 OR NEW.source IS NOT OLD.source
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id IN (OLD.id, NEW.id);
                   DELETE FROM memory_embeddings WHERE memory_id IN (OLD.id, NEW.id);
                   DELETE FROM memory_embedding_leases
                   WHERE memory_id IN (OLD.id, NEW.id);
               END;
CREATE TRIGGER ordinary_memory_quality_provenance_inserted
               AFTER INSERT ON ordinary_memory_provenance
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id=NEW.memory_id;
                   DELETE FROM memory_embeddings WHERE memory_id=NEW.memory_id;
                   DELETE FROM memory_embedding_leases WHERE memory_id=NEW.memory_id;
               END;
CREATE TRIGGER ordinary_memory_quality_provenance_changed
               AFTER UPDATE OF memory_id,origin,eligible,content_sha256,provenance_sha256
               ON ordinary_memory_provenance
               WHEN NEW.memory_id IS NOT OLD.memory_id
                 OR NEW.origin IS NOT OLD.origin
                 OR NEW.eligible IS NOT OLD.eligible
                 OR NEW.content_sha256 IS NOT OLD.content_sha256
                 OR NEW.provenance_sha256 IS NOT OLD.provenance_sha256
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
                   DELETE FROM memory_embeddings
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
                   DELETE FROM memory_embedding_leases
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
               END;
CREATE TRIGGER ordinary_memory_quality_provenance_deleted
               AFTER DELETE ON ordinary_memory_provenance
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id=OLD.memory_id;
                   DELETE FROM memory_embeddings WHERE memory_id=OLD.memory_id;
                   DELETE FROM memory_embedding_leases WHERE memory_id=OLD.memory_id;
               END;
CREATE TRIGGER ordinary_memory_quality_assessment_inserted
               AFTER INSERT ON ordinary_memory_quality_assessments
               BEGIN
                   DELETE FROM memory_embeddings WHERE memory_id=NEW.memory_id;
                   DELETE FROM memory_embedding_leases WHERE memory_id=NEW.memory_id;
               END;
CREATE TRIGGER ordinary_memory_quality_assessment_changed
               AFTER UPDATE ON ordinary_memory_quality_assessments
               WHEN NEW.memory_id IS NOT OLD.memory_id
                 OR NEW.contract_version IS NOT OLD.contract_version
                 OR NEW.recall_allowed IS NOT OLD.recall_allowed
                 OR NEW.content_sha256 IS NOT OLD.content_sha256
                 OR NEW.source_is_null IS NOT OLD.source_is_null
                 OR NEW.source_sha256 IS NOT OLD.source_sha256
                 OR NEW.provenance_sha256 IS NOT OLD.provenance_sha256
               BEGIN
                   DELETE FROM ordinary_memory_quality_assessments
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
                   DELETE FROM memory_embeddings
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
                   DELETE FROM memory_embedding_leases
                   WHERE memory_id IN (OLD.memory_id, NEW.memory_id);
               END;
CREATE TRIGGER ordinary_memory_quality_assessment_deleted
               AFTER DELETE ON ordinary_memory_quality_assessments
               BEGIN
                   DELETE FROM memory_embeddings WHERE memory_id=OLD.memory_id;
                   DELETE FROM memory_embedding_leases WHERE memory_id=OLD.memory_id;
               END;
CREATE INDEX idx_memory_fact_proposals_conversation
               ON memory_fact_proposals(conversation_id, status, id);
CREATE INDEX idx_memory_spine_events_subject ON memory_spine_events(subject_kind, subject_id, id);
CREATE INDEX idx_memory_spine_events_kind ON memory_spine_events(kind, id);
CREATE UNIQUE INDEX idx_memory_claims_spine_event ON memory_claims(spine_event_id);
CREATE UNIQUE INDEX idx_memories_spine_event ON memories(spine_event_id);
CREATE INDEX idx_memory_graph_entities_key ON memory_graph_entities(entity_key, scope);
CREATE INDEX idx_memory_graph_edges_out ON memory_graph_edges(scope, src_entity_id, status, predicate_key, claim_id);
CREATE INDEX idx_memory_graph_edges_in ON memory_graph_edges(scope, dst_entity_id, status, predicate_key, claim_id);
CREATE INDEX idx_memory_graph_edges_key ON memory_graph_edges(scope, claim_key, claim_id);
CREATE TRIGGER memory_spine_events_no_delete
BEFORE DELETE ON memory_spine_events
BEGIN SELECT RAISE(ABORT, 'memory spine events are append-only'); END;
CREATE TRIGGER memory_spine_events_redaction_only
BEFORE UPDATE ON memory_spine_events
WHEN NOT (
    NEW.id IS OLD.id AND NEW.created_at IS OLD.created_at AND NEW.kind IS OLD.kind
    AND NEW.actor IS OLD.actor AND NEW.source IS OLD.source AND NEW.scope IS OLD.scope
    AND NEW.permission IS OLD.permission
    AND NEW.conversation_id IS OLD.conversation_id
    AND NEW.subject_kind IS OLD.subject_kind AND NEW.subject_id IS OLD.subject_id
    AND NEW.parent_event_id IS OLD.parent_event_id
    AND NEW.payload_sha256 IS OLD.payload_sha256 AND NEW.prev_sha256 IS OLD.prev_sha256
    AND NEW.event_sha256 IS OLD.event_sha256 AND NEW.outcome IS OLD.outcome
    AND OLD.payload_json IS NOT NULL AND NEW.payload_json IS NULL
    AND OLD.payload_salt IS NOT NULL AND NEW.payload_salt IS NULL
    AND OLD.redacted_by_event_id IS NULL AND NEW.redacted_by_event_id IS NOT NULL
    AND NEW.redacted_by_event_id > OLD.id
    AND OLD.kind IN ('claim.imported','claim.created','claim.superseded',
                     'claim.reasserted','claim.disputed','claim.retracted',
                     'proposal.not_stored','proposal.confirmed')
    AND EXISTS (SELECT 1 FROM memory_spine_events AS t
                WHERE t.id = NEW.redacted_by_event_id AND t.kind = 'claim.tombstoned'
                  AND t.scope = OLD.scope
                  AND json_extract(t.payload_json,'$.claim_key')
                      = json_extract(OLD.payload_json,'$.claim_key'))
)
BEGIN SELECT RAISE(ABORT, 'memory spine events accept only one tombstone redaction'); END;
CREATE TRIGGER memory_claims_require_spine_event
BEFORE INSERT ON memory_claims
WHEN NEW.spine_event_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM memory_spine_events AS e
    WHERE e.id = NEW.spine_event_id
      AND e.kind IN ('claim.imported','claim.created')
      AND e.subject_kind = 'claim' AND e.subject_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'memory claim rows require a spine event'); END;
CREATE TRIGGER memories_require_spine_event
BEFORE INSERT ON memories
WHEN NEW.spine_event_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM memory_spine_events AS e
    WHERE e.id = NEW.spine_event_id
      AND ((e.kind IN ('memory.imported','memory.created','lesson.created')
            AND e.subject_kind = 'memory' AND e.subject_id = NEW.id)
           OR (NEW.kind = 'claim'
               AND e.kind IN ('claim.imported','claim.created')
               AND e.subject_kind = 'claim')))
BEGIN SELECT RAISE(ABORT, 'memory rows require a spine event'); END;
CREATE INDEX idx_memory_calibration_ledger_family
       ON memory_calibration_ledger(family, epoch);
CREATE UNIQUE INDEX idx_ladder_promotions_one_staged
       ON ladder_promotions(project_id, skill_name) WHERE stage='staged';
CREATE UNIQUE INDEX idx_ladder_promotions_one_live
       ON ladder_promotions(project_id, skill_name)
       WHERE stage IN ('approved','unapproved_legacy');
CREATE INDEX idx_ladder_promotions_scope
       ON ladder_promotions(project_id, family, stage, id);
CREATE TRIGGER ladder_promotions_require_spine_event
BEFORE INSERT ON ladder_promotions
WHEN NEW.spine_event_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM memory_spine_events AS e
    WHERE e.id = NEW.spine_event_id
      AND e.kind IN ('ladder.candidate','ladder.grandfathered')
      AND e.subject_kind = 'ladder' AND e.subject_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'ladder promotions require a spine event'); END;
CREATE TRIGGER memory_calibration_ledger_require_spine_event
BEFORE INSERT ON memory_calibration_ledger
WHEN NEW.spine_event_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM memory_spine_events AS e
    WHERE e.id = NEW.spine_event_id
      AND e.kind = 'ladder.calibration_sealed'
      AND e.subject_kind = 'calibration' AND e.subject_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'calibration ledger rows require a spine event'); END;
CREATE TRIGGER memory_calibration_ledger_append_only
BEFORE UPDATE ON memory_calibration_ledger
BEGIN SELECT RAISE(ABORT, 'the calibration ledger is append-only'); END;
CREATE TRIGGER memory_calibration_ledger_no_delete
BEFORE DELETE ON memory_calibration_ledger
BEGIN SELECT RAISE(ABORT, 'the calibration ledger is append-only'); END;
COMMIT;
-- ==== PASS 2: virtual tables, their rebuild, and their triggers ====
CREATE VIRTUAL TABLE memory_fts USING fts5(
                   content, source, content='memories', content_rowid='id',
                   tokenize='unicode61 remove_diacritics 2'
               );
INSERT INTO memory_fts(memory_fts) VALUES('rebuild');
CREATE VIRTUAL TABLE message_fts USING fts5(
                   content, content='messages', content_rowid='id',
                   tokenize='unicode61 remove_diacritics 2'
               );
INSERT INTO message_fts(message_fts) VALUES('rebuild');
CREATE TRIGGER memories_fts_delete AFTER DELETE ON memories BEGIN
                   INSERT INTO memory_fts(memory_fts, rowid, content, source)
                   VALUES ('delete', old.id, old.content, old.source);
               END;
CREATE TRIGGER memories_fts_insert AFTER INSERT ON memories BEGIN
                   INSERT INTO memory_fts(rowid, content, source)
                   VALUES (new.id, new.content, new.source);
               END;
CREATE TRIGGER memories_fts_update AFTER UPDATE ON memories BEGIN
                   INSERT INTO memory_fts(memory_fts, rowid, content, source)
                   VALUES ('delete', old.id, old.content, old.source);
                   INSERT INTO memory_fts(rowid, content, source)
                   VALUES (new.id, new.content, new.source);
               END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
                   INSERT INTO message_fts(message_fts, rowid, content)
                   VALUES ('delete', old.id, old.content);
               END;
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                   INSERT INTO message_fts(rowid, content) VALUES (new.id, new.content);
               END;
CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
                   INSERT INTO message_fts(message_fts, rowid, content)
                   VALUES ('delete', old.id, old.content);
                   INSERT INTO message_fts(rowid, content) VALUES (new.id, new.content);
               END;
