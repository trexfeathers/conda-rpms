# (c) 2012-2014 Continuum Analytics, Inc. / http://continuum.io
# All Rights Reserved
#
# conda is distributed under the terms of the BSD 3-clause license.
# Consult LICENSE.txt or http://opensource.org/licenses/BSD-3-Clause.
from __future__ import print_function, division, absolute_import
"""
This is a copy of the conda/install.py script, with the additional
"fake prefix" modification roughly proposed in
https://github.com/conda/conda/pull/1222 and a collection of functions from
conda that add support for the installation of noarch python packages.

In addition, modified the get_python_version function to correctly determine
the linked version of python available in the distribution.

"""


''' This module contains:
  * all low-level code for extracting, linking and unlinking packages
  * a very simple CLI

These API functions have argument names referring to:

    dist:        canonical package name (e.g. 'numpy-1.6.2-py26_0')

    pkgs_dir:    the "packages directory" (e.g. '/opt/anaconda/pkgs' or
                 '/home/joe/envs/.pkgs')

    prefix:      the prefix of a particular environment, which may also
                 be the "default" environment (i.e. sys.prefix),
                 but is otherwise something like '/opt/anaconda/envs/foo',
                 or even any prefix, e.g. '/home/joe/myenv'

Also, this module is directly invoked by the (self extracting (sfx)) tarball
installer to create the initial environment, therefore it needs to be
standalone, i.e. not import any other parts of `conda` (only depend on
the standard library).
'''

import json
import logging
import os
from os.path import abspath, basename, dirname, exists, isdir, isfile, islink, \
    join, lexists, split, splitext
import re
import shlex
import shutil
import stat
from stat import S_IMODE, S_IXGRP, S_IXOTH, S_IXUSR
import subprocess
import sys
import tarfile
from textwrap import dedent
import time
import traceback
import warnings


try:
    from conda.lock import Locked
except ImportError:
    # Make sure this still works as a standalone script for the Anaconda
    # installer.
    class Locked(object):
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            pass
        def __exit__(self, exc_type, exc_value, traceback):
            pass

on_win = bool(sys.platform == 'win32')

if on_win:
    import ctypes
    from ctypes import wintypes

    # on Windows we cannot update these packages in the root environment
    # because of the file lock problem
    win_ignore_root = set(['python', 'pycosat', 'psutil'])

    CreateHardLink = ctypes.windll.kernel32.CreateHardLinkW
    CreateHardLink.restype = wintypes.BOOL
    CreateHardLink.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                               wintypes.LPVOID]
    try:
        CreateSymbolicLink = ctypes.windll.kernel32.CreateSymbolicLinkW
        CreateSymbolicLink.restype = wintypes.BOOL
        CreateSymbolicLink.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                       wintypes.DWORD]
    except AttributeError:
        CreateSymbolicLink = None

    def win_hard_link(src, dst):
        "Equivalent to os.link, using the win32 CreateHardLink call."
        if not CreateHardLink(dst, src, None):
            raise OSError('win32 hard link failed')

    def win_soft_link(src, dst):
        "Equivalent to os.symlink, using the win32 CreateSymbolicLink call."
        if CreateSymbolicLink is None:
            raise OSError('win32 soft link not supported')
        if not CreateSymbolicLink(dst, src, isdir(src)):
            raise OSError('win32 soft link failed')


log = logging.getLogger(__name__)
stdoutlog = logging.getLogger('stdoutlog')

class NullHandler(logging.Handler):
    """ Copied from Python 2.7 to avoid getting
        `No handlers could be found for logger "patch"`
        http://bugs.python.org/issue16539
    """
    def handle(self, record):
        pass
    def emit(self, record):
        pass
    def createLock(self):
        self.lock = None

log.addHandler(NullHandler())

LINK_HARD = 1
LINK_SOFT = 2
LINK_COPY = 3
link_name_map = {
    LINK_HARD: 'hard-link',
    LINK_SOFT: 'soft-link',
    LINK_COPY: 'copy',
}

