import os
import tempfile
import time
import unittest
from pathlib import Path

from skills.audit.engine.c_io import IoRejected, RepoRoot, append_line, check_platform_support, clear_write_guard, ensure_dir_fd, iter_lines, open_lock_file, publish_exclusive, read_bytes, read_text, register_write_guard, stat_regular, unlink_owned_temporary, unlink_regular, write_atomic
from tests.acceptance import acceptance


class CioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.repo = RepoRoot(self.root)

    def tearDown(self):
        clear_write_guard()
        self.repo.close()
        self.temp.cleanup()

    @acceptance("T-SAFE-1", targets=12)
    def test_rejects_unsafe_relative_paths_for_reads_and_writes(self):
        cases = (
            ("../x", "parent-segment"),
            ("/abs/x", "absolute-path"),
            ("a/../../b", "parent-segment"),
            ("..", "parent-segment"),
            ("C:\\x", "absolute-path"),
            ("a\x00b", "nul-in-path"),
        )
        for path, reason in cases:
            for operation in (lambda: read_bytes(self.repo, path), lambda: write_atomic(self.repo, path, b"x")):
                with self.subTest(path=path, operation=operation):
                    with self.assertRaises(IoRejected) as raised:
                        operation()
                    self.assertEqual(raised.exception.reason, reason)

    @acceptance("T-SAFE-2", targets=6)
    def test_rejects_all_symbolic_links_for_reads_and_writes(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        external_file = outside / "external.txt"
        external_file.write_text("secret", encoding="utf-8")
        (self.root / "inside.txt").write_text("inside", encoding="utf-8")
        os.symlink(external_file, self.root / "external-file")
        os.symlink(outside, self.root / "external-dir")
        os.symlink(self.root / "inside.txt", self.root / "internal-file")
        for path in ("external-file", "external-dir/new.txt", "internal-file"):
            for operation in (lambda p=path: read_bytes(self.repo, p), lambda p=path: write_atomic(self.repo, p, b"changed")):
                with self.subTest(path=path, operation=operation):
                    with self.assertRaises(IoRejected) as raised:
                        operation()
                    self.assertEqual(raised.exception.reason, "symlink-component")
        self.assertEqual(external_file.read_text(encoding="utf-8"), "secret")
        self.assertFalse((outside / "new.txt").exists())
        self.assertEqual((self.root / "inside.txt").read_text(encoding="utf-8"), "inside")

    def test_regular_file_helpers_are_descriptor_relative(self):
        write_atomic(self.repo, "state/item.txt", "one")
        append_line(self.repo, "state/item.txt", b"\ntwo")
        self.assertEqual(read_text(self.repo, "state/item.txt"), "one\ntwo")
        self.assertEqual(stat_regular(self.repo, "state/item.txt").st_size, 7)
        state_fd = os.open(self.root / "state", os.O_RDONLY | os.O_DIRECTORY)
        try:
            write_atomic(state_fd, "by-fd.txt", b"fd")
            self.assertEqual(read_bytes(state_fd, "by-fd.txt"), b"fd")
        finally:
            os.close(state_fd)

    def test_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        started = time.monotonic()
        with self.assertRaises(IoRejected) as raised:
            read_bytes(self.repo, "pipe")
        self.assertEqual(raised.exception.reason, "not-regular")
        self.assertLess(time.monotonic() - started, 1)

    def test_append_to_fifo_is_rejected_without_blocking(self):
        os.mkfifo(self.root / "append-pipe")
        started = time.monotonic()
        with self.assertRaises(IoRejected) as raised:
            append_line(self.repo, "append-pipe", b"x")
        self.assertEqual(raised.exception.reason, "not-regular")
        self.assertLess(time.monotonic() - started, 1)

    def test_invalid_utf8_and_size_are_rejected(self):
        write_atomic(self.repo, "bad", b"\xff")
        with self.assertRaisesRegex(IoRejected, "not-utf8"):
            read_text(self.repo, "bad")
        write_atomic(self.repo, "large", b"012")
        with self.assertRaisesRegex(IoRejected, "too-large"):
            read_bytes(self.repo, "large", max_bytes=2)

    def test_platform_support_probe(self):
        check_platform_support()

    def test_missing_regular_path_is_not_a_safety_rejection(self):
        with self.assertRaises(FileNotFoundError):
            read_bytes(self.repo, "missing/file")

    def test_iter_lines_rejects_fifo_without_waiting(self):
        os.mkfifo(self.root / "history-pipe")
        started=time.monotonic()
        with self.assertRaisesRegex(IoRejected,"not-regular"):
            list(iter_lines(self.repo,"history-pipe",1024))
        self.assertLess(time.monotonic()-started,1)

    def test_final_8_unterminated_large_line(self):
        write_atomic(self.repo,"large-line",b"x"*(3*1024*1024))
        with self.assertRaisesRegex(IoRejected,"too-large"):
            list(iter_lines(self.repo,"large-line",1024*1024))

    def test_deterministic_temporary_is_not_removed_when_owned_by_another_writer(self):
        other = self.root / ".tmp-result.json"
        other.write_bytes(b"other")
        for operation in (
            lambda: write_atomic(self.repo, "result.json", b"ours"),
            lambda: publish_exclusive(self.repo, "result.json", b"ours"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(IoRejected, "tmp-conflict"):
                    operation()
                self.assertEqual(other.read_bytes(), b"other")
                self.assertFalse((self.root / "result.json").exists())

    def test_publication_callback_and_atomic_write_return_temporary_identity(self):
        observed = []
        published = publish_exclusive(self.repo, "published.md", b"body", observed.append)
        written = write_atomic(self.repo, "state.json", b"{}")
        self.assertEqual((published.st_dev, published.st_ino), (observed[0].st_dev, observed[0].st_ino))
        self.assertGreater(written.st_ino, 0)
        self.assertFalse((self.root / ".tmp-published.md").exists())
        self.assertFalse((self.root / ".tmp-state.json").exists())

    def test_write_guard_uses_exact_paths_records_mutations_and_freezes(self):
        guard = register_write_guard(
            self.repo,
            {"output/item", "output/.tmp-item", "locks/mutex"},
            {"output", "locks"},
        )
        write_atomic(self.repo, "output/item", b"ok")
        lock_dir = ensure_dir_fd(self.repo, "locks")
        try:
            first = open_lock_file(lock_dir, "mutex")
            os.close(first)
            before = list(guard.operations)
            second = open_lock_file(lock_dir, "mutex")
            os.close(second)
            self.assertEqual(guard.operations, before)
        finally:
            os.close(lock_dir)
        self.assertEqual(
            guard.operations,
            [
                {"operation": "mkdir", "path": "output"},
                {"operation": "tmp-create", "path": "output/.tmp-item"},
                {"operation": "write", "path": "output/item"},
                {"operation": "mkdir", "path": "locks"},
                {"operation": "lock-create", "path": "locks/mutex"},
            ],
        )
        with self.assertRaisesRegex(IoRejected, "write-not-allowed"):
            append_line(self.repo, "output/extra", b"x")
        guard.freeze()
        with self.assertRaisesRegex(IoRejected, "write-not-allowed"):
            guard.allow({"output/extra"})

    def test_write_guard_bootstrap_is_narrow_and_ends_when_bound(self):
        state = ".claude/state/docaudit"
        guard = register_write_guard(
            self.repo,
            {f"{state}/mutex"},
            {".claude", ".claude/state", state, f"{state}/runs"},
            allow_run_bootstrap=True,
        )
        run_fd = ensure_dir_fd(self.repo, f"{state}/runs/run-1")
        os.close(run_fd)
        append_line(self.repo, f"{state}/runs/run-1/journal.jsonl", b"line\n")
        guard.bind_run("run-1")
        with self.assertRaisesRegex(IoRejected, "write-not-allowed"):
            ensure_dir_fd(self.repo, f"{state}/runs/run-2")

    def test_guarded_unlink_requires_a_regular_file_and_matching_temporary_identity(self):
        guard = register_write_guard(self.repo, {"artifact.json", ".tmp-report.md", "link"})
        (self.root / "artifact.json").write_bytes(b"partial")
        self.assertTrue(unlink_regular(self.repo, "artifact.json"))
        self.assertFalse(unlink_regular(self.repo, "artifact.json", missing_ok=True))
        temporary = self.root / ".tmp-report.md"
        temporary.write_bytes(b"partial")
        info = temporary.stat()
        with self.assertRaisesRegex(IoRejected, "tmp-conflict"):
            unlink_owned_temporary(self.repo, ".tmp-report.md", info.st_dev, info.st_ino + 1)
        self.assertTrue(temporary.exists())
        self.assertTrue(unlink_owned_temporary(self.repo, ".tmp-report.md", info.st_dev, info.st_ino))
        outside = Path(self.temp.name) / "outside-file"
        outside.write_bytes(b"keep")
        (self.root / "link").symlink_to(outside)
        with self.assertRaisesRegex(IoRejected, "symlink-component"):
            unlink_regular(self.repo, "link")
        self.assertTrue((self.root / "link").is_symlink())
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertEqual(
            [row["operation"] for row in guard.operations],
            ["unlink", "tmp-unlink"],
        )
