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

import datetime
from types import SimpleNamespace
from unittest import mock

from oslo_utils import timeutils
import sushy

from ironic.common import exception
from ironic.common import states
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.drivers.modules.drac import firmware as drac_fw
from ironic.drivers.modules.redfish import firmware
from ironic.drivers.modules.redfish import utils
from ironic.tests.unit.db import base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils


class FirmwareStateMachineTestCase(base.DbTestCase):

    def setUp(self):
        super().setUp()
        self.config(enabled_hardware_types=['redfish'],
                    enabled_power_interfaces=['redfish'],
                    enabled_boot_interfaces=['redfish-virtual-media'],
                    enabled_management_interfaces=['redfish'],
                    enabled_firmware_interfaces=['redfish'])
        self.node = obj_utils.create_test_node(
            self.context, driver='redfish',
            driver_info=db_utils.get_test_redfish_info(),
            properties={'vendor': 'HPE'})
        self.now = datetime.datetime(2026, 1, 1)
        self._patch(timeutils, 'utcnow', side_effect=self._now)
        self.firmware = firmware.RedfishFirmware()
        self.system = self._patch(utils, 'get_system').return_value
        self._boot('OSRunning', 'old')
        self.manager = self._patch(utils, 'get_manager').return_value
        self.manager.firmware_version = '1.0'
        self.chassis = self._patch(utils, 'get_chassis').return_value
        self.service = self._patch(utils, 'get_update_service').return_value
        self.service.simple_update.side_effect = self._submit
        collection = self._patch(utils, 'get_system_collection').return_value
        collection.members_identities = ['/Systems/1']
        self.get_monitor = self._patch(utils, 'get_task_monitor')
        self.task_state = SimpleNamespace(
            task_state=sushy.TASK_STATE_COMPLETED,
            task_status=sushy.HEALTH_OK, messages=[])
        self.get_monitor.return_value.get_task.return_value = self.task_state
        self.get_monitor.return_value.is_processing = False
        self.power = self._patch(manager_utils, 'node_power_action')
        self.resume = self._patch(firmware.RedfishFirmware, '_resume_step')
        self.cache = self._patch(firmware.RedfishFirmware,
                                 'cache_firmware_components')
        self.jobs = self._patch(drac_fw, 'get_jobs', return_value=None)

    def _now(self, with_timezone=False):
        return (self.now.replace(tzinfo=datetime.timezone.utc)
                if with_timezone else self.now)

    def _patch(self, target, attribute, **kwargs):
        patcher = mock.patch.object(target, attribute, autospec=True, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _submit(self, url, **kwargs):
        return SimpleNamespace(task_monitor_uri='/TaskMonitors/JID_%s' %
                               self.service.simple_update.call_count)

    def _boot(self, state, reset=None, power='On'):
        self.system.json = {'PowerState': power,
                            'BootProgress': {'LastState': state},
                            'LastResetTime': reset}

    def _start(self, step='clean', components=('bios', 'nic:1'), wait=None):
        setattr(self.node, step + '_step',
                {'interface': 'firmware', 'step': 'update'})
        self.node.provision_state = {
            'clean': states.CLEANWAIT, 'service': states.SERVICEWAIT,
            'deploy': states.DEPLOYWAIT}[step]
        self.node.save()
        settings = [{'component': comp, 'url': 'https://firmware/' + comp}
                    for comp in components]
        if wait is not None:
            settings[0]['wait'] = wait
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.firmware.update(task, settings)
        self.node.refresh()

    def _poll(self, seconds=60):
        self.now += datetime.timedelta(seconds=seconds)
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.firmware._check_node_redfish_firmware_update(task)
        self.node.refresh()

    def _state(self):
        return self.node.driver_internal_info.get(
            firmware.FIRMWARE_UPDATE_STATE)

    def test_default_keeps_per_component_reboots(self):
        self._start()
        self._poll()
        self.assertEqual(1, self.service.simple_update.call_count)
        self._boot('SystemHardwareInitializationComplete', 'boot1')
        self._poll()
        self._poll()  # Inventory and next segment's first submission.
        self.assertEqual(2, self.service.simple_update.call_count)
        self.assertEqual(1, self.power.call_count)
        self._poll()
        self.assertEqual(2, self.power.call_count)
        self._boot('SystemHardwareInitializationComplete', 'boot2')
        self._poll()
        self._poll()
        self.resume.assert_called_once()

    def test_servicing_requires_os_running(self):
        self._start(step='service', components=('bios',))
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self.resume.assert_not_called()
        self._boot('OSBootStarted', 'new')
        self._poll()
        self.resume.assert_not_called()
        self._boot('OSRunning', 'new')
        self._poll()
        self._poll()
        self.resume.assert_called_once()

    def test_deploy_accepts_setup(self):
        self._start(step='deploy', components=('bios',))
        self._poll()
        self._boot('SetupEntered', 'new')
        self._poll()
        self._poll()
        self.resume.assert_called_once()

    def test_bmc_version_is_captured_before_submission(self):
        self.config(firmware_update_reboot_delay=0,
                    firmware_update_required_successes=2, group='redfish')

        def submit(url, **kwargs):
            self.manager.firmware_version = '2.0'
            return self._submit(url, **kwargs)

        self.service.simple_update.side_effect = submit
        self._start(components=('bmc',))
        self.assertEqual('1.0', self._state()['bmc']['version_before'])
        self._poll()
        self.assertEqual(firmware.STATE_VALIDATING_BMC, self._state()['state'])
        self._poll()
        self.cache.assert_not_called()
        self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_INVENTORY,
                         self._state()['state'])
        self._poll()
        self.resume.assert_called_once()
        self.power.assert_not_called()

    def test_all_waits_are_bounded_when_overall_timeout_disabled(self):
        self.config(firmware_update_overall_timeout=0,
                    firmware_update_apply_timeout=120, group='redfish')
        self.config(poweroff_in_cleanfail=True, group='conductor')
        self._start(components=('bios',))
        self.task_state.task_state = sushy.TASK_STATE_RUNNING
        self._poll(120)
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.assertIn('timed out', self.node.last_error)
        self.assertIn('Do not power-cycle', self.node.last_error)
        self.power.assert_not_called()
        self.resume.assert_not_called()
        self.assertIsNone(self._state())

    def test_outage_is_not_reset_evidence(self):
        self._start(components=('bios',))
        self._poll()
        reader = utils.get_system
        reader.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='down')
        self._poll()
        reader.side_effect = None
        self._poll()
        self.assertFalse(self._state()['verify']['new_boot_observed'])
        self.resume.assert_not_called()

    def test_reboot_is_not_repeated_after_lost_response(self):
        self._start(components=('bios',))
        self.power.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='response lost')
        self._poll()
        self.firmware = firmware.RedfishFirmware()
        self.power.side_effect = None
        self._poll()
        self.power.assert_called_once()
        self.resume.assert_not_called()

    def test_no_telemetry_uses_fallback_wait(self):
        self._boot(None)
        self._start(components=('bios',))
        self._poll()
        self._poll(300)
        self.resume.assert_not_called()
        self._poll(300)
        self._poll()
        self.resume.assert_called_once()

    def test_partial_telemetry_requires_explicit_policy(self):
        self._boot('MemoryInitializationStarted')
        self._start(components=('bios',))
        self._poll()
        self._poll(600)
        self.resume.assert_not_called()
        self.node.driver_info = dict(
            self.node.driver_info, firmware_update_boot_progress='limited')
        self.node.save()
        self._poll()
        self._poll()
        self.resume.assert_called_once()

    def test_inventory_failure_retries_without_resubmitting(self):
        self._start(components=('bios',))
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self.cache.side_effect = exception.RedfishError(error='inventory busy')
        self._poll()
        self.resume.assert_not_called()
        self.cache.side_effect = None
        self._poll()
        self.resume.assert_called_once()
        self.service.simple_update.assert_called_once()
        self.power.assert_called_once()

    def test_per_component_wait_remains_supported(self):
        self._start(components=('bios',), wait=120)
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self._poll()
        self.resume.assert_not_called()
        self._poll()
        self.resume.assert_called_once()

    def _job(self, identity, state):
        return {'id': identity, 'state': state, 'type': 'FirmwareUpdate',
                'message': 'LC diagnostic'}

    def test_dell_failed_late_job_blocks_next_segment(self):
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=('bios', 'bmc'))
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.jobs.return_value = [self._job('JID_1', 'Completed'),
                                  self._job('JID_child', 'Failed')]
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertIn('JID_child', self.node.last_error)
        self.service.simple_update.assert_called_once()
        self.power.assert_called_once()
        self.resume.assert_not_called()

    def test_synchronous_success_without_monitor_is_not_ambiguous(self):
        self.service.simple_update.side_effect = None
        self.service.simple_update.return_value = SimpleNamespace(
            task_monitor_uri=None)
        self._start(components=('bios',))
        self._poll()
        self.power.assert_called_once()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self._poll()
        self.resume.assert_called_once()

    def test_non_dell_starting_compatibility_is_single_component_only(self):
        self._start(components=('nic:1',))
        self.task_state.task_state = sushy.TASK_STATE_STARTING
        self._poll()
        self.power.assert_not_called()
        self._poll(30)
        self.power.assert_called_once()