def _link(src, dst, linktype=LINK_HARD):
    if linktype == LINK_HARD:
        if on_win:
            win_hard_link(src, dst)
        else:
            os.link(src, dst)
    elif linktype == LINK_SOFT:
        if on_win:
            win_soft_link(src, dst)
        else:
            os.symlink(src, dst)
    elif linktype == LINK_COPY:
        # copy relative symlinks as symlinks
        if not on_win and islink(src) and not os.readlink(src).startswith('/'):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
    else:
        raise Exception("Did not expect linktype=%r" % linktype)


def _remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rm_rf(path, max_retries=5):
    """
    Completely delete path

    max_retries is the number of times to retry on failure. The default is
    5. This only applies to deleting a directory.

    """
    if islink(path) or isfile(path):
        # Note that we have to check if the destination is a link because
        # exists('/path/to/dead-link') will return False, although
        # islink('/path/to/dead-link') is True.
        os.unlink(path)

    elif isdir(path):
        for i in range(max_retries):
            try:
                shutil.rmtree(path)
                return
            except OSError as e:
                msg = "Unable to delete %s\n%s\n" % (path, e)
                if on_win:
                    try:
                        shutil.rmtree(path, onerror=_remove_readonly)
                        return
                    except OSError as e1:
                        msg += "Retry with onerror failed (%s)\n" % e1

                    try:
                        subprocess.check_call(['cmd', '/c', 'rd', '/s', '/q', path])
                        return
                    except subprocess.CalledProcessError as e2:
                        msg += '%s\n' % e2
                log.debug(msg + "Retrying after %s seconds..." % i)
                time.sleep(i)
        # Final time. pass exceptions to caller.
        shutil.rmtree(path)

def rm_empty_dir(path):
    """
    Remove the directory `path` if it is a directory and empty.
    If the directory does not exist or is not empty, do nothing.
    """
    try:
        os.rmdir(path)
    except OSError: # directory might not exist or not be empty
        pass


def yield_lines(path):
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        yield line


prefix_placeholder = ('/opt/anaconda1anaconda2'
                      # this is intentionally split into parts,
                      # such that running this program on itself
                      # will leave it unchanged
                      'anaconda3')

def read_has_prefix(path):
    """
    reads `has_prefix` file and return dict mapping filenames to
    tuples(placeholder, mode)
    """
    res = {}
    try:
        for line in yield_lines(path):
            try:
                placeholder, mode, f = [x.strip('"\'') for x in
                                        shlex.split(line, posix=False)]
                res[f] = (placeholder, mode)
            except ValueError:
                res[line] = (prefix_placeholder, 'text')
    except IOError:
        pass
    return res

class PaddingError(Exception):
    pass

def binary_replace(data, a, b):
    """
    Perform a binary replacement of `data`, where the placeholder `a` is
    replaced with `b` and the remaining string is padded with null characters.
    All input arguments are expected to be bytes objects.
    """
    import re

    def replace(match):
        occurances = match.group().count(a)
        padding = (len(a) - len(b))*occurances
        if padding < 0:
            raise PaddingError(a, b, padding)
        return match.group().replace(a, b) + b'\0' * padding
    pat = re.compile(re.escape(a) + b'([^\0]*?)\0')
    res = pat.sub(replace, data)
    assert len(res) == len(data)
    return res

def update_prefix(path, new_prefix, placeholder=prefix_placeholder,
                  mode='text'):
    if on_win and (placeholder != prefix_placeholder) and ('/' in placeholder):
        # original prefix uses unix-style path separators
        # replace with unix-style path separators
        new_prefix = new_prefix.replace('\\', '/')

    path = os.path.realpath(path)
    with open(path, 'rb') as fi:
        data = fi.read()
    if mode == 'text':
        new_data = data.replace(placeholder.encode('utf-8'),
                                new_prefix.encode('utf-8'))
    elif mode == 'binary':
        new_data = binary_replace(data, placeholder.encode('utf-8'),
                                  new_prefix.encode('utf-8'))
    else:
        sys.exit("Invalid mode:" % mode)

    if new_data == data:
        return
    st = os.lstat(path)
    with open(path, 'wb') as fo:
        fo.write(new_data)
    os.chmod(path, stat.S_IMODE(st.st_mode))


