# Copyright (C) 2009-2018 Jelmer Vernooij <jelmer@jelmer.uk>
# Copyright (C) 2012 Canonical Ltd
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

"""Map from Git sha's to Bazaar objects."""

from __future__ import absolute_import

from dulwich.objects import (
    Blob,
    Commit,
    Tree,
    sha_to_hex,
    ZERO_SHA,
    )
from dulwich.object_store import (
    BaseObjectStore,
    )
from dulwich.pack import (
    pack_objects_to_data,
    PackData,
    Pack,
    )

from ... import (
    errors,
    lru_cache,
    trace,
    osutils,
    ui,
    urlutils,
    )
from ...lock import LogicalLockResult
from ...revision import (
    NULL_REVISION,
    )
from ...testament import(
    StrictTestament3,
    )

from .cache import (
    from_repository as cache_from_repository,
    )
from .mapping import (
    default_mapping,
    entry_mode,
    extract_unusual_modes,
    mapping_registry,
    symlink_to_blob,
    )
from .unpeel_map import (
    UnpeelMap,
    )

import posixpath
import stat


def get_object_store(repo, mapping=None):
    git = getattr(repo, "_git", None)
    if git is not None:
        git.object_store.unlock = lambda: None
        git.object_store.lock_read = lambda: LogicalLockResult(lambda: None)
        git.object_store.lock_write = lambda: LogicalLockResult(lambda: None)
        return git.object_store
    return BazaarObjectStore(repo, mapping)


MAX_TREE_CACHE_SIZE = 50 * 1024 * 1024


class LRUTreeCache(object):

    def __init__(self, repository):
        def approx_tree_size(tree):
            # Very rough estimate, 250 per inventory entry
            try:
                inv = tree.root_inventory
            except AttributeError:
                inv = tree.inventory
            return len(inv) * 250
        self.repository = repository
        self._cache = lru_cache.LRUSizeCache(max_size=MAX_TREE_CACHE_SIZE,
            after_cleanup_size=None, compute_size=approx_tree_size)

    def revision_tree(self, revid):
        try:
            tree = self._cache[revid]
        except KeyError:
            tree = self.repository.revision_tree(revid)
            self.add(tree)
        return tree

    def iter_revision_trees(self, revids):
        trees = {}
        todo = []
        for revid in revids:
            try:
                tree = self._cache[revid]
            except KeyError:
                todo.append(revid)
            else:
                if tree.get_revision_id() != revid:
                    raise AssertionError(
                            "revision id did not match: %s != %s" % (
                                tree.get_revision_id(), revid))
                trees[revid] = tree
        for tree in self.repository.revision_trees(todo):
            trees[tree.get_revision_id()] = tree
            self.add(tree)
        return (trees[r] for r in revids)

    def revision_trees(self, revids):
        return list(self.iter_revision_trees(revids))

    def add(self, tree):
        self._cache[tree.get_revision_id()] = tree


def _find_missing_bzr_revids(graph, want, have):
    """Find the revisions that have to be pushed.

    :param get_parent_map: Function that returns the parents for a sequence
        of revisions.
    :param want: Revisions the target wants
    :param have: Revisions the target already has
    :return: Set of revisions to fetch
    """
    handled = set(have)
    todo = set()
    for rev in want:
        extra_todo = graph.find_unique_ancestors(rev, handled)
        todo.update(extra_todo)
        handled.update(extra_todo)
    if NULL_REVISION in todo:
        todo.remove(NULL_REVISION)
    return todo


def _check_expected_sha(expected_sha, object):
    """Check whether an object matches an expected SHA.

    :param expected_sha: None or expected SHA as either binary or as hex digest
    :param object: Object to verify
    """
    if expected_sha is None:
        return
    if len(expected_sha) == 40:
        if expected_sha != object.sha().hexdigest():
            raise AssertionError("Invalid sha for %r: %s" % (object,
                expected_sha))
    elif len(expected_sha) == 20:
        if expected_sha != object.sha().digest():
            raise AssertionError("Invalid sha for %r: %s" % (object,
                sha_to_hex(expected_sha)))
    else:
        raise AssertionError("Unknown length %d for %r" % (len(expected_sha),
            expected_sha))


