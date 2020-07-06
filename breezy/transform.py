# Copyright (C) 2006-2011 Canonical Ltd
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

from __future__ import absolute_import

import os
import errno
from stat import S_ISREG, S_IEXEC
import time

from . import (
    config as _mod_config,
    controldir,
    errors,
    lazy_import,
    osutils,
    registry,
    trace,
    )
lazy_import.lazy_import(globals(), """
from breezy import (
    cleanup,
    conflicts,
    multiparent,
    revision as _mod_revision,
    ui,
    urlutils,
    )
from breezy.i18n import gettext
""")

from .errors import (DuplicateKey,
                     BzrError, InternalBzrError)
from .filters import filtered_output_bytes, ContentFilterContext
from .mutabletree import MutableTree
from .osutils import (
    delete_any,
    file_kind,
    pathjoin,
    sha_file,
    splitpath,
    supports_symlinks,
    )
from .progress import ProgressPhase
from .sixish import (
    text_type,
    viewitems,
    viewvalues,
    )
from .tree import (
    InterTree,
    TreeChange,
    find_previous_path,
    )


ROOT_PARENT = "root-parent"


class NoFinalPath(BzrError):

    _fmt = ("No final name for trans_id %(trans_id)r\n"
            "file-id: %(file_id)r\n"
            "root trans-id: %(root_trans_id)r\n")

    def __init__(self, trans_id, transform):
        self.trans_id = trans_id
        self.file_id = transform.final_file_id(trans_id)
        self.root_trans_id = transform.root


class ReusingTransform(BzrError):

    _fmt = "Attempt to reuse a transform that has already been applied."


class MalformedTransform(InternalBzrError):

    _fmt = "Tree transform is malformed %(conflicts)r"


class CantMoveRoot(BzrError):

    _fmt = "Moving the root directory is not supported at this time"


class ImmortalLimbo(BzrError):

    _fmt = """Unable to delete transform temporary directory %(limbo_dir)s.
    Please examine %(limbo_dir)s to see if it contains any files you wish to
    keep, and delete it when you are done."""

    def __init__(self, limbo_dir):
        BzrError.__init__(self)
        self.limbo_dir = limbo_dir


class TransformRenameFailed(BzrError):

    _fmt = "Failed to rename %(from_path)s to %(to_path)s: %(why)s"

    def __init__(self, from_path, to_path, why, errno):
        self.from_path = from_path
        self.to_path = to_path
        self.why = why
        self.errno = errno


def unique_add(map, key, value):
    if key in map:
        raise DuplicateKey(key=key)
    map[key] = value


class _TransformResults(object):

    def __init__(self, modified_paths, rename_count):
        object.__init__(self)
        self.modified_paths = modified_paths
        self.rename_count = rename_count


