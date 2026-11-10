"""Development probe contracts with substitutes, never target imports on host."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from repair_workbench import repository_probe_worker as worker


class RepositoryProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        original_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))

    def test_unknown_workload_and_probe_rejected_before_import_or_artifacts(self):
        for workload, probe in (('arbitrary.module', 'benchmark'), ('sql-format-dev-v1', 'shell'),
                                (True, 'cpu'), ('iterator-slice-dev-v1', False)):
            with self.subTest(workload=workload, probe=probe), self.assertRaises(ValueError):
                worker.execute(workload, probe, self.root, self.root)
        self.assertFalse((self.root / 'probe.json').exists())

    def test_benchmark_recreates_iterators_and_preserves_every_sample_output(self):
        seen = []
        def sliced(values, start, stop, step):
            data = list(values)
            seen.append(data)
            return iter(data[slice(start, stop, step)])
        with patch.dict(sys.modules, {'more_itertools': SimpleNamespace(islice_extended=sliced)}), \
             patch.dict(worker.WORKLOADS, {'iterator-slice-dev-v1': (5, 10)}):
            report = worker.execute('iterator-slice-dev-v1', 'benchmark', self.root, self.root)
        self.assertEqual(len(seen), 2 * (worker.REPEATS + 1))
        for row in report['rows']:
            self.assertEqual(row['output_lengths'], [row['size'] - 1] * worker.REPEATS)
            self.assertEqual(len(set(row['output_sha256'])), 1)
            self.assertEqual(row['output_sha256'][0], worker._digest(list(range(row['size'] - 1))))

    def test_sql_probe_hashes_observed_output_without_embedding_an_expected_answer(self):
        fake = SimpleNamespace(format=lambda text, **kwargs: 'observed:' + str(len(text)))
        with patch.dict(sys.modules, {'sqlparse': fake}), \
             patch.dict(worker.WORKLOADS, {'sql-format-dev-v1': (2, 3)}):
            report = worker.execute('sql-format-dev-v1', 'memory', self.root, self.root)
        for row in report['rows']:
            expected = 'observed:' + str(len('-- development comment\n') * row['size'])
            self.assertEqual(row['output_sha256'], [worker._digest(expected)])
            self.assertIs(type(row['samples'][0]), int)
        self.assertIn('not RSS', report['scope'])

    def test_cpu_profile_is_observation_and_records_raw_pstats(self):
        with patch.dict(sys.modules, {'sqlparse': SimpleNamespace(format=lambda text, **kwargs: text)}), \
             patch.dict(worker.WORKLOADS, {'sql-format-dev-v1': (1, 2)}):
            report = worker.execute('sql-format-dev-v1', 'cpu', self.root, self.root)
        self.assertTrue((self.root / 'cpu-1.pstats').is_file())
        self.assertTrue((self.root / 'cpu-2.pstats').is_file())
        self.assertTrue(all(len(row['samples'][0]['top_functions']) <= 15 for row in report['rows']))
        self.assertEqual(json.loads((self.root / 'probe.json').read_text()), report)


if __name__ == '__main__':
    unittest.main()
