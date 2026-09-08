from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_retrieval
from tests.fixtures import fake_mdq, init_repo


def _env(base: Path, path_dir: Path | None = None):
    value = {
        "PATH": str(path_dir) if path_dir is not None else "",
        "HOME": str(base / "home"),
        "TMPDIR": str(base),
        "LANG": "C",
        "LC_ALL": "C",
    }
    return value


def _store_retrieval(repo: Path, run_id: str, details: dict) -> None:
    run_dir = repo / ".claude" / "state" / "docaudit" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "retrieval.json").write_text(
        json.dumps(details), encoding="utf-8",
    )


class RetrievalTests(unittest.TestCase):
    def test_temporary_mirror_write_failure_falls_back_and_removes_index(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            _, path_dir = fake_mdq(base)
            real_exclusive_file = c_retrieval.exclusive_file

            def fail_mirror_write(path, data=b""):
                if path.endswith(os.path.join("corpus", "docs", "a.md")):
                    raise OSError(errno.ENOSPC, "fixture storage full")
                return real_exclusive_file(path, data)

            with mock.patch.object(
                c_retrieval, "exclusive_file", side_effect=fail_mirror_write,
            ):
                manifest, details = c_retrieval.prepare(
                    repo, "run-storage-full", ["docs/a.md"], _env(base, path_dir),
                )

            self.assertEqual(
                (manifest["method"], manifest["indexAvailable"],
                 manifest["indexHealthy"], manifest["reason"]),
                ("grep", True, False, "index-unavailable"),
            )
            self.assertEqual(details, {
                "method": "grep", "indexDb": None,
                "indexCwd": None, "indexLang": None,
            })
            self.assertFalse((base / "docaudit-index-run-storage-full").exists())

    def test_healthy_index_uses_a_corpus_only_independent_mirror(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            original = (repo / "docs" / "a.md").read_bytes()
            _, path_dir = fake_mdq(base, mode="mutate-mirror")

            manifest, details = c_retrieval.prepare(
                repo, "run-healthy", ["docs/a.md"], _env(base, path_dir)
            )
            try:
                self.assertEqual(manifest, {
                    "method": "index", "indexAvailable": True,
                    "indexHealthy": True, "reason": None, "files": 1, "chunks": 1,
                })
                self.assertEqual(details["method"], "index")
                self.assertEqual(details["indexLang"], "ja-jp")
                mirror = Path(details["indexCwd"])
                database = Path(details["indexDb"])
                owner = json.loads(
                    (mirror.parent / "owner.json").read_text(encoding="utf-8")
                )
                self.assertEqual(owner, {
                    "repo": hashlib.sha256(
                        os.path.realpath(repo).encode("utf-8")
                    ).hexdigest(),
                    "runId": "run-healthy",
                })
                self.assertTrue(database.is_file())
                self.assertEqual((repo / "docs" / "a.md").read_bytes(), original)
                self.assertEqual((mirror / "docs" / "a.md").read_text(encoding="utf-8"),
                                 "# Mirror changed\n")
                mirrored = {
                    path.relative_to(mirror).as_posix()
                    for path in mirror.rglob("*")
                    if path.is_file() and ".mdq" not in path.parts
                }
                self.assertEqual(mirrored, {"docs/a.md"})
                self.assertTrue((mirror / ".mdq" / "usage.jsonl").is_file())
                self.assertFalse((repo / ".mdq").exists())
                payload = json.loads(database.read_text(encoding="utf-8"))
                self.assertEqual([row["path"] for row in payload["files"]], ["docs/a.md"])
            finally:
                _store_retrieval(repo, "run-healthy", details)
                c_retrieval.cleanup(repo, "run-healthy")
            self.assertFalse(Path(details["indexCwd"]).parent.exists())

    def test_every_mdq_command_receives_the_fixed_index_language(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            _, path_dir = fake_mdq(base)
            calls = []
            run_group = c_retrieval.procs.run_group

            def capture(argv, **kwargs):
                calls.append(argv)
                return run_group(argv, **kwargs)

            with mock.patch.object(
                c_retrieval.procs, "run_group", side_effect=capture,
            ):
                manifest, _details = c_retrieval.prepare(
                    repo, "run-language", ["docs/a.md"], _env(base, path_dir),
                )

            self.assertEqual(manifest["method"], "index")
            self.assertEqual(
                {argv[1] for argv in calls}, {"index", "stats", "list", "search"},
            )
            for argv in calls:
                with self.subTest(command=argv[1]):
                    position = argv.index("--lang")
                    self.assertEqual(argv[position + 1], c_retrieval.INDEX_LANG)

    def test_unavailable_unhealthy_and_timeout_fail_open_to_grep(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)

            manifest, details = c_retrieval.prepare(
                repo, "run-missing", ["docs/a.md"], _env(base)
            )
            self.assertEqual((manifest["method"], manifest["reason"], details["indexDb"]),
                             ("grep", "mdq-not-installed", None))

            for mode, run_id, expected in (
                ("chunks-zero", "run-empty", "index-stats-unhealthy"),
                ("nonutf8-stats", "run-nonutf8", "index-stats-unhealthy"),
                ("timeout", "run-timeout", "index-timeout"),
            ):
                with self.subTest(mode=mode):
                    tool_base = base / mode
                    tool_base.mkdir()
                    _, path_dir = fake_mdq(tool_base, mode=mode)
                    environment = _env(base, path_dir)
                    timeout = 0.1 if mode == "timeout" else c_retrieval.INDEX_TIMEOUT_SEC
                    grace = 0.1 if mode == "timeout" else c_retrieval.KILL_GRACE_SEC
                    with mock.patch.object(c_retrieval, "INDEX_TIMEOUT_SEC", timeout), \
                         mock.patch.object(c_retrieval, "KILL_GRACE_SEC", grace):
                        manifest, details = c_retrieval.prepare(
                            repo, run_id, ["docs/a.md"], environment
                        )
                    self.assertEqual((manifest["method"], manifest["indexHealthy"], manifest["reason"]),
                                     ("grep", False, expected))
                    self.assertEqual(details, {
                        "method": "grep", "indexDb": None,
                        "indexCwd": None, "indexLang": None,
                    })
                    self.assertFalse((repo / ".mdq").exists())
                    c_retrieval.cleanup(repo, run_id)

    def test_tmp_candidates_inside_repo_fail_open_to_grep(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            internal_tmp = repo / "tmp"
            internal_tmp.mkdir()
            _, path_dir = fake_mdq(base)
            environment = _env(internal_tmp, path_dir)
            realpath = c_retrieval.os.path.realpath

            def redirect_system_candidates(path):
                if path in ("/tmp", "/var/tmp"):
                    return realpath(internal_tmp)
                return realpath(path)

            with mock.patch.object(
                c_retrieval.os.path, "realpath", side_effect=redirect_system_candidates,
            ):
                manifest, details = c_retrieval.prepare(
                    repo, "run-no-tmp", ["docs/a.md"], environment,
                )

            self.assertEqual(
                (manifest["method"], manifest["indexHealthy"], manifest["reason"]),
                ("grep", False, "tmp-unavailable"),
            )
            self.assertEqual(details, {
                "method": "grep", "indexDb": None,
                "indexCwd": None, "indexLang": None,
            })

    def test_copy_rejects_a_document_replaced_by_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            external = base / "external.md"
            external.write_text("outside fixture", encoding="utf-8")
            _, path_dir = fake_mdq(base)
            changed = False

            def replace(_context):
                nonlocal changed
                if not changed:
                    changed = True
                    path = repo / "docs" / "a.md"
                    path.unlink()
                    path.symlink_to(external)

            with self.assertRaisesRegex(c_retrieval.RetrievalRejected, "corpus-unreadable"):
                c_retrieval.prepare(
                    repo, "run-symlink", ["docs/a.md"], _env(base, path_dir), hook=replace
                )
            self.assertEqual(external.read_text(encoding="utf-8"), "outside fixture")
            self.assertFalse((base / "docaudit-index-run-symlink").exists())

    def test_deterministic_directory_is_not_reused_and_orphans_are_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            _, path_dir = fake_mdq(base)
            environment = _env(base, path_dir)
            occupied = base / "docaudit-index-run-collision"
            occupied.mkdir()
            marker = occupied / "owned"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(c_retrieval.RetrievalRejected, "tmp-unavailable"):
                c_retrieval.prepare(
                    repo, "run-collision", ["docs/a.md"], environment
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            c_retrieval.cleanup(repo, "run-collision")
            self.assertTrue(occupied.exists())

            current = base / "docaudit-index-current"
            current.mkdir()
            ownerless = base / "docaudit-index-ownerless"
            ownerless.mkdir()
            foreign = base / "docaudit-index-foreign"
            foreign.mkdir()
            own_orphan = base / "docaudit-index-own-orphan"
            own_orphan.mkdir()
            repo_hash = hashlib.sha256(
                os.path.realpath(repo).encode("utf-8")
            ).hexdigest()
            (foreign / "owner.json").write_text(json.dumps({
                "repo": hashlib.sha256(b"another-repository").hexdigest(),
                "runId": "foreign",
            }), encoding="utf-8")
            (own_orphan / "owner.json").write_text(json.dumps({
                "repo": repo_hash,
                "runId": "own-orphan",
            }), encoding="utf-8")
            unrelated = base / "ordinary-directory"
            unrelated.mkdir()
            c_retrieval.cleanup_orphans(repo, "current", environment)
            self.assertTrue(occupied.exists())
            self.assertTrue(current.exists())
            self.assertTrue(ownerless.exists())
            self.assertTrue(foreign.exists())
            self.assertFalse(own_orphan.exists())
            self.assertTrue(unrelated.exists())

    def test_cleanup_uses_recorded_index_path_after_tmpdir_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            init_repo(repo)
            original_tmp = base / "first-tmp"
            changed_tmp = base / "second-tmp"
            original_tmp.mkdir()
            changed_tmp.mkdir()
            _, path_dir = fake_mdq(base)
            run_id = "run-moved-tmp"
            manifest, details = c_retrieval.prepare(
                repo, run_id, ["docs/a.md"], _env(original_tmp, path_dir),
            )
            self.assertEqual(manifest["method"], "index")
            index_base = Path(details["indexDb"]).parent
            self.assertTrue(index_base.exists())
            _store_retrieval(repo, run_id, details)

            with mock.patch.dict(os.environ, {"TMPDIR": str(changed_tmp)}):
                c_retrieval.cleanup(repo, run_id)

            self.assertFalse(index_base.exists())


if __name__ == "__main__":
    unittest.main()
