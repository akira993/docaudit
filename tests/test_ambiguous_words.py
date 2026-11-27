import re
import unittest
from pathlib import Path


# Use stems for inflected words to catch polite forms as well as plain forms.
JAPANESE_WORDS = (
    "など", "目安", "ことがあ", "こともあ", "場合があ", "場合もあ", "基本的",
    "適宜", "なるべく", "できるだけ", "程度", "可能性があ", "可能性もあ",
    "し得", "あり得", "必要に応じて", "適切に",
)
TOU = re.compile(r"(?<![同平均対上下高初優劣何])等(?![一-龯]|し)")
ENGLISH_WORDS = (
    "may", "might", "usually", "typically", "generally", "sometimes", "basically",
    "roughly", "approximately", "possibly", "etc.", "and so on", "as needed",
    "as appropriate", "if necessary", "where possible", "as much as possible",
)
ENGLISH_PATTERNS = tuple(
    (word, re.compile(r"\b" + re.escape(word) + (r"\b" if word[-1].isalnum() else ""), re.I))
    for word in ENGLISH_WORDS
)

SKIP_DIRS_ANY = {".git", "__pycache__"}
SKIP_TOP = {"tasks", "AGENTS.md", "CLAUDE.md", "CONTEXT.md", "PHILOSOPHY.md", "mdrg.toml"}
KEEP_DOT_TOP = {".claude-plugin", ".gitignore"}
SKIP_NAMES_ANY = {".DS_Store"}
SKIP_PREFIX = ("docs/local/",)

FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
SPAN = re.compile(r"(`+)(.+?)\1")

# Each entry maps (path, word) to the reason for allowing that word.
EXCLUSIONS = {
    ("tests/test_ambiguous_words.py", word): "この試験自身の語の一覧"
    for word in (*JAPANESE_WORDS, "等", *ENGLISH_WORDS)
}


def skip_root_entry(name):
    return name in SKIP_TOP or (name.startswith(".") and name not in KEEP_DOT_TOP)


def repository_files(root):
    for directory, directories, files in root.walk():
        relative_directory = directory.relative_to(root)
        if directory == root:
            directories[:] = [name for name in directories if not skip_root_entry(name)]
            files = [name for name in files if not skip_root_entry(name)]
        directories[:] = [
            name for name in directories
            if name not in SKIP_DIRS_ANY
            and not ((relative_directory / name).as_posix() + "/").startswith(SKIP_PREFIX)
        ]
        for name in files:
            if name in SKIP_NAMES_ANY or name in SKIP_DIRS_ANY:
                continue
            path = directory / name
            if path.is_file():
                yield path.relative_to(root).as_posix()


def scan_text(text, is_markdown):
    fence = None
    for line_number, line in enumerate(text.split("\n"), 1):
        if is_markdown:
            marker = FENCE.match(line)
            if fence is None and marker:
                fence = marker.group(1)[0]
                continue
            if fence is not None:
                if marker and marker.group(1)[0] == fence:
                    fence = None
                continue
            line = SPAN.sub("", line)

        words = []
        for word in JAPANESE_WORDS:
            words.extend([word] * line.count(word))
        words.extend(["等"] * len(TOU.findall(line)))
        for word, pattern in ENGLISH_PATTERNS:
            words.extend([word] * len(pattern.findall(line)))
        for word in words:
            yield line_number, word, line.strip()[:120]


def scan_repository(root):
    scanned = set()
    skipped_binary = 0
    hits = []
    for relative_path in sorted(repository_files(root)):
        try:
            text = (root / relative_path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped_binary += 1
            continue
        scanned.add(relative_path)
        for line_number, word, line in scan_text(text, relative_path.endswith(".md")):
            if (relative_path, word) not in EXCLUSIONS:
                hits.append(f"{relative_path}:{line_number}: {word}: {line}")
    return scanned, skipped_binary, hits


class AmbiguousWordsTests(unittest.TestCase):
    def test_repository_has_no_ambiguous_words(self):
        root = Path(__file__).resolve().parents[1]
        scanned, skipped_binary, hits = scan_repository(root)
        summary = f"scanned={len(scanned)} skipped_binary={skipped_binary} hits={len(hits)}"
        self.assertGreater(len(scanned), 0, summary)
        missing = {"README.md", "skills/audit/engine/cli.py"} - scanned
        self.assertFalse(missing, f"{summary}\nRequired files not scanned: {', '.join(sorted(missing))}")
        if hits:
            self.fail("\n".join([summary, *hits]))

    def test_detects_a_word_in_prose(self):
        self.assertEqual(list(scan_text("It MAY happen.", True)), [(1, "may", "It MAY happen.")])

    def test_detects_polite_japanese_form(self):
        text = "変わることがあります"
        self.assertEqual(list(scan_text(text, True)), [(1, "ことがあ", text)])

    def test_ignores_code_spans_and_fences_in_markdown(self):
        text = "`may`\n```python\nmay\n```\n~~~\n適宜\n~~~"
        self.assertEqual(list(scan_text(text, True)), [])
        self.assertEqual(list(scan_text("`may`", False)), [(1, "may", "`may`")])

    def test_ignores_tou_with_excluded_neighbors(self):
        self.assertEqual(list(scan_text("同等、平均等、等級、等しい。", True)), [])
        self.assertEqual(list(scan_text("項目等。", True)), [(1, "等", "項目等。")])
