# Platform Foundry workflow pack

This pack projects Octopus Platform Foundry’s structurally different evidence-to-promotion lifecycle into Loom. It is deliberately read-only at the adapter boundary: Loom may inspect and reason over Foundry state, while Octopus remains the sole authority for observations, validation, shadowing, promotion, rollback, and repair.
