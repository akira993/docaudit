import hashlib
import json
import unittest

from skills.audit.engine.c_profile import ProfileRejected, resolve, validate_table
from skills.audit.engine.layers import LAYER_REGISTRY
from skills.audit.engine.profiles import PROFILE_TABLE

from .acceptance import acceptance


def copied_table():
    return [dict(row, enabledLayers=tuple(row["enabledLayers"])) for row in PROFILE_TABLE]


def hash_plan_without_self(plan):
    payload = {
        "profileName": plan.profileName,
        "profileSelectionSource": plan.profileSelectionSource,
        "enabledLayers": list(plan.enabledLayers),
        "backendDirective": plan.backendDirective,
        "profileTableHash": plan.profileTableHash,
        "registryHash": plan.registryHash,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProfileAcceptanceTests(unittest.TestCase):
    @acceptance("T-PROFILE-1", targets=5)
    def test_fixed_and_new_rows_have_deterministic_plans(self):
        table = copied_table()
        table.extend(
            [
                {
                    "name": "novel",
                    "enabledLayers": ("L-SCOPE", "L-PROJECT"),
                    "backendDirective": "auto",
                    "default": False,
                    "targetDuration": None,
                    "maxModelCalls": None,
                },
                {
                    "name": "novel2",
                    "enabledLayers": ("L-SCOPE", "L-DOC", "L-ADVERSARIAL", "L-CLAIM"),
                    "backendDirective": "auto",
                    "default": False,
                    "targetDuration": None,
                    "maxModelCalls": None,
                },
            ]
        )
        expected = {
            "focused": ("L-SCOPE", "L-DOC"),
            "standard": ("L-SCOPE", "L-DOC", "L-PROJECT"),
            "extended": ("L-SCOPE", "L-DOC", "L-PROJECT", "L-ENRICH", "L-SECURITY", "L-ADVERSARIAL", "L-CLAIM"),
            "novel": ("L-SCOPE", "L-PROJECT"),
            "novel2": ("L-SCOPE", "L-DOC", "L-ADVERSARIAL", "L-CLAIM"),
        }
        capabilities = frozenset(layer["id"] for layer in LAYER_REGISTRY)
        for name, enabled in expected.items():
            plan = resolve(table, name, capabilities, LAYER_REGISTRY)
            self.assertEqual(plan.enabledLayers, enabled)
            self.assertEqual(plan.planHash, hash_plan_without_self(plan))

    @acceptance("T-PROFILE-2", targets=2)
    def test_unavailable_capability_and_open_dependency_are_rejected(self):
        table = copied_table()
        table.append(
            {
                "name": "security-only",
                "enabledLayers": ("L-SCOPE", "L-SECURITY"),
                "backendDirective": "auto",
                "default": False,
                "targetDuration": None,
                "maxModelCalls": None,
            }
        )
        without_security = frozenset(layer["id"] for layer in LAYER_REGISTRY if layer["id"] != "L-SECURITY")
        with self.assertRaisesRegex(ProfileRejected, "capability-missing:L-SECURITY"):
            resolve(table, "security-only", without_security, LAYER_REGISTRY)

        broken = copied_table()
        broken.append(
            {
                "name": "claim-alone",
                "enabledLayers": ("L-CLAIM",),
                "backendDirective": "auto",
                "default": False,
                "targetDuration": None,
                "maxModelCalls": None,
            }
        )
        with self.assertRaisesRegex(ProfileRejected, "dependency-not-closed:L-CLAIM->L-ADVERSARIAL"):
            resolve(broken, "claim-alone", frozenset(layer["id"] for layer in LAYER_REGISTRY), LAYER_REGISTRY)

    @acceptance("T-PROFILE-5", targets=4)
    def test_invalid_fixed_tables_are_distinguished(self):
        duplicate = copied_table()
        duplicate.append(dict(duplicate[0]))
        no_default = [dict(row, default=False) for row in copied_table()]
        two_default = copied_table()
        two_default[0]["default"] = True
        fixtures = (
            (duplicate, "profile-name-duplicate:"),
            (no_default, "profile-default-count:0"),
            (two_default, "profile-default-count:2"),
            ([], "profile-table-empty"),
        )
        for table, reason in fixtures:
            with self.subTest(reason=reason), self.assertRaisesRegex(ProfileRejected, reason):
                validate_table(table, LAYER_REGISTRY)


class ProfileUnitTests(unittest.TestCase):
    def test_default_row_is_resolved_as_explicit_selection_in_p1(self):
        plan = resolve(PROFILE_TABLE, None, frozenset(row["id"] for row in LAYER_REGISTRY))
        self.assertEqual(plan.profileSelectionSource, "explicit")

    def test_coselection_is_rejected(self):
        table = copied_table()
        table.append(
            {
                "name": "one-of-pair",
                "enabledLayers": ("L-SCOPE", "L-ADVERSARIAL"),
                "backendDirective": "auto",
                "default": False,
                "targetDuration": None,
                "maxModelCalls": None,
            }
        )
        with self.assertRaisesRegex(ProfileRejected, "coselection-required"):
            validate_table(table, LAYER_REGISTRY)