def name_dist(dist):
    return dist.rsplit('-', 2)[0]


def create_meta(prefix, dist, info_dir, extra_info):
    """
    Create the conda metadata, in a given prefix, for a given package.
    """
    # read info/index.json first
    with open(join(info_dir, 'index.json')) as fi:
        meta = json.load(fi)
    # add extra info
    meta.update(extra_info)
    # write into <env>/conda-meta/<dist>.json
    meta_dir = join(prefix, 'conda-meta')
    if not isdir(meta_dir):
        os.makedirs(meta_dir)
    with open(join(meta_dir, dist + '.json'), 'w') as fo:
        json.dump(meta, fo, indent=2, sort_keys=True)


def mk_menus(prefix, files, remove=False):
    if abspath(prefix) != abspath(sys.prefix):
        # we currently only want to create menu items for packages
        # in default environment
        return
    menu_files = [f for f in files
                  if f.startswith('Menu/') and f.endswith('.json')]
    if not menu_files:
        return
    try:
        import menuinst
    except ImportError:
        return
    for f in menu_files:
        try:
            menuinst.install(join(prefix, f), remove, prefix)
        except:
            stdoutlog.error("menuinst Exception:")
            stdoutlog.error(traceback.format_exc())


def run_script(prefix, dist, action='post-link', env_prefix=None):
    """
    call the post-link (or pre-unlink) script, and return True on success,
    False on failure
    """
    path = join(prefix, 'Scripts' if on_win else 'bin', '.%s-%s.%s' % (
            name_dist(dist),
            action,
            'bat' if on_win else 'sh'))
    if not isfile(path):
        return True
    if on_win:
        try:
            args = [os.environ['COMSPEC'], '/c', path]
        except KeyError:
            return False
    else:
        args = ['/bin/bash', path]
    env = os.environ
    env['PREFIX'] = str(env_prefix or prefix)
    env['PKG_NAME'], env['PKG_VERSION'], env['PKG_BUILDNUM'] = \
                str(dist).rsplit('-', 2)
    if action == 'pre-link':
        env['SOURCE_DIR'] = str(prefix)
    try:
        subprocess.check_call(args, env=env)
    except subprocess.CalledProcessError:
        return False
    return True


def read_url(pkgs_dir, dist):
    try:
        data = open(join(pkgs_dir, 'urls.txt')).read()
        urls = data.split()
        for url in urls[::-1]:
            if url.endswith('/%s.tar.bz2' % dist):
                return url
    except IOError:
        pass
    return None


def read_icondata(source_dir):
    import base64

    try:
        data = open(join(source_dir, 'info', 'icon.png'), 'rb').read()
        return base64.b64encode(data).decode('utf-8')
    except IOError:
        pass
    return None

def read_no_link(info_dir):
    res = set()
    for fn in 'no_link', 'no_softlink':
        try:
            res.update(set(yield_lines(join(info_dir, fn))))
        except IOError:
            pass
    return res

# Should this be an API function?
def symlink_conda(prefix, root_dir):
    root_conda = join(root_dir, 'bin', 'conda')
    root_activate = join(root_dir, 'bin', 'activate')
    root_deactivate = join(root_dir, 'bin', 'deactivate')
    prefix_conda = join(prefix, 'bin', 'conda')
    prefix_activate = join(prefix, 'bin', 'activate')
    prefix_deactivate = join(prefix, 'bin', 'deactivate')
    if not os.path.lexists(join(prefix, 'bin')):
        os.makedirs(join(prefix, 'bin'))
    if not os.path.lexists(prefix_conda):
        os.symlink(root_conda, prefix_conda)
    if not os.path.lexists(prefix_activate):
        os.symlink(root_activate, prefix_activate)
    if not os.path.lexists(prefix_deactivate):
        os.symlink(root_deactivate, prefix_deactivate)

