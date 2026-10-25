"""Publish the reviewed fe9e85d→4aea75f readiness repair without a fake migration.

This is deliberately restricted to one exact upgrade. Ordinary upgrades retain
the general installer's copied-store migration rehearsal. Here the store,
schema, provider definitions, dependencies and migration entry function are
unchanged; the independently verified rollback snapshot and normal publication
and automatic rollback machinery remain mandatory.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path

import install

OLD_RUNTIME = '0.3.1-fe9e85d9bfae-eaabac32c4b9'
NEW_RUNTIME = '0.3.1-4aea75faf143-ecdca2e239e5'
OLD_REVISION = 'fe9e85d9bfae574df7875d3eb96c24a77e4221e6'
NEW_REVISION = '4aea75faf1431edc7d7108ded7baa98a185a2148'
REVIEWED_CHANGES = {'cli.py', 'diagnostics.py', 'durability.py', 'run_journal.py', 'server.py', 'snapshot.py'}


def module_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*.py')}


def migration_entry(root: Path) -> str:
    tree = ast.parse((root / 'snapshot.py').read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'pre_migration_snapshot_if_needed')
    return ast.dump(function, include_attributes=False)


def verify_retained_runtime(target: Path, manifest: dict, revision: str) -> None:
    """Verify an older installed payload against its own retained source tree."""
    manifest = install._validate_runtime_manifest(manifest, target=target,
        runtime_id=target.name, lattice_revision=revision)
    if install.canonical_runtime_manifest(manifest, target / '.venv/bin/python') != (target / 'runtime.json').read_bytes():
        raise RuntimeError('retained runtime manifest is not exact canonical JSON')
    locations = install.probe_runtime(target)
    if locations != manifest['module_locations'] or any(not Path(p).resolve().is_relative_to(target.resolve()) for p in locations.values()):
        raise RuntimeError('retained runtime imports outside its immutable installation')
    if install.installed_tree_inventories(locations) != manifest['source_trees']:
        raise RuntimeError('retained runtime application bytes disagree with their attested tree')


def validate_equivalence(old: dict, new: dict, before: dict, after: dict,
                         old_entry: str, new_entry: str, *, saved_schema: int, expected_schema: int) -> list[str]:
    if old['runtime_id'] != OLD_RUNTIME or new['runtime_id'] != NEW_RUNTIME:
        raise RuntimeError('unchanged-migration proof applies only to the reviewed readiness upgrade')
    if old['packages'] != new['packages'] or old['python'] != new['python']:
        raise RuntimeError('runtime dependencies or Python changed; rehearse the migration')
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    if set(changed) != REVIEWED_CHANGES:
        raise RuntimeError('unreviewed module changes require migration rehearsal: ' + repr(changed))
    if old_entry != new_entry or saved_schema != expected_schema:
        raise RuntimeError('migration entry or schema changed; rehearse the migration')
    return changed


def prove(old_target: Path, target: Path, snapshot: Path) -> dict:
    # Verify actual installed payloads; a Git label alone cannot certify them.
    old_manifest = json.loads((old_target / 'runtime.json').read_text())
    new_manifest = json.loads((target / 'runtime.json').read_text())
    old_revision = old_manifest['generated_object']['sources'][0]['revision']
    if old_revision != OLD_REVISION:
        raise RuntimeError('old runtime source does not match the reviewed predecessor')
    verify_retained_runtime(old_target, old_manifest, old_revision)
    install.verify_runtime_target(target, runtime_id=NEW_RUNTIME, lattice_revision=NEW_REVISION)
    roots = [Path(install.probe_runtime(p)['lattice']).parent for p in (old_target, target)]
    install.import_source_modules()
    from lattice.store import SCHEMA_VERSION
    manifest = json.loads((snapshot / 'snapshot.json').read_text())
    changed = validate_equivalence(old_manifest, new_manifest,
                                   module_hashes(roots[0]), module_hashes(roots[1]),
                                   migration_entry(roots[0]), migration_entry(roots[1]),
                                   saved_schema=manifest['schema_version'], expected_schema=SCHEMA_VERSION)
    return {'contract_version': 'LatticeUnchangedMigration/1.0',
            'old_runtime': OLD_RUNTIME, 'new_runtime': NEW_RUNTIME, 'changed_modules': changed,
            'unchanged_store_sha256': install.exact_sha256(roots[1] / 'store.py'),
            'schema_version': SCHEMA_VERSION,
            'proof_limit': 'Exact reviewed transition only. Changed modules cover readiness, CLI diagnostics and backup durability; the equal-schema migration entry returns before invoking backup changes. No claim about future releases.',
            'recovery': 'Independent current rollback snapshot verification and canonical automatic rollback remain required.'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback-snapshot', type=Path, required=True)
    args = parser.parse_args()
    target = install.VERSIONS_ROOT / NEW_RUNTIME
    old_target = install.CURRENT_LINK.resolve(strict=True)
    if old_target.name != OLD_RUNTIME:
        raise RuntimeError('current runtime is not the reviewed predecessor; inspect installation state')
    proof = prove(old_target, target, args.rollback_snapshot)
    print('Verified unchanged migration implementation and equal schema for the exact readiness repair', flush=True)
    prior = install.capture_publication_state()
    quiescence_started = False
    try:
        with install.upgrade_quiescence():
            quiescence_started = True
            install.unload_job(install.SERVICE_LABEL)
            install.unload_job(install.BACKUP_LABEL)
        print('Independently verifying the current rollback snapshot', flush=True)
        snapshot = install.reuse_rollback_snapshot(args.rollback_snapshot)
        from lattice.canonical import canonical_bytes, generated_object
        proof['generated_object'] = generated_object(kind='lattice-unchanged-migration',
            schema_name='lattice-unchanged-migration', subject=f'lattice:upgrade:{NEW_RUNTIME}', role='snapshot',
            sources=[{'ref': str(path), 'revision': None, 'sha256': install.exact_sha256(path)}
                     for path in (old_target / 'runtime.json', target / 'runtime.json', snapshot / 'snapshot.json', Path(__file__))],
            producer_name='lattice-readiness-upgrade', producer_version='1.0.0')
        receipt = install.DEFAULT_DATA_ROOT / 'acceptance' / ('unchanged-migration-' + proof['generated_object']['generated_at'] + '-' + proof['generated_object']['artifact_id'].split(':')[-1] + '.json')
        install.write_atomic(receipt, canonical_bytes(proof), mode=0o600)
        print(f'Unchanged migration evidence: {receipt}', flush=True)
    except BaseException:
        if quiescence_started:
            install.restore_publication_state(prior)
        raise
    if install.CURRENT_LINK.resolve() != old_target:
        raise RuntimeError('current runtime changed during preparation; inspect competing publication')
    # These are the same live migration/startup and rollback functions used by
    # the general installer. The database is never restored on a successful run.
    install.publish_runtime(target, snapshot=snapshot, prior=prior)
    print(f'Installed Lattice runtime {target.name}', flush=True)


if __name__ == '__main__':
    main()
