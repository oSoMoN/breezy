# Copyright (C) 2006 Canonical Ltd
# Copyright (C) 2008 Aaron Bentley <aaron@aaronbentley.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

import os

from breezy.tests import TestCase, TestCaseInTempDir, features

from ..errors import BinaryFile
from ..osutils import IterableFile
from ..patch import PatchInvokeError, diff3, iter_patched_from_hunks, run_patch
from ..patches import parse_patch


class TestPatch(TestCaseInTempDir):

    def test_diff3_binaries(self):
        with open('this', 'wb') as f:
            f.write(b'a')
        with open('other', 'wb') as f:
            f.write(b'a')
        with open('base', 'wb') as f:
            f.write(b'\x00')
        self.assertRaises(BinaryFile, diff3, 'unused', 'this', 'other', 'base')

    def test_missing_patch(self):
        self.assertRaises(PatchInvokeError, run_patch, '.', [],
                          _patch_cmd='/unlikely/to/exist')


class PatchesTester(TestCase):

    def setUp(self):
        super(PatchesTester, self).setUp()
        self.requireFeature(features.patch_feature)

    def datafile(self, filename):
        data_path = os.path.join(os.path.dirname(__file__),
                                 "test_patches_data", filename)
        return open(data_path, "rb")

    def data_lines(self, filename):
        with self.datafile(filename) as datafile:
            return datafile.readlines()

    def test_iter_patched_from_hunks(self):
        """Test a few patch files, and make sure they work."""
        files = [
            ('diff-2', 'orig-2', 'mod-2'),
            ('diff-3', 'orig-3', 'mod-3'),
            ('diff-4', 'orig-4', 'mod-4'),
            ('diff-5', 'orig-5', 'mod-5'),
            ('diff-6', 'orig-6', 'mod-6'),
            ('diff-7', 'orig-7', 'mod-7'),
        ]
        for diff, orig, mod in files:
            parsed = parse_patch(self.datafile(diff))
            orig_lines = list(self.datafile(orig))
            mod_lines = list(self.datafile(mod))
            iter_patched = iter_patched_from_hunks(orig_lines, [h.as_bytes() for h in parsed.hunks])
            patched_file = IterableFile(iter_patched)
            count = 0
            for patch_line in patched_file:
                self.assertEqual(patch_line, mod_lines[count], f'for file {diff}')
                count += 1
            self.assertEqual(count, len(mod_lines))


class RunPatchTests(TestCaseInTempDir):

    def setUp(self):
        super(RunPatchTests, self).setUp()
        self.requireFeature(features.patch_feature)

    def test_new_file(self):
        run_patch('.', b"""\
 message           | 3 +++
 1 files changed, 14 insertions(+)
 create mode 100644 message

diff --git a/message b/message
new file mode 100644
index 0000000..05ec0b1
--- /dev/null
+++ b/message
@@ -0,0 +1,3 @@
+Update standards version, no changes needed.
+Certainty: certain
+Fixed-Lintian-Tags: out-of-date-standards-version
""".splitlines(True), strip=1)
        self.assertFileEqual("""\
Update standards version, no changes needed.
Certainty: certain
Fixed-Lintian-Tags: out-of-date-standards-version
""", 'message')