# ========================== begin noarch functions =========================
# Below are a collection of functions that are used when installing noarch
# packages. The functions have been copied (and in some places modified) from
# conda at commit sha be8c08c083f4d5e05b06bd2689d2cd0d410c2ffe.

python_entry_point_template = dedent("""
# -*- coding: utf-8 -*-
import re
import sys
from %(module)s import %(import_name)s
if __name__ == '__main__':
    sys.argv[0] = re.sub(r'(-script\.pyw?|\.exe)?$', '', sys.argv[0])
    sys.exit(%(func)s())
""").lstrip()

# three capture groups: whole_shebang, executable, options
SHEBANG_REGEX = (br'^(#!'  # pretty much the whole match string
                 br'(?:[ ]*)'  # allow spaces between #! and beginning of the executable path
                 br'(/(?:\\ |[^ \n\r\t])*)'  # the executable is the next text block without an escaped space or non-space whitespace character  # NOQA
                 br'(.*)'  # the rest of the line can contain option flags
                 br')$')  # end whole_shebang group


def pyc_path(py_path, python_major_minor_version):
    """
    Copy of conda/common/path.py:pyc_path at
    be8c08c083f4d5e05b06bd2689d2cd0d410c2ffe.

    """
    pyver_string = python_major_minor_version.replace('.', '')
    if pyver_string.startswith('2'):
        return py_path + 'c'
    else:
        directory, py_file = split(py_path)
        basename_root, extension = splitext(py_file)
        pyc_file = "__pycache__/%s.cpython-%s%sc" % (basename_root, pyver_string, extension)
        return "%s/%s" % (directory, pyc_file) if directory else pyc_file


def get_python_noarch_target_path(source_short_path, target_site_packages_short_path):
    """
    Modified version of conda/common/path.py:get_python_noarch_target_path at
    be8c08c083f4d5e05b06bd2689d2cd0d410c2ffe.

    Modifications included:
        * removed windows specific support

    """
    if source_short_path.startswith('site-packages/'):
        sp_dir = target_site_packages_short_path
        return source_short_path.replace('site-packages', sp_dir, 1)
    elif source_short_path.startswith('python-scripts/'):
        bin_dir = 'bin'
        return source_short_path.replace('python-scripts', bin_dir, 1)
    else:
        return source_short_path


def compile_pyc(python_exe_full_path, py_full_path, pyc_full_path):
    """
    Modified version of conda/gateways/disk/create.py:compile_pyc at
    be8c08c083f4d5e05b06bd2689d2cd0d410c2ffe.

    Modification included:
        * changed log.trace -> log.info (log.trace not supported)

    """
    if os.path.lexists(pyc_full_path):
        warnings.warn('{} already exists'.format(pyc_full_path))
    command = [python_exe_full_path, '-Wi', '-m', 'py_compile', py_full_path]
    subprocess.call(command)

    if not isfile(pyc_full_path):
        message = """
                pyc file failed to compile successfully
                  python_exe_full_path: %()s\n
                  py_full_path: %()s\n
                  pyc_full_path: %()s\n
                """
        log.info(message, python_exe_full_path, py_full_path, pyc_full_path)
        return None

    return pyc_full_path


def parse_entry_point_def(ep_definition):
    """
    Copy of conda/common/path.py:parse_entry_point_def at
    be8c08c083f4d5e05b06bd2689d2cd0d410c2ffe.

    """
    cmd_mod, func = ep_definition.rsplit(':', 1)
    command, module = cmd_mod.rsplit("=", 1)
    command, module, func = command.strip(), module.strip(), func.strip()
    return command, module, func


