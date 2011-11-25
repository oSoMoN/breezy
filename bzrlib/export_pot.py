# Copyright (C) 2011 Canonical Ltd
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

# The normalize function is taken from pygettext which is distributed
# with Python under the Python License, which is GPL compatible.

"""Extract docstrings from Bazaar commands.

This module only handles bzrlib objects that use strings not directly wrapped
by a gettext() call. To generate a complete translation template file, this
output needs to be combined with that of xgettext or a similar command for
extracting those strings, as is done in the bzr Makefile. Sorting the output
is also left to that stage of the process.
"""

import inspect
import os

from bzrlib import (
    commands as _mod_commands,
    errors,
    help_topics,
    plugin,
    help,
    )
from bzrlib.trace import (
    mutter,
    note,
    )
from bzrlib.i18n import gettext


def _escape(s):
    s = (s.replace('\\', '\\\\')
        .replace('\n', '\\n')
        .replace('\r', '\\r')
        .replace('\t', '\\t')
        .replace('"', '\\"')
        )
    return s

def _normalize(s):
    # This converts the various Python string types into a format that
    # is appropriate for .po files, namely much closer to C style.
    lines = s.split('\n')
    if len(lines) == 1:
        s = '"' + _escape(s) + '"'
    else:
        if not lines[-1]:
            del lines[-1]
            lines[-1] = lines[-1] + '\n'
        lines = map(_escape, lines)
        lineterm = '\\n"\n"'
        s = '""\n"' + lineterm.join(lines) + '"'
    return s


class _PotExporter(object):
    """Write message details to output stream in .pot file format"""

    def __init__(self, outf):
        self.outf = outf
        self._msgids = set()

    def poentry(self, path, lineno, s, comment=None):
        if s in self._msgids:
            return
        self._msgids.add(s)
        if comment is None:
            comment = ''
        else:
            comment = "# %s\n" % comment
        mutter("Exporting msg %r at line %d in %r", s[:20], lineno, path)
        self.outf.write(
            "#: {path}:{lineno}\n"
            "{comment}"
            "msgid {msg}\n"
            "msgstr \"\"\n"
            "\n".format(
                path=path, lineno=lineno, comment=comment, msg=_normalize(s)))

    def poentry_per_paragraph(self, path, lineno, msgid, include=None):
        # TODO: How to split long help?
        paragraphs = msgid.split('\n\n')
        if include is not None:
            paragraphs = filter(include, paragraphs)
        for p in paragraphs:
            self.poentry(path, lineno, p)
            lineno += p.count('\n') + 2


_LAST_CACHE = _LAST_CACHED_SRC = None

def _offsets_of_literal(src):
    global _LAST_CACHE, _LAST_CACHED_SRC
    if src == _LAST_CACHED_SRC:
        return _LAST_CACHE.copy()

    import ast
    root = ast.parse(src)
    offsets = {}
    for node in ast.walk(root):
        if not isinstance(node, ast.Str):
            continue
        offsets[node.s] = node.lineno - node.s.count('\n')

    _LAST_CACHED_SRC = src
    _LAST_CACHE = offsets.copy()
    return offsets

def _standard_options(exporter):
    from bzrlib.option import Option
    src = inspect.findsource(Option)[0]
    src = ''.join(src)
    path = 'bzrlib/option.py'
    offsets = _offsets_of_literal(src)

    for name in sorted(Option.OPTIONS.keys()):
        opt = Option.OPTIONS[name]
        if getattr(opt, 'hidden', False):
            continue
        if getattr(opt, 'title', None):
            lineno = offsets.get(opt.title, 9999)
            if lineno == 9999:
                note(gettext("%r is not found in bzrlib/option.py") % opt.title)
            exporter.poentry(path, lineno, opt.title,
                     'title of %r option' % name)
        if getattr(opt, 'help', None):
            lineno = offsets.get(opt.help, 9999)
            if lineno == 9999:
                note(gettext("%r is not found in bzrlib/option.py") % opt.help)
            exporter.poentry(path, lineno, opt.help,
                     'help of %r option' % name)

