"""No-key dataset integrity and wheel-content checks; never starts inference."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from repair_workbench.evaluation import load_dataset


def check(wheel=None):
    datasets=[]
    for directory in sorted((ROOT/'benchmarks').iterdir()):
        if not (directory/'manifest.json').is_file():continue
        manifest,sha=load_dataset(directory)
        datasets.append({'dataset':directory.name,'tasks':len(manifest['tasks']),'sha256':sha})
    result={'datasets':datasets,'real_model_calls':0}
    if wheel:
        with zipfile.ZipFile(wheel) as archive:
            if archive.testzip() is not None:raise ValueError('wheel integrity failure')
            files=list((ROOT/'repair_workbench').glob('*.py'))+list((ROOT/'repair_workbench/static').glob('*'))
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
