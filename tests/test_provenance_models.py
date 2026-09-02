# Copyright (c) 2025-2026 Huawei Technologies Co., Ltd.
"""Tests for the producer-facing sift-records v2 schema models."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sift.models import SiftRecordsV2

from test_structured_v2_provenance import _bundle


def test_sift_records_v2_validates_bundle_and_preserves_extensions():
    data = _bundle()
    data["producer_extra"] = {"source": "test"}
    data["captures"]["cap-b"]["capture_extra"] = "kept"

    bundle = SiftRecordsV2.model_validate(data)

    assert bundle.format == "sift-records"
    assert bundle.version == 2
    assert bundle.records[0].tool_calls[0].capture_id == "cap-b"
    assert bundle.captures["cap-b"].effects[0].path == "/repo/foo.txt"
    assert bundle.blobs
    assert bundle.model_extra["producer_extra"] == {"source": "test"}
    assert bundle.captures["cap-b"].model_extra["capture_extra"] == "kept"


def test_sift_records_v2_model_validate_json_is_strict():
    data = _bundle()
    data["version"] = "2"

    with pytest.raises(ValidationError):
        SiftRecordsV2.model_validate_json(json.dumps(data))


def test_sift_records_v2_requires_core_collections():
    with pytest.raises(ValidationError):
        SiftRecordsV2.model_validate({"format": "sift-records", "version": 2})


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("records", 0, "tool_calls"), "not-a-list"),
        (("records", 0, "tool_calls", 0, "input"), ["not", "a", "dict"]),
        (("captures", "cap-b", "effects", 0, "additions"), "one"),
        (("blobs", next(iter(_bundle()["blobs"])), "size"), "4"),
    ],
)
def test_sift_records_v2_rejects_known_field_type_errors(path, value):
    data = _bundle()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        SiftRecordsV2.model_validate(data)
