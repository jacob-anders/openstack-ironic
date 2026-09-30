#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Firmware interface, inventory and image-staging contracts.

Sequential application and recovery are exercised through update()/polling in
test_firmware_sequential.
"""

import inspect
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlparse

import ddt
import sushy

from ironic.common import exception
from ironic.common import states
from ironic.conductor import task_manager
from ironic.drivers.modules.redfish import firmware
from ironic.drivers.modules.redfish import firmware_utils
from ironic.drivers.modules.redfish import utils
from ironic import objects
from ironic.tests.unit.db import base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils


@ddt.ddt
class RedfishFirmwareTestCase(base.DbTestCase):

    def setUp(self):
        super().setUp()
        self.config(enabled_bios_interfaces=['redfish'],
                    enabled_hardware_types=['redfish'],
                    enabled_power_interfaces=['redfish'],
                    enabled_boot_interfaces=['redfish-virtual-media'],
                    enabled_management_interfaces=['redfish'],
                    enabled_firmware_interfaces=['redfish'])
        self.node = obj_utils.create_test_node(
            self.context, driver='redfish',
            driver_info=db_utils.get_test_redfish_info(),
            properties={'vendor': 'Generic'})
        self.firmware = firmware.RedfishFirmware()
        self.system = self._patch(utils, 'get_system').return_value
        self.system.bios_version = '1.0'
        self.manager = self._patch(utils, 'get_manager').return_value
        self.manager.firmware_version = '2.0'
        self.manager.model = 'BMC'
        self.chassis = self._patch(utils, 'get_chassis').return_value
        self.adapter = SimpleNamespace(
            identity='1', serial_number=None,
            manufacturer='NIC vendor', model='NIC',
            controllers=[SimpleNamespace(firmware_package_version='3.0')])
        self.chassis.network_adapters.get_members.return_value = [self.adapter]

    def _patch(self, target, attribute, **kwargs):
        patcher = mock.patch.object(target, attribute, autospec=True, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _cache(self, **kwargs):
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.firmware.cache_firmware_components(task, **kwargs)
        return {component.component: component.current_version
                for component in objects.FirmwareComponentList.get_by_node_id(
                    self.context, self.node.id)}

    def test_get_properties(self):
        properties = self.firmware.get_properties()
        for name in utils.COMMON_PROPERTIES:
            self.assertIn(name, properties)
        self.assertIn('firmware_update_boot_progress', properties)
        self.assertIn('firmware_update_bios_pending_reset', properties)

    def test_settings_only_api_and_no_declared_state_machine(self):
        self.assertEqual(['self', 'task', 'settings'], list(
            inspect.signature(firmware.RedfishFirmware.update).parameters))
        self.assertFalse(hasattr(firmware, 'FIRMWARE_UPDATE_STATE'))
        self.assertFalse(hasattr(firmware, '_TRANSITIONS'))

    def test_validate(self):
        parse = self._patch(utils, 'parse_driver_info')
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.firmware.validate(task)
            parse.assert_called_once_with(task.node)

    def test_status_callback_ignores_a_node_that_failed_after_selection(self):
        self.node.provision_state = states.SERVICEFAIL
        self.node.maintenance = True
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid) as task:
            with mock.patch.object(self.firmware,
                                   '_check_node_redfish_firmware_update',
                                   autospec=True) as poll:
                callback = inspect.unwrap(
                    firmware.RedfishFirmware._query_update_status)
                callback(self.firmware, task, None, None)
                poll.assert_not_called()

    def test_failure_callback_does_not_clear_a_new_update(self):
        self.node.provision_state = states.SERVICEWAIT
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid) as task:
            with mock.patch.object(self.firmware, '_clear_updates',
                                   autospec=True) as cleanup:
                callback = inspect.unwrap(
                    firmware.RedfishFirmware._query_update_failed)
                callback(self.firmware, task, None, None)
                cleanup.assert_not_called()

    @ddt.data([], [{'component': 'unknown', 'url': 'https://firmware/image'}],
              [{'component': 'bios'}], [{'url': 'https://firmware/image'}])
    def test_invalid_settings(self, settings):
        service = self._patch(utils, 'get_update_service')
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.assertRaises(exception.InvalidParameterValue,
                              self.firmware.update, task, settings)
        service.assert_not_called()

    def test_missing_update_service(self):
        self._patch(utils, 'get_update_service',
                    side_effect=exception.RedfishConnectionError(
                        node=self.node.uuid, error='unavailable'))
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.assertRaises(exception.RedfishConnectionError,
                              self.firmware.update, task,
                              [{'component': 'bios',
                                'url': 'https://firmware/image'}])

    def test_create_all_components(self):
        self.assertEqual({'bios': '1.0', 'bmc': '2.0', 'nic:1': '3.0'},
                         self._cache())

    def test_update_existing_components(self):
        self._cache()
        self.system.bios_version = '1.1'
        self.assertEqual({'bios': '1.1', 'bmc': '2.0', 'nic:1': '3.0'},
                         self._cache())
        self.assertEqual(3, len(
            objects.FirmwareComponentList.get_by_node_id(
                self.context, self.node.id)))

    @ddt.data('bios', 'bmc', 'nic:1')
    def test_discovery_can_omit_unavailable_component(self, component):
        if component == 'bios':
            self.system.bios_version = None
        elif component == 'bmc':
            self.manager.firmware_version = None
        else:
            self.adapter.controllers[0].firmware_package_version = None
        versions = self._cache()
        self.assertNotIn(component, versions)
        self.assertEqual(2, len(versions))

    def test_missing_all_components(self):
        self.system.bios_version = None
        self.manager.firmware_version = None
        self.chassis.network_adapters = None
        self.assertRaises(exception.UnsupportedDriverExtension, self._cache)

    def test_nic_serial_preferred_for_discovery(self):
        self.adapter.serial_number = 'SERIAL'
        versions = self._cache()
        self.assertIn('nic:SERIAL', versions)
        self.assertNotIn('nic:1', versions)

    @ddt.data('manager', 'chassis', 'members')
    def test_discovery_read_failures_are_best_effort(self, resource):
        reader = {'manager': utils.get_manager,
                  'chassis': utils.get_chassis,
                  'members': self.chassis.network_adapters.get_members}[
                      resource]
        reader.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='temporarily down')
        versions = self._cache()
        self.assertIn('bios', versions)
        self.assertNotIn('bmc' if resource == 'manager' else 'nic:1', versions)

    @ddt.data('manager', 'chassis', 'members')
    def test_requested_inventory_read_failure_propagates(self, resource):
        reader = {'manager': utils.get_manager,
                  'chassis': utils.get_chassis,
                  'members': self.chassis.network_adapters.get_members}[
                      resource]
        reader.side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='temporarily down')
        requested = 'bmc' if resource == 'manager' else 'nic:1'
        self.assertRaises(exception.RedfishConnectionError, self._cache,
                          required_components=[requested])
        self.assertEqual([], list(
            objects.FirmwareComponentList.get_by_node_id(
                self.context, self.node.id)))

    @ddt.data(sushy.exceptions.BadRequestError,
              sushy.exceptions.MissingAttributeError)
    def test_member_read_errors_do_not_become_unsupported(self, error_type):
        if error_type is sushy.exceptions.BadRequestError:
            error = error_type('GET', '/NetworkAdapters',
                               mock.Mock(status_code=400))
        else:
            error = error_type(attribute='Members', resource='NIC')
        self.chassis.network_adapters.get_members.side_effect = error
        self.assertRaises(error_type, self._cache,
                          required_components=['nic:1'])

    @ddt.data(False, True)
    def test_explicitly_absent_nic_capability(self, previously_supported):
        self.chassis.network_adapters = None
        if previously_supported:
            self.assertRaises(exception.RedfishError, self._cache,
                              required_components=['nic:1'],
                              known_supported_types=['nic'])
        else:
            self.assertEqual({'bios': '1.0', 'bmc': '2.0'},
                             self._cache(required_components=['nic:1']))

    def test_missing_requested_component_does_not_publish_partial_cache(self):
        self.assertRaises(exception.RedfishError, self._cache,
                          required_components=['nic:2'])
        self.assertEqual([], list(
            objects.FirmwareComponentList.get_by_node_id(
                self.context, self.node.id)))

    @ddt.data('duplicate_serial', 'id_serial_collision')
    def test_unbound_alias_uses_identities_without_versions(self, ambiguity):
        self.adapter.serial_number = 'SHARED'
        self.adapter.controllers = []
        peer = SimpleNamespace(
            identity='2' if ambiguity == 'duplicate_serial' else 'SHARED',
            serial_number=('SHARED'
                           if ambiguity == 'duplicate_serial' else None),
            manufacturer='NIC vendor', model='NIC',
            controllers=[SimpleNamespace(firmware_package_version='4.0')])
        self.chassis.network_adapters.get_members.return_value = [
            self.adapter, peer]
        self.assertRaises(exception.RedfishError, self._cache,
                          required_components=['nic:SHARED'])
        self.assertEqual([], list(
            objects.FirmwareComponentList.get_by_node_id(
                self.context, self.node.id)))

    @ddt.data('http://firmware/image', 'https://firmware/image')
    def test_direct_image_needs_no_staging(self, url):
        download = self._patch(firmware_utils, 'download_to_temp')
        self.assertEqual((url, None), self.firmware._stage_firmware_file(
            self.node, {'component': 'bios', 'url': url}))
        download.assert_not_called()

    def test_stage_firmware_file_https(self):
        self.config(firmware_source='local', group='redfish')
        download = self._patch(firmware_utils, 'download_to_temp',
                               return_value='/tmp/test1')
        stage = self._patch(firmware_utils, 'stage',
                            return_value=('http://staged/test1', 'http'))
        self.assertEqual(('http://staged/test1', 'http'),
                         self.firmware._stage_firmware_file(
                             self.node, {'component': 'bios',
                                         'url': 'https://test1'}))
        download.assert_called_once_with(self.node, 'https://test1')
        stage.assert_called_once_with(self.node, 'local', '/tmp/test1')

    def test_stage_firmware_file_swift(self):
        self.config(firmware_source='swift', group='redfish')
        temporary_url = self._patch(firmware_utils, 'get_swift_temp_url',
                                    return_value='http://temp')
        download = self._patch(firmware_utils, 'download_to_temp')
        url = 'swift://container/bios.exe'
        self.assertEqual(('http://temp', None),
                         self.firmware._stage_firmware_file(
                             self.node, {'component': 'bios', 'url': url}))
        temporary_url.assert_called_once_with(urlparse(url))
        download.assert_not_called()

    def test_stage_firmware_file_error(self):
        self.config(firmware_source='local', group='redfish')
        self._patch(firmware_utils, 'download_to_temp',
                    return_value='/tmp/test1')
        self._patch(firmware_utils, 'stage',
                    side_effect=exception.IronicException())
        cleanup = self._patch(firmware_utils, 'cleanup')
        self.assertRaises(exception.IronicException,
                          self.firmware._stage_firmware_file, self.node,
                          {'component': 'bios', 'url': 'https://test1'})
        cleanup.assert_called_once_with(self.node)