class TreeTransform(object):
    """Represent a tree transformation.

    This object is designed to support incremental generation of the transform,
    in any order.

    However, it gives optimum performance when parent directories are created
    before their contents.  The transform is then able to put child files
    directly in their parent directory, avoiding later renames.

    It is easy to produce malformed transforms, but they are generally
    harmless.  Attempting to apply a malformed transform will cause an
    exception to be raised before any modifications are made to the tree.

    Many kinds of malformed transforms can be corrected with the
    resolve_conflicts function.  The remaining ones indicate programming error,
    such as trying to create a file with no path.

    Two sets of file creation methods are supplied.  Convenience methods are:
     * new_file
     * new_directory
     * new_symlink

    These are composed of the low-level methods:
     * create_path
     * create_file or create_directory or create_symlink
     * version_file
     * set_executability

    Transform/Transaction ids
    -------------------------
    trans_ids are temporary ids assigned to all files involved in a transform.
    It's possible, even common, that not all files in the Tree have trans_ids.

    trans_ids are only valid for the TreeTransform that generated them.
    """

    def __init__(self, tree, pb=None):
        self._tree = tree
        # A progress bar
        self._pb = pb
        self._id_number = 0
        # Mapping of path in old tree -> trans_id
        self._tree_path_ids = {}
        # Mapping trans_id -> path in old tree
        self._tree_id_paths = {}
        # mapping of trans_id -> new basename
        self._new_name = {}
        # mapping of trans_id -> new parent trans_id
        self._new_parent = {}
        # mapping of trans_id with new contents -> new file_kind
        self._new_contents = {}
        # Set of trans_ids whose contents will be removed
        self._removed_contents = set()
        # Mapping of trans_id -> new execute-bit value
        self._new_executability = {}
        # Mapping of trans_id -> new tree-reference value
        self._new_reference_revision = {}
        # Set of trans_ids that will be removed
        self._removed_id = set()
        # Indicator of whether the transform has been applied
        self._done = False

    def __enter__(self):
        """Support Context Manager API."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Support Context Manager API."""
        self.finalize()

    def iter_tree_children(self, trans_id):
        """Iterate through the entry's tree children, if any.

        :param trans_id: trans id to iterate
        :returns: Iterator over paths
        """
        raise NotImplementedError(self.iter_tree_children)

    def canonical_path(self, path):
        return path

    def tree_kind(self, trans_id):
        raise NotImplementedError(self.tree_kind)

    def by_parent(self):
        """Return a map of parent: children for known parents.

        Only new paths and parents of tree files with assigned ids are used.
        """
        by_parent = {}
        items = list(viewitems(self._new_parent))
        items.extend((t, self.final_parent(t))
                     for t in list(self._tree_id_paths))
        for trans_id, parent_id in items:
            if parent_id not in by_parent:
                by_parent[parent_id] = set()
            by_parent[parent_id].add(trans_id)
        return by_parent

    def finalize(self):
        """Release the working tree lock, if held.

        This is required if apply has not been invoked, but can be invoked
        even after apply.
        """
        raise NotImplementedError(self.finalize)

    def create_path(self, name, parent):
        """Assign a transaction id to a new path"""
        trans_id = self._assign_id()
        unique_add(self._new_name, trans_id, name)
        unique_add(self._new_parent, trans_id, parent)
        return trans_id

    def adjust_path(self, name, parent, trans_id):
        """Change the path that is assigned to a transaction id."""
        if parent is None:
            raise ValueError("Parent trans-id may not be None")
        if trans_id == self._new_root:
            raise CantMoveRoot
        self._new_name[trans_id] = name
        self._new_parent[trans_id] = parent

    def adjust_root_path(self, name, parent):
        """Emulate moving the root by moving all children, instead.

        We do this by undoing the association of root's transaction id with the
        current tree.  This allows us to create a new directory with that
        transaction id.  We unversion the root directory and version the
        physically new directory, and hope someone versions the tree root
        later.
        """
        raise NotImplementedError(self.adjust_root_path)

    def fixup_new_roots(self):
        """Reinterpret requests to change the root directory

        Instead of creating a root directory, or moving an existing directory,
        all the attributes and children of the new root are applied to the
        existing root directory.

        This means that the old root trans-id becomes obsolete, so it is
        recommended only to invoke this after the root trans-id has become
        irrelevant.
        """
        raise NotImplementedError(self.fixup_new_roots)

    def _assign_id(self):
        """Produce a new tranform id"""
        new_id = "new-%s" % self._id_number
        self._id_number += 1
        return new_id

    def trans_id_tree_path(self, path):
        """Determine (and maybe set) the transaction ID for a tree path."""
        path = self.canonical_path(path)
        if path not in self._tree_path_ids:
            self._tree_path_ids[path] = self._assign_id()
            self._tree_id_paths[self._tree_path_ids[path]] = path
        return self._tree_path_ids[path]

    def get_tree_parent(self, trans_id):
        """Determine id of the parent in the tree."""
        path = self._tree_id_paths[trans_id]
        if path == "":
            return ROOT_PARENT
        return self.trans_id_tree_path(os.path.dirname(path))

    def delete_contents(self, trans_id):
        """Schedule the contents of a path entry for deletion"""
        kind = self.tree_kind(trans_id)
        if kind is not None:
            self._removed_contents.add(trans_id)

    def cancel_deletion(self, trans_id):
        """Cancel a scheduled deletion"""
        self._removed_contents.remove(trans_id)

    def delete_versioned(self, trans_id):
        """Delete and unversion a versioned file"""
        self.delete_contents(trans_id)
        self.unversion_file(trans_id)

    def set_executability(self, executability, trans_id):
        """Schedule setting of the 'execute' bit
        To unschedule, set to None
        """
        if executability is None:
            del self._new_executability[trans_id]
        else:
            unique_add(self._new_executability, trans_id, executability)

    def set_tree_reference(self, revision_id, trans_id):
        """Set the reference associated with a directory"""
        unique_add(self._new_reference_revision, trans_id, revision_id)

    def version_file(self, trans_id, file_id=None):
        """Schedule a file to become versioned."""
        raise NotImplementedError(self.version_file)

    def cancel_versioning(self, trans_id):
        """Undo a previous versioning of a file"""
        raise NotImplementedError(self.cancel_versioning)

    def unversion_file(self, trans_id):
        """Schedule a path entry to become unversioned"""
        self._removed_id.add(trans_id)

    def new_paths(self, filesystem_only=False):
        """Determine the paths of all new and changed files.

        :param filesystem_only: if True, only calculate values for files
            that require renames or execute bit changes.
        """
        raise NotImplementedError(self.new_paths)

    def final_kind(self, trans_id):
        """Determine the final file kind, after any changes applied.

        :return: None if the file does not exist/has no contents.  (It is
            conceivable that a path would be created without the corresponding
            contents insertion command)
        """
        if trans_id in self._new_contents:
            return self._new_contents[trans_id]
        elif trans_id in self._removed_contents:
            return None
        else:
            return self.tree_kind(trans_id)

    def tree_path(self, trans_id):
        """Determine the tree path associated with the trans_id."""
        return self._tree_id_paths.get(trans_id)

    def final_is_versioned(self, trans_id):
        raise NotImplementedError(self.final_is_versioned)

    def final_parent(self, trans_id):
        """Determine the parent file_id, after any changes are applied.

        ROOT_PARENT is returned for the tree root.
        """
        try:
            return self._new_parent[trans_id]
        except KeyError:
            return self.get_tree_parent(trans_id)

    def final_name(self, trans_id):
        """Determine the final filename, after all changes are applied."""
        try:
            return self._new_name[trans_id]
        except KeyError:
            try:
                return os.path.basename(self._tree_id_paths[trans_id])
            except KeyError:
                raise NoFinalPath(trans_id, self)

    def path_changed(self, trans_id):
        """Return True if a trans_id's path has changed."""
        return (trans_id in self._new_name) or (trans_id in self._new_parent)

    def new_contents(self, trans_id):
        return (trans_id in self._new_contents)

    def find_conflicts(self):
        """Find any violations of inventory or filesystem invariants"""
        raise NotImplementedError(self.find_conflicts)

    def new_file(self, name, parent_id, contents, file_id=None,
                 executable=None, sha1=None):
        """Convenience method to create files.

        name is the name of the file to create.
        parent_id is the transaction id of the parent directory of the file.
        contents is an iterator of bytestrings, which will be used to produce
        the file.
        :param file_id: The inventory ID of the file, if it is to be versioned.
        :param executable: Only valid when a file_id has been supplied.
        """
        raise NotImplementedError(self.new_file)

    def new_directory(self, name, parent_id, file_id=None):
        """Convenience method to create directories.

        name is the name of the directory to create.
        parent_id is the transaction id of the parent directory of the
        directory.
        file_id is the inventory ID of the directory, if it is to be versioned.
        """
        raise NotImplementedError(self.new_directory)

    def new_symlink(self, name, parent_id, target, file_id=None):
        """Convenience method to create symbolic link.

        name is the name of the symlink to create.
        parent_id is the transaction id of the parent directory of the symlink.
        target is a bytestring of the target of the symlink.
        file_id is the inventory ID of the file, if it is to be versioned.
        """
        raise NotImplementedError(self.new_symlink)

    def new_orphan(self, trans_id, parent_id):
        """Schedule an item to be orphaned.

        When a directory is about to be removed, its children, if they are not
        versioned are moved out of the way: they don't have a parent anymore.

        :param trans_id: The trans_id of the existing item.
        :param parent_id: The parent trans_id of the item.
        """
        raise NotImplementedError(self.new_orphan)

    def iter_changes(self):
        """Produce output in the same format as Tree.iter_changes.

        Will produce nonsensical results if invoked while inventory/filesystem
        conflicts (as reported by TreeTransform.find_conflicts()) are present.

        This reads the Transform, but only reproduces changes involving a
        file_id.  Files that are not versioned in either of the FROM or TO
        states are not reflected.
        """
        raise NotImplementedError(self.iter_changes)

    def get_preview_tree(self):
        """Return a tree representing the result of the transform.

        The tree is a snapshot, and altering the TreeTransform will invalidate
        it.
        """
        raise NotImplementedError(self.get_preview_tree)

    def commit(self, branch, message, merge_parents=None, strict=False,
               timestamp=None, timezone=None, committer=None, authors=None,
               revprops=None, revision_id=None):
        """Commit the result of this TreeTransform to a branch.

        :param branch: The branch to commit to.
        :param message: The message to attach to the commit.
        :param merge_parents: Additional parent revision-ids specified by
            pending merges.
        :param strict: If True, abort the commit if there are unversioned
            files.
        :param timestamp: if not None, seconds-since-epoch for the time and
            date.  (May be a float.)
        :param timezone: Optional timezone for timestamp, as an offset in
            seconds.
        :param committer: Optional committer in email-id format.
            (e.g. "J Random Hacker <jrandom@example.com>")
        :param authors: Optional list of authors in email-id format.
        :param revprops: Optional dictionary of revision properties.
        :param revision_id: Optional revision id.  (Specifying a revision-id
            may reduce performance for some non-native formats.)
        :return: The revision_id of the revision committed.
        """
        raise NotImplementedError(self.commit)

    def create_file(self, contents, trans_id, mode_id=None, sha1=None):
        """Schedule creation of a new file.

        :seealso: new_file.

        :param contents: an iterator of strings, all of which will be written
            to the target destination.
        :param trans_id: TreeTransform handle
        :param mode_id: If not None, force the mode of the target file to match
            the mode of the object referenced by mode_id.
            Otherwise, we will try to preserve mode bits of an existing file.
        :param sha1: If the sha1 of this content is already known, pass it in.
            We can use it to prevent future sha1 computations.
        """
        raise NotImplementedError(self.create_file)

    def create_directory(self, trans_id):
        """Schedule creation of a new directory.

        See also new_directory.
        """
        raise NotImplementedError(self.create_directory)

    def create_symlink(self, target, trans_id):
        """Schedule creation of a new symbolic link.

        target is a bytestring.
        See also new_symlink.
        """
        raise NotImplementedError(self.create_symlink)

    def create_hardlink(self, path, trans_id):
        """Schedule creation of a hard link"""
        raise NotImplementedError(self.create_hardlink)

    def cancel_creation(self, trans_id):
        """Cancel the creation of new file contents."""
        raise NotImplementedError(self.cancel_creation)