def _command_options(exporter, path, cmd):
    src, default_lineno = inspect.findsource(cmd.__class__)
    offsets = _offsets_of_literal(''.join(src))
    for opt in cmd.takes_options:
        if isinstance(opt, str):
            continue
        if getattr(opt, 'hidden', False):
            continue
        name = opt.name
        if getattr(opt, 'title', None):
            lineno = offsets.get(opt.title, default_lineno)
            exporter.poentry(path, lineno, opt.title,
                     'title of %r option of %r command' % (name, cmd.name()))
        if getattr(opt, 'help', None):
            lineno = offsets.get(opt.help, default_lineno)
            exporter.poentry(path, lineno, opt.help,
                     'help of %r option of %r command' % (name, cmd.name()))


def _write_command_help(exporter, cmd):
    path = inspect.getfile(cmd.__class__)
    if path.endswith('.pyc'):
        path = path[:-1]
    path = os.path.relpath(path)
    src, lineno = inspect.findsource(cmd.__class__)
    offsets = _offsets_of_literal(''.join(src))
    lineno = offsets[cmd.__doc__]
    doc = inspect.getdoc(cmd)

    def exclude_usage(p):
        # ':Usage:' has special meaning in help topics.
        # This is usage example of command and should not be translated.
        if p.splitlines()[0] != ':Usage:':
            return True

    exporter.poentry_per_paragraph(path, lineno, doc, exclude_usage)
    _command_options(exporter, path, cmd)


def _command_helps(exporter, plugin_name=None):
    """Extract docstrings from path.

    This respects the Bazaar cmdtable/table convention and will
    only extract docstrings from functions mentioned in these tables.
    """
    from glob import glob

    # builtin commands
    for cmd_name in _mod_commands.builtin_command_names():
        command = _mod_commands.get_cmd_object(cmd_name, False)
        if command.hidden:
            continue
        if plugin_name is not None:
            # only export builtins if we are not exporting plugin commands
            continue
        note(gettext("Exporting messages from builtin command: %s"), cmd_name)
        _write_command_help(exporter, command)

    plugin_path = plugin.get_core_plugin_path()
    core_plugins = glob(plugin_path + '/*/__init__.py')
    core_plugins = [os.path.basename(os.path.dirname(p))
                        for p in core_plugins]
    # plugins
    for cmd_name in _mod_commands.plugin_command_names():
        command = _mod_commands.get_cmd_object(cmd_name, False)
        if command.hidden:
            continue
        if plugin_name is not None and command.plugin_name() != plugin_name:
            # if we are exporting plugin commands, skip plugins we have not specified.
            continue
        if plugin_name is None and command.plugin_name() not in core_plugins:
            # skip non-core plugins
            # TODO: Support extracting from third party plugins.
            continue
        note(gettext("Exporting messages from plugin command: {0} in {1}").format(
             cmd_name, command.plugin_name() ))
        _write_command_help(exporter, command)


def _error_messages(exporter):
    """Extract fmt string from bzrlib.errors."""
    path = errors.__file__
    if path.endswith('.pyc'):
        path = path[:-1]
    offsets = _offsets_of_literal(open(path).read())

    base_klass = errors.BzrError
    for name in dir(errors):
        klass = getattr(errors, name)
        if not inspect.isclass(klass):
            continue
        if not issubclass(klass, base_klass):
            continue
        if klass is base_klass:
            continue
        if klass.internal_error:
            continue
        fmt = getattr(klass, "_fmt", None)
        if fmt:
            note(gettext("Exporting message from error: %s"), name)
            exporter.poentry('bzrlib/errors.py',
                     offsets.get(fmt, 9999), fmt)

def _help_topics(exporter):
    topic_registry = help_topics.topic_registry
    for key in topic_registry.keys():
        doc = topic_registry.get(key)
        if isinstance(doc, str):
            exporter.poentry_per_paragraph(
                    'dummy/help_topics/'+key+'/detail.txt',
                    1, doc)
        elif callable(doc): # help topics from files
            exporter.poentry_per_paragraph(
                    'en/help_topics/'+key+'.txt',
                    1, doc(key))
        summary = topic_registry.get_summary(key)
        if summary is not None:
            exporter.poentry('dummy/help_topics/'+key+'/summary.txt',
                     1, summary)

def export_pot(outf, plugin=None):
    exporter = _PotExporter(outf)
    if plugin is None:
        _standard_options(exporter)
        _command_helps(exporter)
        _error_messages(exporter)
        _help_topics(exporter)
    else:
        _command_helps(exporter, plugin)
