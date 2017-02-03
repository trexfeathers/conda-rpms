import os
import shutil
import unittest

from conda_gitenv import manifest_branch_prefix
import conda_gitenv.tests.integration.setup_samples as setup_samples

from conda_rpms.build_rpm_structure import create_rpmbuild_content
import conda_rpms.tests as tests


class Test(tests.CommonTest):
    def setUp(self):
        name = 'conda_rpms_{}'.format(self._testMethodName)
        self.repo = setup_samples.create_repo(name)
        self.bname = 'default'
        self.env_spec = """
                        channels:
                            - defaults
                        env:
                            - python
                        """
        # Require to create a dummy manifest branch.
        self.repo.create_head(manifest_branch_prefix + self.bname)
        self.branch = setup_samples.add_env(self.repo, self.bname,
                                            self.env_spec)
        func = 'conda_rpms.build_rpm_structure.create_rpmbuild_for_tag'
        self.mock_create_tag = self.patch(func)
        func = 'conda_rpms.generate.render_env'
        self.mock_render_env = self.patch(func, return_value='dummy-env')
        self.config = dict(rpm=dict(prefix='prefix'))
        self.ctag = 'env-{}-2017_01_01'.format(self.bname)
        self.add_label(self.repo, self.branch, 'current.txt', self.ctag)
        self.ntag = 'env-{}-2017_02_02'.format(self.bname)
        self.add_label(self.repo, self.branch, 'next.txt', self.ntag)
        self.count = self.branch.commit.count()

    def tearDown(self):
        if os.path.exists(self.repo.working_dir):
            shutil.rmtree(self.repo.working_dir)

    def _check_full_build(self, dname, state):
        create_rpmbuild_content(self.repo, dname, self.config, state)
        self.assertEqual(self.mock_create_tag.call_count, 2)
        expected = [((self.repo, self.ctag, dname, self.config),),
                    ((self.repo, self.ntag, dname, self.config),)]
        self.assertEqual(self.mock_create_tag.call_args_list, expected)
        self.assertEqual(self.mock_render_env.call_count, 2)
        expected = [((self.bname, 'current', self.config, self.ctag,
                      self.count),),
                    ((self.bname, 'next', self.config, self.ntag,
                      self.count),)]
        self.assertEqual(self.mock_render_env.call_args_list, expected)

    def test_build_all_with_no_state(self):
        with self.temp_dir() as dname:
            # Make the expected RPM build SPECS directory.
            os.makedirs(os.path.join(dname, 'SPECS'))
            # Test - no prior build, so both labels build.
            state = {}
            self._check_full_build(dname, state)

    def test_build_all_with_no_state_match(self):
        with self.temp_dir() as dname:
            # Make the expected RPM build SPECS directory.
            os.makedirs(os.path.join(dname, 'SPECS'))
            # Test - prior build of current label but mismatching tag,
            #        so both labels build.
            tag = 'env-{}-2018_01_01'.format(self.bname)
            state = dict(default=dict(current=tag))
            self._check_full_build(dname, state)

    def test_build_next_skip_current(self):
        with self.temp_dir() as dname:
            # Make the expected RPM build SPECS directory.
            os.makedirs(os.path.join(dname, 'SPECS'))
            # Test - prior build of current label with matching tag,
            #        so only next label builds.
            state = dict(default=dict(current=self.ctag))
            create_rpmbuild_content(self.repo, dname, self.config, state)
            self.assertEqual(self.mock_create_tag.call_count, 1)
            expected = [((self.repo, self.ntag, dname, self.config),)]
            self.assertEqual(self.mock_create_tag.call_args_list, expected)
            self.assertEqual(self.mock_render_env.call_count, 1)
            expected = [((self.bname, 'next', self.config, self.ntag,
                          self.count),)]
            self.assertEqual(self.mock_render_env.call_args_list, expected)

    def test_build_current_skip_next(self):
        with self.temp_dir() as dname:
            # Make the expected RPM build SPECS directory.
            os.makedirs(os.path.join(dname, 'SPECS'))
            # Test - prior build of next label with matching tag,
            #        so only current label builds.
            state = dict(default=dict(next=self.ntag))
            create_rpmbuild_content(self.repo, dname, self.config, state)
            self.assertEqual(self.mock_create_tag.call_count, 1)
            expected = [((self.repo, self.ctag, dname, self.config),)]
            self.assertEqual(self.mock_create_tag.call_args_list, expected)
            self.assertEqual(self.mock_render_env.call_count, 1)
            expected = [((self.bname, 'current', self.config, self.ctag,
                          self.count),)]
            self.assertEqual(self.mock_render_env.call_args_list, expected)

    def test_build_skip_all(self):
        with self.temp_dir() as dname:
            # Make the expected RPM build SPECS directory.
            os.makedirs(os.path.join(dname, 'SPECS'))
            # Test - prior build of both labels, so no artifacts are built.
            state = dict(default=dict(current=self.ctag, next=self.ntag))
            create_rpmbuild_content(self.repo, dname, self.config, state)
            self.assertEqual(self.mock_create_tag.call_count, 0)
            self.assertEqual(self.mock_render_env.call_count, 0)


if __name__ == '__main__':
    unittest.main()