class OrphaningError(errors.BzrError):

    # Only bugs could lead to such exception being seen by the user
    internal_error = True
    _fmt = "Error while orphaning %s in %s directory"

    def __init__(self, orphan, parent):
        errors.BzrError.__init__(self)
        self.orphan = orphan
        self.parent = parent


class OrphaningForbidden(OrphaningError):

    _fmt = "Policy: %s doesn't allow creating orphans."

    def __init__(self, policy):
        errors.BzrError.__init__(self)
        self.policy = policy


def move_orphan(tt, orphan_id, parent_id):
    """See TreeTransformBase.new_orphan.

    This creates a new orphan in the `brz-orphans` dir at the root of the
    `TreeTransform`.

    :param tt: The TreeTransform orphaning `trans_id`.

    :param orphan_id: The trans id that should be orphaned.

    :param parent_id: The orphan parent trans id.
    """
    # Add the orphan dir if it doesn't exist
    orphan_dir_basename = 'brz-orphans'
    od_id = tt.trans_id_tree_path(orphan_dir_basename)
    if tt.final_kind(od_id) is None:
        tt.create_directory(od_id)
    parent_path = tt._tree_id_paths[parent_id]
    # Find a name that doesn't exist yet in the orphan dir
    actual_name = tt.final_name(orphan_id)
    new_name = tt._available_backup_name(actual_name, od_id)
    tt.adjust_path(new_name, od_id, orphan_id)
    trace.warning('%s has been orphaned in %s'
                  % (joinpath(parent_path, actual_name), orphan_dir_basename))


def refuse_orphan(tt, orphan_id, parent_id):
    """See TreeTransformBase.new_orphan.

    This refuses to create orphan, letting the caller handle the conflict.
    """
    raise OrphaningForbidden('never')


orphaning_registry = registry.Registry()
orphaning_registry.register(
    u'conflict', refuse_orphan,
    'Leave orphans in place and create a conflict on the directory.')
