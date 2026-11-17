"""Unsupported system libraries fail at the public package-build boundary."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CancellationBuildTest(unittest.TestCase):
    def test_system_library_without_extension_fails_with_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'ares.h').write_text('#error unsupported_system_cares_fixture\n')
            environment = os.environ.copy()
            environment['PYCARES_USE_SYSTEM_LIB'] = '1'
            include = '/I' if sys.platform == 'win32' else '-I'
            environment['CFLAGS'] = environment.get('CFLAGS', '') + f' {include}"{root}"'
            result = subprocess.run(
                [sys.executable, 'setup.py', 'build_ext', '--force',
                 '--build-temp', str(root / 'temp'), '--build-lib', str(root / 'lib')],
                env=environment, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('native per-operation cancellation extension',
                          result.stdout + result.stderr)
