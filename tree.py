# Copyright (C) 2005-2006 Jelmer Vernooij <jelmer@samba.org>

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
"""Access to stored Subversion basis trees."""

from bzrlib import osutils, urlutils
from bzrlib.branch import Branch
from bzrlib.inventory import Inventory, InventoryDirectory, TreeReference

from bzrlib.revision import CURRENT_REVISION
from bzrlib.trace import mutter
from bzrlib.revisiontree import RevisionTree

import os
import md5
from cStringIO import StringIO
import urllib

import svn.core, svn.wc, svn.delta
from svn.core import Pool

from bzrlib.plugins.svn import errors, properties, core

def parse_externals_description(base_url, val):
    """Parse an svn:externals property value.

    :param base_url: URL on which the property is set. Used for 
        relative externals.

    :returns: dictionary with local names as keys, (revnum, url)
              as value. revnum is the revision number and is 
              set to None if not applicable.
    """
    ret = {}
    for l in val.splitlines():
        if l == "" or l[0] == "#":
            continue
        pts = l.rsplit(None, 2) 
        if len(pts) == 3:
            if not pts[1].startswith("-r"):
                raise errors.InvalidExternalsDescription()
            ret[pts[0]] = (int(pts[1][2:]), urlutils.join(base_url, pts[2]))
        elif len(pts) == 2:
            if pts[1].startswith("//"):
                raise NotImplementedError("Relative to the scheme externals not yet supported")
            if pts[1].startswith("^/"):
                raise NotImplementedError("Relative to the repository root externals not yet supported")
            ret[pts[0]] = (None, urlutils.join(base_url, pts[1]))
        else:
            raise errors.InvalidExternalsDescription()
    return ret


def inventory_add_external(inv, parent_id, path, revid, ref_revnum, url):
    """Add an svn:externals entry to an inventory as a tree-reference.
    
    :param inv: Inventory to add to.
    :param parent_id: File id of directory the entry was set on.
    :param path: Path of the entry, relative to entry with parent_id.
    :param revid: Revision to store in newly created inventory entries.
    :param ref_revnum: Referenced revision of tree that's being referenced, or 
        None if no specific revision is being referenced.
    :param url: URL of referenced tree.
    """
    assert ref_revnum is None or isinstance(ref_revnum, int)
    assert revid is None or isinstance(revid, str)
    (dir, name) = os.path.split(path)
    parent = inv[parent_id]
    if dir != "":
        for part in dir.split("/"):
            if parent.children.has_key(part):
                parent = parent.children[part]
            else:
                # Implicitly add directory if it doesn't exist yet
                # TODO: Generate a file id
                parent = inv.add(InventoryDirectory('someid', part, 
                                 parent_id=parent.file_id))
                parent.revision = revid

    reference_branch = Branch.open(url)
    file_id = reference_branch.get_root_id()
    ie = TreeReference(file_id, name, parent.file_id, revision=revid)
    if ref_revnum is not None:
        ie.reference_revision = reference_branch.get_rev_id(ref_revnum)
    inv.add(ie)


# Deal with Subversion 1.5 and the patched Subversion 1.4 (which are 
# slightly different).

if hasattr(svn.delta, 'tx_invoke_window_handler'):
    def apply_txdelta_handler(src_stream, target_stream, pool):
        assert hasattr(src_stream, 'read')
        assert hasattr(target_stream, 'write')
        window_handler, baton = svn.delta.tx_apply(src_stream, target_stream, 
                                                   None, pool)

        def wrapper(window):
            window_handler(window, baton)

        return wrapper
else:
    def apply_txdelta_handler(src_stream, target_stream, pool):
        assert hasattr(src_stream, 'read')
        assert hasattr(target_stream, 'write')
        ret = svn.delta.svn_txdelta_apply(src_stream, target_stream, None, pool)

        def wrapper(window):
            svn.delta.invoke_txdelta_window_handler(
                ret[1], window, ret[2])

        return wrapper