orphaning_registry.register(
    u'move', move_orphan,
    'Move orphans into the brz-orphans directory.')
orphaning_registry._set_default_key(u'conflict')


opt_transform_orphan = _mod_config.RegistryOption(
    'transform.orphan_policy', orphaning_registry,
    help='Policy for orphaned files during transform operations.',
    invalid='warning')


def joinpath(parent, child):
    """Join tree-relative paths, handling the tree root specially"""
    if parent is None or parent == "":
        return child
    else:
        return pathjoin(parent, child)


class FinalPaths(object):
    """Make path calculation cheap by memoizing paths.

    The underlying tree must not be manipulated between calls, or else
    the results will likely be incorrect.
    """

    def __init__(self, transform):
        object.__init__(self)
        self._known_paths = {}
        self.transform = transform

    def _determine_path(self, trans_id):
        if (trans_id == self.transform.root or trans_id == ROOT_PARENT):
            return u""
        name = self.transform.final_name(trans_id)
        parent_id = self.transform.final_parent(trans_id)
        if parent_id == self.transform.root:
            return name
        else:
            return pathjoin(self.get_path(parent_id), name)

    def get_path(self, trans_id):
        """Find the final path associated with a trans_id"""
        if trans_id not in self._known_paths:
            self._known_paths[trans_id] = self._determine_path(trans_id)
        return self._known_paths[trans_id]

    def get_paths(self, trans_ids):
        return [(self.get_path(t), t) for t in trans_ids]


def build_tree(tree, wt, accelerator_tree=None, hardlink=False,
               delta_from_tree=False):
    """Create working tree for a branch, using a TreeTransform.

    This function should be used on empty trees, having a tree root at most.
    (see merge and revert functionality for working with existing trees)

    Existing files are handled like so:

    - Existing bzrdirs take precedence over creating new items.  They are
      created as '%s.diverted' % name.
    - Otherwise, if the content on disk matches the content we are building,
      it is silently replaced.
    - Otherwise, conflict resolution will move the old file to 'oldname.moved'.

    :param tree: The tree to convert wt into a copy of
    :param wt: The working tree that files will be placed into
    :param accelerator_tree: A tree which can be used for retrieving file
        contents more quickly than tree itself, i.e. a workingtree.  tree
        will be used for cases where accelerator_tree's content is different.
    :param hardlink: If true, hard-link files to accelerator_tree, where
        possible.  accelerator_tree must implement abspath, i.e. be a
        working tree.
    :param delta_from_tree: If true, build_tree may use the input Tree to
        generate the inventory delta.
    """
    with cleanup.ExitStack() as exit_stack:
        exit_stack.enter_context(wt.lock_tree_write())
        exit_stack.enter_context(tree.lock_read())
        if accelerator_tree is not None:
            exit_stack.enter_context(accelerator_tree.lock_read())
        return _build_tree(tree, wt, accelerator_tree, hardlink,
                           delta_from_tree)


def _build_tree(tree, wt, accelerator_tree, hardlink, delta_from_tree):
    """See build_tree."""
    for num, _unused in enumerate(wt.all_versioned_paths()):
        if num > 0:  # more than just a root
            raise errors.WorkingTreeAlreadyPopulated(base=wt.basedir)
    file_trans_id = {}
    top_pb = ui.ui_factory.nested_progress_bar()
    pp = ProgressPhase("Build phase", 2, top_pb)
    if tree.path2id('') is not None:
        # This is kind of a hack: we should be altering the root
        # as part of the regular tree shape diff logic.
        # The conditional test here is to avoid doing an
        # expensive operation (flush) every time the root id
        # is set within the tree, nor setting the root and thus
        # marking the tree as dirty, because we use two different
        # idioms here: tree interfaces and inventory interfaces.
        if wt.path2id('') != tree.path2id(''):
            wt.set_root_id(tree.path2id(''))
            wt.flush()
    tt = wt.transform()
    divert = set()
    try:
        pp.next_phase()
        file_trans_id[find_previous_path(wt, tree, '')] = tt.trans_id_tree_path('')
        with ui.ui_factory.nested_progress_bar() as pb:
            deferred_contents = []
            num = 0
            total = len(tree.all_versioned_paths())
            if delta_from_tree:
                precomputed_delta = []
            else:
                precomputed_delta = None
            # Check if tree inventory has content. If so, we populate
            # existing_files with the directory content. If there are no
            # entries we skip populating existing_files as its not used.
            # This improves performance and unncessary work on large
            # directory trees. (#501307)
            if total > 0:
                existing_files = set()
                for dir, files in wt.walkdirs():
                    existing_files.update(f[0] for f in files)
            for num, (tree_path, entry) in \
                    enumerate(tree.iter_entries_by_dir()):
                pb.update(gettext("Building tree"), num
                          - len(deferred_contents), total)
                if entry.parent_id is None:
                    continue
                reparent = False
                file_id = entry.file_id
                if delta_from_tree:
                    precomputed_delta.append((None, tree_path, file_id, entry))
                if tree_path in existing_files:
                    target_path = wt.abspath(tree_path)
                    kind = file_kind(target_path)
                    if kind == "directory":
                        try:
                            controldir.ControlDir.open(target_path)
                        except errors.NotBranchError:
                            pass
                        else:
                            divert.add(tree_path)
                    if (tree_path not in divert
                        and _content_match(
                            tree, entry, tree_path, kind, target_path)):
                        tt.delete_contents(tt.trans_id_tree_path(tree_path))
                        if kind == 'directory':
                            reparent = True
                parent_id = file_trans_id[osutils.dirname(tree_path)]
                if entry.kind == 'file':
                    # We *almost* replicate new_by_entry, so that we can defer
                    # getting the file text, and get them all at once.
                    trans_id = tt.create_path(entry.name, parent_id)
                    file_trans_id[tree_path] = trans_id
                    tt.version_file(trans_id, file_id=file_id)
                    executable = tree.is_executable(tree_path)
                    if executable:
                        tt.set_executability(executable, trans_id)
                    trans_data = (trans_id, tree_path, entry.text_sha1)
                    deferred_contents.append((tree_path, trans_data))
                else:
                    file_trans_id[tree_path] = new_by_entry(
                        tree_path, tt, entry, parent_id, tree)
                if reparent:
                    new_trans_id = file_trans_id[tree_path]
                    old_parent = tt.trans_id_tree_path(tree_path)
                    _reparent_children(tt, old_parent, new_trans_id)
            offset = num + 1 - len(deferred_contents)
            _create_files(tt, tree, deferred_contents, pb, offset,
                          accelerator_tree, hardlink)
        pp.next_phase()
        divert_trans = set(file_trans_id[f] for f in divert)

        def resolver(t, c):
            return resolve_checkout(t, c, divert_trans)
        raw_conflicts = resolve_conflicts(tt, pass_func=resolver)
        if len(raw_conflicts) > 0:
            precomputed_delta = None
        conflicts = cook_conflicts(raw_conflicts, tt)
        for conflict in conflicts:
            trace.warning(text_type(conflict))
        try:
            wt.add_conflicts(conflicts)
        except errors.UnsupportedOperation:
            pass
        result = tt.apply(no_conflicts=True,
                          precomputed_delta=precomputed_delta)
    finally:
        tt.finalize()
        top_pb.finished()
    return result


