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

"""Sequential firmware application and recovery across conductor polls.

These cases assert hardware actions, recovery and completion rather than a
particular phase schema. Polls reload persisted node data to exercise recovery
across conductor restarts.
"""

import datetime
from types import SimpleNamespace
from unittest import mock

import ddt
from oslo_utils import timeutils
import sushy

from ironic.common import async_steps
from ironic.common import exception
from ironic.common import states
from ironic.conductor import cleaning
from ironic.conductor import servicing
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.drac import firmware as drac_fw
from ironic.drivers.modules.redfish import firmware
from ironic.drivers.modules.redfish import utils
from ironic import objects
from ironic.tests.unit.db import base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils


@ddt.ddt
class FirmwareSequentialTestCase(base.DbTestCase):

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
            self._nic('1')]
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

    def _updates(self):
        info = self.node.driver_internal_info
        if info.get('redfish_fw_update'):
            return info['redfish_fw_update']['settings']
        return info.get('redfish_fw_updates')

    def _record(self):
        return self.node.driver_internal_info[firmware.FIRMWARE_UPDATE_STATE]

    def _job(self, identity, state):
        return {'id': identity, 'state': state, 'type': 'FirmwareUpdate',
                'message': 'LC diagnostic'}

    def _finish_boot(self, reset='new'):
        self._boot('OSRunning', reset)
        self._poll()
        self._poll()

    def _use_real_inventory(self):
        self.cache.side_effect = self.real_cache
        self.system.bios_version = '2.0'
        self.manager.model = 'BMC'
        self.chassis.network_adapters.get_members.return_value = [
            self._nic('1')]

    def _nic(self, identity, serial=None):
        return SimpleNamespace(
            identity=identity, serial_number=serial,
            path='/Chassis/1/NetworkAdapters/' + identity,
            manufacturer='NIC vendor', model='NIC',
            controllers=[SimpleNamespace(firmware_package_version='2.0')])

    @ddt.data((sushy.TASK_STATE_STARTING, sushy.TASK_STATE_RUNNING),
              (sushy.TASK_STATE_STARTING, sushy.TASK_STATE_EXCEPTION),
              (sushy.TASK_STATE_COMPLETED, sushy.TASK_STATE_RUNNING),
              (sushy.TASK_STATE_COMPLETED, sushy.TASK_STATE_EXCEPTION),
              (sushy.TASK_STATE_COMPLETED, None),
              (sushy.TASK_STATE_STARTING, None))
    @ddt.unpack
    def test_reset_retry_revalidates_staged_task(self, before, after):
        self._start(components=('nic:1',))
        self.task_state.task_state = before
        if before == sushy.TASK_STATE_STARTING:
            self._poll()
        with mock.patch.object(
                self.firmware, '_boot_observation', autospec=True,
                side_effect=exception.RedfishConnectionError(
                    node=self.node.uuid, error='baseline unavailable')):
            self._poll()
        self.assertTrue(self._updates()[0]['staged'])
        self.power.assert_not_called()
        self.firmware = firmware.RedfishFirmware()
        if after is None:
            self.get_monitor.side_effect = (
                exception.RedfishTaskMonitorNotFound(
                    node=self.node.uuid, error='purged'))
        else:
            self.task_state.task_state = after
        self._poll()
        if after is None and before == sushy.TASK_STATE_COMPLETED:
            self.power.assert_called_once()
            self._finish_boot()
            self.resume.assert_called_once()
        else:
            self.power.assert_not_called()
            self.resume.assert_not_called()
            if after == sushy.TASK_STATE_EXCEPTION:
                self.assertEqual(states.CLEANFAIL, self.node.provision_state)
                self.assertTrue(self.node.maintenance)
                self.assertIsNone(self._updates())
            else:
                self.assertEqual(states.CLEANWAIT, self.node.provision_state)
                self.get_monitor.side_effect = None
                self.task_state.task_state = sushy.TASK_STATE_COMPLETED
                self._poll()
                self._finish_boot()
                self.power.assert_called_once()
                self.resume.assert_called_once()

    @ddt.data('bios', 'bmc')
    def test_restart_before_reboot_transition_retries_valid_preparation(
            self, component):
        self.config(firmware_update_reboot_delay=0,
                    firmware_update_required_successes=1, group='redfish')
        self._start(components=(('bmc', 'bios') if component == 'bmc'
                                else ('bios',)))
        if component == 'bmc':
            self._poll()
        transition = self.firmware._transition

        def interrupt(task, state, new_state, **kwargs):
            if new_state == firmware.STATE_REBOOTING:
                raise SystemExit('conductor stopped before intent save')
            return transition(task, state, new_state, **kwargs)

        with mock.patch.object(self.firmware, '_transition', autospec=True,
                               side_effect=interrupt):
            self.assertRaises(SystemExit, self._poll)
        self.node.refresh()
        self.assertEqual(firmware.STATE_VALIDATING_BMC if component == 'bmc'
                         else firmware.STATE_STAGING, self._record()['state'])
        if component == 'bios':
            self.assertEqual(0, self._record()['segment']['current'])
        self.assertIsNone(self._record()['verify'])
        self.assertIsNone(self._record()['reboot_time'])
        self.power.assert_not_called()
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.power.assert_called_once()
        self.assertEqual(firmware.STATE_REBOOTING, self._record()['state'])

    @ddt.data('bios', 'bmc')
    def test_async_flag_save_publishes_complete_reboot_intent(self, component):
        self.config(firmware_update_reboot_delay=0,
                    firmware_update_required_successes=1, group='redfish')
        self._start(components=(('bmc', 'bios') if component == 'bmc'
                                else ('bios',)))
        if component == 'bmc':
            self._poll()
        set_flags = deploy_utils.set_async_step_flags

        def interrupt(node, **kwargs):
            set_flags(node, **kwargs)
            if kwargs.get('reboot'):
                raise SystemExit('conductor stopped after async flag save')

        with mock.patch.object(deploy_utils, 'set_async_step_flags',
                               autospec=True, side_effect=interrupt):
            self.assertRaises(SystemExit, self._poll)
        self.node.refresh()
        state = self._record()
        self.assertEqual(firmware.STATE_REBOOTING, state['state'])
        self.assertIsNone(state['segment']['current'])
        self.assertTrue(state['verify']['before'])
        self.assertTrue(state['reboot_time'])
        self.assertTrue(self.node.driver_internal_info['cleaning_reboot'])
        self.power.assert_not_called()
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self._poll()
        self.assertNotEqual(firmware.STATE_STAGING, self._record()['state'])
        self.assertFalse(self.node.maintenance)
        self.power.assert_not_called()  # A committed intent is never replayed.
        self.resume.assert_not_called()

    @ddt.data('bios', 'bmc')
    def test_old_saved_reset_preparation_can_recover(self, component):
        self.config(firmware_update_reboot_delay=0,
                    firmware_update_required_successes=1, group='redfish')
        self._start(components=(('bmc', 'bios') if component == 'bmc'
                                else ('bios',)))
        if component == 'bmc':
            self._poll()
        state = self._record()
        state['verify'] = self.firmware._build_verify([])
        state['reboot_time'] = self.now.isoformat()
        if component == 'bmc':
            state['bmc']['reboot_requested'] = False
        else:
            state['segment']['current'] = None
            state['settings'][0]['staged'] = True
        self.node.set_driver_internal_info(firmware.FIRMWARE_UPDATE_STATE,
                                           state)
        self.node.save()
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.assertEqual(firmware.STATE_REBOOTING, self._record()['state'])
        self.power.assert_called_once()

    def test_late_job_restarts_persisted_inventory_settling_wait(self):
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=('bios',), wait=180)
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.jobs.return_value[0]['state'] = 'Completed'
        self._boot('OSRunning', 'new')
        self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_INVENTORY,
                         self._record()['state'])
        inventory_entered_at = self._record()['entered_at']
        self.jobs.return_value.append(self._job('JID_child', 'Running'))
        for _ in range(3):
            self.firmware = firmware.RedfishFirmware()
            self._poll()
            self.assertEqual(inventory_entered_at,
                             self._record()['entered_at'])
            self.assertTrue(
                self._record()['inventory_wait_restart_pending'])
        self.jobs.return_value[-1]['state'] = 'Completed'
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.cache.assert_not_called()
        self.resume.assert_not_called()
        self.assertEqual(self.now.isoformat(), self._record()['entered_at'])
        self.assertNotIn('inventory_wait_restart_pending', self._record())
        self._poll(179)
        self.cache.assert_not_called()
        self.resume.assert_not_called()
        self._poll(1)
        self.cache.assert_called_once()
        self.resume.assert_called_once()
        self.power.assert_called_once()
        self.service.simple_update.assert_called_once()

    def test_late_job_with_zero_settling_wait_hands_off_when_complete(self):
        self.config(firmware_update_inventory_wait=0, group='redfish')
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=('bios',))
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.jobs.return_value[0]['state'] = 'Completed'
        self._boot('MemoryInitializationStarted', 'new')
        self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_BOOT,
                         self._record()['state'])

        self.jobs.return_value.append(self._job('JID_child', 'Running'))
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self.assertEqual(firmware.STATE_VERIFYING_INVENTORY,
                         self._record()['state'])
        self.assertTrue(
            self._record()['inventory_wait_restart_pending'])
        self.cache.assert_not_called()
        self.resume.assert_not_called()

        self.jobs.return_value[-1]['state'] = 'Completed'
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.cache.assert_called_once()
        self.resume.assert_called_once()
        self.power.assert_called_once()

    @ddt.data('clean', 'service', 'deploy')
    def test_undeclared_handler_transition_fails_without_power_action(
            self, step):
        self._start(step=step, components=('bios',))
        with mock.patch.object(self.firmware, '_handle_staging',
                               autospec=True,
                               return_value=firmware.STATE_STARTING):
            self._poll()
        self.assertEqual({'clean': states.CLEANFAIL,
                          'service': states.SERVICEFAIL,
                          'deploy': states.DEPLOYFAIL}[step],
                         self.node.provision_state)
        self.assertIn('Invalid firmware state transition',
                      self.node.last_error)
        self.assertIn('state-machine error', self.node.last_error)
        self.assertTrue(self.node.maintenance)
        self.assertIsNone(self._updates())
        self._poll()
        self.power.assert_not_called()
        self.resume.assert_not_called()
        self.service.simple_update.assert_called_once()

    def test_initial_preparation_error_propagates_with_guard_intact(self):
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.side_effect = RuntimeError('preparation bug')
        self.assertRaisesRegex(RuntimeError, 'preparation bug', self._start,
                               components=('bios',))
        self.node.refresh()
        self.assertEqual(states.CLEANWAIT, self.node.provision_state)
        self.assertTrue(self.node.driver_internal_info.get(
            async_steps.FIRMWARE_UPDATE_IN_PROGRESS))
        self.assertIsNotNone(self._updates())
        self.service.simple_update.assert_not_called()
        self.power.assert_not_called()

    @ddt.data('clean', 'service')
    def test_post_accepted_submission_error_has_one_failure_owner(self, step):
        self.config(poweroff_in_cleanfail=True, poweroff_in_servicefail=True,
                    group='conductor')
        self.service.simple_update.side_effect = None
        self.service.simple_update.return_value = SimpleNamespace()
        clean_step = {
            'interface': 'firmware', 'step': 'update',
            'args': {'settings': [{'component': 'bios',
                                  'url': 'https://firmware/bios'}]}}
        if step == 'clean':
            self.node.provision_state = states.CLEANWAIT
            self.node.clean_step = clean_step
            self.node.driver_internal_info = {'clean_steps': [clean_step]}
            runner = cleaning.do_next_clean_step
            failed_state = states.CLEANFAIL
        else:
            self.node.provision_state = states.SERVICEWAIT
            self.node.service_step = clean_step
            self.node.driver_internal_info = {'service_steps': [clean_step]}
            runner = servicing.do_next_service_step
            failed_state = states.SERVICEFAIL
        self.node.save()

        teardown_name = ('tear_down_cleaning' if step == 'clean'
                         else 'tear_down_service')
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            with mock.patch.object(task.driver.deploy, teardown_name,
                                   autospec=True) as teardown:
                runner(task, 0, disable_ramdisk=True)
                teardown.assert_not_called()

        self.node.refresh()
        self.assertEqual(1, self.service.simple_update.call_count)
        self.assertEqual(failed_state, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.assertIn('Do not power-cycle', self.node.maintenance_reason)
        self.assertIsNotNone(self._updates())
        self.power.assert_not_called()

    def test_one_component_per_reboot_and_no_early_next_submission(self):
        self._start()
        self._poll()
        self.service.simple_update.assert_called_once()
        self.power.assert_called_once()
        self._poll()  # Old readiness remains latched.
        self.resume.assert_not_called()
        self.cache.assert_not_called()
        self.service.simple_update.assert_called_once()
        self._boot('SystemHardwareInitializationComplete', 'boot1')
        self._poll()
        self.service.simple_update.assert_called_once()
        self._poll()
        self.assertEqual(2, self.service.simple_update.call_count)
        self.assertEqual(1, self.power.call_count)
        self._poll()
        self.assertEqual(2, self.power.call_count)
        self._finish_boot('boot2')
        self.resume.assert_called_once()
        self.assertIsNone(self._updates())
        self.assertEqual(['https://firmware/bios', 'https://firmware/nic:1'],
                         [call.args[0] for call in
                          self.service.simple_update.call_args_list])

    @ddt.data('service', 'clean', 'deploy')
    def test_step_specific_boot_targets(self, step):
        self._start(step=step, components=('bios',))
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        if step == 'service':
            self._poll()
            self.resume.assert_not_called()
            self._finish_boot()
        else:
            self._poll()
        self.resume.assert_called_once()
        self.power.assert_called_once()

    def test_deploy_accepts_setup(self):
        self._start(step='deploy', components=('bios',))
        self._poll()
        self._boot('SetupEntered', 'new')
        self._poll()
        self._poll()
        self.resume.assert_called_once()

    @ddt.data('clean', 'service', 'deploy')
    def test_timeout_preserves_power_with_overall_timeout_disabled(self, step):
        self.config(firmware_update_overall_timeout=0,
                    firmware_update_apply_timeout=120, group='redfish')
        self.config(poweroff_in_cleanfail=True, poweroff_in_servicefail=True,
                    group='conductor')
        self._start(step=step, components=('bios',))
        self.task_state.task_state = sushy.TASK_STATE_RUNNING
        self._poll(120)
        expected = {'clean': states.CLEANFAIL, 'service': states.SERVICEFAIL,
                    'deploy': states.DEPLOYFAIL}[step]
        self.assertEqual(expected, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.assertIn('timed out', self.node.last_error)
        self.assertIn('Do not power-cycle', self.node.last_error)
        self.power.assert_not_called()
        self.resume.assert_not_called()
        self.assertIsNone(self._updates())

    def test_outage_is_not_reset_evidence(self):
        self._start(components=('bios',))
        self._poll()
        utils.get_system.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='down')
        self._poll()
        utils.get_system.side_effect = None
        self._poll()
        self.resume.assert_not_called()
        self.cache.assert_not_called()
        self.power.assert_called_once()

    def test_one_boot_observation_per_poll_and_no_stale_reuse(self):
        self._start(components=('bios',))
        with mock.patch.object(self.firmware, '_boot_observation',
                               wraps=self.firmware._boot_observation) as read:
            self._poll()
            read.assert_called_once()
            read.reset_mock()
            self._boot('OSRunning', 'new')
            self._poll()
            read.assert_called_once()
            read.reset_mock()
            read.side_effect = exception.RedfishConnectionError(
                node=self.node.uuid, error='boot observation unavailable')
            self._poll()
            read.assert_called_once()
            self.resume.assert_not_called()
            self.cache.assert_not_called()
            read.side_effect = None
            self._poll()
            self.resume.assert_called_once()

    def test_vendor_discovery_is_retained_across_polls_and_components(self):
        self.node.properties = {}
        self.system.manufacturer = 'Dell Inc.'
        self.jobs.return_value = []
        resolve = self._patch(utils, 'get_system_vendor',
                              side_effect=utils.get_system_vendor)
        self._start()
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.jobs.return_value[0]['state'] = 'Completed'
        self.firmware = firmware.RedfishFirmware()
        self._finish_boot()
        self.assertEqual(2, self.service.simple_update.call_count)
        self._poll()
        resolve.assert_called_once()
        self.assertEqual({}, self.node.properties)

    def test_failed_vendor_discovery_does_not_choose_non_dell_path(self):
        self.node.properties = {}
        self.system.manufacturer = 'Dell Inc.'
        self.jobs.return_value = []
        utils.get_system.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='manufacturer unavailable')
        self._start(components=('bios',))
        self.assertNotIn('vendor', self._record())
        self.service.simple_update.assert_not_called()
        utils.get_system.side_effect = None
        self._poll()
        self.assertEqual('Dell Inc.', self._record()['vendor'])
        self._poll()  # No OEM job yet: cannot use the completed Task alone.
        self.power.assert_not_called()
        self.service.simple_update.assert_called_once()

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
        self._finish_boot()
        self.resume.assert_called_once()
        self.power.assert_called_once()

    @ddt.data('bios', 'nic:1', 'bmc')
    def test_failed_reset_preparation_recovers_after_reload(self, component):
        self.config(firmware_update_reboot_delay=0,
                    firmware_update_required_successes=1, group='redfish')
        components = ((component, 'bios') if component == 'bmc'
                      else (component,))
        self._start(components=components)
        if component == 'bmc':
            self._poll()  # BMC completion before its host handoff.
        with mock.patch.object(
                self.firmware, '_boot_observation', autospec=True,
                side_effect=exception.RedfishConnectionError(
                    node=self.node.uuid, error='baseline temporarily absent')):
            self._poll()
        self.assertIsNone(self._record().get('verify'))
        self.assertIsNone(self._record().get('reboot_time'))
        self.power.assert_not_called()
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.power.assert_called_once()
        self._finish_boot()
        if component == 'bmc':
            self._poll()
            self._finish_boot('second')
        self.resume.assert_called_once()
        self.assertEqual(len(components), self.power.call_count)

    def test_old_partial_reset_record_can_retry_preparation(self):
        self._start(components=('bios',))
        record = self._record()
        record['verify'] = {
            'jids': ['JID_1'], 'lc': 'pending', 'boot': 'pending',
            'new_boot_observed': False, 'os_boot_started_at': None}
        self.node.set_driver_internal_info(firmware.FIRMWARE_UPDATE_STATE,
                                           record)
        self.node.save()
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.power.assert_called_once()
        self._finish_boot()
        self.resume.assert_called_once()
        self.power.assert_called_once()

    def test_lost_verification_after_reset_never_replays_power_action(self):
        self._start(components=('bios',))
        self._poll()
        record = self._record()
        del record['verify']
        self.node.set_driver_internal_info(firmware.FIRMWARE_UPDATE_STATE,
                                           record)
        self.node.save()
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.power.assert_called_once()
        self.resume.assert_not_called()

    def test_ambiguous_submission_is_not_replayed(self):
        self.service.simple_update.side_effect = (
            exception.RedfishConnectionError(
                node=self.node.uuid, error='POST response lost'))
        try:
            self._start(components=('bios',))
        except exception.RedfishConnectionError:
            self.node.refresh()
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.service.simple_update.assert_called_once()
        self.power.assert_not_called()
        self.resume.assert_not_called()
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)

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

    def test_per_component_wait_remains_supported(self):
        self._start(components=('bios',), wait=120)
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        self._poll()
        self.resume.assert_not_called()
        self._poll()
        self.resume.assert_called_once()

    @ddt.data('Scheduled', 'Running', 'UserIntervention', 'Unknown')
    def test_dell_unfinished_job_holds_completion(self, job_state):
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=('nic:1',))
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.jobs.return_value[0]['state'] = job_state
        self._finish_boot()
        self.resume.assert_not_called()
        self.cache.assert_not_called()
        self.jobs.return_value[0]['state'] = 'Completed'
        self._poll()
        self._poll()
        self.resume.assert_called_once()
        self.power.assert_called_once()

    def test_dell_failed_late_job_blocks_next_component(self):
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

    @ddt.data('clean', 'service', 'deploy')
    def test_failed_job_preserves_power_while_another_job_runs(self, step):
        self.config(poweroff_in_cleanfail=True, poweroff_in_servicefail=True,
                    group='conductor')
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(step=step)
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.jobs.return_value = [self._job('JID_1', 'Failed'),
                                  self._job('JID_child', 'Running')]
        self._poll()
        expected = {'clean': states.CLEANFAIL, 'service': states.SERVICEFAIL,
                    'deploy': states.DEPLOYFAIL}[step]
        self.assertEqual(expected, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.assertIsNone(self._updates())
        self.assertIn('JID_1=Failed', self.node.last_error)
        self.assertIn('JID_child=Running', self.node.maintenance_reason)
        self.firmware = firmware.RedfishFirmware()
        self._poll()  # Retired queue cannot resume another image or reset.
        self.power.assert_called_once()
        self.assertEqual(states.REBOOT, self.power.call_args.args[1])
        self.service.simple_update.assert_called_once()
        self.resume.assert_not_called()

    def test_unavailable_dell_gate_warns_once_across_polls_and_reload(self):
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = None
        self._start(components=('bios',))
        with mock.patch.object(firmware, 'LOG', autospec=True) as log:
            self._poll()
            self._poll()
            self.firmware = firmware.RedfishFirmware()
            self._finish_boot()
            warnings = [call for call in log.warning.call_args_list
                        if 'Cannot verify Dell LC jobs' in call.args[0]]
            self.assertEqual(1, len(warnings))
        self.resume.assert_called_once()

    @ddt.data('Running', 'Failed')
    def test_late_job_during_inventory_still_blocks_completion(self, outcome):
        self.config(firmware_update_inventory_wait=60, group='redfish')
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=('bios',))
        self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        self.jobs.return_value[0]['state'] = 'Completed'
        self._boot('OSRunning', 'new')
        self._poll()
        self.jobs.return_value.append(self._job('JID_child', outcome))
        self._poll()
        self.resume.assert_not_called()
        self.cache.assert_not_called()
        if outcome == 'Running':
            self.jobs.return_value[1]['state'] = 'Completed'
            self._poll()
            self.resume.assert_not_called()
            self._poll(60)
            self.resume.assert_called_once()
        else:
            self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.power.assert_called_once()

    @ddt.data(('inventory', True), ('inventory', False),
              ('late_job', True), ('late_job', False))
    @ddt.unpack
    def test_inventory_recovery_respects_nested_deadlines(
            self, blocker, verify_enabled):
        self.config(firmware_update_apply_timeout=480,
                    firmware_update_post_reboot_verify_timeout=(
                        240 if verify_enabled else 0), group='redfish')
        if blocker == 'late_job':
            self.node.properties = {'vendor': 'Dell Inc.'}
            self.jobs.return_value = []
        self._start(components=('bios',))
        if blocker == 'late_job':
            self.jobs.return_value = [self._job('JID_1', 'Scheduled')]
        self._poll()
        if blocker == 'late_job':
            self.jobs.return_value[0]['state'] = 'Completed'
        self._boot('OSRunning', 'new')
        self._poll()  # Enter inventory verification.
        if blocker == 'late_job':
            self.jobs.return_value.append(self._job('JID_child', 'Running'))
        else:
            self.cache.side_effect = exception.RedfishError(
                error='inventory unavailable')
        self._poll()
        self._poll(120)  # 240 seconds since the apply reset.
        if not verify_enabled:
            self.assertEqual(states.CLEANWAIT, self.node.provision_state)
            self._poll(180)  # The finite component deadline still applies.
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.power.assert_called_once()
        self.resume.assert_not_called()

    def test_synchronous_success_without_monitor_is_not_ambiguous(self):
        self.service.simple_update.side_effect = None
        self.service.simple_update.return_value = SimpleNamespace(
            task_monitor_uri=None)
        self._start(components=('bios',))
        self._poll()
        self.power.assert_called_once()
        self._finish_boot()
        self.resume.assert_called_once()

    @ddt.data(False, True)
    def test_disappeared_task_requires_persisted_positive_outcome(
            self, success):
        self._start(components=('bios',))
        if success:
            self._poll()
        self.get_monitor.side_effect = exception.RedfishTaskMonitorNotFound()
        self.firmware = firmware.RedfishFirmware()
        self._finish_boot()
        if success:
            self.resume.assert_called_once()
            self.power.assert_called_once()
        else:
            self.resume.assert_not_called()
            self.power.assert_not_called()
        self.service.simple_update.assert_called_once()

    def test_non_dell_starting_compatibility(self):
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
        self.resume.assert_not_called()
        self._poll(120)
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.resume.assert_not_called()
        self.power.assert_called_once()

    def test_bmc_version_is_captured_before_submission(self):
        self.config(firmware_update_reboot_delay=0,
                    firmware_update_required_successes=2, group='redfish')

        def submit(url, **kwargs):
            self.manager.firmware_version = '2.0'
            return self._submit(url, **kwargs)

        self.service.simple_update.side_effect = submit
        self._start(components=('bmc',))
        self._poll()
        self._poll()
        self.cache.assert_not_called()
        self._poll()
        self._poll()
        self.resume.assert_called_once()
        self.power.assert_not_called()

    def test_bmc_resource_recovery_requires_consecutive_samples(self):
        self.config(firmware_update_reboot_delay=0,
                    firmware_update_required_successes=2, group='redfish')
        self._start(components=('bmc',))
        self._poll()
        self._poll()  # One successful recovery sample.
        utils.get_manager.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='BMC still recovering')
        self._poll()
        utils.get_manager.side_effect = None
        self._poll()  # First success after the interruption.
        self.resume.assert_not_called()
        self.cache.assert_not_called()
        self._poll()
        self._poll()
        self.resume.assert_called_once()
        self.power.assert_not_called()

    def test_hpe_nic_visibility_is_required_before_submission(self):
        adapters = self.chassis.network_adapters
        self.chassis.network_adapters = None
        self._start(components=('nic:1',))
        self._poll()
        self.service.simple_update.assert_not_called()
        self.power.assert_not_called()
        self.chassis.network_adapters = adapters
        self._poll()
        self.service.simple_update.assert_called_once()
        self._poll()
        self._finish_boot()
        self.resume.assert_called_once()

    def test_failed_task_preserves_power_and_prevents_next_image(self):
        self.config(poweroff_in_servicefail=True, group='conductor')
        self._start(step='service')
        self.task_state.task_state = sushy.TASK_STATE_EXCEPTION
        self.task_state.task_status = sushy.HEALTH_CRITICAL
        self.task_state.messages = [SimpleNamespace(
            message='Firmware staging failed',
            message_id='Update.ApplyFailed')]
        self._poll()
        self.assertEqual(states.SERVICEFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.assertIn('Firmware staging failed', self.node.last_error)
        self.service.simple_update.assert_called_once()
        self.power.assert_not_called()
        self.resume.assert_not_called()

    def test_multiple_systems_use_explicit_target(self):
        utils.get_system_collection.return_value.members_identities = [
            '/Systems/1', '/Systems/2']
        self._start(components=('bios',))
        self.service.simple_update.assert_called_once_with(
            'https://firmware/bios',
            targets=[self.node.driver_info['redfish_system_id']])

    def test_read_error_does_not_authorize_reset(self):
        self._start(components=('bios',))
        self.get_monitor.side_effect = exception.RedfishError(
            error='unauthorized')
        self._poll()
        self._poll()
        self.power.assert_not_called()
        self.resume.assert_not_called()
        self.get_monitor.side_effect = None
        self._poll()
        self._finish_boot()
        self.resume.assert_called_once()

    @ddt.data('bios', 'bmc')
    def test_unreadable_application_task_is_retried(self, component):
        self.config(firmware_update_reboot_delay=0,
                    firmware_update_required_successes=1, group='redfish')
        self._start(components=(component,))
        if component == 'bios':
            self._poll()  # Apply reset before testing application monitoring.
            self._boot('OSRunning', 'new')
        self.get_monitor.return_value.get_task.side_effect = ValueError(
            'invalid Task representation')
        self._poll()
        self.resume.assert_not_called()
        self.cache.assert_not_called()
        self.assertEqual(states.CLEANWAIT, self.node.provision_state)
        self.get_monitor.return_value.get_task.side_effect = None
        self._poll()
        self._poll()
        if component == 'bmc':
            self._poll()
        self.resume.assert_called_once()
        self.service.simple_update.assert_called_once()

    @ddt.data('bios', 'nic:1')
    def test_unreadable_staging_task_recovers_without_early_reset(
            self, component):
        self._start(components=(component,))
        self.get_monitor.return_value.get_task.side_effect = ValueError(
            'invalid staging Task representation')
        self._poll()
        self.assertIn('invalid staging Task', self._record()['last_error'])
        self.power.assert_not_called()
        self.resume.assert_not_called()
        self.firmware = firmware.RedfishFirmware()
        self.get_monitor.return_value.get_task.side_effect = None
        self._poll()
        self._finish_boot()
        self.resume.assert_called_once()
        self.power.assert_called_once()

    @ddt.data(ValueError, TypeError, RuntimeError)
    def test_bad_task_does_not_starve_another_node(self, error_type):
        self._start(components=('bios',))
        other = obj_utils.create_test_node(
            self.context, uuid='11111111-1111-4111-8111-111111111111',
            driver='redfish', driver_info=self.node.driver_info,
            properties=self.node.properties, provision_state=states.CLEANWAIT,
            clean_step=self.node.clean_step,
            driver_internal_info=self.node.driver_internal_info)
        bad_monitor = SimpleNamespace(get_task=mock.Mock(
            side_effect=error_type('bad first-node Task')))
        good_monitor = self.get_monitor.return_value
        self.get_monitor.side_effect = lambda node, uri: (
            bad_monitor if node.uuid == self.node.uuid else good_monitor)
        candidates = [(node.uuid, node.driver, node.conductor_group,
                       node.driver_internal_info)
                      for node in (self.node, other)]
        manager = SimpleNamespace(iter_nodes=mock.Mock(
            return_value=candidates))
        powered = []
        self.power.side_effect = lambda task, *args, **kwargs: powered.append(
            task.node.uuid)
        self.firmware._query_update_status(manager, self.context)
        self.node.refresh()
        self.power.assert_called_once()
        self.assertEqual([other.uuid], powered)
        if error_type is RuntimeError:
            self.assertEqual(states.CLEANFAIL, self.node.provision_state)
            self.assertIn('RuntimeError', self.node.last_error)
            self.assertTrue(self.node.maintenance)
        else:
            self.assertEqual(states.CLEANWAIT, self.node.provision_state)
            self.assertIn('bad first-node Task',
                          self._record()['last_error'])
            self.assertIsNone(self._record().get('verify'))

    def test_unreadable_staging_task_remains_deadline_bounded(self):
        self.config(firmware_update_apply_timeout=120, group='redfish')
        self._start(components=('bios',))
        self.get_monitor.return_value.get_task.side_effect = ValueError('bad')
        self._poll()
        self._poll()
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.power.assert_not_called()
        self.resume.assert_not_called()

    @ddt.data(('bmc',), ('bmc', 'bios'))
    def test_dell_missing_bmc_job_blocks_recovery(self, components):
        self.config(firmware_update_apply_timeout=900, group='redfish')
        self.node.properties = {'vendor': 'Dell Inc.'}
        self.jobs.return_value = []
        self._start(components=components)
        self.get_monitor.side_effect = exception.RedfishTaskMonitorNotFound()
        self._poll(300)
        self._poll(300)
        self.resume.assert_not_called()
        self._poll(300)
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.service.simple_update.assert_called_once()
        self.power.assert_not_called()

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
    def test_dell_bmc_completed_job_survives_purge_and_reload(
            self, components):
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
        self.jobs.return_value = []
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        if len(components) > 1:
            self.power.assert_called_once()
            self._finish_boot()
            self.assertEqual(2, self.service.simple_update.call_count)
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
        self._finish_boot()
        self.resume.assert_called_once()
        self.power.assert_called_once()

    @ddt.data('nic', 'manager', 'chassis')
    def test_real_inventory_read_failures_hold_update(self, resource):
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
        reader = {'nic': self.chassis.network_adapters.get_members,
                  'manager': utils.get_manager,
                  'chassis': utils.get_chassis}[resource]
        reader.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='inventory temporarily unavailable')
        self._poll()
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
        self.resume.assert_not_called()
        self._poll(120)
        self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.assertTrue(self.node.maintenance)
        self.power.assert_called_once()

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

    @ddt.data('nic:1', 'nic:SERIAL')
    def test_nic_id_and_serial_aliases_verify_the_bound_adapter(
            self, requested):
        self._use_real_inventory()
        self.chassis.network_adapters.get_members.return_value = [
            self._nic('1', 'SERIAL')]
        self._start(components=(requested,))
        self.assertEqual('SERIAL',
                         self._updates()[0]['nic_identity']['serial'])
        self._poll()
        self.firmware = firmware.RedfishFirmware()
        self._finish_boot()
        self.resume.assert_called_once()
        self.power.assert_called_once()
        self.service.simple_update.assert_called_once()

    @ddt.data('renumbered', 'replaced', 'missing', 'ambiguous')
    def test_nic_binding_survives_reload_and_checks_physical_identity(
            self, change):
        self._use_real_inventory()
        self.chassis.network_adapters.get_members.return_value = [
            self._nic('1', 'SERIAL')]
        self._start(components=('nic:1',))
        self._poll()
        replacements = {
            'renumbered': [self._nic('2', 'SERIAL')],
            'replaced': [self._nic('1', 'DIFFERENT')],
            'missing': [],
            # Neither candidate retains the saved ID/URI, and the serial is
            # no longer unique enough to establish a renumbered adapter.
            'ambiguous': [self._nic('2', 'SERIAL'), self._nic('3', 'SERIAL')],
        }
        self.chassis.network_adapters.get_members.return_value = replacements[
            change]
        self.firmware = firmware.RedfishFirmware()
        self._finish_boot()
        if change == 'renumbered':
            self.resume.assert_called_once()
        else:
            self.resume.assert_not_called()
            self.assertIn('nic:1', self._record()['last_error'])
            self._poll(1800)
            self.assertEqual(states.CLEANFAIL, self.node.provision_state)
        self.power.assert_called_once()
        self.service.simple_update.assert_called_once()

    @ddt.data(('nic:1', False), ('nic:SERIAL', False),
              ('nic:1', True), ('nic:SERIAL', True))
    @ddt.unpack
    def test_new_duplicate_cannot_replace_unreadable_target(
            self, requested, queued):
        self._use_real_inventory()
        target = self._nic('1', 'SERIAL')
        adapters = self.chassis.network_adapters.get_members
        adapters.return_value = [target]
        components = (requested, 'bios') if queued else (requested,)
        self._start(components=components)
        self.assertTrue(self._updates()[0]['nic_identity']['serial_unique'])
        self._poll()

        target.controllers[0].firmware_package_version = None
        adapters.return_value = [target, self._nic('2', 'SERIAL')]
        self.firmware = firmware.RedfishFirmware()
        self._finish_boot()
        self._poll()
        self.resume.assert_not_called()
        self.service.simple_update.assert_called_once()
        self.power.assert_called_once()
        self.assertIn(requested, self._record()['last_error'])
        self.assertEqual([], list(
            objects.FirmwareComponentList.get_by_node_id(
                self.context, self.node.id)))

        # The duplicate remains. Only the bound target's own readable version
        # allows verification and any subsequent image submission to proceed.
        target.controllers[0].firmware_package_version = '2.1'
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        versions = {entry.component: entry.current_version for entry in
                    objects.FirmwareComponentList.get_by_node_id(
                        self.context, self.node.id)}
        self.assertEqual('2.1', versions['nic:1'])
        self.assertEqual('2.0', versions['nic:2'])
        self.power.assert_called_once()
        if queued:
            self.resume.assert_not_called()
            self.assertEqual(2, self.service.simple_update.call_count)
            self._poll()
            self._finish_boot('next')
        self.resume.assert_called_once()
        self.assertEqual(len(components), self.power.call_count)
        self.assertEqual(len(components),
                         self.service.simple_update.call_count)

    @ddt.data('unknown', 'duplicate_serial', 'alias_collision')
    def test_invalid_nic_alias_is_rejected_before_submission(self, invalid):
        self._use_real_inventory()
        adapters = [self._nic('1', 'SERIAL')]
        requested = 'nic:missing'
        if invalid == 'duplicate_serial':
            adapters.append(self._nic('2', 'SERIAL'))
            requested = 'nic:SERIAL'
        elif invalid == 'alias_collision':
            adapters.append(self._nic('SERIAL', 'DIFFERENT'))
            requested = 'nic:SERIAL'
        self.chassis.network_adapters.get_members.return_value = adapters
        self.assertRaises(exception.InvalidParameterValue, self._start,
                          components=(requested,))
        self.service.simple_update.assert_not_called()
        self.power.assert_not_called()

    def test_duplicate_serial_discovery_survives_partial_inventory_and_reload(
            self):
        self._use_real_inventory()
        target = self._nic('1', 'SERIAL')
        peer = self._nic('2', 'SERIAL')
        adapters = self.chassis.network_adapters.get_members
        adapters.return_value = [target]
        self._start(components=('nic:1',))
        self._poll()
        target.controllers[0].firmware_package_version = None
        adapters.return_value = [target, peer]
        self._finish_boot()
        self.resume.assert_not_called()
        self.assertFalse(self._updates()[0]['nic_identity']['serial_unique'])

        adapters.return_value = [peer]
        self.firmware = firmware.RedfishFirmware()
        self._poll()
        self.resume.assert_not_called()
        self.assertEqual([], list(
            objects.FirmwareComponentList.get_by_node_id(
                self.context, self.node.id)))

        target.controllers[0].firmware_package_version = '2.1'
        adapters.return_value = [target]
        self._poll()
        self.resume.assert_called_once()
        self.power.assert_called_once()
        self.service.simple_update.assert_called_once()

    @ddt.data(False, True)
    def test_duplicate_serials_still_allow_unambiguous_redfish_id(
            self, unreadable_peer):
        self._use_real_inventory()
        peer = self._nic('2', 'SERIAL')
        if unreadable_peer:
            peer.controllers = []
        self.chassis.network_adapters.get_members.return_value = [
            self._nic('1', 'SERIAL'), peer]
        self._start(components=('nic:1',))
        self.assertFalse(self._updates()[0]['nic_identity']['serial_unique'])
        self._poll()
        self._finish_boot()
        self.resume.assert_called_once()

    def test_transient_nic_preflight_failure_does_not_submit_an_image(self):
        self._use_real_inventory()
        reader = self.chassis.network_adapters.get_members
        reader.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='inventory temporarily unavailable')
        self._start(components=('nic:1',))
        self._poll()
        self.service.simple_update.assert_not_called()
        reader.side_effect = None
        self._poll()
        self._poll()
        self._finish_boot()
        self.resume.assert_called_once()
        self.service.simple_update.assert_called_once()

    @ddt.data('preflight', 'cached')
    def test_previously_supported_nic_inventory_cannot_disappear(self, source):
        self._use_real_inventory()
        if source == 'cached':
            self.node.properties = {'vendor': 'Generic'}
            self.node.save()
            with task_manager.acquire(self.context, self.node.uuid) as task:
                self.firmware.cache_firmware_components(task)
        self._start(components=('nic:1',))
        self._poll()
        self._boot('SystemHardwareInitializationComplete', 'new')
        self._poll()
        adapters = self.chassis.network_adapters
        self.chassis.network_adapters = None
        self._poll()
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
            self._finish_boot()
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
        self._finish_boot()
        self.assertEqual(2, self.service.simple_update.call_count)
        self.assertEqual('bios', self._updates()[0]['component'])

    def test_latched_target_never_becomes_reset_evidence_by_waiting(self):
        self._start(components=('bios',))
        self._poll()
        self._poll(660)
        self.resume.assert_not_called()
        self.power.assert_called_once()

    def test_invalid_bios_compatibility_policy_rejected(self):
        self.node.driver_info = dict(
            self.node.driver_info, firmware_update_bios_pending_reset='yes')
        self.assertRaises(exception.InvalidParameterValue, self._start)
        self.service.simple_update.assert_not_called()
