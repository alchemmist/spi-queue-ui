"""Discover both backend and frontend suites without importing or calling production."""
import os
import pathlib
import sys
import unittest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
loader=unittest.TestLoader()
suite=unittest.TestSuite([loader.discover(str(ROOT/'tests/backend')), unittest.TestLoader().discover(str(ROOT/'tests'))])
result=unittest.TextTestRunner(verbosity=2).run(suite)
if os.environ.get('SPI_REQUIRE_NATIVE_TESTS') == 'true' and result.skipped:
    print('Native image test run must not skip tests.', file=sys.stderr)
    sys.exit(1)
sys.exit(0 if result.wasSuccessful() else 1)
