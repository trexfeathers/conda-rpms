import unittest

from conda_rpms.generate import render_env


class Test_tag(unittest.TestCase):
    def check(self, tag, branch_name='default', expected=None):
        config = {'install': {'prefix': '/data/local'},
                  'rpm': {'prefix': 'Tools'}
                  }
        r = render_env(branch_name=branch_name, label='next', config=config,
                       tag=tag, commit_num=30)
        result_requires = [
            line for line in r.split('\n') if line.startswith('Requires: ')]
        self.assertEqual(result_requires, expected)

    def test_tag(self):
        self.check(tag='env-default-2016_12_15',
                   expected=['Requires: Tools-env-default-tag-2016_12_15'])

    def test_tag_with_count(self):
        self.check(tag='env-default-2016_12_15-2',
                   expected=['Requires: Tools-env-default-tag-2016_12_15-2'])

    def test_alphanumeric_branch_name(self):
        branch = 'default12'
        self.check(tag='env-{}-2016_12_15-2'.format(branch),
                   branch_name=branch,
                   expected=['Requires: Tools-env-default12-tag-2016_12_15-2'])

    def test_bad_tag(self):
        msg = "Cannot create an environment for the tag"
        with self.assertRaisesRegexp(ValueError, msg):
            self.check(tag='env-defa-ult-2016_12_15-2')

if __name__ == '__main__':
    unittest.main()
