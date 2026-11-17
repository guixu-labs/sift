"""Eval loader — loads and validates evals.json."""

from __future__ import annotations

import json
from pathlib import Path

from sift.errors import EvalValidationError
from sift.models import EvalCase, EvalSet, TriggerProbe


class EvalLoader:
    @staticmethod
    def _validate(data: dict) -> EvalSet:
        if "skill_name" not in data:
            raise EvalValidationError("Missing 'skill_name' in evals.json")
        if not data["skill_name"]:
            raise EvalValidationError("'skill_name' must be non-empty")

        raw_evals = data.get("evals", [])
        if not raw_evals:
            raise EvalValidationError("'evals' must be non-empty")

        seen_ids: set[int] = set()
        cases: list[EvalCase] = []
        for raw in raw_evals:
            if "id" not in raw:
                raise EvalValidationError("Each eval must have an 'id'")
            if "prompt" not in raw or not raw["prompt"]:
                raise EvalValidationError(
                    f"Eval {raw.get('id', '?')}: 'prompt' must be non-empty"
                )
            case = EvalCase(**raw)
            eid = case.id
            if eid in seen_ids:
                raise EvalValidationError(f"Duplicate eval id: {eid}")
            seen_ids.add(eid)
            cases.append(case)

        # trigger_probes 段(可选,纯 scoring 卷无此段 → []):逐条校验后透传,
        # 否则 loader 重建 EvalSet 时会丢掉它(inspect/freeze 等走 loader 的路径就看不到)。
        trigger_probes: list[TriggerProbe] = []
        seen_tp_ids: set[int] = set()
        for raw_tp in data.get("trigger_probes", []):
            for req in ("id", "prompt", "expect_trigger"):
                if not (req not in raw_tp):
                    continue
                raise EvalValidationError(
                    f"Trigger probe {raw_tp.get('id', '?')}: missing '{req}'"
                )
            probe = TriggerProbe(**raw_tp)
            tpid = probe.id
            if tpid in seen_tp_ids:
                raise EvalValidationError(f"Duplicate trigger probe id: {tpid}")
            seen_tp_ids.add(tpid)
            trigger_probes.append(probe)

        return EvalSet(
            skill_name=data["skill_name"], evals=cases, trigger_probes=trigger_probes
        )

    def load(self, path: Path) -> EvalSet:
        if not path.exists():
            raise EvalValidationError(f"evals.json not found: {path}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EvalValidationError(f"Invalid JSON in {path}: {exc}") from exc

        return self._validate(data)
