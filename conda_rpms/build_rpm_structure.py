#!/usr/bin/env python
"""
Turn the gitenv into RPM spec files which can be built at a later stage.

"""
from __future__ import print_function

import os
import shutil

import conda.api
import conda.fetch
from conda.resolve import Resolve, MatchSpec
from conda_gitenv import manifest_branch_prefix
from conda_gitenv.deploy import tags_by_label, tags_by_env
from conda_gitenv.resolve import tempdir, create_tracking_branches
from git import Repo
import yaml

import logging
import conda_rpms.generate as generate
import conda_rpms.install as conda_install


class Config(dict):
    def __init__(self, fname, store=None, key=None):
        self.fname = os.path.abspath(os.path.expanduser(fname))
        self._store = store
        if store is None:
            self._load()
        self._key = []
        if key is not None:
            self._key = key

    def _load(self):
        if not os.path.exists(self.fname):
            emsg = 'The configuration file {!r} does not exist.'
            raise ValueError(emsg.format(os.path.basename(self.fname)))
        with open(self.fname, 'r') as fh:
            try:
                self._store = yaml.safe_load(fh)
            except yaml.YAMLError as e:
                emsg = 'YAML error in configuration file {!r}: {}'
                line, column = e.problem_mark.line, e.problem_mark.column
                ymsg = '{}, line {}, column {}.'.format(e.context,
                                                        line+1,
                                                        column+1)
                raise ValueError(emsg.format(os.path.basename(self.fname),
                                             ymsg))

    def __getitem__(self, key):
        try:
            result = self._store[key]
        except KeyError:
            emsg = 'The YAML file {!r} does not contain key [{}].'
            full_key = ']['.join(self._key + [key])
            raise ValueError(emsg.format(os.path.basename(self.fname),
                                         full_key))
        if isinstance(result, dict):
            result = Config(self.fname, result, self._key + [key])
        return result

    def __contains__(self, key):
        return key in self._store

    def __iter__(self):
        return iter(self._store)

    def __len__(self):
        return len(self._store)

    def __repr__(self):
        return repr(self._store)


def create_rpmbuild_for_env(pkgs, target, config):
    rpm_prefix = config['rpm']['prefix']
    pkg_cache = os.path.join(target, 'SOURCES')
    pkg_names = set(pkg for _, pkg in pkgs)
    if os.path.exists(target):
        # The environment we want to deploy already exists. We should
        # just double check that there aren't already packages in there which
        # we need to remove before we install anything new.
        linked = conda_install.linked(target)
        for pkg in linked:
            if pkg not in pkg_names:
                conda_install.unlink(target, pkg)
    else:
        linked = []

    if set(linked) == pkg_names:
        # We don't need to re-link everything - it is already as expected.
        # The downside is that we are not verifying that each package is
        # installed correctly.
        return

    spec_dir = os.path.join(target, 'SPECS')
    if not os.path.exists(spec_dir):
        os.makedirs(spec_dir)
    for source, pkg in pkgs:
        index = conda.fetch.fetch_index([source], use_cache=False)
        pkg_index = {pkg_info['fn']: pkg_info for pkg_info in index.values()}
        tar_name = pkg + '.tar.bz2'
        pkg_info = pkg_index.get(tar_name, None)
        if pkg_info is None:
            raise ValueError('Distribution {} is no longer available '
                             'in the channel {}.'.format(tar_name, source))
        dist_name = pkg 
        if not conda_install.is_fetched(pkg_cache, dist_name):
            print('Fetching {}'.format(dist_name))
            conda.fetch.fetch_pkg(pkg_info, pkg_cache)
        spec_path = os.path.join(spec_dir, '{}-pkg-{}.spec'.format(rpm_prefix,
                                                                   pkg))
        if not os.path.exists(spec_path):
            spec = generate.render_dist_spec(os.path.join(pkg_cache,
                                                          tar_name), config)
            with open(spec_path, 'w') as fh:
                fh.write(spec)


def create_rpmbuild_for_tag(repo, tag_name, target, config):
    rpm_prefix = config['rpm']['prefix']
    print("CREATE FOR {}".format(tag_name))
    tag = repo.tags[tag_name]
    # Checkout the tag in a detached head form.
    repo.head.reference = tag.commit
    repo.head.reset(working_tree=True)

    manifest_fname = os.path.join(repo.working_dir, 'env.manifest')
    if not os.path.exists(manifest_fname):
        raise ValueError("The tag '{}' doesn't have a manifested "
                         "environment.".format(tag_name))
    with open(manifest_fname, 'r') as fh:
        manifest = sorted(line.strip().split('\t') for line in fh)

    spec_fname = os.path.join(repo.working_dir, 'env.spec')
    if not os.path.exists(spec_fname):
        raise ValueError("The tag '{}' doesn't have an environment specification.".format(tag_name))
    with open(spec_fname, 'r') as fh:
        env_spec = yaml.safe_load(fh).get('env', [])
    create_rpmbuild_for_env(manifest, target, config)

    index = conda.fetch.fetch_index(list(set([url for url, _ in manifest])),
                                    use_cache=False)
    resolver = Resolve(index)

    # To sort, the distributions must match the format of the keys of the index.
    # For example, most will look like `http://channel::pkg
    # However channels on anaconda go by their name rather than their url,
    # i.e. `conda-forge::pkg`
    dists = []
    for url, pkg in manifest:
        anaconda_url = 'https://conda.anaconda.org/'
        if url.startswith(anaconda_url):
            url = url[len(anaconda_url):]
        dists.append('::'.join([os.path.dirname(url), pkg]))
    sorted_dists = resolver.dependency_sort(dists)
    sorted_pkgs = [dist.split('::')[-1] for dist in sorted_dists]

    env_name, tag = tag_name.split('-', 2)[1:]
    fname = '{}-env-{}-tag-{}.spec'.format(rpm_prefix, env_name, tag)
    with open(os.path.join(target, 'SPECS', fname), 'w') as fh:
        fh.write(generate.render_taggedenv(env_name, tag, sorted_pkgs, config,
                                           env_spec))