def make_executable(path):
    """
    Modified version of conda/gateways/disk/permissions.py:make_executable at
    be8c08c083f4d5e05b06bd2689d2cd0d410c2ffe.

    Modifications included:
        * changed lstat -> os.lstat
        * changed chmod -> os.chmod
        * changed log.trace -> log.info (log.trace not supported)

    """
    if isfile(path):
        mode = os.lstat(path).st_mode
        log.info('chmod +x %s', path)
        os.chmod(path, S_IMODE(mode) | S_IXUSR | S_IXGRP | S_IXOTH)
    else:
        log.error("Cannot make path '%s' executable", path)


def replace_long_shebang(data):
    """
    Modified version of conda/core/portability.py:replace_long_shebang
    at be8c08c083f4d5e05b06bd2689d2cd0d410c2ffe.

    Modifications included:
        * removed mode check for non-binary shebang

    """
    shebang_match = re.match(SHEBANG_REGEX, data, re.MULTILINE)
    if shebang_match:
        whole_shebang, executable, options = shebang_match.groups()
        if len(whole_shebang) > 127:
            executable_name = executable.decode('utf-8').split('/')[-1]
            new_shebang = '#!/usr/bin/env %s%s' % (
            executable_name, options.decode('utf-8'))
            data = data.replace(whole_shebang, new_shebang.encode('utf-8'))
    return data


def create_python_entry_point(target_full_path, python_full_path, module, func):
    """
    Modified version of conda/gateways/disk/create.py:create_python_entry_point
    at be8c08c083f4d5e05b06bd2689d2cd0d410c2ffe.

    Modifications included:
        * Modified the error for the check that the entry point already
        exists to raise a warning rather than an error

    """

    if os.path.lexists(target_full_path):
        warnings.warn('Entrypoint {} already exists.'.format(target_full_path))

    import_name = func.split('.')[0]
    pyscript = python_entry_point_template % {
        'module': module,
        'func': func,
        'import_name': import_name,
    }

    if python_full_path is not None:
        shebang = '#!%s\n' % python_full_path
        if hasattr(shebang, 'encode'):
            shebang = shebang.encode()

        shebang = replace_long_shebang(shebang)

        if hasattr(shebang, 'decode'):
            shebang = shebang.decode()
    else:
        shebang = None

    with open(target_full_path, str('w')) as fo:
        if shebang is not None:
            fo.write(shebang)
        fo.write(pyscript)

    if shebang is not None:
        make_executable(target_full_path)

    return target_full_path


def get_python_version(prefix):
    """
    Returns the version of the python that is already linked in the environment.

    Args:
        * prefix - path to environment

    """
    py_ver = None
    for dist in linked(prefix):
        match = re.search('^python-(\d+.\d+)', dist)
        if match:
            py_ver = match.group(1)
            break
    if py_ver is None:
        log.info('Python has not been linked in the environment')
    return py_ver

# =========================== end noarch functions ==========================


# ========================== begin API functions =========================

def try_hard_link(pkgs_dir, prefix, dist):
    src = join(pkgs_dir, dist, 'info', 'index.json')
    dst = join(prefix, '.tmp-%s' % dist)
    assert isfile(src), src
    assert not isfile(dst), dst
    if not isdir(prefix):
        os.makedirs(prefix)
    try:
        _link(src, dst, LINK_HARD)
        return True
    except OSError:
        return False
    finally:
        rm_rf(dst)
        rm_empty_dir(prefix)

# ------- package cache ----- fetched

def fetched(pkgs_dir):
    if not isdir(pkgs_dir):
        return set()
    return set(fn[:-8] for fn in os.listdir(pkgs_dir)
               if fn.endswith('.tar.bz2'))

def is_fetched(pkgs_dir, dist):
    return isfile(join(pkgs_dir, dist + '.tar.bz2'))

def rm_fetched(pkgs_dir, dist):
    with Locked(pkgs_dir):
        path = join(pkgs_dir, dist + '.tar.bz2')
        rm_rf(path)

# ------- package cache ----- extracted

