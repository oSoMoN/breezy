# Copyright (C) 2005 Canonical Ltd

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA


import shutil
import sys
import os
import errno
from warnings import warn
from cStringIO import StringIO


import bzrlib
import bzrlib.inventory as inventory
from bzrlib.trace import mutter, note
from bzrlib.osutils import (isdir, quotefn,
                            rename, splitpath, sha_file, appendpath, 
                            file_kind, abspath)
import bzrlib.errors as errors
from bzrlib.errors import (BzrError, InvalidRevisionNumber, InvalidRevisionId,
                           NoSuchRevision, HistoryMissing, NotBranchError,
                           DivergedBranches, LockError, UnlistableStore,
                           UnlistableBranch, NoSuchFile, NotVersionedError,
                           NoWorkingTree)
from bzrlib.textui import show_status
from bzrlib.revision import (Revision, is_ancestor, get_intervening_revisions)

from bzrlib.config import TreeConfig
from bzrlib.delta import compare_trees
from bzrlib.tree import EmptyTree, RevisionTree
from bzrlib.inventory import Inventory
from bzrlib.lockablefiles import LockableFiles
from bzrlib.revstorage import RevisionStorage
from bzrlib.store import copy_all
from bzrlib.store.text import TextStore
from bzrlib.store.weave import WeaveStore
from bzrlib.testament import Testament
import bzrlib.transactions as transactions
from bzrlib.transport import Transport, get_transport
import bzrlib.ui
import bzrlib.xml5


BZR_BRANCH_FORMAT_4 = "Bazaar-NG branch, format 0.0.4\n"
BZR_BRANCH_FORMAT_5 = "Bazaar-NG branch, format 5\n"
BZR_BRANCH_FORMAT_6 = "Bazaar-NG branch, format 6\n"
## TODO: Maybe include checks for common corruption of newlines, etc?


# TODO: Some operations like log might retrieve the same revisions
# repeatedly to calculate deltas.  We could perhaps have a weakref
# cache in memory to make this faster.  In general anything can be
# cached in memory between lock and unlock operations.

def find_branch(*ignored, **ignored_too):
    # XXX: leave this here for about one release, then remove it
    raise NotImplementedError('find_branch() is not supported anymore, '
                              'please use one of the new branch constructors')


def needs_read_lock(unbound):
    """Decorate unbound to take out and release a read lock."""
    def decorated(self, *args, **kwargs):
        self.lock_read()
        try:
            return unbound(self, *args, **kwargs)
        finally:
            self.unlock()
    return decorated


def needs_write_lock(unbound):
    """Decorate unbound to take out and release a write lock."""
    def decorated(self, *args, **kwargs):
        self.lock_write()
        try:
            return unbound(self, *args, **kwargs)
        finally:
            self.unlock()
    return decorated

######################################################################
# branch objects

