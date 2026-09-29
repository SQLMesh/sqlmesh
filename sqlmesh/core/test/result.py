from __future__ import annotations

import types
import typing as t
import unittest

from sqlmesh.core.test.definition import ModelTest

if t.TYPE_CHECKING:
    ErrorType = t.Union[
        t.Tuple[type[BaseException], BaseException, types.TracebackType],
        t.Tuple[None, None, None],
    ]


class ModelTextTestResult(unittest.TextTestResult):
    successes: t.List[unittest.TestCase]
    tests_skipped: int

    def __init__(self, *args: t.Any, **kwargs: t.Any):
        self.console = kwargs.pop("console", None)
        super().__init__(*args, **kwargs)
        self.successes = []
        self.original_failures: t.List[t.Tuple[unittest.TestCase, ErrorType]] = []
        self.failure_tables: t.List[t.Tuple[t.Any, ...]] = []
        self.original_errors: t.List[t.Tuple[unittest.TestCase, ErrorType]] = []
        self.duration: t.Optional[float] = None
        self.tests_skipped = 0

    def addSubTest(
        self,
        test: unittest.TestCase,
        subtest: unittest.TestCase,
        err: t.Optional[ErrorType],
    ) -> None:
        """Called at the end of a subtest.

        The traceback is suppressed because it is redundant and not useful.

        Args:
            test: The test case.
            subtest: The subtest instance.
            err: A tuple of the form returned by sys.exc_info(), i.e., (type, value, traceback).
        """
        if err:
            exctype, value, tb = err
            err = (exctype, value, None)  # type: ignore

            if err[0] and issubclass(err[0], test.failureException):
                self.addFailure(test, err)
            else:
                self.addError(test, err)

    def _print_char(self, char: str) -> None:
        from sqlmesh.core.console import TerminalConsole

        if isinstance(self.console, TerminalConsole):
            self.console._print(char, end="")

    def addFailure(self, test: unittest.TestCase, err: ErrorType) -> None:
        """Called when the test case test signals a failure.

        The traceback is suppressed because it is redundant and not useful.

        Args:
            test: The test case.
            err: A tuple of the form returned by sys.exc_info(), i.e., (type, value, traceback).
        """
        exctype, value, _ = err

        if value and value.args:
            exception_msg, rich_tables = value.args[:1], value.args[1:]
            value.args = exception_msg

            if rich_tables:
                self.failure_tables.append(rich_tables)

        self._print_char("F")

        self.original_failures.append((test, err))

        # Intentionally ignore the traceback to hide it from the user
        return super().addFailure(test, (exctype, value, None))  # type: ignore

    def addError(self, test: unittest.TestCase, err: ErrorType) -> None:
        """Called when the test case test signals an error.

        Args:
            test: The test case.
            err: A tuple of the form returned by sys.exc_info(), i.e., (type, value, traceback).
        """
        exctype, value, _ = err
        self.original_errors.append((test, err))

        self._print_char("E")

        # Intentionally ignore the traceback to hide it from the user
        return super().addError(test, (exctype, value, None))  # type: ignore

    def addSuccess(self, test: unittest.TestCase) -> None:
        """Called when the test case test succeeds.

        Args:
            test: The test case
        """
        super().addSuccess(test)

        self._print_char(".")

        self.successes.append(test)

    def merge(self, other: ModelTextTestResult) -> None:
        if other.successes:
            self.addSuccess(other.successes[0])
        elif other.errors:
            for error_test, error in other.original_errors:
                self.addError(error_test, error)
        elif other.failures:
            for failure_test, failure in other.original_failures:
                self.addFailure(failure_test, failure)

            self.failure_tables.extend(other.failure_tables)
        elif other.skipped:
            skipped_args = other.skipped[0]
            self.addSkip(skipped_args[0], skipped_args[1])

        self.testsRun += other.testsRun
        self.tests_skipped += other.tests_skipped

    def get_fail_and_error_tests(self) -> t.List[ModelTest]:
        # If tests contain failed subtests (e.g testing CTE outputs) we don't want
        # to report it as different test failures
        test_name_to_test = {
            test.test_name: test
            for test, _ in self.failures + self.errors
            if isinstance(test, ModelTest)
        }
        return list(test_name_to_test.values())
