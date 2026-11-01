"""Run pinned upstream suites in separate interpreters to prevent module collisions."""
import os
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
targets=[('sales',ROOT/'vendor/sales/src',ROOT/'vendor/sales/tests'),
         ('sales_saas_prompt',ROOT/'vendor/saas',ROOT/'vendor/saas/tests'),
         ('integration',ROOT,ROOT/'tests')]
failed=False
for name,source,tests in targets:
    print('\n'+name,flush=True)
    code='import sys,pytest;sys.path.insert(0,'+repr(str(source))+');sys.path.insert(0,'+repr(str(ROOT))+');raise SystemExit(pytest.main(['+repr(str(tests))+',"-q"]))'
    result=subprocess.run([sys.executable,'-c',code],cwd=ROOT)
    failed=failed or result.returncode!=0
raise SystemExit(1 if failed else 0)
