from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_retrieval
from tests.fixtures import fake_mdq, init_repo


def _env(base, path_dir=None):
    return {"PATH": str(path_dir) if path_dir else "", "TMPDIR": str(base)}


def _legacy_index(base, repo, run_id):
    path = base / ("docaudit-index-" + run_id)
    path.mkdir()
    (path / "owner.json").write_text(json.dumps({
        "repo": hashlib.sha256(os.path.realpath(repo).encode()).hexdigest(),
        "runId": run_id,
    }))
    (path / "index.sqlite").write_bytes(b"old database")
    return path


class RetrievalTests(unittest.TestCase):
    def prepare(self, base, repo, path_dir=None, corpus=("docs/a.md",)):
        with mock.patch.dict(os.environ, _env(base, path_dir)):
            manifest, details = c_retrieval.prepare(repo, list(corpus))
        self.assertEqual(manifest, {
            "method": "grep", "indexAvailable": False, "indexHealthy": False,
            "reason": "mdq-retired", "files": None, "chunks": None,
        })
        self.assertEqual(details, {
            "method": "grep", "indexDb": None, "indexCwd": None, "indexLang": None,
        })
        return manifest, details

    def test_no_temporary_mirror_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            before = sorted(p.relative_to(base).as_posix() for p in base.rglob("*"))
            with mock.patch("os.mkdir", side_effect=AssertionError("must not create a mirror")):
                self.prepare(base, repo)
            self.assertEqual(before, sorted(p.relative_to(base).as_posix() for p in base.rglob("*")))

    def test_healthy_mdq_is_never_invoked(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            binary, path_dir = fake_mdq(base)
            marker = base / "mdq-called"
            binary.write_text("#!/bin/sh\nprintf called > '" + str(marker) + "'\nexit 9\n")
            for locale in ("C", "ja_JP.UTF-8"):
                with self.subTest(locale=locale), mock.patch.dict(os.environ, {"LANG": locale}):
                    self.prepare(base, repo, path_dir)
            self.assertFalse(marker.exists())

    def test_repository_tmpdir_does_not_create_an_index(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            self.prepare(repo, repo)
            self.assertFalse((repo / ".mdq").exists())

    def test_corpus_symlink_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            outside = base / "outside.md"; outside.write_text("external")
            (repo / "docs" / "external.md").symlink_to(outside)
            _, path_dir = fake_mdq(base)
            with self.assertRaisesRegex(c_retrieval.RetrievalRejected, "^corpus-unreadable$"):
                self.prepare(base, repo, path_dir, corpus=("docs/a.md", "docs/external.md"))
            self.assertEqual(outside.read_text(), "external")

    def test_missing_corpus_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            with self.assertRaisesRegex(c_retrieval.RetrievalRejected, "^corpus-unreadable$"):
                self.prepare(base, repo, corpus=("docs/missing.md",))

    def test_corpus_parent_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            (repo / "linked-docs").symlink_to(repo / "docs", target_is_directory=True)
            with self.assertRaisesRegex(c_retrieval.RetrievalRejected, "^corpus-unreadable$"):
                self.prepare(base, repo, corpus=("linked-docs/a.md",))

    def test_non_regular_corpus_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            for rel in ("docs", "docs/pipe.md"):
                if rel.endswith("pipe.md"):
                    os.mkfifo(repo / rel)
                with self.subTest(path=rel), self.assertRaisesRegex(c_retrieval.RetrievalRejected, "^corpus-unreadable$"):
                    self.prepare(base, repo, corpus=(rel,))

    def test_corpus_read_error_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            with mock.patch.object(c_retrieval.c_io.os, "read", side_effect=OSError("unreadable")):
                with self.assertRaisesRegex(c_retrieval.RetrievalRejected, "^corpus-unreadable$"):
                    self.prepare(base, repo)

    def test_oversized_corpus_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            with (repo / "docs/a.md").open("wb") as stream:
                stream.truncate(c_retrieval.MAX_DOC_BYTES + 1)
            with self.assertRaisesRegex(c_retrieval.RetrievalRejected, "^corpus-unreadable$"):
                self.prepare(base, repo)

    def test_legacy_orphan_cleanup_preserves_foreign_and_ownerless_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            own = _legacy_index(base, repo, "own")
            current = _legacy_index(base, repo, "current")
            foreign = _legacy_index(base, base / "other-repo", "foreign")
            ownerless = base / "docaudit-index-ownerless"; ownerless.mkdir()
            c_retrieval.cleanup_orphans(repo, "current", _env(base))
            self.assertFalse(own.exists())
            self.assertTrue(current.exists())
            self.assertTrue(foreign.exists())
            self.assertTrue(ownerless.exists())

    def test_orphan_cleanup_preserves_owned_index_inside_repository_tmpdir(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            index = _legacy_index(repo, repo, "old-run")
            before = {path.name: path.read_bytes() for path in index.iterdir()}
            c_retrieval.cleanup_orphans(repo, "current", _env(repo))
            self.assertTrue(index.is_dir())
            self.assertEqual(before, {path.name: path.read_bytes() for path in index.iterdir()})

    def test_cleanup_uses_recorded_legacy_index_path_after_tmpdir_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); repo = base / "repo"; repo.mkdir(); init_repo(repo)
            path = _legacy_index(base, repo, "old-run")
            run = repo / ".claude/state/docaudit/runs/old-run"; run.mkdir(parents=True)
            (run / "retrieval.json").write_text(json.dumps({"indexDb": str(path / "index.sqlite")}))
            with mock.patch.dict(os.environ, {"TMPDIR": str(repo)}):
                c_retrieval.cleanup(repo, "old-run")
            self.assertFalse(path.exists())