def directory_to_tree(path, children, lookup_ie_sha1, unusual_modes, empty_file_name,
                      allow_empty=False):
    """Create a Git Tree object from a Bazaar directory.

    :param path: directory path
    :param children: Children inventory entries
    :param lookup_ie_sha1: Lookup the Git SHA1 for a inventory entry
    :param unusual_modes: Dictionary with unusual file modes by file ids
    :param empty_file_name: Name to use for dummy files in empty directories,
        None to ignore empty directories.
    """
    tree = Tree()
    for value in children:
        child_path = osutils.pathjoin(path, value.name)
        try:
            mode = unusual_modes[child_path]
        except KeyError:
            mode = entry_mode(value)
        hexsha = lookup_ie_sha1(child_path, value)
        if hexsha is not None:
            tree.add(value.name.encode("utf-8"), mode, hexsha)
    if not allow_empty and len(tree) == 0:
        # Only the root can be an empty tree
        if empty_file_name is not None:
            tree.add(empty_file_name, stat.S_IFREG | 0644, Blob().id)
        else:
            return None
    return tree


def _tree_to_objects(tree, parent_trees, idmap, unusual_modes,
                     dummy_file_name=None):
    """Iterate over the objects that were introduced in a revision.

    :param idmap: id map
    :param parent_trees: Parent revision trees
    :param unusual_modes: Unusual file modes dictionary
    :param dummy_file_name: File name to use for dummy files
        in empty directories. None to skip empty directories
    :return: Yields (path, object, ie) entries
    """
    dirty_dirs = set()
    new_blobs = []
    new_contents = {}
    shamap = {}
    try:
        base_tree = parent_trees[0]
        other_parent_trees = parent_trees[1:]
    except IndexError:
        base_tree = tree._repository.revision_tree(NULL_REVISION)
        other_parent_trees = []
    def find_unchanged_parent_ie(file_id, kind, other, parent_trees):
        for ptree in parent_trees:
            try:
                ppath = ptree.id2path(file_id)
            except errors.NoSuchId:
                pass
            else:
                pkind = ptree.kind(ppath, file_id)
                if kind == "file":
                    if (pkind == "file" and
                        ptree.get_file_sha1(ppath, file_id) == other):
                        return (file_id, ptree.get_file_revision(ppath, file_id))
                if kind == "symlink":
                    if (pkind == "symlink" and
                        ptree.get_symlink_target(ppath, file_id) == other):
                        return (file_id, ptree.get_file_revision(ppath, file_id))
        raise KeyError

    # Find all the changed blobs
    for (file_id, path, changed_content, versioned, parent, name, kind,
         executable) in tree.iter_changes(base_tree):
        if kind[1] == "file":
            if changed_content:
                try:
                    (pfile_id, prevision) = find_unchanged_parent_ie(file_id, kind[1], tree.get_file_sha1(path[1], file_id), other_parent_trees)
                except KeyError:
                    pass
                else:
                    try:
                        shamap[path[1]] = idmap.lookup_blob_id(
                            pfile_id, prevision)
                    except KeyError:
                        # no-change merge ?
                        blob = Blob()
                        blob.data = tree.get_file_text(path[1], file_id)
                        shamap[path[1]] = blob.id
            if not path[1] in shamap:
                new_blobs.append((path[1], file_id))
        elif kind[1] == "symlink":
            if changed_content:
                target = tree.get_symlink_target(path[1], file_id)
                blob = symlink_to_blob(target)
                shamap[path[1]] = blob.id
                try:
                    find_unchanged_parent_ie(file_id, kind[1], target, other_parent_trees)
                except KeyError:
                    yield path[1], blob, (file_id, tree.get_file_revision(path[1], file_id))
        elif kind[1] is None:
            shamap[path[1]] = None
        elif kind[1] != 'directory':
            raise AssertionError(kind[1])
        for p in parent:
            if p is None:
                continue
            dirty_dirs.add(tree.id2path(p))

    # Fetch contents of the blobs that were changed
    for (path, file_id), chunks in tree.iter_files_bytes(
        [(path, (path, file_id)) for (path, file_id) in new_blobs]):
        obj = Blob()
        obj.chunked = chunks
        yield path, obj, (file_id, tree.get_file_revision(path, file_id))
        shamap[path] = obj.id

    for path in unusual_modes:
        dirty_dirs.add(posixpath.dirname(path))

    try:
        inv = tree.root_inventory
    except AttributeError:
        inv = tree.inventory

    for dir in list(dirty_dirs):
        for parent in osutils.parent_directories(dir):
            if parent in dirty_dirs:
                break
            dirty_dirs.add(parent)

    def ie_to_hexsha(path, ie):
        # FIXME: Should be the same as in parent
        if ie.kind in ("file", "symlink"):
            try:
                return idmap.lookup_blob_id(ie.file_id, ie.revision)
            except KeyError:
                # no-change merge ?
                blob = Blob()
                blob.data = tree.get_file_text(path, ie.file_id)
                return blob.id
        elif ie.kind == "directory":
            # Not all cache backends store the tree information,
            # calculate again from scratch
            ret = directory_to_tree(path, ie.children.values(), ie_to_hexsha,
                unusual_modes, dummy_file_name, ie.parent_id is None)
            if ret is None:
                return ret
            return ret.id
        else:
            raise AssertionError

    for path in sorted(dirty_dirs, reverse=True):
        if tree.kind(path) != 'directory':
            raise AssertionError

        obj = Tree()
        for value in tree.iter_child_entries(path):
            child_path = osutils.pathjoin(path, value.name)
            try:
                mode = unusual_modes[child_path]
            except KeyError:
                mode = entry_mode(value)
            try:
                hexsha = shamap[child_path]
            except KeyError:
                hexsha = ie_to_hexsha(child_path, value)
            if hexsha is not None:
                obj.add(value.name.encode("utf-8"), mode, hexsha)

        if len(obj) == 0:
            obj = None

        if obj is not None:
            yield path, obj, (tree.path2id(path), tree.get_revision_id())
            shamap[path] = obj.id


