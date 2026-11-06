"""Contract regression tests; no model calls or task-specific gold fixtures."""
from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[3]
ENGINE = ROOT / "runtime/metric_rubric_impl_v4_modular"
MODULE = ROOT / "tools/eval_bridge/profiles.py"
SPEC = importlib.util.spec_from_file_location("rubric_profiles_under_test", MODULE)
profiles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profiles)


class ProfileContracts(unittest.TestCase):
    def test_all_22_configured_templates_are_preserved(self):
        loaded = profiles.load_profiles(ENGINE)
        self.assertEqual(len(loaded), 22)
        self.assertEqual({p["id"] for p in loaded}, {p.stem for p in (ENGINE / "configs/rubrics").glob("*.yaml")})
        for profile in loaded:
            source = ENGINE / profile["source_path"]
            raw = source.read_bytes()
            original = yaml.safe_load(raw.decode("utf-8"))
            self.assertEqual(profile["how"], original)
            self.assertEqual(profile["what"], original["definition"])
            self.assertEqual(profile["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(profile["metric_id"], original["metric_id"])

    def test_p_only_and_j_only_methods_cannot_be_changed_by_profiles(self):
        loaded = {p["id"]: p for p in profiles.load_profiles(ENGINE)}
        for rid in ("AC-3", "BC-1"):
            self.assertEqual(loaded[rid]["how"]["allowed_methods"], ["P"])
        for rid in ("AC-1", "AC-2", "TU-1", "PE-1", "EG-1", "EG-3"):
            self.assertEqual(loaded[rid]["how"]["allowed_methods"], ["J"])

    def test_missing_answer_does_not_disable_process_evidence_contracts(self):
        loaded = {p["id"]: p for p in profiles.load_profiles(ENGINE)}
        for rid in ("TU-1", "TU-2", "TU-3", "PE-2", "PE-3", "EG-1", "EG-3", "ER-1", "ER-2", "ER-3", "EE-4", "HC-1", "HC-2", "HC-3", "HC-4", "BC-1"):
            self.assertNotIn(["final_answer"], loaded[rid]["required_evidence"])
        self.assertEqual(loaded["TU-1"]["required_evidence"][1], ["tool_contract"])

    def test_required_evidence_and_where_agree(self):
        for profile in profiles.load_profiles(ENGINE):
            self.assertTrue(set(profile["where"]) <= profiles.EVIDENCE_KINDS)
            for alternatives in profile["required_evidence"]:
                self.assertTrue(alternatives)
                self.assertTrue(set(alternatives) <= set(profile["where"]))
            self.assertIn("unverifiable", profile["applicability_policy"])
            self.assertIn("not_applicable", profile["applicability_policy"])

    def test_empty_library_fails_instead_of_silently_dropping_rubrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "coverage mismatch"):
                profiles.load_profiles(Path(tmp))

    def test_returned_profiles_do_not_mutate_future_loads(self):
        first = profiles.load_profiles(ENGINE)
        first[0]["where"].clear()
        first[0]["how"]["allowed_methods"].clear()
        first[0]["instantiation_coverage"]["include"].clear()
        second = profiles.load_profiles(ENGINE)
        self.assertTrue(second[0]["where"])
        self.assertTrue(second[0]["how"]["allowed_methods"])
        self.assertTrue(second[0]["instantiation_coverage"]["include"])

    def test_all_rubrics_have_distinct_nonempty_coverage_guides(self):
        loaded = profiles.load_profiles(ENGINE)
        self.assertEqual(len({p['instantiation_coverage']['unit'] for p in loaded}), 22)
        for profile in loaded:
            guide = profile['instantiation_coverage']
            self.assertEqual(set(guide), {'unit', 'include', 'bind', 'boundary'})
            self.assertTrue(all(isinstance(item, str) and item.strip() for item in guide['include']))
            self.assertTrue(guide['include'])
            for key in ('unit', 'bind', 'boundary'):
                self.assertIsInstance(guide[key], str)
                self.assertTrue(guide[key].strip())

    def test_missing_or_invalid_coverage_guides_fail_closed(self):
        with patch.dict(profiles._COVERAGE, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'coverage guides must cover exactly'):
                profiles.load_profiles(ENGINE)
        with patch.dict(profiles._COVERAGE['HC-1'], {'include': []}):
            with self.assertRaisesRegex(ValueError, 'Invalid instantiation coverage guide: HC-1'):
                profiles.load_profiles(ENGINE)

    def test_recovery_approval_and_budget_guides_preserve_object_granularity(self):
        guides = {p['id']: p['instantiation_coverage'] for p in profiles.load_profiles(ENGINE)}
        self.assertIn('每次实际恢复尝试', guides['ER-2']['unit'])
        self.assertIn('每个必要审批机会', guides['HC-1']['unit'])
        self.assertIn('到下一决定的行为区间', guides['HC-4']['unit'])
        self.assertIn('统计范围和计量窗口', guides['BC-1']['unit'])
        self.assertIn('P-only', guides['AC-3']['boundary'])
        self.assertIn('P-only', guides['BC-1']['boundary'])
        self.assertIn('当前P处理器检查全部请求', guides['HC-2']['boundary'])


if __name__ == "__main__":
    unittest.main()
