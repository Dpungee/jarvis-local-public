---
name: long-running-operations
description: Run durable background tasks with checkpoints, idempotency, leases, bounded retries, and recovery. Use for workers, monitors, curricula, and multi-session work.
---

# Long-running operations

## Workflow

1. Give the operation a stable task identity, bounded scope, owner, deadline, and completion condition.
2. Persist checkpoints only after verified progress; make every retry safe and idempotent.
3. Use leases and heartbeats so one live worker owns a task and stale work can be recovered.
4. Distinguish queued, running, awaiting approval, retryable, complete, failed, and cancelled states.
5. Do not burn retries while waiting for a human decision or an unavailable external dependency.
6. Resume from durable evidence after restart instead of relying on conversational memory.

## Failure handling

- Bound attempts, execution time, output, and daily autonomous work.
- Record the last verified checkpoint and a redacted failure class.
- Preserve partial artifacts safely and prevent duplicate external effects.
- Escalate repeated failures with evidence rather than looping indefinitely.

## Verification

- Test clean restart, crash recovery, duplicate workers, approval-before-park, denial, cancellation, and stale leases.
- A running heartbeat is not completion evidence.
