from image23mf.derivation import DerivationIdentity, explain_staleness


def identity(**changes) -> DerivationIdentity:
    values = {
        "pipeline": "preview",
        "source_fingerprint": "source-a",
        "config_fingerprint": "config-a",
        "operations_fingerprint": "operations-a",
        "engine_version": "1.0.0",
        "adapter_versions": {"render": "1", "cleanup": "2"},
        "dependencies": {"profiles": "catalog-a"},
    }
    values.update(changes)
    return DerivationIdentity(**values)


def test_identity_is_canonical_and_every_derivation_dimension_invalidates() -> None:
    baseline = identity()
    reordered = identity(adapter_versions={"cleanup": "2", "render": "1"})
    assert baseline.key() == reordered.key()
    assert len(baseline.key()) == 64

    variants = (
        identity(source_fingerprint="source-b"),
        identity(config_fingerprint="config-b"),
        identity(operations_fingerprint="operations-b"),
        identity(engine_version="2.0.0"),
        identity(adapter_versions={"render": "2", "cleanup": "2"}),
        identity(dependencies={"profiles": "catalog-b"}),
        identity(policy_schema_version=2),
    )
    assert len({baseline.key(), *(item.key() for item in variants)}) == len(variants) + 1


def test_staleness_explains_upgrade_inputs_and_legacy_artifacts() -> None:
    recorded = identity().metadata()
    assert "engine was upgraded" in explain_staleness(recorded, identity(engine_version="2.0.0"))
    assert "adapter versions changed" in explain_staleness(
        recorded, identity(adapter_versions={"render": "2", "cleanup": "2"})
    )
    assert "source image changed" in explain_staleness(
        recorded, identity(source_fingerprint="source-b")
    )
    assert "configuration changed" in explain_staleness(
        recorded, identity(config_fingerprint="config-b")
    )
    assert "edit operations changed" in explain_staleness(
        recorded, identity(operations_fingerprint="operations-b")
    )
    assert "dependencies changed" in explain_staleness(
        recorded, identity(dependencies={"profiles": "catalog-b"})
    )
    assert "predates versioned derivation metadata" in explain_staleness({}, identity())