def _create_files(tt, tree, desired_files, pb, offset, accelerator_tree,
                  hardlink):
    total = len(desired_files) + offset
    wt = tt._tree
    if accelerator_tree is None:
        new_desired_files = desired_files
    else:
        iter = accelerator_tree.iter_changes(tree, include_unchanged=True)
        unchanged = [
            change.path for change in iter
            if not (change.changed_content or change.executable[0] != change.executable[1])]
        if accelerator_tree.supports_content_filtering():
            unchanged = [(tp, ap) for (tp, ap) in unchanged
                         if not next(accelerator_tree.iter_search_rules([ap]))]
        unchanged = dict(unchanged)
        new_desired_files = []
        count = 0
        for unused_tree_path, (trans_id, tree_path, text_sha1) in desired_files:
            accelerator_path = unchanged.get(tree_path)
            if accelerator_path is None:
                new_desired_files.append((tree_path,
                                          (trans_id, tree_path, text_sha1)))
                continue
            pb.update(gettext('Adding file contents'), count + offset, total)
            if hardlink:
                tt.create_hardlink(accelerator_tree.abspath(accelerator_path),
                                   trans_id)
            else:
                with accelerator_tree.get_file(accelerator_path) as f:
                    chunks = osutils.file_iterator(f)
                    if wt.supports_content_filtering():
                        filters = wt._content_filter_stack(tree_path)
                        chunks = filtered_output_bytes(chunks, filters,
                                                       ContentFilterContext(tree_path, tree))
                    tt.create_file(chunks, trans_id, sha1=text_sha1)
            count += 1
        offset += count
    for count, ((trans_id, tree_path, text_sha1), contents) in enumerate(
            tree.iter_files_bytes(new_desired_files)):
        if wt.supports_content_filtering():
            filters = wt._content_filter_stack(tree_path)
            contents = filtered_output_bytes(contents, filters,
                                             ContentFilterContext(tree_path, tree))
        tt.create_file(contents, trans_id, sha1=text_sha1)
        pb.update(gettext('Adding file contents'), count + offset, total)


def _reparent_children(tt, old_parent, new_parent):
    for child in tt.iter_tree_children(old_parent):
        tt.adjust_path(tt.final_name(child), new_parent, child)


def _reparent_transform_children(tt, old_parent, new_parent):
    by_parent = tt.by_parent()
    for child in by_parent[old_parent]:
        tt.adjust_path(tt.final_name(child), new_parent, child)
    return by_parent[old_parent]


def _content_match(tree, entry, tree_path, kind, target_path):
    if entry.kind != kind:
        return False
    if entry.kind == "directory":
        return True
    if entry.kind == "file":
        with open(target_path, 'rb') as f1, \
                tree.get_file(tree_path) as f2:
            if osutils.compare_files(f1, f2):
                return True
    elif entry.kind == "symlink":
        if tree.get_symlink_target(tree_path) == os.readlink(target_path):
            return True
    return False


def resolve_checkout(tt, conflicts, divert):
    new_conflicts = set()
    for c_type, conflict in ((c[0], c) for c in conflicts):
        # Anything but a 'duplicate' would indicate programmer error
        if c_type != 'duplicate':
            raise AssertionError(c_type)
        # Now figure out which is new and which is old
        if tt.new_contents(conflict[1]):
            new_file = conflict[1]
            old_file = conflict[2]
        else:
            new_file = conflict[2]
            old_file = conflict[1]

        # We should only get here if the conflict wasn't completely
        # resolved
        final_parent = tt.final_parent(old_file)
        if new_file in divert:
            new_name = tt.final_name(old_file) + '.diverted'
            tt.adjust_path(new_name, final_parent, new_file)
            new_conflicts.add((c_type, 'Diverted to',
                               new_file, old_file))
        else:
            new_name = tt.final_name(old_file) + '.moved'
            tt.adjust_path(new_name, final_parent, old_file)
            new_conflicts.add((c_type, 'Moved existing file to',
                               old_file, new_file))
    return new_conflicts


