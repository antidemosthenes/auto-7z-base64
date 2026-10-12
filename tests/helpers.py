import tempfile
import unittest
from pathlib import Path

from auto_7z_base64.errors import BundleError
from auto_7z_base64.sevenzip import find_sevenzip

TEST_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp"
HTML_PREFIX = b'<pre style="white-space: pre-wrap; overflow-wrap: anywhere;">\n'
HTML_SUFFIX = b"\n</pre>"
try:
    SEVENZIP = find_sevenzip()
except BundleError:
    SEVENZIP = None


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
