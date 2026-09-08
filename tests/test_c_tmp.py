import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_check, c_dispatch, c_tmp
from skills.audit.engine.c_tmp import TmpUnavailable, private_temp
from tests.test_c_check import ctx as check_context
from tests.test_c_dispatch import context as dispatch_context


class PrivateTemporaryTests(unittest.TestCase):
    def test_repository_tmpdir_is_skipped_without_cached_default_probe(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); before = set(root.rglob("*")); old = tempfile.tempdir
            tempfile.tempdir = None
            try:
                with private_temp(root, {"TMPDIR": str(root)}, "fixture") as directory:
                    self.assertNotEqual(os.path.commonpath((os.path.realpath(directory), os.path.realpath(root))),
                                        os.path.realpath(root))
                    self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
                    self.assertEqual(set(root.rglob("*")), before)
            finally:
                tempfile.tempdir = old
            self.assertEqual(set(root.rglob("*")), before)

    def test_creation_and_permission_failures_become_tmp_unavailable(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            with mock.patch.object(c_tmp.tempfile, "mkdtemp", side_effect=PermissionError("fixture")):
                with self.assertRaises(TmpUnavailable):
                    with private_temp(root, {}, "fixture"): pass
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); real_mkdtemp = tempfile.mkdtemp; created = []
            def create(*args, **kwargs):
                directory = real_mkdtemp(*args, **kwargs); created.append(directory); return directory
            with mock.patch.object(c_tmp.tempfile, "mkdtemp", side_effect=create), \
                 mock.patch.object(c_tmp.os, "chmod", side_effect=OSError("fixture")):
                with self.assertRaises(TmpUnavailable):
                    with private_temp(root, {}, "fixture"): pass
            self.assertEqual(len(created), 1)
            self.assertFalse(Path(created[0]).exists())

    def test_adapters_report_tmp_unavailable_without_starting_process(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); dispatch = dispatch_context(root)
            with mock.patch.object(c_tmp.tempfile, "mkdtemp", side_effect=PermissionError("fixture")), \
                 mock.patch.object(c_dispatch.procs, "run_group") as run:
                result = c_dispatch.adapter(dispatch)
            self.assertEqual((result.status, result.reason), ("incomplete", "tmp-unavailable"))
            run.assert_not_called()
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); check = check_context(root, ({"id": "x", "argv": ["fixture"], "timeoutSec": 1},))
            with mock.patch.object(c_check.sys, "platform", "darwin"), \
                 mock.patch.object(c_check.os.path, "isfile", return_value=True), \
                 mock.patch.object(c_tmp.tempfile, "mkdtemp", side_effect=PermissionError("fixture")), \
                 mock.patch.object(c_check.procs, "run_group") as run:
                result = c_check.adapter(check)
            self.assertEqual((result.status, result.reason), ("incomplete", "tmp-unavailable"))
            run.assert_not_called()


if __name__ == "__main__": unittest.main()