def new_by_entry(path, tt, entry, parent_id, tree):
    """Create a new file according to its inventory entry"""
    name = entry.name
    kind = entry.kind
    if kind == 'file':
        with tree.get_file(path) as f:
            executable = tree.is_executable(path)
            return tt.new_file(
                name, parent_id, osutils.file_iterator(f), entry.file_id,
                executable)
    elif kind in ('directory', 'tree-reference'):
        trans_id = tt.new_directory(name, parent_id, entry.file_id)
        if kind == 'tree-reference':
            tt.set_tree_reference(entry.reference_revision, trans_id)
        return trans_id
    elif kind == 'symlink':
        target = tree.get_symlink_target(path)
        return tt.new_symlink(name, parent_id, target, entry.file_id)
    else:
        raise errors.BadFileKindError(name, kind)


def create_from_tree(tt, trans_id, tree, path, chunks=None,
                     filter_tree_path=None):
    """Create new file contents according to tree contents.

    :param filter_tree_path: the tree path to use to lookup
      content filters to apply to the bytes output in the working tree.
      This only applies if the working tree supports content filtering.
    """
    kind = tree.kind(path)
    if kind == 'directory':
        tt.create_directory(trans_id)
    elif kind == "file":
        if chunks is None:
            f = tree.get_file(path)
            chunks = osutils.file_iterator(f)
        else:
            f = None
        try:
            wt = tt._tree
            if wt.supports_content_filtering() and filter_tree_path is not None:
                filters = wt._content_filter_stack(filter_tree_path)
                chunks = filtered_output_bytes(
                    chunks, filters,
                    ContentFilterContext(filter_tree_path, tree))
            tt.create_file(chunks, trans_id)
        finally:
            if f is not None:
                f.close()
    elif kind == "symlink":
        tt.create_symlink(tree.get_symlink_target(path), trans_id)
    else:
        raise AssertionError('Unknown kind %r' % kind)


def create_entry_executability(tt, entry, trans_id):
    """Set the executability of a trans_id according to an inventory entry"""
    if entry.kind == "file":
        tt.set_executability(entry.executable, trans_id)


def revert(working_tree, target_tree, filenames, backups=False,
           pb=None, change_reporter=None):
    """Revert a working tree's contents to those of a target tree."""
    pb = ui.ui_factory.nested_progress_bar()
    try:
        with target_tree.lock_read(), working_tree.transform(pb) as tt:
            pp = ProgressPhase("Revert phase", 3, pb)
            conflicts, merge_modified = _prepare_revert_transform(
                working_tree, target_tree, tt, filenames, backups, pp)
            if change_reporter:
                from . import delta
                change_reporter = delta._ChangeReporter(
                    unversioned_filter=working_tree.is_ignored)
                delta.report_changes(tt.iter_changes(), change_reporter)
            for conflict in conflicts:
                trace.warning(text_type(conflict))
            pp.next_phase()
            tt.apply()
            if working_tree.supports_merge_modified():
                working_tree.set_merge_modified(merge_modified)
    finally:
        pb.clear()
    return conflicts


def _prepare_revert_transform(working_tree, target_tree, tt, filenames,
                              backups, pp, basis_tree=None,
                              merge_modified=None):
    with ui.ui_factory.nested_progress_bar() as child_pb:
        if merge_modified is None:
            merge_modified = working_tree.merge_modified()
        merge_modified = _alter_files(working_tree, target_tree, tt,
                                      child_pb, filenames, backups,
                                      merge_modified, basis_tree)
    with ui.ui_factory.nested_progress_bar() as child_pb:
        raw_conflicts = resolve_conflicts(
            tt, child_pb, lambda t, c: conflict_pass(t, c, target_tree))
    conflicts = cook_conflicts(raw_conflicts, tt)
    return conflicts, merge_modified


