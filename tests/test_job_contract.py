import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from image23mf.contracts import JobConfig, load_job_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_v0_fixture_migrates_and_round_trips_as_v1() -> None:
    payload = json.loads((FIXTURES / "job_config_v0.json").read_text())

    config = load_job_config(payload)
    restored = load_job_config(json.loads(config.canonical_json()))

    assert config.schema_version == 1
    assert config.printer.profile_catalog_id == "image23mf-bundled-printers"
    assert config.printer.profile_catalog_version == "2026.07.16"
    assert config.printer.nozzle_id == "nozzle-0.4-hardened-steel"
    assert len(config.palette.colors) == 4
    assert restored == config
    assert restored.fingerprint() == config.fingerprint()


def test_canonical_hash_ignores_json_key_order() -> None:
    payload = json.loads((FIXTURES / "job_config_v0.json").read_text())
    config = load_job_config(payload)
    reversed_payload = dict(reversed(list(config.model_dump(mode="json").items())))

    assert load_job_config(reversed_payload).fingerprint() == config.fingerprint()


def test_future_schema_is_rejected_before_validation() -> None:
    with pytest.raises(ValueError, match="unsupported job schema version 99"):
        load_job_config({"schema_version": 99})


def test_crop_outside_source_is_rejected() -> None:
    payload = json.loads((FIXTURES / "job_config_v0.json").read_text())
    config_payload = load_job_config(payload).model_dump(mode="json")
    config_payload["crop"] = {"mode": "cover", "x": 0.8, "y": 0, "width": 0.5, "height": 1}

    with pytest.raises(ValidationError, match="crop bounds"):
        JobConfig.model_validate(config_payload)


def test_job_contract_does_not_hard_code_supported_nozzle_diameters() -> None:
    payload = json.loads((FIXTURES / "job_config_v0.json").read_text())
    config_payload = load_job_config(payload).model_dump(mode="json")
    config_payload["printer"].update(
        {
            "nozzle_id": "future-nozzle-profile",
            "nozzle_mm": 0.25,
            "layer_height_mm": 0.12,
        }
    )

    config = JobConfig.model_validate(config_payload)

    assert config.printer.nozzle_mm == 0.25
    assert config.printer.nozzle_id == "future-nozzle-profile"


def test_older_v1_payload_derives_profile_id_from_its_nozzle_without_mutation() -> None:
    payload = load_job_config(json.loads((FIXTURES / "job_config_v0.json").read_text())).model_dump(
        mode="json"
    )
    payload["printer"]["nozzle_mm"] = 0.2
    payload["printer"].pop("nozzle_id")
    payload["printer"].pop("profile_catalog_id")
    payload["printer"].pop("profile_catalog_version")
    original = json.loads(json.dumps(payload))

    config = load_job_config(payload)

    assert config.printer.nozzle_id == "nozzle-0.2-hardened-steel"
    assert config.printer.nozzle_mm == 0.2
    assert payload == original


def test_cleanup_override_provenance_is_canonical_and_requires_values() -> None:
    payload = load_job_config(json.loads((FIXTURES / "job_config_v0.json").read_text())).model_dump(
        mode="json"
    )
    payload["cleanup"].update(
        {
            "printability_profile_id": "bambu-p2s-0.4-hardened-steel-pla-v1",
            "printability_profile_catalog_fingerprint": "a" * 64,
            "override_fields": ["minimum_line_width_mm", "min_island_mm2"],
        }
    )
    with pytest.raises(ValidationError, match="canonical"):
        JobConfig.model_validate(payload)

    payload["cleanup"]["override_fields"] = ["minimum_line_width_mm"]
    with pytest.raises(ValidationError, match="require numeric values"):
        JobConfig.model_validate(payload)

    payload["cleanup"]["minimum_line_width_mm"] = 0.52
    config = JobConfig.model_validate(payload)
    assert config.cleanup.override_fields[0].value == "minimum_line_width_mm"
