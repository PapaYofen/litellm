# ADR 002: Poll persisted Tripo tasks and settle through the spend delivery ledger

Status: Proposed

## Context

Tripo creation responses supply a task ID, while this integration reads final credits from a later task query. Clients can stop polling and proxy workers can restart. Charging from a client poll alone would leave some tasks unaccounted for

## Decision

The route validates the native response and registers a task or uploaded file before returning it. `TripoRepository` serializes the domain objects and composes the existing `ManagedObjectRepository` table access and `SpendLogsRepository` delivery writer. It does not inherit `PrismaTableRepository` or own database connections. Routes receive `TripoObjectStore` through a FastAPI dependency and the scheduler passes a repository explicitly to the polling job

Ownership combines the creating gateway key with a hash of the normalized upstream base URL and upstream credential. The task stores its creation-time USD-per-credit rate and accounting entity IDs. Changing the configured base URL or credential changes the polling scope, so outstanding tasks in the old scope need reconciliation

When the upstream key is configured at startup and the database scheduler is running, each worker schedules a poll every 30 seconds. A poll selects at most 20 tasks and claims each using its stored update timestamp. Claims can be retried after five minutes. This limits concurrent polling but does not guarantee exactly one provider poll

A success with valid credits creates a final spend row and delivery using the stable `<task storage ID>:cost` identifier. The existing ledger inserts both transactionally and ignores a repeated delivery ID. The repository then separately marks the task processed. A crash between those steps can repeat the poll and delivery attempt without adding another charge while the delivery record is retained. The existing accounting worker applies the delivery to spend counters asynchronously

Only `failed`, `cancelled`, `banned` and `expired` settle at zero cost. Success without credits and unrecognized statuses remain pending. Invalid usage, transport failures and provider business errors are retried. The job stops its batch on HTTP 429 or 503 and leaves its claims for lease expiry

## Alternatives Considered

Charging on creation cannot use the final task usage. Charging on client polling alone depends on clients continuing to poll

The existing batch poller handles batch identifiers and result files. The response poller retrieves background Responses API objects. This native task endpoint needs its own retrieval and ownership rules, but reuses their spend delivery mechanism instead of adding another counter writer

Keeping the domain operations in the shared table wrapper would mix Tripo-specific policies into a generic repository. The composed repository retains the existing table access owner

## Consequences

Persisted tasks survive worker replacement, and the delivery ledger provides charge deduplication. Polling is bounded per worker and per run, not globally

Task processing and counter application are separate stages. Budgets are checked before generation or processing requests, but in-flight costs are not reserved. Concurrent tasks can exceed a budget. Key and team fixed budget windows are read at settlement time, so a task crossing a reset is attributed to the window then configured

A process failure after upstream acceptance but before local registration can leave an orphan task. There is no automatic adoption or reconciliation. Credential rotation, base URL changes and removal of retained delivery records require operational care. Uploaded file records are tracked for ownership and are not polled or billed by this job