def _alter_files(working_tree, target_tree, tt, pb, specific_files,
                 backups, merge_modified, basis_tree=None):
    if basis_tree is not None:
        basis_tree.lock_read()
    # We ask the working_tree for its changes relative to the target, rather
    # than the target changes relative to the working tree. Because WT4 has an
    # optimizer to compare itself to a target, but no optimizer for the
    # reverse.
    change_list = working_tree.iter_changes(
        target_tree, specific_files=specific_files, pb=pb)
    if not target_tree.is_versioned(u''):
        skip_root = True
    else:
        skip_root = False
    try:
        deferred_files = []
        for id_num, change in enumerate(change_list):
            file_id = change.file_id
            target_path, wt_path = change.path
            target_versioned, wt_versioned = change.versioned
            target_parent, wt_parent = change.parent_id
            target_name, wt_name = change.name
            target_kind, wt_kind = change.kind
            target_executable, wt_executable = change.executable
            if skip_root and wt_parent is None:
                continue
            trans_id = tt.trans_id_file_id(file_id)
            mode_id = None
            if change.changed_content:
                keep_content = False
                if wt_kind == 'file' and (backups or target_kind is None):
                    wt_sha1 = working_tree.get_file_sha1(wt_path)
                    if merge_modified.get(wt_path) != wt_sha1:
                        # acquire the basis tree lazily to prevent the
                        # expense of accessing it when it's not needed ?
                        # (Guessing, RBC, 200702)
                        if basis_tree is None:
                            basis_tree = working_tree.basis_tree()
                            basis_tree.lock_read()
                        basis_inter = InterTree.get(basis_tree, working_tree)
                        basis_path = basis_inter.find_source_path(wt_path)
                        if basis_path is None:
                            if target_kind is None and not target_versioned:
                                keep_content = True
                        else:
                            if wt_sha1 != basis_tree.get_file_sha1(basis_path):
                                keep_content = True
                if wt_kind is not None:
                    if not keep_content:
                        tt.delete_contents(trans_id)
                    elif target_kind is not None:
                        parent_trans_id = tt.trans_id_file_id(wt_parent)
                        backup_name = tt._available_backup_name(
                            wt_name, parent_trans_id)
                        tt.adjust_path(backup_name, parent_trans_id, trans_id)
                        new_trans_id = tt.create_path(wt_name, parent_trans_id)
                        if wt_versioned and target_versioned:
                            tt.unversion_file(trans_id)
                            tt.version_file(new_trans_id, file_id=file_id)
                        # New contents should have the same unix perms as old
                        # contents
                        mode_id = trans_id
                        trans_id = new_trans_id
                if target_kind in ('directory', 'tree-reference'):
                    tt.create_directory(trans_id)
                    if target_kind == 'tree-reference':
                        revision = target_tree.get_reference_revision(
                            target_path)
                        tt.set_tree_reference(revision, trans_id)
                elif target_kind == 'symlink':
                    tt.create_symlink(target_tree.get_symlink_target(
                        target_path), trans_id)
                elif target_kind == 'file':
                    deferred_files.append(
                        (target_path, (trans_id, mode_id, file_id)))
                    if basis_tree is None:
                        basis_tree = working_tree.basis_tree()
                        basis_tree.lock_read()
                    new_sha1 = target_tree.get_file_sha1(target_path)
                    basis_inter = InterTree.get(basis_tree, target_tree)
                    basis_path = basis_inter.find_source_path(target_path)
                    if (basis_path is not None and
                            new_sha1 == basis_tree.get_file_sha1(basis_path)):
                        # If the new contents of the file match what is in basis,
                        # then there is no need to store in merge_modified.
                        if basis_path in merge_modified:
                            del merge_modified[basis_path]
                    else:
                        merge_modified[target_path] = new_sha1

                    # preserve the execute bit when backing up
                    if keep_content and wt_executable == target_executable:
                        tt.set_executability(target_executable, trans_id)
                elif target_kind is not None:
                    raise AssertionError(target_kind)
            if not wt_versioned and target_versioned:
                tt.version_file(trans_id, file_id=file_id)
            if wt_versioned and not target_versioned:
                tt.unversion_file(trans_id)
            if (target_name is not None
                    and (wt_name != target_name or wt_parent != target_parent)):
                if target_name == '' and target_parent is None:
                    parent_trans = ROOT_PARENT
                else:
                    parent_trans = tt.trans_id_file_id(target_parent)
                if wt_parent is None and wt_versioned:
                    tt.adjust_root_path(target_name, parent_trans)
                else:
                    tt.adjust_path(target_name, parent_trans, trans_id)
            if wt_executable != target_executable and target_kind == "file":
                tt.set_executability(target_executable, trans_id)
        if working_tree.supports_content_filtering():
            for (trans_id, mode_id, file_id), bytes in (
                    target_tree.iter_files_bytes(deferred_files)):
                # We're reverting a tree to the target tree so using the
                # target tree to find the file path seems the best choice
                # here IMO - Ian C 27/Oct/2009
                filter_tree_path = target_tree.id2path(file_id)
                filters = working_tree._content_filter_stack(filter_tree_path)
                bytes = filtered_output_bytes(
                    bytes, filters,
                    ContentFilterContext(filter_tree_path, working_tree))
                tt.create_file(bytes, trans_id, mode_id)
        else:
            for (trans_id, mode_id, file_id), bytes in target_tree.iter_files_bytes(
                    deferred_files):
                tt.create_file(bytes, trans_id, mode_id)
        tt.fixup_new_roots()
    finally:
        if basis_tree is not None:
            basis_tree.unlock()
    return merge_modified


def resolve_conflicts(tt, pb=None, pass_func=None):
    """Make many conflict-resolution attempts, but die if they fail"""
    if pass_func is None:
        pass_func = conflict_pass
    new_conflicts = set()
    with ui.ui_factory.nested_progress_bar() as pb:
        for n in range(10):
            pb.update(gettext('Resolution pass'), n + 1, 10)
            conflicts = tt.find_conflicts()
            if len(conflicts) == 0:
                return new_conflicts
            new_conflicts.update(pass_func(tt, conflicts))
        raise MalformedTransform(conflicts=conflicts)