def extracted(pkgs_dir):
    """
    return the (set of canonical names) of all extracted packages
    """
    if not isdir(pkgs_dir):
        return set()
    return set(dn for dn in os.listdir(pkgs_dir)
               if (isfile(join(pkgs_dir, dn, 'info', 'files')) and
                   isfile(join(pkgs_dir, dn, 'info', 'index.json'))))

def extract(pkgs_dir, dist):
    """
    Extract a package, i.e. make a package available for linkage.  We assume
    that the compressed packages is located in the packages directory.
    """
    with Locked(pkgs_dir):
        path = join(pkgs_dir, dist)
        t = tarfile.open(path + '.tar.bz2')
        t.extractall(path=path)
        t.close()
        if sys.platform.startswith('linux') and os.getuid() == 0:
            # When extracting as root, tarfile will by restore ownership
            # of extracted files.  However, we want root to be the owner
            # (our implementation of --no-same-owner).
            for root, dirs, files in os.walk(path):
                for fn in files:
                    p = join(root, fn)
                    os.lchown(p, 0, 0)

def is_extracted(pkgs_dir, dist):
    return (isfile(join(pkgs_dir, dist, 'info', 'files')) and
            isfile(join(pkgs_dir, dist, 'info', 'index.json')))

def rm_extracted(pkgs_dir, dist):
    with Locked(pkgs_dir):
        path = join(pkgs_dir, dist)
        rm_rf(path)

# ------- linkage of packages

def linked(prefix):
    """
    Return the (set of canonical names) of linked packages in prefix.
    """
    meta_dir = join(prefix, 'conda-meta')
    if not isdir(meta_dir):
        return set()
    return set(fn[:-5] for fn in os.listdir(meta_dir) if fn.endswith('.json'))


def is_linked(prefix, dist):
    """
    Return the install meta-data for a linked package in a prefix, or None
    if the package is not linked in the prefix.
    """
    meta_path = join(prefix, 'conda-meta', dist + '.json')
    try:
        with open(meta_path) as fi:
            return json.load(fi)
    except IOError:
        return None


