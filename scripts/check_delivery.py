"""No-key dataset integrity and wheel-content checks; never starts inference."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from repair_workbench.evaluation import load_dataset


UGC_DATASET = 'ugc-diagnostics-dev-v1'
# Registry code is part of this reviewed checkout, never selected by manifest data.
UGC_QUALIFICATION = Path(__file__).resolve().parents[1] / 'benchmarks' / UGC_DATASET / 'qualification.py'


def check_ugc_dataset(directory):
    if directory.is_symlink() or (directory / 'manifest.json').is_symlink():
        raise ValueError('symlink in UGC dataset registry')
    if UGC_QUALIFICATION.is_symlink() or UGC_QUALIFICATION.parent.is_symlink():
        raise ValueError('untrusted UGC qualification path')
    spec = importlib.util.spec_from_file_location('repair_delivery_ugc_qualification', UGC_QUALIFICATION)
    qualification = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qualification)
    # The fixed qualification module owns expected IDs, contracts and file hashes.
    # Redirect only its data root; neither this function nor the manifest loads a fixture.
    qualification.ROOT = directory.resolve(strict=True)
    manifest = qualification.fixture_manifest()
    verified_files = 0
    for task in manifest['tasks']:
        for relative in task['files']:
            qualification._trusted_bytes(task['id'], relative)
            verified_files += 1
    return {'dataset': directory.name, 'tasks': len(manifest['tasks']),
            'sha256': hashlib.sha256((directory / 'manifest.json').read_bytes()).hexdigest(),
            'split': manifest['split'], 'synthetic': manifest['synthetic'],
            'public': manifest['public'], 'independent_evaluation': manifest['independent_evaluation'],
            'verified_files': verified_files}


def check(wheel=None):
    datasets=[]
    for directory in sorted((ROOT/'benchmarks').iterdir()):
        if not (directory/'manifest.json').is_file():continue
        if directory.name == UGC_DATASET:
            datasets.append(check_ugc_dataset(directory))
            continue
        manifest,sha=load_dataset(directory)
        datasets.append({'dataset':directory.name,'tasks':len(manifest['tasks']),'sha256':sha})
    result={'datasets':datasets,'real_model_calls':0}
    if wheel:
        with zipfile.ZipFile(wheel) as archive:
            if archive.testzip() is not None:raise ValueError('wheel integrity failure')
            files=(list((ROOT/'repair_workbench').glob('*.py'))+
                   list((ROOT/'repair_workbench').glob('swebench_harness_lock.json'))+
                   list((ROOT/'repair_workbench/static').glob('*'))+
                   list((ROOT/'repair_workbench/migrations').glob('*.sql'))+
                   list((ROOT/'repair_workbench/skills').glob('*/SKILL.md')))
            for path in files:
                if path.is_file() and archive.read(path.relative_to(ROOT).as_posix())!=path.read_bytes():
                    raise ValueError('wheel differs from source: '+path.name)
            for name in archive.namelist():
                if name.startswith(('runtime/','tests/')) or Path(name).name.startswith('.env'):
                    raise ValueError('private/test data in wheel')
        result['wheel']={'sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'verified_files':len(files)}
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel',type=Path)
    print(json.dumps(check(parser.parse_args().wheel),indent=2))