def create_rpmbuild_content(repo, target, config, state):
    rpm_prefix = config['rpm']['prefix']
    for branch in repo.branches:
        # We only want environment branches, not manifest branches.
        if not branch.name.startswith(manifest_branch_prefix):
            manifest_branch_name = manifest_branch_prefix + branch.name
            # If there is no equivalent manifest branch, we need to
            # skip this environment.
            if manifest_branch_name not in repo.branches:
                continue
            branch.checkout()
            fname = os.path.join(repo.working_dir, 'labels')
            branch_labelled_tags = tags_by_label(fname)

            # Get the number of commits in this branch, and use this as the
            # version number in the environment label RPM spec.
            commit_num = branch.commit.count()

            # Determine the environment tags that are new or have been changed
            # for each branch label, and thus require to have its associated
            # RPM build.
            if branch.name in state:
                # This branch has been built before and has a history ...
                labelled_tags = {}
                for label, tag in branch_labelled_tags.items():
                    if label in state[branch.name] and \
                            tag in state[branch.name][label]:
                        # Skip - the associated RPM already exists.
                        continue
                    # This label or this labels tag is new, so register it
                    # for RPM building.
                    labelled_tags[label] = tag
            else:
                # This is a new environment branch, so register all of its
                # labels for RPM building.
                labelled_tags = branch_labelled_tags

            # Keep track of the labels which have tags - its those we want.
            for label, tag in sorted(labelled_tags.items()):
                create_rpmbuild_for_tag(repo, tag, target, config)
                fname = '{}-env-{}-label-{}.spec'.format(rpm_prefix, branch.name, label)
                with open(os.path.join(target, 'SPECS', fname), 'w') as fh:
                    fh.write(generate.render_env(branch.name, label,
                                                 config, tag, commit_num))


def create_rpm_installer(target, config, python_spec='python'):
    rpm_prefix = config['rpm']['prefix']
    index = conda.api.get_index()
    matches = Resolve(index).get_pkgs(MatchSpec(python_spec))
    if not matches:
        raise RuntimeError('No python found in the channels.')
    # Pick the latest Python match.
    pkg_info = sorted(matches)[-1].info
    dist_name = '{}-{}-{}'.format(pkg_info['name'], pkg_info['version'],
                                  pkg_info['build'])
    pkg_cache = os.path.join(target, 'SOURCES') 
    if not conda_install.is_fetched(pkg_cache, dist_name):
        print('Fetching {}'.format(dist_name))
        conda.fetch.fetch_pkg(pkg_info, pkg_cache)

    installer_source = os.path.join(os.path.dirname(__file__), 'install.py')
    installer_target = os.path.join(pkg_cache, 'install.py')

    shutil.copyfile(installer_source, installer_target)

    spec_dir = os.path.join(target, 'SPECS')
    if not os.path.exists(spec_dir):
        os.makedirs(spec_dir)

    specfile = os.path.join(spec_dir, '{}-installer.spec'.format(rpm_prefix))
    with open(specfile, 'w') as fh:
        fh.write(generate.render_installer(pkg_info, config))


def configure_parser(parser):
    parser.add_argument('repo_uri', help='Repo to deploy.')
    parser.add_argument('target', help='Location to put the RPMBUILD content.')
    parser.add_argument('--config', '-c', type=str, default='config.yaml',
                        help='YAML configuration filename.')
    parser.add_argument('--state', '-s',
                        help='YAML label RPM state filename.')
    parser.set_defaults(function=handle_args)
    return parser


def handle_args(args):
    # To reduce the noise coming from conda/conda-build we set
    # all loggers to WARN level.
    logging.getLogger('').setLevel(logging.WARNING)
    for logger_name in logging.Logger.manager.loggerDict.keys():
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.WARNING)

    config = Config(args.config)
    state = {}
    if args.state is not None:
        fname = os.path.abspath(os.path.expanduser(args.state))
        with open(fname, 'r') as fi:
            state = yaml.safe_load(fi) or {}
    with tempdir() as repo_directory:
        repo = Repo.clone_from(args.repo_uri, repo_directory)
        create_tracking_branches(repo)
        create_rpmbuild_content(repo, args.target, config, state)
        create_rpm_installer(args.target, config)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Deploy the tracked '
                                                 'environments.')
    configure_parser(parser)
    args = parser.parse_args()
    return args.function(args)


if __name__ == '__main__':
    main()