def conflict_pass(tt, conflicts, path_tree=None):
    """Resolve some classes of conflicts.

    :param tt: The transform to resolve conflicts in
    :param conflicts: The conflicts to resolve
    :param path_tree: A Tree to get supplemental paths from
    """
    new_conflicts = set()
    for c_type, conflict in ((c[0], c) for c in conflicts):
        if c_type == 'duplicate id':
            tt.unversion_file(conflict[1])
            new_conflicts.add((c_type, 'Unversioned existing file',
                               conflict[1], conflict[2], ))
        elif c_type == 'duplicate':
            # files that were renamed take precedence
            final_parent = tt.final_parent(conflict[1])
            if tt.path_changed(conflict[1]):
                existing_file, new_file = conflict[2], conflict[1]
            else:
                existing_file, new_file = conflict[1], conflict[2]
            new_name = tt.final_name(existing_file) + '.moved'
            tt.adjust_path(new_name, final_parent, existing_file)
            new_conflicts.add((c_type, 'Moved existing file to',
                               existing_file, new_file))
        elif c_type == 'parent loop':
            # break the loop by undoing one of the ops that caused the loop
            cur = conflict[1]
            while not tt.path_changed(cur):
                cur = tt.final_parent(cur)
            new_conflicts.add((c_type, 'Cancelled move', cur,
                               tt.final_parent(cur),))
            tt.adjust_path(tt.final_name(cur), tt.get_tree_parent(cur), cur)

        elif c_type == 'missing parent':
            trans_id = conflict[1]
            if trans_id in tt._removed_contents:
                cancel_deletion = True
                orphans = tt._get_potential_orphans(trans_id)
                if orphans:
                    cancel_deletion = False
                    # All children are orphans
                    for o in orphans:
                        try:
                            tt.new_orphan(o, trans_id)
                        except OrphaningError:
                            # Something bad happened so we cancel the directory
                            # deletion which will leave it in place with a
                            # conflict. The user can deal with it from there.
                            # Note that this also catch the case where we don't
                            # want to create orphans and leave the directory in
                            # place.
                            cancel_deletion = True
                            break
                if cancel_deletion:
                    # Cancel the directory deletion
                    tt.cancel_deletion(trans_id)
                    new_conflicts.add(('deleting parent', 'Not deleting',
                                       trans_id))
            else:
                create = True
                try:
                    tt.final_name(trans_id)
                except NoFinalPath:
                    if path_tree is not None:
                        file_id = tt.final_file_id(trans_id)
                        if file_id is None:
                            file_id = tt.inactive_file_id(trans_id)
                        _, entry = next(path_tree.iter_entries_by_dir(
                            specific_files=[path_tree.id2path(file_id)]))
                        # special-case the other tree root (move its
                        # children to current root)
                        if entry.parent_id is None:
                            create = False
                            moved = _reparent_transform_children(
                                tt, trans_id, tt.root)
                            for child in moved:
                                new_conflicts.add((c_type, 'Moved to root',
                                                   child))
                        else:
                            parent_trans_id = tt.trans_id_file_id(
                                entry.parent_id)
                            tt.adjust_path(entry.name, parent_trans_id,
                                           trans_id)
                if create:
                    tt.create_directory(trans_id)
                    new_conflicts.add((c_type, 'Created directory', trans_id))
        elif c_type == 'unversioned parent':
            file_id = tt.inactive_file_id(conflict[1])
            # special-case the other tree root (move its children instead)
            if path_tree and path_tree.path2id('') == file_id:
                # This is the root entry, skip it
                continue
            tt.version_file(conflict[1], file_id=file_id)
            new_conflicts.add((c_type, 'Versioned directory', conflict[1]))
        elif c_type == 'non-directory parent':
            parent_id = conflict[1]
            parent_parent = tt.final_parent(parent_id)
            parent_name = tt.final_name(parent_id)
            parent_file_id = tt.final_file_id(parent_id)
            new_parent_id = tt.new_directory(parent_name + '.new',
                                             parent_parent, parent_file_id)
            _reparent_transform_children(tt, parent_id, new_parent_id)
            if parent_file_id is not None:
                tt.unversion_file(parent_id)
            new_conflicts.add((c_type, 'Created directory', new_parent_id))
        elif c_type == 'versioning no contents':
            tt.cancel_versioning(conflict[1])
    return new_conflicts


def cook_conflicts(raw_conflicts, tt):
    """Generate a list of cooked conflicts, sorted by file path"""
    conflict_iter = iter_cook_conflicts(raw_conflicts, tt)
    return sorted(conflict_iter, key=conflicts.Conflict.sort_key)


def iter_cook_conflicts(raw_conflicts, tt):
    fp = FinalPaths(tt)
    for conflict in raw_conflicts:
        c_type = conflict[0]
        action = conflict[1]
        modified_path = fp.get_path(conflict[2])
        modified_id = tt.final_file_id(conflict[2])
        if len(conflict) == 3:
            yield conflicts.Conflict.factory(
                c_type, action=action, path=modified_path, file_id=modified_id)

        else:
            conflicting_path = fp.get_path(conflict[3])
            conflicting_id = tt.final_file_id(conflict[3])
            yield conflicts.Conflict.factory(
                c_type, action=action, path=modified_path,
                file_id=modified_id,
                conflict_path=conflicting_path,
                conflict_file_id=conflicting_id)


class _FileMover(object):
    """Moves and deletes files for TreeTransform, tracking operations"""

    def __init__(self):
        self.past_renames = []
        self.pending_deletions = []

    def rename(self, from_, to):
        """Rename a file from one path to another."""
        try:
            os.rename(from_, to)
        except OSError as e:
            if e.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise errors.FileExists(to, str(e))
            # normal OSError doesn't include filenames so it's hard to see where
            # the problem is, see https://bugs.launchpad.net/bzr/+bug/491763
            raise TransformRenameFailed(from_, to, str(e), e.errno)
        self.past_renames.append((from_, to))

    def pre_delete(self, from_, to):
        """Rename a file out of the way and mark it for deletion.

        Unlike os.unlink, this works equally well for files and directories.
        :param from_: The current file path
        :param to: A temporary path for the file
        """
        self.rename(from_, to)
        self.pending_deletions.append(to)

    def rollback(self):
        """Reverse all renames that have been performed"""
        for from_, to in reversed(self.past_renames):
            try:
                os.rename(to, from_)
            except OSError as e:
                raise TransformRenameFailed(to, from_, str(e), e.errno)
        # after rollback, don't reuse _FileMover
        self.past_renames = None
        self.pending_deletions = None

    def apply_deletions(self):
        """Apply all marked deletions"""
        for path in self.pending_deletions:
            delete_any(path)
        # after apply_deletions, don't reuse _FileMover
        self.past_renames = None
        self.pending_deletions = None


def link_tree(target_tree, source_tree):
    """Where possible, hard-link files in a tree to those in another tree.

    :param target_tree: Tree to change
    :param source_tree: Tree to hard-link from
    """
    with target_tree.transform() as tt:
        for change in target_tree.iter_changes(source_tree, include_unchanged=True):
            if change.changed_content:
                continue
            if change.kind != ('file', 'file'):
                continue
            if change.executable[0] != change.executable[1]:
                continue
            trans_id = tt.trans_id_tree_path(change.path[1])
            tt.delete_contents(trans_id)
            tt.create_hardlink(source_tree.abspath(change.path[0]), trans_id)
        tt.apply()
