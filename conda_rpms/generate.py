import os
import jinja2


template_dir = os.path.join(os.path.dirname(__file__), 'templates')
loader = jinja2.FileSystemLoader(template_dir)

env = jinja2.Environment(loader=loader)
env_trim_blocks = jinja2.Environment(loader=loader, trim_blocks=True)

pkg_spec_tmpl = env.get_template('pkg.spec.template')
env_spec_tmpl = env_trim_blocks.get_template('env.spec.template')
taggedenv_spec_tmpl = env.get_template('taggedenv.spec.template')
installer_spec_tmpl = env.get_template('installer.spec.template')

import json
import re
import tarfile
import yaml

TAG_PATTERN = '^env-\w+-(\d{4}_\d{2}_\d{2}(-\d+)?)$'
tag_pattern = re.compile(TAG_PATTERN)
ENV_PATTERN = '^\s*set\s+modulesversion\s+[\'\"](.*)[\'\"]\s*$'
env_pattern = re.compile(ENV_PATTERN, re.IGNORECASE)


def render_dist_spec(dist, config):
    with tarfile.open(dist, 'r:bz2') as tar:
        m = tar.getmember('info/index.json')
        fh = tar.extractfile(m)
        import codecs

        reader = codecs.getreader("utf-8")
        pkginfo = json.load(reader(fh))

        try:
            m = tar.getmember('info/recipe.json')
        except KeyError:
            m = None

        if m:
            fh = tar.extractfile(m)
            meta = yaml.safe_load(reader(fh))
        else:
            meta = {}

    meta_about = meta.setdefault('about', {})
    meta_about.setdefault('license', pkginfo.get('license'))
    meta_about.setdefault('summary', 'The {} package'.format(pkginfo['name']))

    rpm_prefix = config['rpm']['prefix']
    install_prefix = config['install']['prefix']

    return pkg_spec_tmpl.render(pkginfo=pkginfo,
                                meta=meta,
                                rpm_prefix=rpm_prefix,
                                install_prefix=install_prefix)


def render_env(branch_name, label, config, tag, commit_num):
    rpm_prefix = config['rpm']['prefix']
    summary = 'A {} environment for {}/{}'.format(rpm_prefix,
                                                  branch_name,
                                                  label)
    env_info = {'url': 'http://link/to/gh',
                'name': branch_name,
                'label': label,
                'summary': summary,
                'version': commit_num,
                'prefix': config['install']['prefix']}
    module = dict(prefix=None)

    if 'module' in config:
        module['prefix'] = config['module']['prefix']
        fname = config['module']['file']
        module_loader = jinja2.FileSystemLoader(os.path.dirname(fname))
        module_env = jinja2.Environment(loader=module_loader)
        module_template = module_env.get_template(os.path.basename(fname))
        module['file'] = module_template.render(env=env_info)
        module['default'] = None
        if 'default' in config['module']:
            with open(config['module']['default'], 'r') as fi:
                lines = fi.readlines()
            for line in lines:
                match = env_pattern.match(line)
                if match:
                    module_name, module_label = match.group(1).split('-', 2)
                    module['name'] = module_name
                    module['label'] = module_label
                    break
            else:
                emsg = ('Cannot find environment name/label within default '
                        'modulefile "{}".')
                raise ValueError(emsg.format(config['module']['default']))
            module['default'] = ''.join(lines)

    # When multiple tags are produced in a day, they have an associated count
    # addded to the end e.g. env-default-2016_12_05-2, which needs to be parsed
    # correctly.
    match = tag_pattern.match(tag)
    if match is None:
        msg = "Cannot create an environment for the tag {}. The name of the " \
              "tag must follow the format " \
              "'env-<environment name>-YYYY-MM-DD(-<count> (optional))'"
        raise ValueError(msg.format(tag))
    tag_name = match.group(1)
    spec = env_spec_tmpl.render(rpm_prefix=rpm_prefix, env=env_info,
                                module=module, labelled_tag=tag_name)
    return spec


def render_taggedenv(env_name, tag, pkgs, config, env_spec):
    env_info = {'url': 'http://link/to/gh',
                'name': env_name,
                'tag': tag,
                'summary': 'An environment in which to rejoice.',
                'version': '1',
                'spec': '\n'.join(env_spec)}
    rpm_prefix = config['rpm']['prefix']
    install_prefix = config['install']['prefix']
    return taggedenv_spec_tmpl.render(install_prefix=install_prefix,
                                      pkgs=pkgs,
                                      rpm_prefix=rpm_prefix,
                                      env=env_info)


def render_installer(pkg_info, config):
    rpm_prefix = config['rpm']['prefix']
    install_prefix = config['install']['prefix']
    return installer_spec_tmpl.render(install_prefix=install_prefix,
                                      rpm_prefix=rpm_prefix,
                                      pkg_info=pkg_info)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("distribution")
    args = parser.parse_args()
    #print(render_dist_spec(args.distribution))
    #print(render_env('my_second_env', pkgs=['udunits2-2.2.20-0']))
    print(args)
    print(render_installer({'name': 'python', 'version': '2.11.1',
                            'build': '0'}))
