from mock import patch, MagicMock
import os

import conda_rpms.tests as tests
from conda_rpms.build_rpm_structure import create_rpmbuild_for_tag


CONFIG = {'rpm' : {'prefix' : 'SciTools'},
          'install' : {'prefix' : 'test_install_location'}}

ENV_SPEC = """channels:
 - https://conda.anaconda.org/conda-forge/linux-64
env:
 - python
"""

ENV_MANIFEST ="""https://conda.anaconda.org/conda-forge/linux-64\tca-certificates-2018.1.18-0
https://conda.anaconda.org/conda-forge/linux-64\tncurses-5.9-10
https://conda.anaconda.org/conda-forge/linux-64\topenssl-1.0.2n-0
https://conda.anaconda.org/conda-forge/linux-64\tpython-3.6.4-0
https://conda.anaconda.org/conda-forge/linux-64\tsqlite-3.20.1-2
https://conda.anaconda.org/conda-forge/linux-64\ttk-8.6.7-0
https://conda.anaconda.org/conda-forge/linux-64\txz-5.2.3-0
https://conda.anaconda.org/conda-forge/linux-64\tzlib-1.2.11-0
"""


class Test(tests.CommonTest):
    @patch('conda_rpms.build_rpm_structure.create_rpmbuild_for_env')
    def test_sorted_order(self, a):
        with self.temp_dir() as target:
            with self.temp_dir() as temp_repo:
                # Create fake env.spec and manifest files
                with open(os.path.join(temp_repo, 'env.spec'), 'w') as \
                        env_spec_file:
                    env_spec_file.write(ENV_SPEC)
                with open(os.path.join(temp_repo, 'env.manifest'), 'w') as \
                        env_manifest_file:
                    env_manifest_file.write(ENV_MANIFEST)

                # Set up arguments for call to create_rpmbuild_for_tag
                repo = MagicMock()
                repo.working_dir = temp_repo
                tag_name = 'env-default-2018_03_26'
                # Create directory that the spec file will be written to
                os.mkdir(os.path.join(target, 'SPECS'))
                create_rpmbuild_for_tag(repo, tag_name, target, CONFIG)

            # Compare the written spec file with what we'd expect
            with open(os.path.join(target, 'SPECS',
                                   'SciTools-env-default-tag-2018_03_26.spec'),
                      'r') as fh:
                result_order = []
                for line in fh.readlines():
                    # Parse the lines of the spec file that are formatted as
                    # `  {INSTALL} xz-5.2.3-0\n`
                    line = line.strip()
                    if line.startswith('${INSTALL}'):
                        result_order.append(line.split(' ')[1])

            expected_order = ['ca-certificates-2018.1.18-0', 'ncurses-5.9-10',
                              'tk-8.6.7-0', 'xz-5.2.3-0', 'zlib-1.2.11-0',
                              'openssl-1.0.2n-0', 'sqlite-3.20.1-2',
                              'python-3.6.4-0']
            self.assertEqual(expected_order,result_order)