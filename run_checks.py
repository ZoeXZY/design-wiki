#!/usr/bin/env python3
"""Run actual backend tests and retain a compact evidence record."""
from datetime import datetime,timezone
import io
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
stream=io.StringIO()
suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
output=ROOT/'outputs';output.mkdir(exist_ok=True)
(output/'test_run.txt').write_text(stream.getvalue(),encoding='utf-8')
summary={'run_at_utc':datetime.now(timezone.utc).isoformat(),'python':sys.version,'passed':result.wasSuccessful(),'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'scope':'确定性开发回归与本地HTTP接口测试；不是模型效果或用户实验','details_file':'test_run.txt'}
(output/'test_results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(stream.getvalue());print(json.dumps(summary,ensure_ascii=False,indent=2))
sys.exit(0 if result.wasSuccessful() else 1)
