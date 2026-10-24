# Provider readiness

## Decision

Service health reports whether Lattice can serve and recover work. It does not inspect optional provider executables. Provider capability checks run for the selected adapter at dispatch and cache their observations until that executable changes. Missing or stale historical receipts do not remove installed routes from the catalogue; current observed capability failures remain visible. Runtime identity validation verifies Lattice's own installed bytes and the shape of its recorded manifest, without requiring external CLIs to remain unchanged.

The owner explicitly requested correction after advisory, prior-art and Test Forge calibration: “bring things into pagani alignment.” The rejected behavior turned an unrelated Google executable change into global conversation unavailability and made ordinary updates depend on manual qualification bookkeeping. The choice is behavioral compatibility for normal use, with exact identities retained as evidence. Frozen experiments and provider-native recovery may still bind the versions they actually depend on; those requirements do not propagate into unrelated service availability.

## Preserved obligations

Installation verifies the Lattice application and its recorded manifest without requiring unchanged optional-provider binaries. Historical provider identities remain diagnostic evidence; the selected adapter's capability check owns execution admission.

Keep selected-provider capability enforcement, tool and permission boundaries, installed application integrity, immutable history, lost-acknowledgement recovery, duplicate-dispatch prevention and budget controls. A capability probe establishes its exercised CLI controls, not universal behavioral correctness. Provider-managed models can change independently of local executable identity. No historical qualification is silently renewed.

## Verification

Permanent checks cover healthy service state with unrelated provider drift, selectable updated routes without stale success claims, visible current capability failures, selected-adapter-only checks after updates, and preserved rejection of modified Lattice application bytes. The preexisting global drift rejection test is replaced because it enforced the rejected requirement. Targeted mutations must restore the former global refusal or bypass the current capability failure and be rejected at the relevant assertions. Installed observation remains distinct from source-test results.

## Prior art

[Google SRE](https://sre.google/sre-book/monitoring-distributed-systems/) separates user-visible symptoms from internal causes. [Consumer-driven contract testing](https://docs.pact.io/) protects interfaces actually consumed while permitting unrelated changes. [Terraform checksum verification](https://developer.hashicorp.com/terraform/language/files/dependency-lock) binds selected package identities; it does not establish compatibility of arbitrary updated software. These mechanisms inform the boundary; no new testing framework is required.
