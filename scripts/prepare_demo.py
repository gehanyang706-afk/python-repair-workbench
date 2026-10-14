"""Copy the existing regression fixture into a new, user-owned Git repository."""
import argparse
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def prepare(output):
    output=output.resolve()
    if output.exists() or output==ROOT or ROOT in output.parents:
        raise ValueError('Use a new destination outside the workbench checkout.')
    shutil.copytree(ROOT/'tests/fixtures/broken_discount',output,
                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for args in (('init','-q'),('add','--all'),('commit','-qm','Reproducible discount boundary fixture')):
        subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
            '-c','user.name=Workbench Demo','-c','user.email=demo@example.invalid','-C',str(output),*args],
            check=True,capture_output=True)
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    print(prepare(parser.parse_args().output))