class SvnRevisionTree(RevisionTree):
    """A tree that existed in a historical Subversion revision."""
    def __init__(self, repository, revision_id):
        self._repository = repository
        self._revision_id = revision_id
        pool = Pool()
        (self.branch_path, self.revnum, mapping) = repository.lookup_revision_id(revision_id)
        self._inventory = Inventory()
        self.id_map = repository.get_fileid_map(self.revnum, self.branch_path, 
                                                mapping)
        editor = TreeBuildEditor(self, pool)
        self.file_data = {}
        root_repos = repository.transport.get_svn_repos_root()
        reporter = repository.transport.do_switch(
                self.revnum, True, 
                urlutils.join(root_repos, self.branch_path), editor, pool)
        reporter.set_path("", 0, True, None, pool)
        reporter.finish_report(pool)
        pool.destroy()

    def get_file_lines(self, file_id):
        return osutils.split_lines(self.file_data[file_id])


class TreeBuildEditor(svn.delta.Editor):
    """Builds a tree given Subversion tree transform calls."""
    def __init__(self, tree, pool):
        self.tree = tree
        self.repository = tree._repository
        self.last_revnum = {}
        self.dir_revnum = {}
        self.dir_ignores = {}
        self.pool = pool

    def set_target_revision(self, revnum):
        self.revnum = revnum

    def open_root(self, revnum, baton):
        file_id, revision_id = self.tree.id_map[""]
        ie = self.tree._inventory.add_path("", 'directory', file_id)
        ie.revision = revision_id
        self.tree._inventory.revision_id = revision_id
        return file_id

    def add_directory(self, path, parent_baton, copyfrom_path, copyfrom_revnum, pool):
        path = path.decode("utf-8")
        file_id, revision_id = self.tree.id_map[path]
        ie = self.tree._inventory.add_path(path, 'directory', file_id)
        ie.revision = revision_id
        return file_id

    def change_dir_prop(self, id, name, value, pool):
        if name == properties.PROP_ENTRY_COMMITTED_REV:
            self.dir_revnum[id] = int(value)
        elif name == properties.PROP_IGNORE:
            self.dir_ignores[id] = value
        elif name in (properties.PROP_ENTRY_COMMITTED_DATE,
                      properties.PROP_ENTRY_LAST_AUTHOR,
                      properties.PROP_ENTRY_LOCK_TOKEN,
                      properties.PROP_ENTRY_UUID,
                      properties.PROP_EXECUTABLE):
            pass
        elif name.startswith(properties.PROP_WC_PREFIX):
            pass
        elif name.startswith(properties.PROP_PREFIX):
            mutter('unsupported dir property %r', name)

    def change_file_prop(self, id, name, value, pool):
        if name == properties.PROP_EXECUTABLE:
            self.is_executable = (value != None)
        elif name == properties.PROP_SPECIAL:
            self.is_symlink = (value != None)
        elif name == properties.PROP_EXTERNALS:
            mutter('%r property on file!', name)
        elif name == properties.PROP_ENTRY_COMMITTED_REV:
            self.last_file_rev = int(value)
        elif name in (properties.PROP_ENTRY_COMMITTED_DATE,
                      properties.PROP_ENTRY_LAST_AUTHOR,
                      properties.PROP_ENTRY_LOCK_TOKEN,
                      properties.PROP_ENTRY_UUID,
                      properties.PROP_MIME_TYPE):
            pass
        elif name.startswith(properties.PROP_WC_PREFIX):
            pass
        elif name.startswith(properties.PROP_PREFIX):
            mutter('unsupported file property %r', name)

    def add_file(self, path, parent_id, copyfrom_path, copyfrom_revnum, baton):
        path = path.decode("utf-8")
        self.is_symlink = False
        self.is_executable = False
        return path

    def close_dir(self, id):
        if id in self.tree._inventory and self.dir_ignores.has_key(id):
            self.tree._inventory[id].ignores = self.dir_ignores[id]

    def close_file(self, path, checksum):
        file_id, revision_id = self.tree.id_map[path]
        if self.is_symlink:
            ie = self.tree._inventory.add_path(path, 'symlink', file_id)
        else:
            ie = self.tree._inventory.add_path(path, 'file', file_id)
        ie.revision = revision_id

        if self.file_stream:
            self.file_stream.seek(0)
            file_data = self.file_stream.read()
        else:
            file_data = ""

        actual_checksum = md5.new(file_data).hexdigest()
        assert(checksum is None or checksum == actual_checksum,
                "checksum mismatch: %r != %r" % (checksum, actual_checksum))

        if self.is_symlink:
            ie.symlink_target = file_data[len("link "):]
            ie.text_sha1 = None
            ie.text_size = None
            ie.text_id = None
            ie.executable = False
        else:
            ie.text_sha1 = osutils.sha_string(file_data)
            ie.text_size = len(file_data)
            self.tree.file_data[file_id] = file_data
            ie.executable = self.is_executable

        self.file_stream = None

    def close_edit(self):
        pass

    def abort_edit(self):
        pass

    def apply_textdelta(self, file_id, base_checksum):
        self.file_stream = StringIO()
        return apply_txdelta_handler(StringIO(""), self.file_stream, self.pool)