class Branch(object):
    """Branch holding a history of revisions.

    base
        Base directory/url of the branch.
    """
    base = None

    def __init__(self, *ignored, **ignored_too):
        raise NotImplementedError('The Branch class is abstract')

    @staticmethod
    def open_downlevel(base):
        """Open a branch which may be of an old format.
        
        Only local branches are supported."""
        return BzrBranch(get_transport(base), relax_version_check=True)
        
    @staticmethod
    def open(base):
        """Open an existing branch, rooted at 'base' (url)"""
        t = get_transport(base)
        mutter("trying to open %r with transport %r", base, t)
        return BzrBranch(t)

    @staticmethod
    def open_containing(url):
        """Open an existing branch which contains url.
        
        This probes for a branch at url, and searches upwards from there.

        Basically we keep looking up until we find the control directory or
        run into the root.  If there isn't one, raises NotBranchError.
        If there is one, it is returned, along with the unused portion of url.
        """
        t = get_transport(url)
        while True:
            try:
                return BzrBranch(t), t.relpath(url)
            except NotBranchError:
                pass
            new_t = t.clone('..')
            if new_t.base == t.base:
                # reached the root, whatever that may be
                raise NotBranchError(path=url)
            t = new_t

    @staticmethod
    def initialize(base):
        """Create a new branch, rooted at 'base' (url)"""
        t = get_transport(base)
        return BzrBranch(t, init=True)

    def setup_caching(self, cache_root):
        """Subclasses that care about caching should override this, and set
        up cached stores located under cache_root.
        """
        self.cache_root = cache_root

    def _get_nick(self):
        cfg = self.tree_config()
        return cfg.get_option(u"nickname", default=self.base.split('/')[-1])

    def _set_nick(self, nick):
        cfg = self.tree_config()
        cfg.set_option(nick, "nickname")
        assert cfg.get_option("nickname") == nick

    nick = property(_get_nick, _set_nick)
        
    def push_stores(self, branch_to):
        """Copy the content of this branches store to branch_to."""
        raise NotImplementedError('push_stores is abstract')

    def get_transaction(self):
        """Return the current active transaction.

        If no transaction is active, this returns a passthrough object
        for which all data is immediately flushed and no caching happens.
        """
        raise NotImplementedError('get_transaction is abstract')

    def lock_write(self):
        raise NotImplementedError('lock_write is abstract')
        
    def lock_read(self):
        raise NotImplementedError('lock_read is abstract')

    def unlock(self):
        raise NotImplementedError('unlock is abstract')

    def abspath(self, name):
        """Return absolute filename for something in the branch
        
        XXX: Robert Collins 20051017 what is this used for? why is it a branch
        method and not a tree method.
        """
        raise NotImplementedError('abspath is abstract')

    def get_root_id(self):
        """Return the id of this branches root"""
        raise NotImplementedError('get_root_id is abstract')

    def print_file(self, file, revno):
        """Print `file` to stdout."""
        raise NotImplementedError('print_file is abstract')

    def append_revision(self, *revision_ids):
        raise NotImplementedError('append_revision is abstract')

    def set_revision_history(self, rev_history):
        raise NotImplementedError('set_revision_history is abstract')

    def get_revision_delta(self, revno):
        """Return the delta for one revision.

        The delta is relative to its mainline predecessor, or the
        empty tree for revision 1.
        """
        assert isinstance(revno, int)
        rh = self.revision_history()
        if not (1 <= revno <= len(rh)):
            raise InvalidRevisionNumber(revno)

        # revno is 1-based; list is 0-based

        new_tree = self.storage.revision_tree(rh[revno-1])
        if revno == 1:
            old_tree = EmptyTree()
        else:
            old_tree = self.storage.revision_tree(rh[revno-2])

        return compare_trees(old_tree, new_tree)

    def get_ancestry(self, revision_id):
        """Return a list of revision-ids integrated by a revision.
        
        This currently returns a list, but the ordering is not guaranteed:
        treat it as a set.
        """
        raise NotImplementedError('get_ancestry is abstract')

    def get_inventory(self, revision_id):
        """Get Inventory object by hash."""
        raise NotImplementedError('get_inventory is abstract')

    def get_inventory_xml(self, revision_id):
        """Get inventory XML as a file object."""
        raise NotImplementedError('get_inventory_xml is abstract')

    def get_inventory_sha1(self, revision_id):
        """Return the sha1 hash of the inventory entry."""
        raise NotImplementedError('get_inventory_sha1 is abstract')

    def get_revision_inventory(self, revision_id):
        """Return inventory of a past revision."""
        raise NotImplementedError('get_revision_inventory is abstract')

    def revision_history(self):
        """Return sequence of revision hashes on to this branch."""
        raise NotImplementedError('revision_history is abstract')

    def revno(self):
        """Return current revision number for this branch.

        That is equivalent to the number of revisions committed to
        this branch.
        """
        return len(self.revision_history())

    def last_revision(self):
        """Return last patch hash, or None if no history."""
        ph = self.revision_history()
        if ph:
            return ph[-1]
        else:
            return None

    def missing_revisions(self, other, stop_revision=None, diverged_ok=False):
        """Return a list of new revisions that would perfectly fit.
        
        If self and other have not diverged, return a list of the revisions
        present in other, but missing from self.

        >>> from bzrlib.commit import commit
        >>> bzrlib.trace.silent = True
        >>> br1 = ScratchBranch()
        >>> br2 = ScratchBranch()
        >>> br1.missing_revisions(br2)
        []
        >>> commit(br2, "lala!", rev_id="REVISION-ID-1")
        >>> br1.missing_revisions(br2)
        [u'REVISION-ID-1']
        >>> br2.missing_revisions(br1)
        []
        >>> commit(br1, "lala!", rev_id="REVISION-ID-1")
        >>> br1.missing_revisions(br2)
        []
        >>> commit(br2, "lala!", rev_id="REVISION-ID-2A")
        >>> br1.missing_revisions(br2)
        [u'REVISION-ID-2A']
        >>> commit(br1, "lala!", rev_id="REVISION-ID-2B")
        >>> br1.missing_revisions(br2)
        Traceback (most recent call last):
        DivergedBranches: These branches have diverged.
        """
        self_history = self.revision_history()
        self_len = len(self_history)
        other_history = other.revision_history()
        other_len = len(other_history)
        common_index = min(self_len, other_len) -1
        if common_index >= 0 and \
            self_history[common_index] != other_history[common_index]:
            raise DivergedBranches(self, other)

        if stop_revision is None:
            stop_revision = other_len
        else:
            assert isinstance(stop_revision, int)
            if stop_revision > other_len:
                raise bzrlib.errors.NoSuchRevision(self, stop_revision)
        return other_history[self_len:stop_revision]
    
    def update_revisions(self, other, stop_revision=None):
        """Pull in new perfect-fit revisions."""
        raise NotImplementedError('update_revisions is abstract')

    def pullable_revisions(self, other, stop_revision):
        raise NotImplementedError('pullable_revisions is abstract')
        
    def revision_id_to_revno(self, revision_id):
        """Given a revision id, return its revno"""
        if revision_id is None:
            return 0
        history = self.revision_history()
        try:
            return history.index(revision_id) + 1
        except ValueError:
            raise bzrlib.errors.NoSuchRevision(self, revision_id)

    def get_rev_id(self, revno, history=None):
        """Find the revision id of the specified revno."""
        if revno == 0:
            return None
        if history is None:
            history = self.revision_history()
        elif revno <= 0 or revno > len(history):
            raise bzrlib.errors.NoSuchRevision(self, revno)
        return history[revno - 1]

    def working_tree(self):
        """Return a `Tree` for the working copy if this is a local branch."""
        raise NotImplementedError('working_tree is abstract')

    def pull(self, source, overwrite=False):
        raise NotImplementedError('pull is abstract')

    def basis_tree(self):
        """Return `Tree` object for last revision.

        If there are no revisions yet, return an `EmptyTree`.
        """
        return self.storage.revision_tree(self.last_revision())

    def rename_one(self, from_rel, to_rel):
        """Rename one file.

        This can change the directory or the filename or both.
        """
        raise NotImplementedError('rename_one is abstract')

    def move(self, from_paths, to_name):
        """Rename files.

        to_name must exist as a versioned directory.

        If to_name exists and is a directory, the files are moved into
        it, keeping their old names.  If it is a directory, 

        Note that to_name is only the last component of the new name;
        this doesn't change the directory.

        This returns a list of (from_path, to_path) pairs for each
        entry that is moved.
        """
        raise NotImplementedError('move is abstract')

    def get_parent(self):
        """Return the parent location of the branch.

        This is the default location for push/pull/missing.  The usual
        pattern is that the user can override it by specifying a
        location.
        """
        raise NotImplementedError('get_parent is abstract')

    def get_push_location(self):
        """Return the None or the location to push this branch to."""
        raise NotImplementedError('get_push_location is abstract')

    def set_push_location(self, location):
        """Set a new push location for this branch."""
        raise NotImplementedError('set_push_location is abstract')

    def set_parent(self, url):
        raise NotImplementedError('set_parent is abstract')

    def check_revno(self, revno):
        """\
        Check whether a revno corresponds to any revision.
        Zero (the NULL revision) is considered valid.
        """
        if revno != 0:
            self.check_real_revno(revno)
            
    def check_real_revno(self, revno):
        """\
        Check whether a revno corresponds to a real revision.
        Zero (the NULL revision) is considered invalid
        """
        if revno < 1 or revno > self.revno():
            raise InvalidRevisionNumber(revno)
        
    def sign_revision(self, revision_id, gpg_strategy):
        raise NotImplementedError('sign_revision is abstract')

    def store_revision_signature(self, gpg_strategy, plaintext, revision_id):
        raise NotImplementedError('store_revision_signature is abstract')