class PackTupleIterable(object):

    def __init__(self, store):
        self.store = store
        self.store.lock_read()
        self.objects = {}

    def __del__(self):
        self.store.unlock()

    def add(self, sha, path):
        self.objects[sha] = path

    def __len__(self):
        return len(self.objects)

    def __iter__(self):
        return ((self.store[object_id], path) for (object_id, path) in
                self.objects.iteritems())


class BazaarObjectStore(BaseObjectStore):
    """A Git-style object store backed onto a Bazaar repository."""

    def __init__(self, repository, mapping=None):
        self.repository = repository
        self._map_updated = False
        self._locked = None
        if mapping is None:
            self.mapping = default_mapping
        else:
            self.mapping = mapping
        self._cache = cache_from_repository(repository)
        self._content_cache_types = ("tree",)
        self.start_write_group = self._cache.idmap.start_write_group
        self.abort_write_group = self._cache.idmap.abort_write_group
        self.commit_write_group = self._cache.idmap.commit_write_group
        self.tree_cache = LRUTreeCache(self.repository)
        self.unpeel_map = UnpeelMap.from_repository(self.repository)

    def _missing_revisions(self, revisions):
        return self._cache.idmap.missing_revisions(revisions)

    def _update_sha_map(self, stop_revision=None):
        if not self.is_locked():
            raise errors.LockNotHeld(self)
        if self._map_updated:
            return
        if (stop_revision is not None and
            not self._missing_revisions([stop_revision])):
            return
        graph = self.repository.get_graph()
        if stop_revision is None:
            all_revids = self.repository.all_revision_ids()
            missing_revids = self._missing_revisions(all_revids)
        else:
            heads = set([stop_revision])
            missing_revids = self._missing_revisions(heads)
            while heads:
                parents = graph.get_parent_map(heads)
                todo = set()
                for p in parents.values():
                    todo.update([x for x in p if x not in missing_revids])
                heads = self._missing_revisions(todo)
                missing_revids.update(heads)
        if NULL_REVISION in missing_revids:
            missing_revids.remove(NULL_REVISION)
        missing_revids = self.repository.has_revisions(missing_revids)
        if not missing_revids:
            if stop_revision is None:
                self._map_updated = True
            return
        self.start_write_group()
        try:
            pb = ui.ui_factory.nested_progress_bar()
            try:
                for i, revid in enumerate(graph.iter_topo_order(missing_revids)):
                    trace.mutter('processing %r', revid)
                    pb.update("updating git map", i, len(missing_revids))
                    self._update_sha_map_revision(revid)
            finally:
                pb.finished()
            if stop_revision is None:
                self._map_updated = True
        except:
            self.abort_write_group()
            raise
        else:
            self.commit_write_group()

    def __iter__(self):
        self._update_sha_map()
        return iter(self._cache.idmap.sha1s())

    def _reconstruct_commit(self, rev, tree_sha, lossy, verifiers):
        """Reconstruct a Commit object.

        :param rev: Revision object
        :param tree_sha: SHA1 of the root tree object
        :param lossy: Whether or not to roundtrip bzr metadata
        :param verifiers: Verifiers for the commits
        :return: Commit object
        """
        def parent_lookup(revid):
            try:
                return self._lookup_revision_sha1(revid)
            except errors.NoSuchRevision:
                return None
        return self.mapping.export_commit(rev, tree_sha, parent_lookup,
            lossy, verifiers)

    def _create_fileid_map_blob(self, tree):
        # FIXME: This can probably be a lot more efficient,
        # not all files necessarily have to be processed.
        file_ids = {}
        for (path, ie) in tree.iter_entries_by_dir():
            if self.mapping.generate_file_id(path) != ie.file_id:
                file_ids[path] = ie.file_id
        return self.mapping.export_fileid_map(file_ids)

    def _revision_to_objects(self, rev, tree, lossy):
        """Convert a revision to a set of git objects.

        :param rev: Bazaar revision object
        :param tree: Bazaar revision tree
        :param lossy: Whether to not roundtrip all Bazaar revision data
        """
        unusual_modes = extract_unusual_modes(rev)
        present_parents = self.repository.has_revisions(rev.parent_ids)
        parent_trees = self.tree_cache.revision_trees(
            [p for p in rev.parent_ids if p in present_parents])
        root_tree = None
        for path, obj, bzr_key_data in _tree_to_objects(tree, parent_trees,
                self._cache.idmap, unusual_modes, self.mapping.BZR_DUMMY_FILE):
            if path == "":
                root_tree = obj
                root_key_data = bzr_key_data
                # Don't yield just yet
            else:
                yield path, obj, bzr_key_data
        if root_tree is None:
            # Pointless commit - get the tree sha elsewhere
            if not rev.parent_ids:
                root_tree = Tree()
            else:
                base_sha1 = self._lookup_revision_sha1(rev.parent_ids[0])
                root_tree = self[self[base_sha1].tree]
            root_key_data = (tree.get_root_id(), )
        if not lossy and self.mapping.BZR_FILE_IDS_FILE is not None:
            b = self._create_fileid_map_blob(tree)
            if b is not None:
                root_tree[self.mapping.BZR_FILE_IDS_FILE] = (
                    (stat.S_IFREG | 0644), b.id)
                yield self.mapping.BZR_FILE_IDS_FILE, b, None
        yield "", root_tree, root_key_data
        if not lossy:
            testament3 = StrictTestament3(rev, tree)
            verifiers = { "testament3-sha1": testament3.as_sha1() }
        else:
            verifiers = {}
        commit_obj = self._reconstruct_commit(rev, root_tree.id,
            lossy=lossy, verifiers=verifiers)
        try:
            foreign_revid, mapping = mapping_registry.parse_revision_id(
                rev.revision_id)
        except errors.InvalidRevisionId:
            pass
        else:
            _check_expected_sha(foreign_revid, commit_obj)
        yield None, commit_obj, None

    def _get_updater(self, rev):
        return self._cache.get_updater(rev)

    def _update_sha_map_revision(self, revid):
        rev = self.repository.get_revision(revid)
        tree = self.tree_cache.revision_tree(rev.revision_id)
        updater = self._get_updater(rev)
        # FIXME JRV 2011-12-15: Shouldn't we try both values for lossy ?
        for path, obj, ie in self._revision_to_objects(rev, tree, lossy=(not self.mapping.roundtripping)):
            if isinstance(obj, Commit):
                testament3 = StrictTestament3(rev, tree)
                ie = { "testament3-sha1": testament3.as_sha1() }
            updater.add_object(obj, ie, path)
        commit_obj = updater.finish()
        return commit_obj.id

    def _reconstruct_blobs(self, keys):
        """Return a Git Blob object from a fileid and revision stored in bzr.

        :param fileid: File id of the text
        :param revision: Revision of the text
        """
        stream = self.repository.iter_files_bytes(
            ((key[0], key[1], key) for key in keys))
        for (file_id, revision, expected_sha), chunks in stream:
            blob = Blob()
            blob.chunked = chunks
            if blob.id != expected_sha and blob.data == "":
                # Perhaps it's a symlink ?
                tree = self.tree_cache.revision_tree(revision)
                path = tree.id2path(file_id)
                if tree.kind(path, file_id) == 'symlink':
                    blob = symlink_to_blob(tree.get_symlink_target(path, file_id))
            _check_expected_sha(expected_sha, blob)
            yield blob

    def _reconstruct_tree(self, fileid, revid, bzr_tree, unusual_modes,
        expected_sha=None):
        """Return a Git Tree object from a file id and a revision stored in bzr.

        :param fileid: fileid in the tree.
        :param revision: Revision of the tree.
        """
        def get_ie_sha1(path, entry):
            if entry.kind == "directory":
                try:
                    return self._cache.idmap.lookup_tree_id(entry.file_id,
                        revid)
                except (NotImplementedError, KeyError):
                    obj = self._reconstruct_tree(entry.file_id, revid, bzr_tree,
                        unusual_modes)
                    if obj is None:
                        return None
                    else:
                        return obj.id
            elif entry.kind in ("file", "symlink"):
                try:
                    return self._cache.idmap.lookup_blob_id(entry.file_id,
                        entry.revision)
                except KeyError:
                    # no-change merge?
                    return self._reconstruct_blobs(
                        [(entry.file_id, entry.revision, None)]).next().id
            elif entry.kind == 'tree-reference':
                # FIXME: Make sure the file id is the root id
                return self._lookup_revision_sha1(entry.reference_revision)
            else:
                raise AssertionError("unknown entry kind '%s'" % entry.kind)
        try:
            inv = bzr_tree.root_inventory
        except AttributeError:
            inv = bzr_tree.inventory
        path = bzr_tree.id2path(fileid)
        tree = directory_to_tree(
                path,
                bzr_tree.iter_child_entries(path),
                get_ie_sha1, unusual_modes, self.mapping.BZR_DUMMY_FILE,
                bzr_tree.get_root_id() == fileid)
        if (bzr_tree.get_root_id() == fileid and
            self.mapping.BZR_FILE_IDS_FILE is not None):
            if tree is None:
                tree = Tree()
            b = self._create_fileid_map_blob(bzr_tree)
            # If this is the root tree, add the file ids
            tree[self.mapping.BZR_FILE_IDS_FILE] = (
                (stat.S_IFREG | 0644), b.id)
        if tree is not None:
            _check_expected_sha(expected_sha, tree)
        return tree

    def get_parents(self, sha):
        """Retrieve the parents of a Git commit by SHA1.

        :param sha: SHA1 of the commit
        :raises: KeyError, NotCommitError
        """
        return self[sha].parents

    def _lookup_revision_sha1(self, revid):
        """Return the SHA1 matching a Bazaar revision."""
        if revid == NULL_REVISION:
            return ZERO_SHA
        try:
            return self._cache.idmap.lookup_commit(revid)
        except KeyError:
            try:
                return mapping_registry.parse_revision_id(revid)[0]
            except errors.InvalidRevisionId:
                self._update_sha_map(revid)
                return self._cache.idmap.lookup_commit(revid)

    def get_raw(self, sha):
        """Get the raw representation of a Git object by SHA1.

        :param sha: SHA1 of the git object
        """
        if len(sha) == 20:
            sha = sha_to_hex(sha)
        obj = self[sha]
        return (obj.type, obj.as_raw_string())

    def __contains__(self, sha):
        # See if sha is in map
        try:
            for (type, type_data) in self.lookup_git_sha(sha):
                if type == "commit":
                    if self.repository.has_revision(type_data[0]):
                        return True
                elif type == "blob":
                    if type_data in self.repository.texts:
                        return True
                elif type == "tree":
                    if self.repository.has_revision(type_data[1]):
                        return True
                else:
                    raise AssertionError("Unknown object type '%s'" % type)
            else:
                return False
        except KeyError:
            return False

    def lock_read(self):
        self._locked = 'r'
        self._map_updated = False
        self.repository.lock_read()
        return LogicalLockResult(self.unlock)

    def lock_write(self):
        self._locked = 'r'
        self._map_updated = False
        self.repository.lock_write()
        return LogicalLockResult(self.unlock)

    def is_locked(self):
        return (self._locked is not None)

    def unlock(self):
        self._locked = None
        self._map_updated = False
        self.repository.unlock()

    def lookup_git_shas(self, shas):
        ret = {}
        for sha in shas:
            if sha == ZERO_SHA:
                ret[sha] = [("commit", (NULL_REVISION, None, {}))]
                continue
            try:
                ret[sha] = list(self._cache.idmap.lookup_git_sha(sha))
            except KeyError:
                # if not, see if there are any unconverted revisions and
                # add them to the map, search for sha in map again
                self._update_sha_map()
                try:
                    ret[sha] = list(self._cache.idmap.lookup_git_sha(sha))
                except KeyError:
                    pass
        return ret

    def lookup_git_sha(self, sha):
        return self.lookup_git_shas([sha])[sha]

    def __getitem__(self, sha):
        if self._cache.content_cache is not None:
            try:
                return self._cache.content_cache[sha]
            except KeyError:
                pass
        for (kind, type_data) in self.lookup_git_sha(sha):
            # convert object to git object
            if kind == "commit":
                (revid, tree_sha, verifiers) = type_data
                try:
                    rev = self.repository.get_revision(revid)
                except errors.NoSuchRevision:
                    if revid == NULL_REVISION:
                        raise AssertionError(
                            "should not try to look up NULL_REVISION")
                    trace.mutter('entry for %s %s in shamap: %r, but not '
                                 'found in repository', kind, sha, type_data)
                    raise KeyError(sha)
                # FIXME: the type data should say whether conversion was lossless
                commit = self._reconstruct_commit(rev, tree_sha,
                    lossy=(not self.mapping.roundtripping), verifiers=verifiers)
                _check_expected_sha(sha, commit)
                return commit
            elif kind == "blob":
                (fileid, revision) = type_data
                blobs = self._reconstruct_blobs([(fileid, revision, sha)])
                return blobs.next()
            elif kind == "tree":
                (fileid, revid) = type_data
                try:
                    tree = self.tree_cache.revision_tree(revid)
                    rev = self.repository.get_revision(revid)
                except errors.NoSuchRevision:
                    trace.mutter('entry for %s %s in shamap: %r, but not found in '
                        'repository', kind, sha, type_data)
                    raise KeyError(sha)
                unusual_modes = extract_unusual_modes(rev)
                try:
                    return self._reconstruct_tree(fileid, revid,
                        tree, unusual_modes, expected_sha=sha)
                except errors.NoSuchRevision:
                    raise KeyError(sha)
            else:
                raise AssertionError("Unknown object type '%s'" % kind)
        else:
            raise KeyError(sha)

    def generate_lossy_pack_data(self, have, want, progress=None,
            get_tagged=None, ofs_delta=False):
        return pack_objects_to_data(
                self.generate_pack_contents(have, want, progress, get_tagged,
            lossy=True))

    def generate_pack_contents(self, have, want, progress=None,
            ofs_delta=False, get_tagged=None, lossy=False):
        """Iterate over the contents of a pack file.

        :param have: List of SHA1s of objects that should not be sent
        :param want: List of SHA1s of objects that should be sent
        """
        processed = set()
        ret = self.lookup_git_shas(have + want)
        for commit_sha in have:
            commit_sha = self.unpeel_map.peel_tag(commit_sha, commit_sha)
            try:
                for (type, type_data) in ret[commit_sha]:
                    if type != "commit":
                        raise AssertionError("Type was %s, not commit" % type)
                    processed.add(type_data[0])
            except KeyError:
                trace.mutter("unable to find remote ref %s", commit_sha)
        pending = set()
        for commit_sha in want:
            if commit_sha in have:
                continue
            try:
                for (type, type_data) in ret[commit_sha]:
                    if type != "commit":
                        raise AssertionError("Type was %s, not commit" % type)
                    pending.add(type_data[0])
            except KeyError:
                pass

        graph = self.repository.get_graph()
        todo = _find_missing_bzr_revids(graph, pending, processed)
        ret = PackTupleIterable(self)
        pb = ui.ui_factory.nested_progress_bar()
        try:
            for i, revid in enumerate(todo):
                pb.update("generating git objects", i, len(todo))
                try:
                    rev = self.repository.get_revision(revid)
                except errors.NoSuchRevision:
                    continue
                tree = self.tree_cache.revision_tree(revid)
                for path, obj, ie in self._revision_to_objects(rev, tree, lossy=lossy):
                    ret.add(obj.id, path)
            return ret
        finally:
            pb.finished()

    def add_thin_pack(self):
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".pack")
        f = os.fdopen(fd, 'wb')
        def commit():
            from .fetch import import_git_objects
            os.fsync(fd)
            f.close()
            if os.path.getsize(path) == 0:
                return
            pd = PackData(path)
            pd.create_index_v2(path[:-5]+".idx", self.object_store.get_raw)

            p = Pack(path[:-5])
            with self.repository.lock_write():
                self.repository.start_write_group()
                try:
                    import_git_objects(self.repository, self.mapping,
                        p.iterobjects(get_raw=self.get_raw),
                        self.object_store)
                except:
                    self.repository.abort_write_group()
                    raise
                else:
                    self.repository.commit_write_group()
        return f, commit

    # The pack isn't kept around anyway, so no point
    # in treating full packs different from thin packs
    add_pack = add_thin_pack
