"""Container-only pytest entry point retaining exact node IDs and xfail states."""
import json
import os
from pathlib import Path
import sys


class NodeRecorder:
    def __init__(self):
        self.collected, self.phases, self.collection_errors = [], {}, []
        self.subtests = {}

    def pytest_collection_modifyitems(self, session, config, items):
        self.collected = [item.nodeid for item in items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    def pytest_runtest_logreport(self, report):
        # Pytest emits additional call reports for unittest.subTest while
        # retaining one collected parent item. Keep these separate from a
        # genuinely repeated parent call and never ignore a failed subtest.
        if (type(report).__module__ == '_pytest.subtests'
                and type(report).__name__ == 'SubtestReport'):
            counts = self.subtests.setdefault(report.nodeid, {})
            counts[report.outcome] = counts.get(report.outcome, 0) + 1
            return
        if report.when in ('setup', 'call', 'teardown'):
            phase = self.phases.setdefault(report.nodeid, {})
            if report.when in phase:
                phase['duplicate'] = True
            phase[report.when] = {'outcome': report.outcome, 'xfail': hasattr(report, 'wasxfail')}

    def outcomes(self):
        result = {}
        for nodeid in self.collected:
            phases = self.phases.get(nodeid, {})
            setup, call, teardown = (phases.get(name, {}) for name in ('setup', 'call', 'teardown'))
            if phases.get('duplicate') or not setup or not teardown:
                status = 'incomplete'
            elif any(phase.get('outcome') == 'failed' for phase in (setup, teardown)):
                status = 'error'
            elif setup.get('outcome') == 'skipped':
                status = 'xfailed' if setup['xfail'] else 'skipped'
            elif not call:
                status = 'incomplete'
            elif call['outcome'] == 'skipped':
                status = 'xfailed' if call['xfail'] else 'skipped'
            elif call['outcome'] == 'passed':
                status = 'xpassed' if call['xfail'] else 'passed'
            else:
                status = 'failed'
            subtests = self.subtests.get(nodeid, {})
            if subtests.get('failed'):
                status = 'failed'
            elif subtests.get('skipped') and status == 'passed':
                status = 'skipped'
            result[nodeid] = status
        return result


def main():
    # Only the selected immutable repository is added, never a host path.
    sys.path.insert(0, '/workspace')
    import pytest
    recorder = NodeRecorder()
    exitcode = pytest.main(['tests', '-q', '-p', 'no:cacheprovider',
                           '--junitxml=/reports/pytest.xml'], plugins=[recorder])
    report = {'schema_version': 1, 'exitcode': int(exitcode), 'collected': recorder.collected,
              'outcomes': recorder.outcomes(), 'collection_errors': recorder.collection_errors,
              'subtest_counts': recorder.subtests}
    with Path('/reports/nodeids.json').open('x', encoding='utf-8') as stream:
        os.chmod(stream.name, 0o600)
        json.dump(report, stream, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    raise SystemExit(exitcode)


if __name__ == '__main__':
    main()
