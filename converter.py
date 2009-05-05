# Copyright (C) 2009 Jelmer Vernooij <jelmer@samba.org>
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

"""Map from Git sha's to Bazaar objects."""

from dulwich.objects import (
    Blob,
    Tree,
    )
from dulwich.object_store import (
    ObjectStoreIterator,
    )
import stat

from bzrlib import (
    errors,
    ui,
    )

from bzrlib.plugins.git.mapping import (
    directory_to_tree,
    mapping_registry,
    revision_to_commit,
    )
from bzrlib.plugins.git.shamap import (
    SqliteGitShaMap,
    )


class BazaarObjectStore(object):
    """A Git-style object store backed onto a Bazaar repository."""

    def __init__(self, repository, mapping=None):
        self.repository = repository
        if mapping is None:
            self.mapping = self.repository.get_mapping()
        else:
            self.mapping = mapping
        self._idmap = SqliteGitShaMap.from_repository(repository)

    def iter_shas(self, shas):
        return ObjectStoreIterator(self, shas)

    def _update_sha_map(self, stop_revision=None):
        if stop_revision is None:
            all_revids = self.repository.all_revision_ids()
        else:
            all_revids = self.repository.get_ancestry(stop_revision)
            first = all_revids.pop(0) # Pop leading None
            assert first is None
        graph = self.repository.get_graph()
        present_revids = set(self._idmap.revids())
        missing_revids = [revid for revid in graph.iter_topo_order(all_revids) if revid not in present_revids]
        pb = ui.ui_factory.nested_progress_bar()
        try:
            for i, revid in enumerate(missing_revids):
                pb.update("updating git map", i, len(missing_revids))
                self._update_sha_map_revision(revid)
        finally:
            self._idmap.commit()
            pb.finished()

    def __iter__(self):
        self._update_sha_map()
        return iter(self._idmap.sha1s())

    def _update_sha_map_revision(self, revid):
        inv = self.repository.get_inventory(revid)
        tree_sha = self._get_ie_sha1(inv.root, inv)
        rev = self.repository.get_revision(revid)
        commit_obj = revision_to_commit(rev, tree_sha,
            self._idmap._parent_lookup)
        try:
            foreign_revid, mapping = mapping_registry.parse_revision_id(revid)
        except errors.InvalidRevisionId:
            pass
        else:
            if foreign_revid != commit_obj.id:
                raise AssertionError("recreated git commit had different sha1: expected %s, got %s" % (foreign_revid, commit_obj.id))
        self._idmap.add_entry(commit_obj.id, "commit", (revid, tree_sha))

    def _check_expected_sha(self, expected_sha, object):
        if expected_sha is None:
            return
        if expected_sha != object.id:
            raise AssertionError("Invalid sha for %r: %s" % (object, expected_sha))

    def _get_ie_object(self, entry, inv):  
        if entry.kind == "directory":
            return self._get_tree(entry.file_id, inv.revision_id, inv=inv)
        else:
            return self._get_blob(entry.file_id, entry.revision)

    def _get_ie_object_or_sha1(self, entry, inv):
        if entry.kind == "directory":
            try:
                return self._idmap.lookup_tree(entry.file_id, inv.revision_id), None
            except KeyError:
                ret = self._get_ie_object(entry, inv)
                self._idmap.add_entry(ret.id, "tree", (entry.file_id, inv.revision_id))
                return ret.id, ret
        else:
            try:
                return self._idmap.lookup_blob(entry.file_id, entry.revision), None
            except KeyError:
                ret = self._get_ie_object(entry, inv)
                self._idmap.add_entry(ret.id, "blob", (entry.file_id, entry.revision))
                return ret.id, ret

    def _get_ie_sha1(self, entry, inv):
        return self._get_ie_object_or_sha1(entry, inv)[0]

    def _get_blob(self, fileid, revision, expected_sha=None):
        """Return a Git Blob object from a fileid and revision stored in bzr.
        
        :param fileid: File id of the text
        :param revision: Revision of the text
        """
        text = self.repository.texts.get_record_stream([(fileid, revision)],
            "unordered", True).next().get_bytes_as("fulltext")
        blob = Blob()
        blob._text = text
        self._check_expected_sha(expected_sha, blob)
        return blob

    def _get_tree(self, fileid, revid, inv=None, expected_sha=None):
        """Return a Git Tree object from a file id and a revision stored in bzr.

        :param fileid: fileid in the tree.
        :param revision: Revision of the tree.
        """
        if inv is None:
            inv = self.repository.get_inventory(revid)
        tree = directory_to_tree(inv[fileid], lambda ie: self._get_ie_sha1(ie, inv))
        self._check_expected_sha(expected_sha, tree)
        return tree

    def _get_commit(self, revid, tree_sha, expected_sha=None):
        rev = self.repository.get_revision(revid)
        commit = revision_to_commit(rev, tree_sha, self._lookup_revision_sha1)
        self._check_expected_sha(expected_sha, commit)
        return commit

    def get_parents(self, sha):
        return self[sha].parents

    def _lookup_revision_sha1(self, revid):
        try:
            return self._idmap._parent_lookup(revid)
        except KeyError:
            inv = self.repository.get_inventory(revid)
            tree_sha = self._get_ie_sha1(inv.root, inv)
            ret = self._get_commit(revid, tree_sha).id
            self._idmap.add_entry(ret, "commit", (revid, tree_sha))
            return ret

    def get_raw(self, sha):
        return self[sha].as_raw_string()

    def __contains__(self, sha):
        # See if sha is in map
        try:
            self._lookup_git_sha(sha)
        except KeyError:
            return False
        else:
            return True

    def _lookup_git_sha(self, sha):
        # See if sha is in map
        try:
            return self._idmap.lookup_git_sha(sha)
        except KeyError:
            # if not, see if there are any unconverted revisions and add them 
            # to the map, search for sha in map again
            self._update_sha_map()
            return self._idmap.lookup_git_sha(sha)

    def __getitem__(self, sha):
        (type, type_data) = self._lookup_git_sha(sha)
        # convert object to git object
        if type == "commit":
            return self._get_commit(type_data[0], type_data[1], 
                                    expected_sha=sha)
        elif type == "blob":
            return self._get_blob(type_data[0], type_data[1], expected_sha=sha)
        elif type == "tree":
            return self._get_tree(type_data[0], type_data[1], expected_sha=sha)
        else:
            raise AssertionError("Unknown object type '%s'" % type)
