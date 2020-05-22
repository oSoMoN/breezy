# Copyright (C) 2006, 2009, 2011 Canonical Ltd
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

"""A generator which creates a python script from the current tree info"""

from __future__ import absolute_import

import pprint

from breezy import errors
from breezy.revision import (
    NULL_REVISION,
    )
from breezy.version_info_formats import (
    create_date_str,
    VersionInfoBuilder,
    )


# Header and footer for the python format
_py_version_header = '''#!/usr/bin/env python3
"""This file is automatically generated by generate_version_info
It uses the current working tree to determine the revision.
So don't edit it. :)
"""

'''


_py_version_footer = '''
if __name__ == '__main__':
    print('revision: %(revno)s' % version_info)
    print('nick: %(branch_nick)s' % version_info)
    print('revision id: %(revision_id)s' % version_info)
'''


class PythonVersionInfoBuilder(VersionInfoBuilder):
    """Create a version file which is a python source module."""

    def generate(self, to_file):
        info = {'build_date': create_date_str(),
                'revno': None,
                'revision_id': None,
                'branch_nick': self._branch.nick,
                'clean': None,
                'date': None
                }
        revisions = []

        revision_id = self._get_revision_id()
        if revision_id == NULL_REVISION:
            info['revno'] = '0'
        else:
            try:
                info['revno'] = self._get_revno_str(revision_id)
            except errors.GhostRevisionsHaveNoRevno:
                pass
            info['revision_id'] = revision_id
            rev = self._branch.repository.get_revision(revision_id)
            info['date'] = create_date_str(rev.timestamp, rev.timezone)

        if self._check or self._include_file_revs:
            self._extract_file_revisions()

        if self._check:
            if self._clean:
                info['clean'] = True
            else:
                info['clean'] = False

        info_str = pprint.pformat(info)
        to_file.write(_py_version_header)
        to_file.write('version_info = ')
        to_file.write(info_str)
        to_file.write('\n\n')

        if self._include_history:
            history = list(self._iter_revision_history())
            revision_str = pprint.pformat(history)
            to_file.write('revisions = ')
            to_file.write(revision_str)
            to_file.write('\n\n')
        else:
            to_file.write('revisions = {}\n\n')

        if self._include_file_revs:
            file_rev_str = pprint.pformat(self._file_revisions)
            to_file.write('file_revisions = ')
            to_file.write(file_rev_str)
            to_file.write('\n\n')
        else:
            to_file.write('file_revisions = {}\n\n')

        to_file.write(_py_version_footer)