class BzrBranch(Branch):
    """A branch stored in the actual filesystem.

    Note that it's "local" in the context of the filesystem; it doesn't
    really matter if it's on an nfs/smb/afs/coda/... share, as long as
    it's writable, and can be accessed via the normal filesystem API.

    _lock_mode
        None, or 'r' or 'w'

    _lock_count
        If _lock_mode is true, a positive count of the number of times the
        lock has been taken.

    _lock
        Lock object from bzrlib.lock.
    """
    # We actually expect this class to be somewhat short-lived; part of its
    # purpose is to try to isolate what bits of the branch logic are tied to
    # filesystem access, so that in a later step, we can extricate them to
    # a separarte ("storage") class.
    _lock_mode = None
    _lock_count = None
    _lock = None
    _inventory_weave = None
    
    # Map some sort of prefix into a namespace
    # stuff like "revno:10", "revid:", etc.
    # This should match a prefix with a function which accepts
    REVISION_NAMESPACES = {}

    def push_stores(self, branch_to):
        """See Branch.push_stores."""
        if (self._branch_format != branch_to._branch_format
            or self._branch_format != 4):
            from bzrlib.fetch import greedy_fetch
            mutter("falling back to fetch logic to push between %s(%s) and %s(%s)",
                   self, self._branch_format, branch_to, branch_to._branch_format)
            greedy_fetch(to_branch=branch_to, from_branch=self,
                         revision=self.last_revision())
            return

        store_pairs = ((self.text_store,      branch_to.text_store),
                       (self.inventory_store, branch_to.inventory_store),
                       (self.revision_store,  branch_to.revision_store))
        try:
            for from_store, to_store in store_pairs: 
                copy_all(from_store, to_store)
        except UnlistableStore:
            raise UnlistableBranch(from_store)

    def __init__(self, transport, init=False,
                 relax_version_check=False):
        """Create new branch object at a particular location.

        transport -- A Transport object, defining how to access files.
        
        init -- If True, create new control files in a previously
             unversioned directory.  If False, the branch must already
             be versioned.

        relax_version_check -- If true, the usual check for the branch
            version is not applied.  This is intended only for
            upgrade/recovery type use; it's not guaranteed that
            all operations will work on old format branches.

        In the test suite, creation of new trees is tested using the
        `ScratchBranch` class.
        """
        assert isinstance(transport, Transport), \
            "%r is not a Transport" % transport
        self.control_files = LockableFiles(transport, 'branch-lock')
        if init:
            self._make_control()
        self._check_format(relax_version_check)
        self.storage = RevisionStorage(transport, self._branch_format)

    def __str__(self):
        return '%s(%r)' % (self.__class__.__name__, self.base)

    __repr__ = __str__

    def __del__(self):
        # TODO: It might be best to do this somewhere else,
        # but it is nice for a Branch object to automatically
        # cache it's information.
        # Alternatively, we could have the Transport objects cache requests
        # See the earlier discussion about how major objects (like Branch)
        # should never expect their __del__ function to run.
        if hasattr(self, 'cache_root') and self.cache_root is not None:
            try:
                shutil.rmtree(self.cache_root)
            except:
                pass
            self.cache_root = None

    def _get_base(self):
        if self.control_files._transport:
            return self.control_files._transport.base
        return None

    base = property(_get_base, doc="The URL for the root of this branch.")

    def _finish_transaction(self):
        """Exit the current transaction."""
        return self.control_files._finish_transaction()

    def get_transaction(self):
        """Return the current active transaction.

        If no transaction is active, this returns a passthrough object
        for which all data is immediately flushed and no caching happens.
        """
        # this is an explicit function so that we can do tricky stuff
        # when the storage in rev_storage is elsewhere.
        # we probably need to hook the two 'lock a location' and 
        # 'have a transaction' together more delicately, so that
        # we can have two locks (branch and storage) and one transaction
        # ... and finishing the transaction unlocks both, but unlocking
        # does not. - RBC 20051121
        return self.control_files.get_transaction()

    def _set_transaction(self, transaction):
        """Set a new active transaction."""
        return self.control_files._set_transaction(transaction)

    def abspath(self, name):
        """See Branch.abspath."""
        return self.control_files._transport.abspath(name)

    def _make_control(self):
        from bzrlib.inventory import Inventory
        from bzrlib.weavefile import write_weave_v5
        from bzrlib.weave import Weave
        
        # Create an empty inventory
        sio = StringIO()
        # if we want per-tree root ids then this is the place to set
        # them; they're not needed for now and so ommitted for
        # simplicity.
        bzrlib.xml5.serializer_v5.write_inventory(Inventory(), sio)
        empty_inv = sio.getvalue()
        sio = StringIO()
        bzrlib.weavefile.write_weave_v5(Weave(), sio)
        empty_weave = sio.getvalue()

        dirs = [[], 'revision-store', 'weaves']
        files = [('README', 
            "This is a Bazaar-NG control directory.\n"
            "Do not change any files in this directory.\n"),
            ('branch-format', BZR_BRANCH_FORMAT_6),
            ('revision-history', ''),
            ('branch-name', ''),
            ('branch-lock', ''),
            ('pending-merges', ''),
            ('inventory', empty_inv),
            ('inventory.weave', empty_weave),
            ('ancestry.weave', empty_weave)
        ]
        cfn = self.control_files._rel_controlfilename
        self.control_files._transport.mkdir_multi([cfn(d) for d in dirs])
        for file, content in files:
            self.control_files.put_utf8(file, content)
        mutter('created control directory in ' + self.base)

    def _check_format(self, relax_version_check):
        """Check this branch format is supported.

        The format level is stored, as an integer, in
        self._branch_format for code that needs to check it later.

        In the future, we might need different in-memory Branch
        classes to support downlevel branches.  But not yet.
        """
        try:
            fmt = self.control_files.controlfile('branch-format', 'r').read()
        except NoSuchFile:
            raise NotBranchError(path=self.base)
        mutter("got branch format %r", fmt)
        if fmt == BZR_BRANCH_FORMAT_6:
            self._branch_format = 6
        elif fmt == BZR_BRANCH_FORMAT_5:
            self._branch_format = 5
        elif fmt == BZR_BRANCH_FORMAT_4:
            self._branch_format = 4

        if (not relax_version_check
            and self._branch_format not in (5, 6)):
            raise errors.UnsupportedFormatError(
                           'sorry, branch format %r not supported' % fmt,
                           ['use a different bzr version',
                            'or remove the .bzr directory'
                            ' and "bzr init" again'])

    @needs_read_lock
    def get_root_id(self):
        """See Branch.get_root_id."""
        inv = self.storage.get_inventory(self.last_revision())
        return inv.root.file_id

    def lock_write(self):
        # TODO: test for failed two phase locks. This is known broken.
        self.control_files.lock_write()
        self.storage.lock_write()

    def lock_read(self):
        # TODO: test for failed two phase locks. This is known broken.
        self.control_files.lock_read()
        self.storage.lock_read()

    def unlock(self):
        # TODO: test for failed two phase locks. This is known broken.
        self.storage.unlock()
        self.control_files.unlock()

    @needs_read_lock
    def print_file(self, file, revno):
        """See Branch.print_file."""
        return self.storage.print_file(file, self.get_rev_id(revno))

    @needs_write_lock
    def append_revision(self, *revision_ids):
        """See Branch.append_revision."""
        for revision_id in revision_ids:
            mutter("add {%s} to revision-history" % revision_id)
        rev_history = self.revision_history()
        rev_history.extend(revision_ids)
        self.set_revision_history(rev_history)

    @needs_write_lock
    def set_revision_history(self, rev_history):
        """See Branch.set_revision_history."""
        self.control_files.put_utf8(
            'revision-history', '\n'.join(rev_history))

    def get_ancestry(self, revision_id):
        """See Branch.get_ancestry."""
        if revision_id is None:
            return [None]
        w = self.storage.get_inventory_weave()
        return [None] + map(w.idx_to_name,
                            w.inclusions([w.lookup(revision_id)]))

    def _get_inventory_weave(self):
        return self.storage.control_weaves.get_weave('inventory',
                                             self.get_transaction())

    def get_inventory(self, revision_id):
        """See Branch.get_inventory."""
        xml = self.get_inventory_xml(revision_id)
        return bzrlib.xml5.serializer_v5.read_inventory_from_string(xml)

    def get_inventory_xml(self, revision_id):
        """See Branch.get_inventory_xml."""
        try:
            assert isinstance(revision_id, basestring), type(revision_id)
            iw = self._get_inventory_weave()
            return iw.get_text(iw.lookup(revision_id))
        except IndexError:
            raise bzrlib.errors.HistoryMissing(self, 'inventory', revision_id)

    def get_inventory_sha1(self, revision_id):
        """See Branch.get_inventory_sha1."""
        return self.get_revision(revision_id).inventory_sha1

    def get_revision_inventory(self, revision_id):
        """See Branch.get_revision_inventory."""
        # TODO: Unify this with get_inventory()
        # bzr 0.0.6 and later imposes the constraint that the inventory_id
        # must be the same as its revision, so this is trivial.
        if revision_id == None:
            # This does not make sense: if there is no revision,
            # then it is the current tree inventory surely ?!
            # and thus get_root_id() is something that looks at the last
            # commit on the branch, and the get_root_id is an inventory check.
            raise NotImplementedError
            # return Inventory(self.get_root_id())
        else:
            return self.get_inventory(revision_id)

    @needs_read_lock
    def revision_history(self):
        """See Branch.revision_history."""
        # FIXME are transactions bound to control files ? RBC 20051121
        transaction = self.get_transaction()
        history = transaction.map.find_revision_history()
        if history is not None:
            mutter("cache hit for revision-history in %s", self)
            return list(history)
        history = [l.rstrip('\r\n') for l in
                self.control_files.controlfile('revision-history', 'r').readlines()]
        transaction.map.add_revision_history(history)
        # this call is disabled because revision_history is 
        # not really an object yet, and the transaction is for objects.
        # transaction.register_clean(history, precious=True)
        return list(history)

    def update_revisions(self, other, stop_revision=None):
        """See Branch.update_revisions."""
        from bzrlib.fetch import greedy_fetch
        if stop_revision is None:
            stop_revision = other.last_revision()
        ### Should this be checking is_ancestor instead of revision_history?
        if (stop_revision is not None and 
            stop_revision in self.revision_history()):
            return
        greedy_fetch(to_branch=self, from_branch=other,
                     revision=stop_revision)
        pullable_revs = self.pullable_revisions(other, stop_revision)
        if len(pullable_revs) > 0:
            self.append_revision(*pullable_revs)

    def pullable_revisions(self, other, stop_revision):
        """See Branch.pullable_revisions."""
        other_revno = other.revision_id_to_revno(stop_revision)
        try:
            return self.missing_revisions(other, other_revno)
        except DivergedBranches, e:
            try:
                pullable_revs = get_intervening_revisions(self.last_revision(),
                                                          stop_revision, 
                                                          self.storage)
                assert self.last_revision() not in pullable_revs
                return pullable_revs
            except bzrlib.errors.NotAncestor:
                if is_ancestor(self.last_revision(), stop_revision, self):
                    return []
                else:
                    raise e
        
    def working_tree(self):
        """See Branch.working_tree."""
        from bzrlib.workingtree import WorkingTree
        if self.base.find('://') != -1:
            raise NoWorkingTree(self.base)
        return WorkingTree(self.base, branch=self)

    @needs_write_lock
    def pull(self, source, overwrite=False):
        """See Branch.pull."""
        source.lock_read()
        try:
            old_count = len(self.revision_history())
            try:
                self.update_revisions(source)
            except DivergedBranches:
                if not overwrite:
                    raise
                self.set_revision_history(source.revision_history())
            new_count = len(self.revision_history())
            return new_count - old_count
        finally:
            source.unlock()

    def get_parent(self):
        """See Branch.get_parent."""
        import errno
        _locs = ['parent', 'pull', 'x-pull']
        for l in _locs:
            try:
                return self.control_files.controlfile(l, 'r').read().strip('\n')
            except IOError, e:
                if e.errno != errno.ENOENT:
                    raise
        return None

    def get_push_location(self):
        """See Branch.get_push_location."""
        config = bzrlib.config.BranchConfig(self)
        push_loc = config.get_user_option('push_location')
        return push_loc

    def set_push_location(self, location):
        """See Branch.set_push_location."""
        config = bzrlib.config.LocationConfig(self.base)
        config.set_user_option('push_location', location)

    @needs_write_lock
    def set_parent(self, url):
        """See Branch.set_parent."""
        # TODO: Maybe delete old location files?
        from bzrlib.atomicfile import AtomicFile
        f = AtomicFile(self.control_files.controlfilename('parent'))
        try:
            f.write(url + '\n')
            f.commit()
        finally:
            f.close()

    def tree_config(self):
        return TreeConfig(self)

    def sign_revision(self, revision_id, gpg_strategy):
        """See Branch.sign_revision."""
        plaintext = Testament.from_revision(self, revision_id).as_short_text()
        self.store_revision_signature(gpg_strategy, plaintext, revision_id)

    @needs_write_lock
    def store_revision_signature(self, gpg_strategy, plaintext, revision_id):
        """See Branch.store_revision_signature."""
        self.revision_store.add(StringIO(gpg_strategy.sign(plaintext)), 
                                revision_id, "sig")


