# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from unittest import mock

from ironic.common import async_steps
from ironic.common import states
from ironic.conductor import task_manager
from ironic.conductor import utils
from ironic.tests.unit.db import base
from ironic.tests.unit.objects import utils as obj_utils


class FirmwareFailureTestCase(base.DbTestCase):

    def _check_callback(self, callback, step, state, teardown, key):
        self.config(poweroff_in_cleanfail=True, poweroff_in_servicefail=True,
                    group='conductor')
        node = obj_utils.create_test_node(
            self.context, driver='fake-hardware', provision_state=state,
            driver_internal_info={key: True},
            **{step + '_step': {'interface': 'firmware', 'step': 'update'}})
        with task_manager.acquire(self.context, node.uuid) as task:
            with mock.patch.object(utils, 'node_power_action',
                                   autospec=True) as power:
                with mock.patch.object(task.driver.deploy, teardown,
                                       autospec=True) as cleanup:
                    callback(task)
                    power.assert_not_called()
                    cleanup.assert_not_called()
            self.assertTrue(task.node.maintenance)
            self.assertIn('Do not power-cycle', task.node.last_error)
            self.assertIn('Do not power-cycle', task.node.maintenance_reason)
            self.assertNotIn(async_steps.FIRMWARE_UPDATE_IN_PROGRESS,
                             task.node.driver_internal_info)

    def test_clean_callback_timeout_preserves_power(self):
        self._check_callback(utils.cleanup_cleanwait_timeout, 'clean',
                             states.CLEANFAIL, 'tear_down_cleaning',
                             async_steps.FIRMWARE_UPDATE_IN_PROGRESS)

    def test_service_callback_timeout_preserves_power(self):
        self._check_callback(utils.cleanup_servicewait_timeout, 'service',
                             states.SERVICEFAIL, 'tear_down_service',
                             'redfish_fw_update')

    def test_deploy_legacy_callback_timeout_preserves_power(self):
        self._check_callback(utils.cleanup_after_timeout, 'deploy',
                             states.DEPLOYWAIT, 'clean_up',
                             'redfish_fw_updates')
