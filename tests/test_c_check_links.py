from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from skills.audit.engine import c_check
from tests.test_c_check import ctx


class LinkTargetTests(unittest.TestCase):
    # Destination/title extraction subset of CommonMark 0.31.2 sections 6.3
    # and 4.7; this does not test a complete CommonMark block/inline parser.
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
                         "[a](b\n(1).md)", "[a](<b\n(1).md>)"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [])

    def test_multiline_links_and_reference_definitions(self):
        # Section 6.3 permits one line ending in separating whitespace
        # (example 510) and soft breaks in link text (section 6.8, example 648).
        # Examples 193, 195, 198 put reference destinations/titles on new lines;
        # example 196 permits multiple nonblank lines inside a title. Examples
        # 208/210 retain destination-only definitions before ordinary/invalid
        # title lines; a bare destination ending at a newline is not multiline.
        cases = (
            ("See the [configuration\nguide](docs/guide.md) for details.", "docs/guide.md"),
            ("[multi\nline text](x.md)", "x.md"),
            ("[a](\nb.md)", "b.md"),
            ('[a](b.md\n"title")', "b.md"),
            ('[a](b.md "multi\nline title")', "b.md"),
            ("[a]:\nb.md", "b.md"),
            ('[a]:\n  b.md\n  "title"', "b.md"),
            ("[a]: b.md\n  'title'", "b.md"),
            ("[a]: b.md\nnext paragraph line", "b.md"),
            ("[a]: b\n.md", "b"),
            ('[a]: b.md\n"title" trailing', "b.md"),
            ('[a]: b.md\n"unterminated', "b.md"),
            ('[a]: b.md\n"multi\n\nline title"', "b.md"),
            ("[a]: b.md\n[b]: c.md", "b.md"),
        )
        for markdown, target in cases:
            for newline in ("\n", "\r\n", "\r"):
                with self.subTest(markdown=markdown, newline=newline):
                    expected = [target, "c.md"] if "[b]:" in markdown else [target]
                    self.assertEqual(list(c_check._targets(markdown.replace("\n", newline))), expected)
        for opening, closing in (("\"", "\""), ("'", "'"), ("(", ")")):
            title = opening + "\ntitle\nline1\nline2\n" + closing
            for markdown in (f"[a](\n <b (1).md>\n {title}\n )",
                             f"![a\nlabel](b(1).md\n{title}\n)",
                             f"   [a]:\n  <b (1).md>\n  {title}\n"):
                with self.subTest(markdown=markdown):
                    target = "b(1).md" if markdown.startswith("!") else "b (1).md"
                    self.assertEqual(list(c_check._targets(markdown)), [target])

    def test_multiline_nonlinks_and_indented_definitions(self):
        # Sections 4.8/6.3: blank lines end paragraphs; example 197 rejects
        # blank lines in titles. Examples 490-491 forbid destination newlines.
        # Example 211 makes definitions indented by four spaces into code.
        invalid = (
            "[multi\n\nline text](b.md)", "[multi\n \t\nline text](b.md)",
            "[a](\n\nb.md)", "[a](b.md\n\n\"title\")", "[a](b.md\n\n)",
            '[a](b.md "title"\n \t\n)',
            '[a](b.md "multi\n\nline title")', '[a]: b.md "multi\n\nline title"',
            "[a](b\n.md)", "[a](<b\n.md>)", "[a]: <b\n.md>",
            "[a]:\n\nb.md", "[a]:\n \t\nb.md",
            "    [a]: b.md", "\t[a]: b.md",
        )
        for markdown in invalid:
            for newline in ("\n", "\r\n", "\r"):
                with self.subTest(markdown=markdown, newline=newline):
                    self.assertEqual(list(c_check._targets(markdown.replace("\n", newline))), [])

    def test_known_unextracted_container_reference_definitions(self):
        # Known subset limitation: these ARE CommonMark definitions in list
        # items/block quotes (sections 5.1/5.2), not invalid definitions or
        # the top-level indented code in example 211. Keep their current [].
        for markdown in ("- item\n\n    [a]: b.md", "- [r]: d.md",
                         "1. [r]: d.md", "> [r]: d.md"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [])

    def test_code_spans_with_equal_backtick_runs(self):
        # Section 6.1, examples 328-330/339-340: delimiters are maximal runs
        # of equal length; shorter/longer internal runs remain code content.
        for markdown in ("`[a](b.md)`", "``[a](b.md)``", "x ```[a](b.md)```",
                         "``x `[a](b.md)` x``", "`x ``[a](b.md)`` x`",
                         "``[r]: b.md``"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [])

    def test_multiline_code_spans_preserve_newlines(self):
        # Section 6.1, examples 335-337 permit line endings; section 2.1
        # defines LF, CRLF and CR. Masking retains each original line ending.
        for newline in ("\n", "\r\n", "\r"):
            for ticks in ("`", "``", "```"):
                markdown = f"x {ticks}code{newline}[a](b.md){ticks}"
                with self.subTest(newline=newline, ticks=ticks):
                    self.assertEqual(list(c_check._targets(markdown)), [])
                    self.assertEqual(list(c_check._targets(markdown + newline + "[r]: c.md")), ["c.md"])

    def test_code_span_mask_preserves_line_ending_bytes(self):
        # Section 2.1 line endings must survive the masking operation itself.
        for newline in ("\n", "\r\n", "\r"):
            for ticks in ("`", "``", "```"):
                with self.subTest(newline=newline, ticks=ticks):
                    markdown = f"x {ticks}code{newline}[a](b.md){ticks}"
                    self.assertEqual(c_check._mask_code_spans(markdown),
                                     "x " + " " * (len(ticks) + 4) + newline
                                     + " " * (len("[a](b.md)") + len(ticks)))

    def test_unmatched_code_opener_keeps_later_pairs(self):
        # Section 6.1, example 349: the unmatched single run stays literal
        # while the following double runs enclose code.
        for markdown in ("`foo``[a](b.md)``", "x ```foo`[a](b.md)`",
                         "x ````foo```[a](b.md)```"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [])

    def test_mismatched_code_runs_do_not_hide_links(self):
        # Section 6.1, examples 347-348: unmatched runs are literal.
        for markdown in ("``[a](b.md)`", "x ```code`` [a](b.md)",
                         "`code`` [a](b.md)"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), ["b.md"])

    def test_blank_lines_end_code_span_search(self):
        # Sections 4.8/6.1, examples 219-220/335: inlines belong to one
        # paragraph, so even whitespace-only blank lines stop the search.
        for blank in ("", " ", "\t", " \t "):
            for newline in ("\n", "\r\n", "\r"):
                markdown = f"`code{newline}{blank}{newline}[a](b.md)`"
                with self.subTest(blank=blank, newline=newline):
                    self.assertEqual(list(c_check._targets(markdown)), ["b.md"])

    def test_new_blocks_end_code_span_search(self):
        # Paragraph interruptions: ATX heading (4.2, example 78), nonempty
        # bullet/1-start ordered item (5.2/5.3, example 305), new quote
        # (5.1, example 245), thematic break (4.1, example 58), and setext
        # underline (4.3, example 91 explicitly stops an open code span).
        for marker in ("# ", "   ######\t", "- ", "+ ", "* ", "1. ", "1) ", "> ", ">"):
            with self.subTest(marker=marker):
                self.assertEqual(list(c_check._targets("`code\n" + marker + "[a](b.md)`")), ["b.md"])
        for marker in ("***", " _ _ _ ", "---", "=", "  ===\t", "--"):
            with self.subTest(marker=marker):
                self.assertEqual(list(c_check._targets("`code\n" + marker + "\n[a](b.md)`")), ["b.md"])
        for markdown in ("# `code\n[a](b.md)`", "> # `code\n> [a](b.md)`",
                         "1. `code\n2. [a](b.md)`", "> `code\n>> [a](b.md)`",
                         "> # ``code\n[a](b.md)``"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), ["b.md"])

    def test_html_block_boundaries_keep_following_links(self):
        # Section 4.6 types 1-6 can interrupt a paragraph (examples 149-151
        # show type 6). Type 2 starts at '<!--', even when closed that line.
        # HTML contents are outside this inline subset: stop conservatively
        # until a blank line rather than suppressing an uncertain real link.
        for marker in ("<!-- -->", "<?pi?>", "<!DOCTYPE html>", "<![CDATA[x]]>",
                       "<script>", "<PRE>", "<style>", "<textarea>",
                       "<div>", "</div>", "  <table>"):
            with self.subTest(marker=marker):
                markdown = "``code\n" + marker + "\n[a](b.md)``"
                self.assertEqual(list(c_check._targets(markdown)), ["b.md"])
        self.assertEqual(list(c_check._targets("``code\n<custom>\n[a](b.md)``")), [])

    def test_paragraph_and_container_continuations_keep_code_spans(self):
        # Ordinary/indented continuation (4.8, examples 220/223), repeated
        # and lazy quote continuation (5.1, examples 232-233/243), list
        # continuation (5.2), non-1 ordered markers (5.3, example 304),
        # and empty markers (5.2, example 285) do not start a new paragraph.
        for markdown in ("`code\n    [a](b.md)`", "`code\n2. [a](b.md)`",
                         "`code\n*\n[a](b.md)`", "`code\n1.\n[a](b.md)`",
                         "`code\n####### [a](b.md)`", "`code\n#no [a](b.md)`",
                         "> `code\n> [a](b.md)`", "> > `code\n> > [a](b.md)`",
                         "> `code\n[a](b.md)`", "> `code\nlazy\n> [a](b.md)`",
                         "- `code\n  [a](b.md)`", "1. `code\n   [a](b.md)`"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [])

    def test_backslash_escaped_backticks_outside_and_inside_code(self):
        # Section 2.4, examples 14-15: escapes outside code, including slash
        # parity. Sections 2.4/6.1, examples 17/338: a backslash inside code
        # does not escape its closing delimiter.
        for markdown in (r"\`[a](b.md)`", r"\\\`[a](b.md)`"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), ["b.md"])
        for markdown in (r"\\`[a](b.md)`", r"`[a](b.md)\`", r"``[a](b.md)\``"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [])
        self.assertEqual(list(c_check._targets(r"`code\`[a](b.md)`")), ["b.md"])

    def test_code_in_link_text_precedes_link_brackets(self):
        # Section 6.1, example 342: code consumes the prospective closing
        # link bracket. Code wholly inside the label still permits a link.
        self.assertEqual(list(c_check._targets("[not a `link](/foo`)")), [])
        self.assertEqual(list(c_check._targets("[a `]` label](b.md)")), ["b.md"])

    def test_backticks_in_valid_destinations_and_titles_stay_literal(self):
        # Section 6.3 destinations/titles are strings, not inline content:
        # after accepting the link, do not pair their ticks with later ticks.
        cases = (
            ('[a](b.md "``") [c](d.md) ``', ["b.md", "d.md"]),
            ("[a](b`c.md) [d](e.md) `", ["b`c.md", "e.md"]),
            ("[a](<b`c.md>) [d](e.md) `", ["b`c.md", "e.md"]),
            ('[a `]` label](b.md "``") [c](d.md) ``', ["b.md", "d.md"]),
            ('[r]: b.md "``"\n[c](d.md) ``', ["b.md", "d.md"]),
        )
        for markdown, expected in cases:
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), expected)

    def test_indented_and_container_fenced_blocks_keep_following_links(self):
        # Sections 4.4/4.5: code blocks cannot lazily continue into an
        # unindented paragraph. Nested fences are conservatively kept at
        # 1.8.0's same-line masking rather than inferred as inline spans.
        for markdown in ("    ``code\n[a](b.md)``", "\t``code\n[a](b.md)``",
                         "> ```\n> code\n[a](b.md) ```"):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), ["b.md"])

    def test_html_and_autolinks_before_code_protect_their_backticks(self):
        # Section 6.1 examples 343-346: HTML/autolinks and code have equal
        # precedence; whichever starts first consumes the enclosed ticks.
        for literal in ('<a title="``">', '<https://example.com/``>',
                        '<a``@example.com>', '<!-- `` -->', '<? `` ?>',
                        '<!DECL ``>', '<![CDATA[``]]>'):
            with self.subTest(literal=literal):
                self.assertEqual(list(c_check._targets(literal + ' [a](b.md) ``')), ["b.md"])
        for markdown in ('``<a title="[a](b.md)``">', '``<https://example.com/[a](b.md)``>'):
            with self.subTest(markdown=markdown):
                self.assertEqual(list(c_check._targets(markdown)), [])

    def test_spacing_empty_destinations_and_multiple_links(self):
        self.assertEqual(list(c_check._targets(
            '[a]( \tb(1).md\t"t (x)" \t) [b](c(2).md)\n'
            '   [c]: <d (3).md>\t(title)\t\n')), ["b(1).md", "c(2).md", "d (3).md"])
        self.assertEqual(list(c_check._targets("[a]() [b](<>) [c]: <>\n")), [])
        self.assertEqual(list(c_check._targets('`[a](b(1).md)`\n```md\n[a](b(1).md)\n```')), [])
        self.assertEqual(list(c_check._targets('~~~md\n[multi\nline](b.md)\n~~~')), [])
        self.assertEqual(list(c_check._targets('[a](b.md "see [b](c.md)")')), ["b.md"])
        # Section 6.3 permits an implementation limit of at least three levels.
        for depth in (3, 32, 33):
            destination = "b" + "(" * depth + "1" + ")" * depth + ".md"
            for markdown in (f"[a]({destination}) [b](c.md)", f"[a]: {destination}\n[b]: c.md"):
                with self.subTest(depth=depth, markdown=markdown):
                    expected = [destination, "c.md"] if depth <= 32 else ["c.md"]
                    self.assertEqual(list(c_check._targets(markdown)), expected)


