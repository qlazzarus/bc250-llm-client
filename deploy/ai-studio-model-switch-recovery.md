> 2026-11-10: Studio PR #1 merge was recorded previously; read-only inspection now confirms the operating source contains `/internal/model-switch/recovery` and protocol 3 checks. The patches below are retained handoff artifacts, not instructions to reapply them to the installed release. Runtime configuration and shared gate/lock/journal integration remain unverified.

# Studio model-switch recovery candidate

`ai-studio-model-switch-recovery.patch` applies to the Studio source in
`docs/handoff/imagegen-20261107/source.tar.gz`. It also applies to the
`docs/handoff/imagegen-closeout-20261107/source.tar.gz` Studio candidate, whose
three affected source files matched the operating files at the last read-only
check. `ai-studio-closeout-model-switch-recovery.patch` is generated against
that closeout source and applies to those operating source copies without fuzz;
verify the exact release again before deployment. It retains that candidate's
GPU admission and Studio recovery paths: both acquire the shared GPU lock and
check the root-controlled switch journal before proceeding. The authenticated
recovery endpoint does not acquire the lock: the switch worker already holds it.
It reads the Linux lock table without acquiring the lock, requiring a root-owned
exclusive flock on the securely opened lock inode; the worker independently
checks its own held lock and the shared file identity. Inaccessible or filtered
`/proc/locks` fails closed. It reports a digest for any valid journal, including
the empty state, and separately reports
whether Studio's own transition and recovery state are clear and the Studio
credential identifies the protocol 3 gate. The worker compares the digest with
its own validated journal snapshot and fails closed on any mismatch.

This is a local patch candidate, not an installed release. Before deployment,
apply and test it against the exact Studio release to be installed, validate the
protocol 3 gate and both credentials together, provision the shared lock and
journal with the required protected ownership and permissions in both service
namespaces, and verify service identity, GPU exclusion, gate holds and recovery
end to end on the target host. Coordinate deployment and rollback of Studio,
gate and worker; the protocol change is not compatible with the old gate.
No installation or server changes are performed by this patch.
