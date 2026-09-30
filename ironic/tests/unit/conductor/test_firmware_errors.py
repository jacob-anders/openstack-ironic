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

import ddt

from ironic.common import async_steps
from ironic.common import faults
from ironic.common import states
from ironic.conductor import manager as conductor_manager
from ironic.conductor import task_manager
from ironic.conductor import utils
from ironic.drivers.modules.redfish import firmware as redfish_firmware
from ironic.tests.unit.db import base
from ironic.tests.unit.objects import utils as obj_utils


@ddt.ddt
class FirmwareFailureTestCase(base.DbTestCase):

    def _check_callback(self, callback, step, state, teardown, key,
                        step_present=True, fault=None):
        self.config(poweroff_in_cleanfail=True, poweroff_in_servicefail=True,
                    group='conductor')
        step_data = ({'interface': 'firmware', 'step': 'update'}
                     if step_present else {})
        node = obj_utils.create_test_node(
            self.context, driver='fake-hardware', provision_state=state,
            fault=fault,
            maintenance_reason='Unreachable BMC' if fault else None,
            driver_internal_info={key: True},
            **{step + '_step': step_data})
        with task_manager.acquire(self.context, node.uuid) as task:
            with mock.patch.object(utils, 'node_power_action',
                                   autospec=True) as power:
                with mock.patch.object(task.driver.deploy, teardown,
                                       autospec=True) as cleanup:
                    callback(task)
                    power.assert_not_called()
                    cleanup.assert_not_called()
            self.assertTrue(task.node.maintenance)
            expected_fault = {'clean': faults.CLEAN_FAILURE,
                              'service': faults.SERVICE_FAILURE,
                              'deploy': None}.get(step)
            self.assertEqual(expected_fault, task.node.fault)
            self.assertIn('Do not power-cycle', task.node.last_error)
            self.assertIn('Do not power-cycle', task.node.maintenance_reason)
            if fault == faults.POWER_FAILURE:
                self.assertIn('power failure', task.node.last_error)
                self.assertIn('Unreachable BMC', task.node.maintenance_reason)
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

    @ddt.data('clean', 'service')
    def test_failure_without_step_still_records_maintenance_fault(self, step):
        callback, state, teardown = {
            'clean': (utils.cleanup_cleanwait_timeout, states.CLEANFAIL,
                      'tear_down_cleaning'),
            'service': (utils.cleanup_servicewait_timeout, states.SERVICEFAIL,
                        'tear_down_service'),
        }[step]
        self._check_callback(callback, step, state, teardown,
                             'redfish_fw_updates', step_present=False)

    @ddt.data('clean', 'service', 'deploy')
    def test_firmware_hold_retires_auto_recoverable_power_fault(self, step):
        callback, state, teardown = {
            'clean': (utils.cleanup_cleanwait_timeout, states.CLEANFAIL,
                      'tear_down_cleaning'),
            'service': (utils.cleanup_servicewait_timeout, states.SERVICEFAIL,
                        'tear_down_service'),
            'deploy': (utils.cleanup_after_timeout, states.DEPLOYWAIT,
                       'clean_up'),
        }[step]
        self._check_callback(callback, step, state, teardown,
                             'redfish_fw_updates', fault=faults.POWER_FAILURE)

    @ddt.data('clean', 'service', 'deploy')
    def test_firmware_failure_hold_survives_power_recovery(self, step):
        self.config(poweroff_in_cleanfail=True, poweroff_in_servicefail=True,
                    group='conductor')
        step_data = {'interface': 'firmware', 'step': 'update'}
        state, step_name = {
            'clean': (states.CLEANWAIT, 'clean_step'),
            'service': (states.SERVICEWAIT, 'service_step'),
            'deploy': (states.DEPLOYWAIT, 'deploy_step'),
        }[step]
        state_record = {'cleanup': []}
        node = obj_utils.create_test_node(
            self.context, driver='fake-hardware', provision_state=state,
            fault=faults.POWER_FAILURE, maintenance=True,
            maintenance_reason='Unreachable BMC',
            driver_internal_info={
                redfish_firmware.FIRMWARE_UPDATE_STATE: state_record,
                async_steps.FIRMWARE_UPDATE_IN_PROGRESS: True},
            **{step_name: step_data})

        with task_manager.acquire(self.context, node.uuid) as task:
            with mock.patch.object(redfish_firmware.firmware_utils, 'cleanup',
                                   autospec=True):
                redfish_firmware.RedfishFirmware()._fail(
                    task, state_record, 'Firmware controller reported failure')

            task.node.refresh()
            self.assertTrue(task.node.maintenance)
            expected_fault = {
                'clean': faults.CLEAN_FAILURE,
                'service': faults.SERVICE_FAILURE,
                'deploy': None,
            }[step]
            self.assertEqual(expected_fault, task.node.fault)
            self.assertIn('power failure', task.node.last_error)
            self.assertIn('Do not power-cycle', task.node.maintenance_reason)
            self.assertIn('Unreachable BMC', task.node.maintenance_reason)
            self.assertNotIn(redfish_firmware.FIRMWARE_UPDATE_STATE,
                             task.node.driver_internal_info)
            self.assertNotIn(async_steps.FIRMWARE_UPDATE_IN_PROGRESS,
                             task.node.driver_internal_info)

            recovery = conductor_manager.ConductorManager
            recovery = recovery._power_failure_recovery
            while hasattr(recovery, '__wrapped__'):
                recovery = recovery.__wrapped__
            with mock.patch.object(task.driver.power, 'get_power_state',
                                   autospec=True,
                                   return_value=states.POWER_ON) as get_power:
                recovery(mock.Mock(), task, self.context)
            get_power.assert_not_called()

            task.node.refresh()
            self.assertTrue(task.node.maintenance)
            self.assertIn('Do not power-cycle', task.node.maintenance_reason)
