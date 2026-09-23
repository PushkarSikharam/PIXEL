"""Milestone 3: Pixel core must not contain product-specific business logic.

Files that still do are listed in KNOWN_PRODUCT_COUPLING with the step expected to clean them.
The list may only shrink, and it must be empty when Milestone 3 exits.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_purity import REPO_ROOT, core_files, product_ids, product_term_hits, relative

KNOWN_PRODUCT_COUPLING = {
    # Backend conversation engine: generalized in 3.2.
    "apps/api/app/product_config.py": "3.2",
    "apps/api/app/services/action_planner.py": "3.2",
    "apps/api/app/services/action_validator.py": "3.2",
    "apps/api/app/services/agent.py": "3.2",
    "apps/api/app/services/agent_reasoner.py": "3.2",
    "apps/api/app/services/conversation_manager.py": "3.2",
    "apps/api/app/services/intent_extractor.py": "3.2",
    "apps/api/app/services/language_normalizer.py": "3.2",
    # Legacy authority adapter (5c plan, section 9): converts the old engine's product-specific
    # decision into a keyed change set. Deleted with the old engine in 5d.
    "apps/api/app/services/legacy_adapter.py": "5d",
    # Knowledge retrieval: 3.4.
    "apps/api/app/services/retriever.py": "3.4",
    # Records, scopes and the demo-data API: 3.5.
    "apps/api/app/db.py": "3.5",
    "apps/api/app/main.py": "3.5",
    "apps/api/app/record_schemas.py": "3.5",
    "apps/api/app/services/demo_data.py": "3.5",
    "apps/api/app/services/product_data_store.py": "3.5",
    "apps/api/app/workspace_config.py": "3.5",
    # Web app: browser decisions leave in 3.3, rendering moves to adapters in 3.6.
    "apps/web/app/(demo)/architecture/page.tsx": "3.6",
    "apps/web/app/(demo)/demo/page.tsx": "3.3 and 3.6",
    "apps/web/lib/action-executor.ts": "3.6",
    "apps/web/lib/agent-api.ts": "3.6",
    "apps/web/lib/demo-data.ts": "3.6",
    "apps/web/lib/hybrid-voice-engine.ts": "3.6",
    "apps/web/lib/product-config.ts": "3.6",
    "apps/web/lib/product-data-api.ts": "3.6",
    "apps/web/types/demo.ts": "3.6",
}


# The single registry entry point that names installed product packages (3.2 plan, section 9).
PRODUCT_REGISTRY = "apps/api/app/installed_products.py"


class CorePurityTest(unittest.TestCase):
    def test_core_files_are_scanned(self):
        scanned = {relative(path) for path in core_files()}
        self.assertIn("apps/api/app/definitions/contract.py", scanned)
        self.assertIn("apps/web/app/(demo)/demo/page.tsx", scanned)

    def test_new_core_code_is_product_neutral(self):
        coupled = {
            relative(path): product_term_hits(path)
            for path in core_files()
            if relative(path) not in KNOWN_PRODUCT_COUPLING
            and relative(path) != PRODUCT_REGISTRY
            and product_term_hits(path)
        }
        self.assertEqual(coupled, {}, "Core files must not name product concepts; move them to a product package")

    def test_the_product_registry_only_names_packages(self):
        # Product names may appear there only as package imports and definition IDs.
        lines = [
            line.strip() for line in (REPO_ROOT / PRODUCT_REGISTRY).read_text(encoding="utf-8").splitlines()
            if any(product_id in line for product_id in product_ids())
        ]
        self.assertTrue(lines)
        for line in lines:
            with self.subTest(line=line):
                self.assertTrue(
                    line.startswith("from products.") or line.startswith("return index_packages("),
                    "the registry may only import and list product packages",
                )

    def test_allowlist_only_shrinks(self):
        existing = {relative(path) for path in core_files()}
        stale = sorted(
            path for path in KNOWN_PRODUCT_COUPLING
            if path not in existing or not product_term_hits(REPO_ROOT / path)
        )
        self.assertEqual(stale, [], "These files are now product-neutral; remove them from the allowlist")


if __name__ == "__main__":
    unittest.main()