class ScratchBranch(BzrBranch):
    """Special test class: a branch that cleans up after itself.

    >>> b = ScratchBranch()
    >>> isdir(b.base)
    True
    >>> bd = b.base
    >>> b.control_files._transport.__del__()
    >>> isdir(bd)
    False
    """

    def __init__(self, files=[], dirs=[], transport=None):
        """Make a test branch.

        This creates a temporary directory and runs init-tree in it.

        If any files are listed, they are created in the working copy.
        """
        if transport is None:
            transport = bzrlib.transport.local.ScratchTransport()
            super(ScratchBranch, self).__init__(transport, init=True)
        else:
            super(ScratchBranch, self).__init__(transport)

        for d in dirs:
            self.control_files._transport.mkdir(d)
            
        for f in files:
            self.control_files._transport.put(f, 'content of %s' % f)

    def clone(self):
        """
        >>> orig = ScratchBranch(files=["file1", "file2"])
        >>> clone = orig.clone()
        >>> if os.name != 'nt':
        ...   os.path.samefile(orig.base, clone.base)
        ... else:
        ...   orig.base == clone.base
        ...
        False
        >>> os.path.isfile(os.path.join(clone.base, "file1"))
        True
        """
        from shutil import copytree
        from tempfile import mkdtemp
        base = mkdtemp()
        os.rmdir(base)
        copytree(self.base, base, symlinks=True)
        return ScratchBranch(
            transport=bzrlib.transport.local.ScratchTransport(base))
    

######################################################################
# predicates


def is_control_file(filename):
    ## FIXME: better check
    filename = os.path.normpath(filename)
    while filename != '':
        head, tail = os.path.split(filename)
        ## mutter('check %r for control file' % ((head, tail), ))
        if tail == bzrlib.BZRDIR:
            return True
        if filename == head:
            break
        filename = head
    return False