def link(pkgs_dir, prefix, dist, linktype=LINK_HARD, index=None, target_prefix=None):
    '''
    Set up a package in a specified (environment) prefix.  We assume that
    the package has been extracted (using extract() above).
    '''
    if target_prefix is None:
        target_prefix = prefix
    index = index or {}
    log.debug('pkgs_dir=%r, prefix=%r, target_prefix=%r, dist=%r, linktype=%r' %
              (pkgs_dir, prefix, target_prefix, dist, linktype))
    if (on_win and abspath(prefix) == abspath(sys.prefix) and
              name_dist(dist) in win_ignore_root):
        # on Windows we have the file lock problem, so don't allow
        # linking or unlinking some packages
        log.warn('Ignored: %s' % dist)
        return

    source_dir = join(pkgs_dir, dist)
    if not run_script(prefix, dist, 'pre-link', target_prefix):
        sys.exit('Error: pre-link failed: %s' % dist)

    info_dir = join(source_dir, 'info')
    files = list(yield_lines(join(info_dir, 'files')))
    has_prefix_files = read_has_prefix(join(info_dir, 'has_prefix'))
    no_link = read_no_link(info_dir)

    noarch = False
    # If the distribution is noarch, it will contain a `link.json` file in
    # the info_dir
    with open(join(info_dir, 'index.json'), 'r') as fh:
        index_data = json.loads(fh.read())
    if 'noarch' in index_data:
        noarch = index_data['noarch']
    elif 'noarch_python' in index_data:
        # `noarch_python` has been deprecated.
        if index_data['noarch_python'] is True:
            noarch = 'python'

    if noarch == 'python':
        if on_win:
            raise ValueError('Windows is not supported.')

        link_json = join(info_dir, 'link.json')
        if exists(link_json):
            with open(link_json, 'r') as fh:
                link_data = json.loads(fh.read())
            if 'noarch' in link_data:
                noarch_json = link_data['noarch']

        target_py_version = get_python_version(prefix)
        target_python_short_path = join('bin', 'python{}'.format(
            target_py_version))
        target_site_packages = join('lib', 'python{}'.format(
            target_py_version), 'site-packages')

    # A list of the files, including pyc files and entrypoints, that will be
    # added to the metadata.
    all_files = []

    with Locked(prefix), Locked(pkgs_dir):
        for f in files:
            src = join(source_dir, f)

            if noarch == 'python':
                noarch_f = get_python_noarch_target_path(f,
                                                         target_site_packages)
                dst = join(prefix, noarch_f)
                all_files.append(noarch_f)
            # Non-noarch packages do not need special handling of the
            # site-packages
            else:
                dst = join(prefix, f)
                all_files.append(f)

            dst_dir = dirname(dst)
            if not isdir(dst_dir):
                os.makedirs(dst_dir)
            if os.path.exists(dst):
                log.warn("file already exists: %r" % dst)
                try:
                    os.unlink(dst)
                except OSError:
                    log.error('failed to unlink: %r' % dst)
            lt = linktype
            if f in has_prefix_files or f in no_link or islink(src):
                lt = LINK_COPY
            try:
                _link(src, dst, lt)
            except OSError as e:
                log.error('failed to link (src=%r, dst=%r, type=%r, error=%r)' %
                          (src, dst, lt, e))

        # noarch package specific installation steps
        if noarch == 'python':
            # Create entrypoints
            if 'entry_points' in noarch_json:
                for entry_point in noarch_json['entry_points']:

                    command, module, func = parse_entry_point_def(entry_point)
                    entry_point_file = create_python_entry_point(
                        join(prefix, 'bin', command),
                        join(prefix, target_python_short_path), module, func)
                    all_files.append(entry_point_file)

            # Compile pyc files
            for f in all_files:
                if f.endswith('.py'):
                    py_path = join(prefix, f)
                    pyc_filepath = compile_pyc(
                        join(prefix,
                        target_python_short_path),
                        py_path,
                        pyc_path(py_path, target_py_version))
                    if pyc_filepath.startswith(prefix):
                        all_files.append(pyc_filepath[len(prefix):])

        if name_dist(dist) == '_cache':
            return

        for f in sorted(has_prefix_files):
            placeholder, mode = has_prefix_files[f]
            try:
                update_prefix(join(prefix, f), target_prefix, placeholder, mode)
            except PaddingError:
                sys.exit("ERROR: placeholder '%s' too short in: %s\n" %
                         (placeholder, dist))

        mk_menus(prefix, files, remove=False)

        if not run_script(prefix, dist, 'post-link', target_prefix):
            sys.exit("Error: post-link failed for: %s" % dist)

        # Make sure the script stays standalone for the installer
        try:
            from conda.config import remove_binstar_tokens
        except ImportError:
            # There won't be any binstar tokens in the installer anyway
            def remove_binstar_tokens(url):
                return url

        meta_dict = index.get(dist + '.tar.bz2', {})
        meta_dict['url'] = read_url(pkgs_dir, dist)
        if meta_dict['url']:
            meta_dict['url'] = remove_binstar_tokens(meta_dict['url'])
        try:
            alt_files_path = join(prefix, 'conda-meta', dist + '.files')
            meta_dict['files'] = list(yield_lines(alt_files_path))
            os.unlink(alt_files_path)
        except IOError:
            meta_dict['files'] = all_files
        meta_dict['link'] = {'source': source_dir,
                             'type': link_name_map.get(linktype)}
        if 'channel' in meta_dict:
            meta_dict['channel'] = remove_binstar_tokens(meta_dict['channel'])
        if 'icon' in meta_dict:
            meta_dict['icondata'] = read_icondata(source_dir)

        create_meta(prefix, dist, info_dir, meta_dict)

