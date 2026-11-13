"""End-to-end convert runs against synthetic pen data (needs pycairo)."""
import json
import os
import shutil
import unittest

import tests
from tests import fixtures


@unittest.skipUnless(fixtures.has_cairo(), "pycairo not installed")
class ConvertPipelineTest(unittest.TestCase):
    def setUp(self):
        self.root = fixtures.fresh_root(tests.SANDBOX, self.id())
        fixtures.write_page(self.root, "2208.1.12.25",
                            fixtures.text_line(500, 600, 6))
        fixtures.write_page(self.root, "2208.1.12.26",
                            fixtures.text_line(500, 600, 3))
        fixtures.write_session(
            self.root, "PRS-1a",
            [fixtures.page_address(2208, 1, 12, 25, section=1)])

    def convert(self, *args):
        proc = fixtures.run_convert(self.root, *args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def index(self):
        with open(os.path.join(self.root, "output", "index.json")) as f:
            return json.load(f)

    def test_renders_pages_and_links_sessions(self):
        self.convert()
        index = self.index()
        self.assertEqual(sorted(p["id"] for p in index["pages"]),
                         ["2208.1.12.25", "2208.1.12.26"])
        for page in index["pages"]:
            for key in ("svg", "thumb"):
                self.assertTrue(os.path.exists(
                    os.path.join(self.root, "output", page[key])))
        [session] = index["sessions"]
        self.assertEqual(session["pages"], ["2208.1.12.25"])
        self.assertEqual(session["urls"], ["audio/PRS-1a.0.aac"])

    def test_second_run_skips_unchanged_sources(self):
        self.convert()
        proc = self.convert()
        self.assertIn("2 unchanged", proc.stdout)
        self.assertIn("(1 unchanged)", proc.stdout)

    def test_deleted_source_is_pruned_from_output(self):
        self.convert()
        shutil.rmtree(os.path.join(self.root, "data", "data", "2208.1.12.26"))
        self.convert()
        self.assertEqual([p["id"] for p in self.index()["pages"]],
                         ["2208.1.12.25"])
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "output", "pages", "2208.1.12.26")))

    def test_titles_flow_into_the_index(self):
        with open(os.path.join(self.root, "titles.json"), "w") as f:
            json.dump({"2208.1.12.25": "Groceries"}, f)
        self.convert()
        titles = {p["id"]: p.get("title") for p in self.index()["pages"]}
        self.assertEqual(titles["2208.1.12.25"], "Groceries")


if __name__ == "__main__":
    unittest.main()
