import os
import unittest

from conda_gitenv import manifest_branch_prefix
import conda_gitenv.tests.integration.setup_samples as setup_samples

from conda_rpms.build_rpm_structure import create_rpmbuild_content
import conda_rpms.tests as tests


class Test(tests.CommonTest):
    def setUp(self):
        self.repo = setup_samples.create_repo('conda-rpms-basic')
        self.bname = 'default'
        env_spec = """
                   channels:
                       - defaults
                   env:
                       - python
                   """
        # Require to create a dummy manifest branch.
        self.repo.create_head(manifest_branch_prefix + self.bname)
        self.branch = setup_samples.add_env(self.repo, self.bname, env_spec)
        func = 'conda_rpms.build_rpm_structure.create_rpmbuild_for_tag'
        self.mock_create_tag = self.patch(func)
        func = 'conda_rpms.generate.render_env'
        self.mock_render_env = self.patch(func, return_value='dummy-env')
        self.config = dict(rpm=dict(prefix='prefix'))

    def _add_label(self, fname, tag):
        self.branch.checkout()
        dname = os.path.join(self.repo.working_dir, 'labels')
        if not os.path.isdir(dname):
            os.makedirs(dname)
        fpath = os.path.join(dname, fname)
        with open(fpath, 'w') as fo:
            fo.write(tag)
        self.repo.index.add([fpath])
        comment = 'Add label {}'.format(fname)
        self.repo.index.commit(comment)

    def _check_full_build(self, dname, state, ctag, ntag, count):
        create_rpmbuild_content(self.repo, dname, self.config, state)
        self.assertEqual(self.mock_create_tag.call_count, 2)
        expected = [((self.repo, ctag, dname, self.config),),
                    ((self.repo, ntag, dname, self.config),)]
        self.assertEqual(self.mock_create_tag.call_args_list, expected)
        self.assertEqual(self.mock_render_env.call_count, 2)
        expected = [((self.bname, 'current', self.config, ctag, count),),
                    ((self.bname, 'next', self.config, ntag, count),)]
        self.assertEqual(self.mock_render_env.call_args_list, expected)
        self.mock_create_tag.reset_mock()
        self.mock_render_env.reset_mock()

    def test_basic(self):
        ctag = 'env-{}-2017_01_01'.format(self.bname)
        self._add_label('current.txt', ctag)
        ntag = 'env-{}-2017_02_02'.format(self.bname)
        self._add_label('next.txt', ntag)
        count = self.branch.commit.count()

        with self.temp_dir() as dname:
            # Make the expected RPM build SPECS directory.
            os.makedirs(os.path.join(dname, 'SPECS'))

            # Test - no prior build, so both labels build.
            state = {}
            self._check_full_build(dname, state, ctag, ntag, count)

            # Test - prior build of current label but mismatching tag,
            #        so both labels build.
            tag = 'env-{}-2018_01_01'.format(self.bname)
            state = dict(default=dict(current=tag))
            self._check_full_build(dname, state, ctag, ntag, count)

            # Test - prior build of current label with matching tag,
            #        so only next label builds.
            state = dict(default=dict(current=ctag))
            create_rpmbuild_content(self.repo, dname, self.config, state)
            self.assertEqual(self.mock_create_tag.call_count, 1)
            expected = [((self.repo, ntag, dname, self.config),)]
            self.assertEqual(self.mock_create_tag.call_args_list, expected)
            self.assertEqual(self.mock_render_env.call_count, 1)
            expected = [((self.bname, 'next', self.config, ntag, count),)]
            self.assertEqual(self.mock_render_env.call_args_list, expected)
            self.mock_create_tag.reset_mock()
            self.mock_render_env.reset_mock()

            # Test - prior build of next label with matching tag,
            #        so only current label builds.
            state = dict(default=dict(next=ntag))
            create_rpmbuild_content(self.repo, dname, self.config, state)
            self.assertEqual(self.mock_create_tag.call_count, 1)
            expected = [((self.repo, ctag, dname, self.config),)]
            self.assertEqual(self.mock_create_tag.call_args_list, expected)
            self.assertEqual(self.mock_render_env.call_count, 1)
            expected = [((self.bname, 'current', self.config, ctag, count),)]
            self.assertEqual(self.mock_render_env.call_args_list, expected)
            self.mock_create_tag.reset_mock()
            self.mock_render_env.reset_mock()

            # Test - prior build of both labels, so no artifacts are built.
            state = dict(default=dict(current=ctag, next=ntag))
            create_rpmbuild_content(self.repo, dname, self.config, state)
            self.assertEqual(self.mock_create_tag.call_count, 0)
            self.assertEqual(self.mock_render_env.call_count, 0)


if __name__ == '__main__':
    unittest.main()
