from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from skills.audit.engine import c_check
from tests.test_c_check import ctx


class LinkTargetTests(unittest.TestCase):
    # CommonMark 0.31.2 sections 6.3 and 4.7 define destinations and titles.
    def test_r2_five_forms(self):
        cases = (
            ('[a](b.md "see) here")', "b.md"),
            ("[a](b.md (title))", "b.md"),
            ("[a](b(1).md)", "b(1).md"),
            ("[a](<b(1).md>)", "b(1).md"),
            ("[x]: <b (1).md>", "b (1).md"),
        )
        for markdown, destination in cases:
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [destination])

    def test_reported_forms_and_control(self):
        cases = (
            ("[a](b%20(1).md)", "b%20(1).md"),
            ('[a](x.md "t (x)")', "x.md"),
            ("[a](x.md (t))", "x.md"),
            ("[a](<b (1).md>)", "b (1).md"),
            ('[a](x.md "t")', "x.md"),
        )
        for markdown, destination in cases:
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [destination])

    def test_destination_and_title_rules_for_inline_images_and_references(self):
        destinations = (
            ("b(1).md", "b(1).md"),
            ("a(b(c(d))).md", "a(b(c(d))).md"),
            (r"b\(1\).md", "b(1).md"),
            (r"b\).md", "b).md"),
            (r"b\(.md", "b(.md"),
            (r"b\\(1).md", "b\\(1).md"),
            (r"b\q.md", r"b\q.md"),
            ("<b (1).md>", "b (1).md"),
            ("<b).md>", "b).md"),
            (r"<b\<1\>.md>", "b<1>.md"),
            ("<b\t(1).md>", "b\t(1).md"),
            ("b\u00a0(1).md", "b\u00a0(1).md"),
            ("b%20(1).md", "b%20(1).md"),
        )
        titles = ("", ' "t (x)"', " 't) x'", " (title)",
                  r' "t \"quoted\""', r" (t \(x\))")
        for destination, target in destinations:
            for title in titles:
                for markdown in (f"[a]({destination}{title})",
                                 f"![a]({destination}{title})",
                                 f"[a]: {destination}{title}"):
                    with self.subTest(markdown=markdown):
                        self.assertEqual(list(c_check._targets(markdown)), [target])

    def test_invalid_destinations_and_titles_are_not_extracted(self):
        invalid = (
            "b(1.md", "b(1).md(", "<b(1).md", "<b<1>.md>",
            r"<b\>", "<b>tail", "b (1).md", "b\t(1).md",
            "b\x00.md", "b\x1f.md", "b\x7f.md",
            'b.md "unterminated', "b.md 'unterminated", "b.md (unterminated",
            'b.md "title" tail', 'b.md "a "b" c"', "b.md (nested (title))",
            'b.md"title with spaces"', '<b.md>"title"',
        )
        for content in invalid:
            for markdown in (f"[a]({content})", f"[a]: {content}"):
                with self.subTest(markdown=markdown):
                    self.assertEqual(list(c_check._targets(markdown)), [])
        for markdown in ("[a](b.md (x)", "[a](b.md", "[a]: b).md",
                         "[a](b\n(1).md)", "[a](<b\n(1).md>)",
                         "[a](b.md\n\"title\")", "[a]:\nb.md"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [])

    def test_spacing_empty_destinations_and_multiple_links(self):
        self.assertEqual(list(c_check._targets(
            '[a]( \tb(1).md\t"t (x)" \t) [b](c(2).md)\n'
            '   [c]: <d (3).md>\t(title)\t\n')), ["b(1).md", "c(2).md", "d (3).md"])
        self.assertEqual(list(c_check._targets("[a]() [b](<>) [c]: <>\n")), [])
        self.assertEqual(list(c_check._targets('`[a](b(1).md)`\n```md\n[a](b(1).md)\n```')), [])
        self.assertEqual(list(c_check._targets('[a](b.md "see [b](c.md)")')), ["b.md"])


class LinkCheckTests(unittest.TestCase):
    def test_existing_and_missing_parenthesized_targets_in_links_and_orphan(self):
        destinations = (("b(1).md", "b(1).md"),
                        (r"b\(1\).md", "b(1).md"),
                        ("<b (1).md>", "b (1).md"),
                        ("b%20(1).md", "b%20(1).md"))
        for destination, filename in destinations:
            for form in ("[a]({dest})", '[a]({dest} "t (x)")',
                         "[a]({dest} (title))", '[a]: {dest} "t (x)"'):
                with self.subTest(destination=destination, form=form), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    context = ctx(root, corpus=("docs/source.md", f"docs/{filename}"))
                    missing = destination.replace("b", "missing", 1)
                    (root / "docs/source.md").write_text(
                        form.format(dest=destination) + "\n" + form.format(dest=missing) + "\n",
                        encoding="utf-8")
                    value = c_check.adapter(context)
                    links = [row for row in value.findings if row["id"].startswith("links:")]
                    self.assertEqual(len(links), 1)
                    self.assertEqual((links[0]["severity"], links[0]["blocking"], links[0]["summary"]),
                                     ("FAIL", True, "broken relative link: " + filename.replace("b", "missing", 1)))
                    orphans = {row["path"] for row in value.findings if row["id"].startswith("orphan:")}
                    self.assertEqual(orphans, {"docs/source.md"})

    def test_index_reference_with_spaces_parentheses_and_title_prevents_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ctx(root, corpus=("docs/b (1).md", "docs/unlinked.md"),
                          document_checks={"indexFiles": ["index.md"]})
            (root / "index.md").write_text('[b]: <docs/b (1).md> "t (x)"\n', encoding="utf-8")
            value = c_check.adapter(context)
            orphans = {row["path"] for row in value.findings if row["id"].startswith("orphan:")}
            self.assertEqual(orphans, {"docs/unlinked.md"})

    def test_invalid_link_does_not_cause_broken_link_or_prevent_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ctx(root, corpus=("docs/source.md", "docs/b(1).md"))
            (root / "docs/source.md").write_text(
                "[bad](b(1).md (unclosed)\n[bad]: <missing (1).md\n", encoding="utf-8")
            value = c_check.adapter(context)
            self.assertFalse(any(row["id"].startswith("links:") for row in value.findings))
            orphans = {row["path"] for row in value.findings if row["id"].startswith("orphan:")}
            self.assertEqual(orphans, {"docs/source.md", "docs/b(1).md"})


if __name__ == "__main__": unittest.main()
