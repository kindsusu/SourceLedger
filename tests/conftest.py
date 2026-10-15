"""Keep test outcomes available to optional browser diagnostic fixtures."""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, 'report_' + report.when, report)
