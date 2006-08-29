# Copyright (C) 2006 Canonical Ltd
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
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""An http transport, using webdav to allow pushing.

This defines the HttpWebDAV transport, which implement the necessary
handling of WebDAV to allow pushing on an http server.
"""

# Don't go further if we are not compatible
import bzrlib
major, minor, micro, releaselevel = bzrlib.version_info[:4]

if major != 0 or minor != 11 or releaselevel != 'dev':
    # Until  the  plugin  is  considered  mature  enough,  better
    # restrict its use to the developers.
    raise BzrCheckError('We need a recent bzr >= 0.11.0dev0')

from bzrlib.transport import (
    register_lazy_transport
    )

register_lazy_transport('https+webdav://',
                        'bzrlib.plugins.webdav.webdav','HttpDavTransport')
register_lazy_transport('http+webdav://',
                        'bzrlib.plugins.webdav.webdav','HttpDavTransport')
