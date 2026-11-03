"""Regression evidence for fault-frame omission found in the public pilot."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_agent import fixture
from repair_workbench.session import RepairSession
from repair_workbench.snapshot import create_snapshot, snapshot_id


class FailureContextRankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = fixture(self.root)

    def packet(self, text, frames, task):
        source = self.root / 'source'
        (source / 'pricing.py').write_text(text)
        subprocess.run(['git', '-C', str(source), '-c', 'core.hooksPath=/dev/null',
                        '-c', 'commit.gpgsign=false', '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'add', '.'],
                       check=True, capture_output=True)
        subprocess.run(['git', '-C', str(source), '-c', 'core.hooksPath=/dev/null',
                        '-c', 'commit.gpgsign=false', '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'nested stack'],
                       check=True, capture_output=True)
        baseline = self.root / 'nested-baseline'
        manifest = create_snapshot(source, baseline)
        shutil.copytree(self.original / 'reports', baseline / 'reports')
        result = json.loads((baseline / 'reports/result.json').read_text())
        result['snapshot_id'] = snapshot_id(manifest)
        (baseline / 'reports/result.json').write_text(json.dumps(result))
        (baseline / 'reports/pytest.log').write_text('\n'.join(
            'File "/workspace/pricing.py", line %d, in %s' % (line, name)
            for name, line in frames) + '\nE ValueError: root fault\n')
        session = RepairSession(baseline, self.root / 'agent', {'pricing.py'}, profile='simple_context')
        session.task = task
        return session, session.initial_context()

    def flow(self):
        text = 'class Flow:\n'
        for i in range(14):
            text += '    def noise%d(self):\n        return %d\n\n' % (i, i)
        text += ('    def entry(self):\n        return self.phase()\n\n'
                 '    def phase(self):\n        return self.dispatch()\n\n'
                 '    def dispatch(self):\n        return self.fail()\n\n'
                 '    def fail(self):\n        raise ValueError("root fault")\n')
        nodes = {n.name: n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)}
        frames = [(name, nodes[name].lineno + 1) for name in ('entry', 'phase', 'dispatch', 'fail')]
        return text, frames

    def test_innermost_source_frame_survives_three_excerpt_budget(self):
        text, frames = self.flow()
        session, packet = self.packet(text, frames, 'Fix Flow behavior.')
        self.assertEqual(packet['source_excerpts'][0]['symbol'], 'Flow.fail')
        self.assertIn('raise ValueError("root fault")', packet['source_excerpts'][0]['content'])
        self.assertLessEqual(len(packet['source_excerpts']), 3)
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False)), 12000)
        for item in packet['source_excerpts']:
            self.assertEqual(item['content'], session.source_refs[item['source_ref']]['content'])

    def test_class_name_does_not_fill_symbol_budget_with_unrelated_methods(self):
        text, frames = self.flow()
        _, packet = self.packet(text, frames, 'Fix Flow behavior.')
        symbols = packet['task_symbols']
        self.assertFalse(any('noise' in s['symbol'] for s in symbols))
        self.assertEqual(symbols[0]['symbol'], 'Flow.fail')
        self.assertEqual(symbols[0]['basis'], 'traceback')

    def test_long_prefix_cannot_push_fault_line_past_character_limit(self):
        text = 'def work():\n' + ''.join('    # %s\n' % ('padding' * 25) for _ in range(40))
        text += '    raise ValueError("root fault")\n'
        line = len(text.splitlines())
        _, packet = self.packet(text, [('work', line)], 'Fix work.')
        self.assertIn('raise ValueError("root fault")', packet['source_excerpts'][0]['content'])


if __name__ == '__main__':
    unittest.main()
