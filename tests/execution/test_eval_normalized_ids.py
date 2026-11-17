# Copyright (c) 2025-2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.

"""Duplicate IDs must be checked after model validation normalizes them."""

import json

import pytest

from sift.errors import EvalValidationError
from sift.evals.loader import EvalLoader


@pytest.mark.parametrize("first,second", [(1, "1"), ("01", 1)])
def test_duplicate_normalized_eval_ids(tmp_path, first, second):
    p = tmp_path / "evals.json"
    p.write_text(
        json.dumps(
            {
                "skill_name": "test",
                "evals": [{"id": first, "prompt": "a"}, {"id": second, "prompt": "b"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvalValidationError, match="Duplicate eval id: 1"):
        EvalLoader().load(p)


def test_duplicate_normalized_trigger_probe_ids(tmp_path):
    p = tmp_path / "evals.json"
    p.write_text(
        json.dumps(
            {
                "skill_name": "test",
                "evals": [{"id": 1, "prompt": "a"}],
                "trigger_probes": [
                    {"id": 1, "prompt": "q1", "expect_trigger": True},
                    {"id": "1", "prompt": "q2", "expect_trigger": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvalValidationError, match="Duplicate trigger probe id: 1"):
        EvalLoader().load(p)


def test_distinct_normalized_ids_remain_valid(tmp_path):
    p = tmp_path / "evals.json"
    p.write_text(
        json.dumps(
            {
                "skill_name": "test",
                "evals": [{"id": 1, "prompt": "a"}, {"id": "2", "prompt": "b"}],
                "trigger_probes": [
                    {"id": "1", "prompt": "q1", "expect_trigger": True},
                    {"id": 2, "prompt": "q2", "expect_trigger": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = EvalLoader().load(p)
    assert [case.id for case in loaded.evals] == [1, 2]
    assert [probe.id for probe in loaded.trigger_probes] == [1, 2]
