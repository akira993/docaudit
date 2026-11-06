"""Audit profile arguments are resolved before any audit work starts."""

import contextlib
import io
import unittest
from unittest import mock

from skills.audit.engine import c_engine, cli
from skills.audit.engine.profiles import profile_names


class CliProfileTests(unittest.TestCase):
    def invoke(self, options, expected_profile, **expected_options):
        with mock.patch.object(cli, "check_platform_support"), \
                mock.patch.object(c_engine, "run", return_value={"exitCode": 0}) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["audit", *options]), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["profile"], expected_profile)
        for name, value in expected_options.items():
            self.assertEqual(run.call_args.kwargs[name], value)

    def reject(self, options, message):
        stderr = io.StringIO()
        with mock.patch.object(cli, "check_platform_support"), \
                mock.patch.object(c_engine, "run") as run, \
                contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as caught:
            cli.main(["audit", *options])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("usage:", stderr.getvalue())
        self.assertIn(message, stderr.getvalue())
        run.assert_not_called()

    def test_positional_only(self):
        for profile in profile_names():
            with self.subTest(profile=profile):
                self.invoke([profile], profile)

    def test_option_only(self):
        for profile in profile_names():
            with self.subTest(profile=profile):
                self.invoke(["--profile", profile], profile)

    def test_matching_arguments_in_either_order(self):
        for profile in profile_names():
            for options in ([profile, "--profile", profile], ["--profile", profile, profile]):
                with self.subTest(options=options):
                    self.invoke(options, profile)

    def test_conflicting_arguments_in_either_order(self):
        first, second = profile_names()[:2]
        for options in ([first, "--profile", second], ["--profile", second, first]):
            with self.subTest(options=options):
                self.reject(options, "positional profile and --profile must match")

    def test_unknown_profile_in_either_form(self):
        for options in (["unknown-profile"], ["--profile", "unknown-profile"]):
            with self.subTest(options=options):
                self.reject(options, "invalid choice")

    def test_no_profile_preserves_automatic_selection(self):
        self.invoke([], None)

    def test_ask_backend_before_or_after_profile(self):
        profile = profile_names()[0]
        for options in (["--ask-backend", profile], [profile, "--ask-backend"]):
            with self.subTest(options=options):
                self.invoke(options, profile, ask_backend=True)

    def test_backend_before_or_after_profile(self):
        profile = profile_names()[0]
        for options in (["--backend", "codex", profile], [profile, "--backend", "codex"]):
            with self.subTest(options=options):
                self.invoke(options, profile, backend="codex")

    def test_full_before_or_after_profile(self):
        profile = profile_names()[0]
        for options in (["--full", profile], [profile, "--full"]):
            with self.subTest(options=options):
                self.invoke(options, profile, full=True)

    def test_scope_still_accepts_only_profile_option(self):
        profile = profile_names()[0]
        self.assertEqual(cli.parser().parse_args(["scope", "--profile", profile]).profile, profile)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.parser().parse_args(["scope", profile])
        self.assertEqual(caught.exception.code, 2)
