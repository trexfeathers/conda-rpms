from conda_rpms.build_rpm_structure import _env_label_filter
import conda_rpms.tests as tests


class Test(tests.CommonTest):
    def test_match_environment_wildcard(self):
        self.assertTrue(_env_label_filter('default', 'current',
                                          ['*']))

    def test_match_environment_label_wildcard(self):       
        self.assertTrue(_env_label_filter('default', 'current',
                                          ['default/*']))

    def test_no_match_environment_label_wildcard(self):       
        self.assertFalse(_env_label_filter('default', 'current',
                                           ['experimental/*']))

    def test_match_environment_label_specific(self):
        self.assertTrue(_env_label_filter('default', 'current',
                                          ['default/current', 'default/next']))

    def test_no_match_environment_label_specific(self):
        self.assertFalse(_env_label_filter('experimental', 'current',
                                           ['default/current', 'default/next']))


if __name__ == '__main__':
    unittest.main()