def unlink(prefix, dist):
    '''
    Remove a package from the specified environment, it is an error if the
    package does not exist in the prefix.
    '''
    if (on_win and abspath(prefix) == abspath(sys.prefix) and
              name_dist(dist) in win_ignore_root):
        # on Windows we have the file lock problem, so don't allow
        # linking or unlinking some packages
        log.warn('Ignored: %s' % dist)
        return

    with Locked(prefix):
        run_script(prefix, dist, 'pre-unlink')

        meta_path = join(prefix, 'conda-meta', dist + '.json')
        with open(meta_path) as fi:
            meta = json.load(fi)

        mk_menus(prefix, meta['files'], remove=True)
        dst_dirs1 = set()

        for f in meta['files']:
            dst = join(prefix, f)
            dst_dirs1.add(dirname(dst))
            try:
                os.unlink(dst)
            except OSError: # file might not exist
                log.debug("could not remove file: '%s'" % dst)

        # remove the meta-file last
        os.unlink(meta_path)

        dst_dirs2 = set()
        for path in dst_dirs1:
            while len(path) > len(prefix):
                dst_dirs2.add(path)
                path = dirname(path)
        # in case there is nothing left
        dst_dirs2.add(join(prefix, 'conda-meta'))
        dst_dirs2.add(prefix)

        for path in sorted(dst_dirs2, key=len, reverse=True):
            rm_empty_dir(path)


def messages(prefix):
    path = join(prefix, '.messages.txt')
    try:
        with open(path) as fi:
            sys.stdout.write(fi.read())
    except IOError:
        pass
    finally:
        rm_rf(path)

# =========================== end API functions ==========================

def main():
    from pprint import pprint
    from optparse import OptionParser

    p = OptionParser(
        usage="usage: %prog [options] [TARBALL/NAME]",
        description="low-level conda install tool, by default extracts "
                    "(if necessary) and links a TARBALL")

    p.add_option('-l', '--list',
                 action="store_true",
                 help="list all linked packages")

    p.add_option('--extract',
                 action="store_true",
                 help="extract package in pkgs cache")

    p.add_option('--link',
                 action="store_true",
                 help="link a package")

    p.add_option('--unlink',
                 action="store_true",
                 help="unlink a package")

    p.add_option('--target-prefix',
                 default=None,
                 help="target prefix (defaults to prefix)")

    p.add_option('-p', '--prefix',
                 action="store",
                 default=sys.prefix,
                 help="prefix (defaults to %default)")

    p.add_option('--pkgs-dir',
                 action="store",
                 default=join(sys.prefix, 'pkgs'),
                 help="packages directory (defaults to %default)")

    p.add_option('--link-all',
                 action="store_true",
                 help="link all extracted packages")

    p.add_option('-v', '--verbose',
                 action="store_true")

    opts, args = p.parse_args()

    logging.basicConfig()

    if opts.list or opts.extract or opts.link_all:
        if args:
            p.error('no arguments expected')
    else:
        if len(args) == 1:
            dist = basename(args[0])
            if dist.endswith('.tar.bz2'):
                dist = dist[:-8]
        else:
            p.error('exactly one argument expected')

    pkgs_dir = opts.pkgs_dir
    prefix = opts.prefix
    target_prefix = opts.target_prefix
    if opts.verbose:
        print("pkgs_dir: %r" % pkgs_dir)
        print("prefix  : %r" % prefix)

    if opts.list:
        pprint(sorted(linked(prefix)))

    elif opts.link_all:
        dists = sorted(extracted(pkgs_dir))
        linktype = (LINK_HARD
                    if try_hard_link(pkgs_dir, prefix, dists[0]) else
                    LINK_COPY)
        if opts.verbose or linktype == LINK_COPY:
            print("linktype: %s" % link_name_map[linktype])
        for dist in dists:
            if opts.verbose or linktype == LINK_COPY:
                print("linking: %s" % dist)
            link(pkgs_dir, prefix, dist, linktype, target_prefix=target_prefix)
        messages(prefix)

    elif opts.extract:
        extract(pkgs_dir, dist)

    elif opts.link:
        link(pkgs_dir, prefix, dist, target_prefix=target_prefix)

    elif opts.unlink:
        unlink(prefix, dist)


if __name__ == '__main__':
    main()