class LinkCheckTests(unittest.TestCase):
    def test_code_spans_do_not_fail_links_or_supply_incoming_links(self):
        # Examples 329/335: a code span supplies neither a missing-link FAIL
        # nor an incoming link for orphan detection; the real link still does.
        for code in ("``[a](missing.md) [b](code-only.md)``",
                     "x ```[a](missing.md) [b](code-only.md)```",
                     "`code\n[a](missing.md) [b](code-only.md)`",
                     "> `code\n> [a](missing.md) [b](code-only.md)`"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                context = ctx(root, corpus=("docs/source.md", "docs/linked.md", "docs/code-only.md"))
                (root / "docs/source.md").write_text(code + "\n[real](linked.md)\n", encoding="utf-8")
                value = c_check.adapter(context)
                self.assertFalse(any(row["id"].startswith("links:") for row in value.findings))
                orphans = {row["path"] for row in value.findings if row["id"].startswith("orphan:")}
                self.assertEqual(orphans, {"docs/source.md", "docs/code-only.md"})

    def test_wrapped_link_text_preserves_broken_links_and_incoming_links(self):
        # Section 6.3 link text includes soft line breaks (section 6.8,
        # example 648); only the absent destination should fail links.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = ctx(root, corpus=("docs/source.md", "docs/guide.md"))
            (root / "docs/source.md").write_text(
                "See the [configuration\nguide](guide.md) and the [missing\npage](missing.md).\n",
                encoding="utf-8")
            value = c_check.adapter(context)
            links = [row for row in value.findings if row["id"].startswith("links:")]
            self.assertEqual([(row["severity"], row["blocking"], row["summary"]) for row in links],
                             [("FAIL", True, "broken relative link: missing.md")])
            orphans = {row["path"] for row in value.findings if row["id"].startswith("orphan:")}
            self.assertEqual(orphans, {"docs/source.md"})

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
