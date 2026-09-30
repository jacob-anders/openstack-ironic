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

import ddt
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


@ddt.ddt
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
        self.chassis.network_adapters.get_members.return_value = [
            self._nic('1'), self._nic('2')]
        self.get_update_service = self._patch(utils, 'get_update_service')
        self.service = self.get_update_service.return_value
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
        self.real_cache = firmware.RedfishFirmware.cache_firmware_components
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
        self.system.boot_progress = (
            SimpleNamespace(last_state=SimpleNamespace(value=state),
                            last_state_updated_at=None)
            if state is not None else None)
        self.system.power_state = (
            SimpleNamespace(value=power) if power is not None else None)

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

    def test_terminal_firmware_error_is_not_a_retryable_redfish_error(self):
        self.assertFalse(issubclass(exception.FirmwareUpdateFailed,
                                    exception.RedfishError))

    def _nic(self, identity, serial=None):
        return SimpleNamespace(
            identity=identity, serial_number=serial,
            path='/Chassis/1/NetworkAdapters/' + identity,
            manufacturer='NIC vendor', model='NIC',
            controllers=[SimpleNamespace(firmware_package_version='2.0')])

    def test_batch_has_one_reboot_and_waits_for_new_boot(self):
        self._start()
        self.assertEqual(firmware.STATE_STAGING, self._state()['state'])
        self._poll()  # Stage the NIC after BIOS staging completed.
        self.power.assert_not_called()
        self._poll()  # All images staged: issue one reset.
        self.power.assert_called_once()
        self.assertEqual(firmware.STATE_REBOOTING, self._state()['state'])
        self._poll()  # Old boot's OSRunning is still latched.
        self.resume.assert_not_called()
        self.cache.assert_not_called()
        self._boot('MemoryInitializationStarted', 'new')
        self._poll()
        self.resume.assert_not_called()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_INVENTORY,
                         self._state()['state'])
        self.cache.assert_not_called()
        self._poll()
        self.cache.assert_called_once()
        self.resume.assert_called_once()
        self.power.assert_called_once()
        self.assertIsNone(self._state())

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

    def test_unobserved_apply_outcome_blocks_completion(self):
        self.config(firmware_update_apply_timeout=300, group='redfish')
        self._start(components=('bios',))
        self.task_state.task_state = sushy.TASK_STATE_STARTING
        self._poll()
        self._poll()
        self.power.assert_called_once()
        self.get_monitor.side_effect = exception.RedfishTaskMonitorNotFound()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self.assertEqual(firmware.STATE_APPLYING, self._state()['state'])
        self._poll(120)
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.resume.assert_not_called()
        self.power.assert_called_once()

    @ddt.data(('bmc',), ('bmc', 'bios'))
    def test_dell_missing_bmc_job_blocks_recovery(self, components):
        self.config(firmware_update_apply_timeout=900, group='redfish')
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=components)
        self.get_monitor.side_effect = exception.RedfishTaskMonitorNotFound()
        self._poll(300)
        self.assertEqual(['JID_1'], self._state()['settings'][0]['jids'])
        self.assertIn('JID_1: missing', self._state()['last_error'])
        self._poll(300)
        self.assertEqual(firmware.STATE_WAITING_BMC, self._state()['state'])
        self._poll(300)
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.service.simple_update.assert_called_once()
        self.power.assert_not_called()
        self.resume.assert_not_called()

    @ddt.data(('bmc',), ('bmc', 'bios'))
    def test_dell_failed_bmc_job_fails_without_handoff(self, components):
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=components)
        self.jobs.return_value = [self._job('JID_1', 'Failed')]
        self._poll(300)
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertIn('JID_1: Failed', self.node.last_error)
        self.assertTrue(self.node.maintenance)
        self.service.simple_update.assert_called_once()
        self.power.assert_not_called()
        self.resume.assert_not_called()

    @ddt.data(('bmc',), ('bmc', 'bios'))
    def test_dell_bmc_running_then_completed_and_purged(self, components):
        self.config(firmware_update_required_successes=1, group='redfish')
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=components)
        self.get_monitor.side_effect = exception.RedfishTaskMonitorNotFound()
        self.jobs.return_value = [self._job('JID_1', 'Running')]
        self._poll(300)
        self.service.simple_update.assert_called_once()
        self.power.assert_not_called()
        self.resume.assert_not_called()
        self.jobs.return_value[0]['state'] = 'Completed'
        self._poll()
        self.assertEqual(firmware.STATE_VALIDATING_BMC, self._state()['state'])
        self.assertEqual('Completed',
                         self._state()['segment']['jobs']['jobs']['JID_1'])
        self.jobs.return_value = []
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        if len(components) > 1:
            self.power.assert_called_once()
            self._boot('SystemHardwareInitializationComplete', 'new')
            self._poll()
            self._poll()
            self.assertEqual(2, self.service.simple_update.call_count)
            self.assertEqual('bios', self._state()['settings'][0]['component'])
            self.resume.assert_not_called()
        else:
            self._poll()
            self.resume.assert_called_once()
            self.power.assert_not_called()

    def test_non_dell_same_version_requires_positive_outcome(self):
        self._start(components=('bmc',))
        self.get_monitor.side_effect = exception.RedfishTaskMonitorNotFound()
        self._poll(300)
        self._poll(300)
        self.assertEqual(firmware.STATE_WAITING_BMC, self._state()['state'])
        self.resume.assert_not_called()
        self.power.assert_not_called()

    @ddt.data(sushy.TASK_STATE_PENDING, sushy.TASK_STATE_RUNNING)
    def test_bios_compatibility_reset_completes_reset_dependent_task(
            self, status):
        self.node.driver_info = dict(
            self.node.driver_info,
            firmware_update_bios_pending_reset='compatibility')
        self._start(components=('bios',))
        self.task_state.task_state = status
        self._poll()
        self.power.assert_not_called()
        self._poll()
        self.power.assert_called_once()
        self._poll()
        self.resume.assert_not_called()
        self.task_state.task_state = sushy.TASK_STATE_COMPLETED
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self._poll()
        self.resume.assert_called_once()
        self.power.assert_called_once()

    def _use_real_inventory(self):
        self.cache.side_effect = self.real_cache
        self.system.bios_version = '2.0'
        self.manager.model = 'BMC'
        self.chassis.network_adapters.get_members.return_value = [
            SimpleNamespace(identity='1', serial_number=None,
                            manufacturer='NIC vendor', model='NIC',
                            controllers=[SimpleNamespace(
                                firmware_package_version='2.0')])]

    @ddt.data('nic', 'manager', 'chassis')
    def test_real_inventory_read_failures_hold_segment(self, resource):
        self.config(firmware_update_required_successes=2, group='redfish')
        self._use_real_inventory()
        components = ('bmc',) if resource == 'manager' else ('nic:1',)
        self._start(components=components)
        if resource == 'manager':
            self._poll(300)
            self._poll()
            self._poll()
        else:
            self._poll()
            self._boot('SystemHardwareInitializationComplete', 'new')
            self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_INVENTORY,
                         self._state()['state'])
        reader = {'nic': self.chassis.network_adapters.get_members,
                  'manager': utils.get_manager,
                  'chassis': utils.get_chassis}[resource]
        reader.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='inventory temporarily unavailable')
        self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_INVENTORY,
                         self._state()['state'])
        self.assertIn('inventory temporarily unavailable',
                      self._state()['last_error'])
        self.resume.assert_not_called()
        reader.side_effect = None
        self._poll()
        self.resume.assert_called_once()
        self.service.simple_update.assert_called_once()

    def test_real_inventory_missing_target_retries_to_deadline(self):
        self.config(firmware_update_apply_timeout=300, group='redfish')
        self._use_real_inventory()
        self._start(components=('nic:1',))
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self.chassis.network_adapters.get_members.return_value = []
        self._poll()
        self.assertIn('nic:1', self._state()['last_error'])
        self._poll(120)
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.power.assert_called_once()
        self.resume.assert_not_called()

    def test_real_inventory_explicitly_unsupported_nics(self):
        self.node.properties = {'vendor': 'Generic'}
        self._use_real_inventory()
        self.chassis.network_adapters = None
        self._start(components=('nic:1',))
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self._poll()
        self.resume.assert_called_once()

    @ddt.data('preflight', 'cached')
    def test_previously_supported_nic_inventory_cannot_disappear(self, source):
        self._use_real_inventory()
        if source == 'cached':
            self.node.properties = {'vendor': 'Generic'}
            self.node.save()
            with task_manager.acquire(self.context, self.node.uuid) as task:
                self.firmware.cache_firmware_components(task)
        self._start(components=('nic:1',))
        self.assertIn('nic', self._state()['inventory_supported'])
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        adapters = self.chassis.network_adapters
        self.chassis.network_adapters = None
        self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_INVENTORY,
                         self._state()['state'])
        self.assertIn('Previously supported', self._state()['last_error'])
        self.resume.assert_not_called()
        self.chassis.network_adapters = adapters
        self._poll()
        self.resume.assert_called_once()
        self.service.simple_update.assert_called_once()
        self.power.assert_called_once()

    @ddt.data(False, True)
    def test_dell_disappeared_bios_task_needs_job_evidence(self, job_found):
        self.config(firmware_update_apply_timeout=300, group='redfish')
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=('bios',))
        self.get_monitor.side_effect = exception.RedfishTaskMonitorNotFound()
        if job_found:
            self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.resume.assert_not_called()
        if job_found:
            self.power.assert_called_once()
            self.jobs.return_value[0]['state'] = 'Completed'
            self._boot('SystemHardwareInitializationComplete', 'new')
            self._poll()
            self._poll()
            self.resume.assert_called_once()
        else:
            self.power.assert_not_called()
            self._poll(240)
            self.assertEqual(states.CLEANFAIL, self.node.provision_state)
            self.assertTrue(self.node.maintenance)
            self.resume.assert_not_called()
        self.service.simple_update.assert_called_once()

    def test_changed_bmc_version_with_purged_task_allows_handoff(self):
        self.config(firmware_update_required_successes=1, group='redfish')
        self._start(components=('bmc', 'bios'))
        self.get_monitor.side_effect = exception.RedfishTaskMonitorNotFound()
        self.manager.firmware_version = '2.0'
        self._poll(300)
        self._poll()
        self.power.assert_called_once()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self._poll()
        self.assertEqual(2, self.service.simple_update.call_count)
        self.assertEqual('bios', self._state()['settings'][0]['component'])

    def test_latched_target_never_becomes_reset_evidence_by_waiting(self):
        self._start(components=('bios',))
        self._poll()
        self._poll(660)
        self.assertEqual(firmware.STATE_VERIFYING_BOOT, self._state()['state'])
        self.assertFalse(self._state()['verify']['new_boot_observed'])
        self.resume.assert_not_called()

    @ddt.data(('before_sample', 'update_service'),
              ('after_sample', 'update_service'),
              ('before_sample', 'lc_jobs'))
    @ddt.unpack
    def test_bmc_validation_deadline_survives_monitoring_outages(
            self, sample, outage):
        self.config(firmware_update_resource_validation_timeout=180,
                    firmware_update_required_successes=3,
                    firmware_update_validation_interval=0,
                    firmware_update_apply_timeout=1800, group='redfish')
        self.assertTrue(self.firmware._resource_validation_enabled())
        if outage == 'lc_jobs':
            self.node.properties = {'vendor': 'Dell Inc.'}
            self.jobs.return_value = []
        self._start(components=('bmc',))

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            state = task.node.driver_internal_info[
                firmware.FIRMWARE_UPDATE_STATE]
            self.assertEqual(firmware.STATE_WAITING_BMC, state['state'])
            self.firmware._continue_after_bmc(task, state, self.service)
            self.assertIn('validation', state['bmc'])
            if sample == 'after_sample':
                self.assertFalse(
                    self.firmware._validate_resources_stability(task.node))

        self.node.refresh()
        validation = self._state()['bmc']['validation']
        self.assertEqual(self.now.isoformat(), validation['started_at'])
        self.firmware = firmware.RedfishFirmware()
        self.get_update_service.reset_mock()
        if outage == 'lc_jobs':
            self.jobs.reset_mock()
            self._poll(240)
            self.jobs.assert_not_called()
        else:
            self.get_update_service.side_effect = (
                exception.RedfishConnectionError(
                    node=self.node.uuid, error='BMC unavailable'))
            self._poll(240)
            self.get_update_service.assert_not_called()

        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.assertIn('BMC resources failed to stabilize',
                      self.node.last_error)
        self.assertIn('180 seconds', self.node.last_error)
        self.assertIsNone(self._state())
        self.power.assert_not_called()
        self.resume.assert_not_called()

    @ddt.data(('firmware_update_resource_validation_timeout', 0),
              ('firmware_update_required_successes', 0))
    @ddt.unpack
    def test_disabled_bmc_validation_uses_segment_deadline(self, option,
                                                           value):
        config = {
            'firmware_update_resource_validation_timeout': 180,
            'firmware_update_required_successes': 3,
            'firmware_update_validation_interval': 0,
            'firmware_update_apply_timeout': 300,
            'firmware_update_overall_timeout': 0,
        }
        config[option] = value
        self.config(group='redfish', **config)
        self._start(components=('bmc',))
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            state = task.node.driver_internal_info[
                firmware.FIRMWARE_UPDATE_STATE]
            self.firmware._continue_after_bmc(task, state, self.service)
        self.node.refresh()
        self.assertNotIn('validation', self._state()['bmc'])

        self.firmware = firmware.RedfishFirmware()
        self.get_update_service.reset_mock()
        self.get_update_service.side_effect = (
            exception.RedfishConnectionError(
                node=self.node.uuid, error='BMC unavailable'))
        self._poll(240)
        self.assertEqual(states.CLEANWAIT, self.node.provision_state)
        self.assertIsNotNone(self._state())
        self.assertEqual(1, self.get_update_service.call_count)
        self._poll(60)
        self.assertEqual(1, self.get_update_service.call_count)

        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertIn('timed out in state validating_bmc',
                      self.node.last_error)
        self.assertNotIn('BMC resources failed to stabilize',
                         self.node.last_error)
        self.power.assert_not_called()

    def test_late_boot_telemetry_promotes_to_strict_gate_after_reload(self):
        self.config(firmware_update_post_reboot_verify_timeout=3600,
                    firmware_update_boot_check_delay=60, group='redfish')
        self._start(components=('bios',))
        state = self._state()
        state['state'] = firmware.STATE_VERIFYING_BOOT
        state['reboot_time'] = (
            self.now - datetime.timedelta(seconds=660)).isoformat()
        state['verify'] = {
            'jids': [], 'lc': 'skipped', 'boot': 'pending',
            'before': {'state': None, 'state_time': None,
                       'reset_time': 'old', 'power': 'Off'},
            'new_boot_observed': False, 'progress_supported': False,
            'os_boot_started_at': None}
        self.node.set_driver_internal_info(firmware.FIRMWARE_UPDATE_STATE,
                                           state)
        self.node.save()
        self._boot('MemoryInitializationStarted', 'new')

        self._poll()
        self.assertTrue(self._state()['verify']['progress_supported'])
        self.assertTrue(self._state()['verify']['new_boot_observed'])
        self.assertEqual('pending', self._state()['verify']['boot'])
        self.assertEqual(firmware.STATE_VERIFYING_BOOT,
                         self._state()['state'])

        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.assertEqual('pending', self._state()['verify']['boot'])
        self.resume.assert_not_called()
        self.cache.assert_not_called()

        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_INVENTORY,
                         self._state()['state'])
        self.resume.assert_not_called()

    def test_off_to_on_without_boot_timestamps_completes_on_target(self):
        self.config(firmware_update_reboot_min_wait=0,
                    firmware_update_boot_check_delay=0, group='redfish')
        self._boot(None, None, power='Off')
        self._start(components=('bios',))
        self._poll()
        verify = self._state()['verify']
        self.assertEqual(
            {'state': None, 'state_time': None, 'reset_time': None,
             'power': 'Off'}, verify['before'])

        # Only the post-boot sample is observed; it has a ready target state
        # but no reset timestamp or BootProgress timestamp to compare.
        self._boot('SystemHardwareInitializationComplete', None, power='On')
        self._poll()
        self._poll()
        self.assertEqual(1, self.resume.call_count)

    @ddt.data(('bios', 'nic:1'), ('bios', 'nic:1', 'nic:2'))
    def test_missing_staged_member_blocks_next_post_or_apply_reboot(
            self, components):
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=components)
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.assertEqual(2, self.service.simple_update.call_count)

        # JID_1 was staged, but disappears while JID_2 is still armed.
        self.jobs.return_value = [self._job('JID_2', 'Scheduled')]
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.assertEqual(2, self.service.simple_update.call_count)
        self.power.assert_not_called()
        self.assertIn('JID_1', self._state()['last_error'])
        self.assertIn('missing', self._state()['last_error'])

        # A terminal success may be retained after the BMC purges that job.
        self.jobs.return_value = [self._job('JID_1', 'Completed'),
                                  self._job('JID_2', 'Scheduled')]
        self._poll()
        if len(components) == 3:
            self.assertEqual(3, self.service.simple_update.call_count)
            self.jobs.return_value.append(self._job('JID_3', 'Scheduled'))
            self._poll()
        self.power.assert_called_once()

    @ddt.data('clean', 'service', 'deploy')
    def test_lc_failure_is_sampled_while_sibling_task_runs(self, step):
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        completed = SimpleNamespace(task_state=sushy.TASK_STATE_COMPLETED,
                                    task_status=sushy.HEALTH_OK, messages=[])
        running = SimpleNamespace(task_state=sushy.TASK_STATE_RUNNING,
                                  task_status=sushy.HEALTH_OK, messages=[])
        monitors = []
        for task in (completed, completed, completed, running):
            monitor = mock.Mock()
            monitor.get_task.return_value = task
            monitors.append(monitor)
        self.get_monitor.side_effect = monitors

        self._start(step=step, components=('bios', 'nic:1'))
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()  # First task stages; second image is submitted.
        self.jobs.return_value = [self._job('JID_1', 'Scheduled'),
                                  self._job('JID_2', 'Scheduled')]
        self._poll()  # Both staged; consolidated reboot is issued.
        self.power.assert_called_once()

        failed_job = self._job('JID_1', 'Failed')
        failed_job['message'] = 'flash failed'
        self.jobs.return_value = [failed_job, self._job('JID_2', 'Running')]
        self.firmware = firmware.RedfishFirmware()
        self._poll()  # One Redfish task completes; its sibling remains active.

        expected_state = {
            'clean': states.CLEANFAIL,
            'service': states.SERVICEFAIL,
            'deploy': states.DEPLOYFAIL,
        }[step]
        self.assertEqual(expected_state, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.assertIn('JID_1=Failed', self.node.last_error)
        self.assertIn('JID_2=Running', self.node.last_error)
        self.assertEqual(2, self.service.simple_update.call_count)
        self.power.assert_called_once()
        self.resume.assert_not_called()
        self.cache.assert_not_called()

    @ddt.data('LastResetTime', 'LastStateTime', 'PowerState')
    def test_periodic_reset_markers_survive_reload(self, marker):
        self.system.json['BootProgress']['LastStateTime'] = 'old'
        self.system.boot_progress.last_state_updated_at = self.now
        self._start(components=('bios',))
        self._poll()
        if marker == 'LastStateTime':
            self.system.json['BootProgress'][marker] = 'new'
            self.system.boot_progress.last_state_updated_at = (
                self.now + datetime.timedelta(seconds=1))
        else:
            self.system.json[marker] = (
                'Off' if marker == 'PowerState' else 'new')
            if marker == 'PowerState':
                self.system.power_state = SimpleNamespace(value='Off')
        self._poll()
        self.assertTrue(self._state()['verify']['new_boot_observed'])
        self.firmware = firmware.RedfishFirmware()
        self.system.json['PowerState'] = 'On'
        self.system.power_state = SimpleNamespace(value='On')
        self._poll()
        self._poll()
        self.resume.assert_called_once()

    def test_invalid_bios_compatibility_policy_rejected(self):
        self.node.driver_info = dict(
            self.node.driver_info, firmware_update_bios_pending_reset='yes')
        self.assertRaises(exception.InvalidParameterValue, self._start)
        self.service.simple_update.assert_not_called()
