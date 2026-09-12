# Independent process ownership review

Reviewer: `english_transport_review`, a separate read-only context. Scope: source and permanent test inspection of the shared process transport, not native approvals or E1 closure. The reviewer did not execute tests or assess XML receipts.

Reviewed source SHA-256: `8bf945ef44c0a9503815f073b535ec2851b7d6cbc09822d47124bcbaeb730f82`. Reviewed ownership tests: `be90c937517990c1e177a77cadf60f315faf97e7a6af51efc720cd812a2d7077`. Reviewed duplex tests: `9e45532be19b559f5467f81ea83e92652099de8264e3e47be3ee992b80097e3a`.

The independent source assessment found both inherited findings repaired and no additional consequential source defect within its bounded scope. Group escalation no longer depends on leader liveness or retained pipes, and the outer ownership context cleans up on normal return, setup failure, observation failure, and joining failure. A sidecar failure stops work before draining and is not repeated in the final observation pass. The parent-death watchdog already addressed the group directly; its earlier lifetime was the defect, not its leader-versus-group targeting.

The permanent descendant fixtures establish readiness before the terminal condition and independently test for work after return. The sidecar fixtures close provider output before introducing a read or projection fault, then inspect provider and watchdog handles. The input-cap assertion observes the pipe-write boundary; the binary burst checks raw bytes, FIFO order, and the exact cumulative input cap.

## Proof limits

The reviewed writer fixture signaled before entering its real write, so its cancellation case demonstrated an attempted oversized write, not deterministic kernel backpressure. The successor fixture waits for the child to consume one byte and verifies that the parent write has not returned before cancellation; that refinement was made after the review and requires its own execution evidence. It does not claim syscall-level scheduling introspection.

The existing parent-death regression observes a live leader after provider readiness, not a dead leader with a surviving descendant. The descendant tests observe a finite post-return window; source establishes group signal delivery, not synchronous completion of every descendant's OS teardown. Caller-owned queue capacity, blocking callbacks, escaped process groups, native authorization, and provider acknowledgement remain outside this review.

This record is the parent's faithful synthesis of the separate review, not an independently generated test receipt or a whole-system closure decision. The associated findings remain non-closed pending their installed consumer and recovery obligations.