class SvnBasisTree(RevisionTree):
    """Optimized version of SvnRevisionTree."""
    def __init__(self, workingtree):
        self.workingtree = workingtree
        self._revision_id = workingtree.branch.generate_revision_id(
                                      workingtree.base_revnum)
        self.id_map = workingtree.branch.repository.get_fileid_map(
                workingtree.base_revnum, 
                workingtree.branch.get_branch_path(workingtree.base_revnum), 
                workingtree.branch.mapping)
        self._inventory = Inventory(root_id=None)
        self._repository = workingtree.branch.repository

        def add_file_to_inv(relpath, id, revid, wc):
            props = svn.wc.get_prop_diffs(self.workingtree.abspath(relpath).encode("utf-8"), wc)
            if isinstance(props, list): # Subversion 1.5
                props = props[1]
            if props.has_key(properties.PROP_SPECIAL):
                ie = self._inventory.add_path(relpath, 'symlink', id)
                ie.symlink_target = open(self._abspath(relpath)).read()[len("link "):]
                ie.text_sha1 = None
                ie.text_size = None
                ie.text_id = None
                ie.executable = False
            else:
                ie = self._inventory.add_path(relpath, 'file', id)
                data = osutils.fingerprint_file(open(self._abspath(relpath)))
                ie.text_sha1 = data['sha1']
                ie.text_size = data['size']
                ie.executable = props.has_key(properties.PROP_EXECUTABLE)
            ie.revision = revid
            return ie

        def find_ids(entry):
            relpath = urllib.unquote(entry.url[len(entry.repos):].strip("/"))
            if entry.schedule in (svn.wc.schedule_normal, 
                                  svn.wc.schedule_delete, 
                                  svn.wc.schedule_replace):
                return self.id_map[workingtree.branch.unprefix(relpath.decode("utf-8"))]
            return (None, None)

        def add_dir_to_inv(relpath, wc, parent_id):
            entries = svn.wc.entries_read(wc, False)
            entry = entries[""]
            (id, revid) = find_ids(entry)
            if id == None:
                return

            # First handle directory itself
            ie = self._inventory.add_path(relpath, 'directory', id)
            ie.revision = revid
            if relpath == u"":
                self._inventory.revision_id = revid

            for name, entry in entries.items():
                name = name.decode("utf-8")
                if name == u"":
                    continue

                assert isinstance(relpath, unicode)
                assert isinstance(name, unicode)

                subrelpath = os.path.join(relpath, name)

                assert entry
                
                if entry.kind == core.NODE_DIR:
                    subwc = svn.wc.adm_open3(wc, 
                            self.workingtree.abspath(subrelpath), 
                                             False, 0, None)
                    try:
                        add_dir_to_inv(subrelpath, subwc, id)
                    finally:
                        svn.wc.adm_close(subwc)
                else:
                    (subid, subrevid) = find_ids(entry)
                    if subid is not None:
                        add_file_to_inv(subrelpath, subid, subrevid, wc)

        wc = workingtree._get_wc() 
        try:
            add_dir_to_inv(u"", wc, None)
        finally:
            svn.wc.adm_close(wc)

    def _abspath(self, relpath):
        return svn.wc.get_pristine_copy_path(self.workingtree.abspath(relpath).encode("utf-8"))

    def get_file_lines(self, file_id):
        base_copy = self._abspath(self.id2path(file_id))
        return osutils.split_lines(open(base_copy).read())

    def annotate_iter(self, file_id,
                      default_revision=CURRENT_REVISION):
        raise NotImplementedError(self.annotate_iter)
