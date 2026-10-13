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

import datetime
import functools
import json
import time
from unittest import mock

from oslo_config import cfg
from oslo_utils import timeutils
import sushy

from ironic.common import exception
from ironic.common import states
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.conf import CONF
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.drac import firmware as drac_fw
from ironic.drivers.modules.redfish import firmware as redfish_fw
from ironic.drivers.modules.redfish import firmware_utils
from ironic.drivers.modules.redfish import utils as redfish_utils
from ironic import objects
from ironic.tests.unit.db import base as db_base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils

INFO_DICT = db_utils.get_test_redfish_info()


def _record(updates, grouping=False, started_at=None):
    """The driver_internal_info record of a firmware update step."""
    return {'version': redfish_fw.RECORD_VERSION,
            'started_at': started_at or timeutils.utcnow().isoformat(),
            'grouping': grouping, 'updates': updates}


def _updates(node):
    """The firmware updates recorded on a node, or None."""
    record = node.driver_internal_info.get(redfish_fw.FW_UPDATE)
    return record['updates'] if record else None


class RedfishFirmwareTestCase(db_base.DbTestCase):

    def setUp(self):
        super(RedfishFirmwareTestCase, self).setUp()
        self.config(enabled_bios_interfaces=['redfish'],
                    enabled_hardware_types=['redfish'],
                    enabled_power_interfaces=['redfish'],
                    enabled_boot_interfaces=['redfish-virtual-media'],
                    enabled_management_interfaces=['redfish'],
                    enabled_firmware_interfaces=['redfish'])
        self.node = obj_utils.create_test_node(
            self.context, driver='redfish', driver_info=INFO_DICT)

    @staticmethod
    def _mock_submit_side_effect(firmware_interface, node, update_service,
                                 fw_upd):
        """Simulate _submit_simple_update accepting a firmware update.

        Lets tests run the real _execute_firmware_update without staging
        a firmware file or calling SimpleUpdate.
        """
        fw_upd['task_monitor'] = '/redfish/v1/TaskService/Tasks/1'
        fw_upd['power_timeout'] = CONF.redfish.firmware_update_reboot_delay
        return fw_upd['task_monitor']

    def test_get_properties(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            properties = task.driver.get_properties()
            for prop in redfish_utils.COMMON_PROPERTIES:
                self.assertIn(prop, properties)

    @mock.patch.object(redfish_utils, 'parse_driver_info', autospec=True)
    def test_validate(self, mock_parse_driver_info):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.firmware.validate(task)
            mock_parse_driver_info.assert_called_once_with(task.node)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects.FirmwareComponentList,
                       'sync_firmware_components', autospec=True)
    def test_missing_all_components(self, sync_fw_cmp_mock, chassis_mock,
                                    manager_mock, system_mock, log_mock):

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            system_mock.return_value.identity = "System1"
            manager_mock.return_value.identity = "Manager1"
            system_mock.return_value.bios_version = None
            manager_mock.return_value.firmware_version = None

            netadp = mock.MagicMock()
            netadp.get_members.return_value = []
            chassis_mock.return_value.network_adapters = netadp

            self.assertRaises(exception.UnsupportedDriverExtension,
                              task.driver.firmware.cache_firmware_components,
                              task)

            sync_fw_cmp_mock.assert_not_called()
            error_msg = (
                'Cannot retrieve firmware for node %s: '
                'no supported components'
                % self.node.uuid)
            log_mock.error.assert_called_once_with(error_msg)

            debug_calls = [
                mock.call('Could not retrieve BiosVersion in node '
                          '%(node_uuid)s system %(system)s',
                          {'node_uuid': self.node.uuid,
                           'system': "System1"}),
                mock.call('Could not retrieve FirmwareVersion in node '
                          '%(node_uuid)s manager %(manager)s',
                          {'node_uuid': self.node.uuid,
                           'manager': "Manager1"}),
                mock.call('Could not retrieve Firmware Package Version '
                          'from NetworkAdapters on node %(node_uuid)s',
                          {'node_uuid': self.node.uuid})]
            log_mock.debug.assert_has_calls(debug_calls)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects.FirmwareComponentList,
                       'sync_firmware_components', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_missing_bios_component(self, fw_cmp_mock, sync_fw_cmp_mock,
                                    manager_mock, system_mock, log_mock):
        create_list = [{'component': 'bmc', 'current_version': 'v1.0.0',
                        'vendor': None, 'model': 'BMC Model',
                        'serial_number': None}]
        sync_fw_cmp_mock.return_value = (
            create_list, [], []
        )

        bmc_component = {'component': 'bmc', 'current_version': 'v1.0.0',
                         'vendor': None, 'model': 'BMC Model',
                         'serial_number': None, 'node_id': self.node.id}

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            system_mock.return_value.identity = "System1"
            system_mock.return_value.bios_version = None
            manager_mock.return_value.firmware_version = "v1.0.0"
            manager_mock.return_value.model = "BMC Model"

            task.driver.firmware.cache_firmware_components(task)
            system_mock.assert_called_once_with(task.node)

            log_mock.debug.assert_any_call(
                'Could not retrieve BiosVersion in node '
                '%(node_uuid)s system %(system)s',
                {'node_uuid': self.node.uuid, 'system': 'System1'})
            sync_fw_cmp_mock.assert_called_once_with(
                task.context, task.node.id,
                [{'component': 'bmc', 'current_version': 'v1.0.0',
                  'vendor': None, 'model': 'BMC Model',
                  'serial_number': None}])
            self.assertTrue(fw_cmp_mock.called)
            fw_cmp_mock.assert_called_once_with(task.context, **bmc_component)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects.FirmwareComponentList,
                       'sync_firmware_components', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_missing_bmc_component(self, fw_cmp_mock, sync_fw_cmp_mock,
                                   manager_mock, system_mock, log_mock):
        create_list = [{'component': 'bios', 'current_version': 'v1.0.0',
                        'vendor': None, 'model': None, 'serial_number': None}]
        sync_fw_cmp_mock.return_value = (
            create_list, [], []
        )

        bios_component = {'component': 'bios', 'current_version': 'v1.0.0',
                          'vendor': None, 'model': None,
                          'serial_number': None, 'node_id': self.node.id}

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            manager_mock.return_value.identity = "Manager1"
            manager_mock.return_value.firmware_version = None
            system_mock.return_value.bios_version = "v1.0.0"
            task.driver.firmware.cache_firmware_components(task)

            log_mock.debug.assert_any_call(
                'Could not retrieve FirmwareVersion in node '
                '%(node_uuid)s manager %(manager)s',
                {'node_uuid': self.node.uuid, 'manager': "Manager1"})
            system_mock.assert_called_once_with(task.node)
            sync_fw_cmp_mock.assert_called_once_with(
                task.context, task.node.id,
                [{'component': 'bios', 'current_version': 'v1.0.0',
                  'vendor': None, 'model': None, 'serial_number': None}])
            self.assertTrue(fw_cmp_mock.called)
            fw_cmp_mock.assert_called_once_with(task.context, **bios_component)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_create_all_components(self, fw_cmp_mock, fw_cmp_list_mock,
                                   chassis_mock, manager_mock, system_mock,
                                   log_mock):
        create_list = [
            {'component': 'bios', 'current_version': 'v1.0.0',
             'vendor': None, 'model': None, 'serial_number': None},
            {'component': 'bmc', 'current_version': 'v1.0.0',
             'vendor': None, 'model': 'BMC Model', 'serial_number': None},
            {'component': 'nic:NIC1', 'current_version': '1',
             'vendor': 'Acme', 'model': 'NIC-X', 'serial_number': None},
        ]
        fw_cmp_list_mock.sync_firmware_components.return_value = (
            create_list, [], []
        )

        bios_component = {'component': 'bios', 'current_version': 'v1.0.0',
                          'vendor': None, 'model': None,
                          'serial_number': None, 'node_id': self.node.id}

        bmc_component = {'component': 'bmc', 'current_version': 'v1.0.0',
                         'vendor': None, 'model': 'BMC Model',
                         'serial_number': None, 'node_id': self.node.id}

        nic_component = {'component': 'nic:NIC1', 'current_version': '1',
                         'vendor': 'Acme', 'model': 'NIC-X',
                         'serial_number': None, 'node_id': self.node.id}

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            manager_mock.return_value.firmware_version = "v1.0.0"
            manager_mock.return_value.model = "BMC Model"
            system_mock.return_value.bios_version = "v1.0.0"
            chassis_mock
            netadp_ctrl = mock.MagicMock()
            netadp_ctrl.firmware_package_version = "1"
            netadp = mock.MagicMock()
            netadp.identity = 'NIC1'
            netadp.serial_number = None
            netadp.manufacturer = 'Acme'
            netadp.model = 'NIC-X'
            netadp.controllers = [netadp_ctrl]
            net_adapters = mock.MagicMock()
            net_adapters.get_members.return_value = [netadp]
            chassis_mock.return_value.network_adapters = net_adapters
            task.driver.firmware.cache_firmware_components(task)

            log_mock.warning.assert_not_called()
            log_mock.debug.assert_called_once_with(
                'Using Identity %(identity)s for '
                'NetworkAdapter %(net_adp_id)s',
                {'identity': 'NIC1', 'net_adp_id': 'NIC1'})
            system_mock.assert_called_once_with(task.node)
            fw_cmp_list_mock.sync_firmware_components.assert_called_once_with(
                task.context, task.node.id,
                [{'component': 'bios', 'current_version': 'v1.0.0',
                  'vendor': None, 'model': None, 'serial_number': None},
                 {'component': 'bmc', 'current_version': 'v1.0.0',
                  'vendor': None, 'model': 'BMC Model', 'serial_number': None},
                 {'component': 'nic:NIC1', 'current_version': '1',
                  'vendor': 'Acme', 'model': 'NIC-X', 'serial_number': None}])
            fw_cmp_calls = [
                mock.call(task.context, **bios_component),
                mock.call().create(),
                mock.call(task.context, **bmc_component),
                mock.call().create(),
                mock.call(task.context, **nic_component),
                mock.call().create()
            ]
            fw_cmp_mock.assert_has_calls(fw_cmp_calls)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_get_chassis_redfish_error(self, sync_fw_cmp_mock, system_mock,
                                       manager_mock, chassis_mock, log_mock):
        system_mock.return_value.identity = "System1"
        system_mock.return_value.bios_version = '1.0.0'
        manager_mock.return_value.identity = "Manager1"
        manager_mock.return_value.firmware_version = '1.0.0'

        chassis_mock.side_effect = exception.RedfishError('not found')

        sync_fw_cmp_mock.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'},],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.firmware.cache_firmware_components(task)

        log_mock.debug.assert_any_call(
            'No chassis available to retrieve NetworkAdapters firmware '
            'information on node %(node_uuid)s',
            {'node_uuid': self.node.uuid}
        )

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_redfish_connection_error(
            self, sync_fw_cmp_mock, manager_mock, system_mock, log_mock):
        """Test that RedfishConnectionError during NIC retrieval is handled."""
        system_mock.return_value.identity = "System1"
        system_mock.return_value.bios_version = '1.0.0'
        manager_mock.return_value.identity = "Manager1"
        manager_mock.return_value.firmware_version = '1.0.0'

        sync_fw_cmp_mock.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(task.driver.firmware,
                                   'retrieve_nic_components',
                                   autospec=True) as mock_retrieve:
                connection_error = exception.RedfishError(
                    'Connection failed')
                mock_retrieve.side_effect = connection_error

                task.driver.firmware.cache_firmware_components(task)

        # Verify warning log for exception is called
        log_mock.warning.assert_any_call(
            'Unable to access NetworkAdapters on node %(node_uuid)s, '
            'Error: %(error)s',
            {'node_uuid': self.node.uuid, 'error': connection_error}
        )

        # Verify debug log for empty NIC list is NOT called
        # (since we caught an exception, not an empty list)
        debug_calls = [call for call in log_mock.debug.call_args_list
                       if 'Could not retrieve Firmware Package Version from '
                          'NetworkAdapters' in str(call)]
        self.assertEqual(len(debug_calls), 0)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_sushy_bad_request_error(
            self, sync_fw_cmp_mock, manager_mock, system_mock, log_mock):
        """Test that sushy BadRequestError during NIC retrieval is handled."""
        system_mock.return_value.identity = "System1"
        system_mock.return_value.bios_version = '1.0.0'
        manager_mock.return_value.identity = "Manager1"
        manager_mock.return_value.firmware_version = '1.0.0'

        sync_fw_cmp_mock.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(task.driver.firmware,
                                   'retrieve_nic_components',
                                   autospec=True) as mock_retrieve:
                bad_request_error = sushy.exceptions.BadRequestError(
                    method='GET', url='/redfish/v1/Chassis/1/NetworkAdapters',
                    response=mock.Mock(status_code=400))
                mock_retrieve.side_effect = bad_request_error

                task.driver.firmware.cache_firmware_components(task)

        # Verify warning log for exception is called
        log_mock.warning.assert_any_call(
            'Unable to access NetworkAdapters on node %(node_uuid)s, '
            'Error: %(error)s',
            {'node_uuid': self.node.uuid, 'error': bad_request_error}
        )

        # Verify debug log for empty NIC list is NOT called
        # (since we caught an exception, not an empty list)
        debug_calls = [call for call in log_mock.debug.call_args_list
                       if 'Could not retrieve Firmware Package Version from '
                          'NetworkAdapters' in str(call)]
        self.assertEqual(len(debug_calls), 0)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_sushy_missing_attribute_error(
            self, sync_fw_cmp_mock, manager_mock, system_mock, log_mock):
        """Test that MissingAttributeError during NIC retrieval is handled."""
        system_mock.return_value.identity = "System1"
        system_mock.return_value.bios_version = '1.0.0'
        manager_mock.return_value.identity = "Manager1"
        manager_mock.return_value.firmware_version = '1.0.0'

        sync_fw_cmp_mock.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(task.driver.firmware,
                                   'retrieve_nic_components',
                                   autospec=True) as mock_retrieve:
                missing_attr_error = sushy.exceptions.MissingAttributeError(
                    attribute='NetworkAdapters',
                    resource='/redfish/v1/Chassis/1')
                mock_retrieve.side_effect = missing_attr_error

                task.driver.firmware.cache_firmware_components(task)

        # Verify warning log for exception is called
        log_mock.warning.assert_any_call(
            'Unable to access NetworkAdapters on node %(node_uuid)s, '
            'Error: %(error)s',
            {'node_uuid': self.node.uuid, 'error': missing_attr_error}
        )

        # Verify debug log for empty NIC list is NOT called
        # (since we caught an exception, not an empty list)
        debug_calls = [call for call in log_mock.debug.call_args_list
                       if 'Could not retrieve Firmware Package Version from '
                          'NetworkAdapters' in str(call)]
        self.assertEqual(len(debug_calls), 0)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_retrieve_nic_components_invalid_firmware_version(
            self, fw_cmp_mock, fw_cmp_list, chassis_mock, manager_mock,
            system_mock, log_mock):
        """Test that NIC components with missing versions are skipped."""
        for invalid_version in [None, ""]:
            fw_cmp_list.reset_mock()
            fw_cmp_mock.reset_mock()
            log_mock.reset_mock()

            create_list = [
                {'component': 'bios', 'current_version': 'v1.0.0',
                 'vendor': None, 'model': None, 'serial_number': None},
                {'component': 'bmc', 'current_version': 'v1.0.0',
                 'vendor': None, 'model': 'BMC Model', 'serial_number': None},
            ]
            fw_cmp_list.sync_firmware_components.return_value = (
                create_list, [], []
            )

            bios_component = {'component': 'bios',
                              'current_version': 'v1.0.0',
                              'vendor': None, 'model': None,
                              'serial_number': None, 'node_id': self.node.id}

            bmc_component = {'component': 'bmc', 'current_version': 'v1.0.0',
                             'vendor': None, 'model': 'BMC Model',
                             'serial_number': None, 'node_id': self.node.id}

            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=True) as task:
                manager_mock.return_value.firmware_version = "v1.0.0"
                manager_mock.return_value.model = "BMC Model"
                system_mock.return_value.bios_version = "v1.0.0"

                netadp_ctrl = mock.MagicMock()
                netadp_ctrl.firmware_package_version = invalid_version
                netadp = mock.MagicMock()
                netadp.identity = 'NIC1'
                netadp.controllers = [netadp_ctrl]
                net_adapters = mock.MagicMock()
                net_adapters.get_members.return_value = [netadp]
                chassis_mock.return_value.network_adapters = net_adapters
                task.driver.firmware.cache_firmware_components(task)

                fw_cmp_list.sync_firmware_components.assert_called_once_with(
                    task.context, task.node.id,
                    [{'component': 'bios', 'current_version': 'v1.0.0',
                      'vendor': None, 'model': None, 'serial_number': None},
                     {'component': 'bmc', 'current_version': 'v1.0.0',
                      'vendor': None, 'model': 'BMC Model',
                      'serial_number': None}])

                fw_cmp_calls = [
                    mock.call(task.context, **bios_component),
                    mock.call().create(),
                    mock.call(task.context, **bmc_component),
                    mock.call().create()
                ]
                fw_cmp_mock.assert_has_calls(fw_cmp_calls)
                log_mock.warning.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_network_adapters_none(
            self, fw_cmp_list, chassis_mock, manager_mock,
            system_mock, log_mock):
        """Test that None network_adapters is handled gracefully."""
        fw_cmp_list.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            system_mock.return_value.bios_version = '1.0.0'
            manager_mock.return_value.firmware_version = '1.0.0'
            # network_adapters is None
            chassis_mock.return_value.network_adapters = None

            task.driver.firmware.cache_firmware_components(task)

        # Should log at debug level, not warning
        log_mock.debug.assert_any_call(
            'NetworkAdapters not available on chassis for '
            'node %(node_uuid)s',
            {'node_uuid': self.node.uuid}
        )
        log_mock.warning.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    def test_retrieve_nic_components_missing_attribute_error(
            self, fw_cmp_list, chassis_mock, manager_mock,
            system_mock, log_mock):
        """Test that MissingAttributeError is handled gracefully."""
        fw_cmp_list.sync_firmware_components.return_value = (
            [{'component': 'bios', 'current_version': '1.0.0'},
             {'component': 'bmc', 'current_version': '1.0.0'}],
            [], [])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            system_mock.return_value.bios_version = '1.0.0'
            manager_mock.return_value.firmware_version = '1.0.0'
            # network_adapters raises MissingAttributeError
            type(chassis_mock.return_value).network_adapters = (
                mock.PropertyMock(
                    side_effect=sushy.exceptions.MissingAttributeError))

            task.driver.firmware.cache_firmware_components(task)

        # Should log at debug level, not warning
        log_mock.debug.assert_any_call(
            'NetworkAdapters not available on chassis for '
            'node %(node_uuid)s',
            {'node_uuid': self.node.uuid}
        )
        log_mock.warning.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_chassis', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponentList', autospec=True)
    @mock.patch.object(objects, 'FirmwareComponent', spec_set=True,
                       autospec=True)
    def test_retrieve_nic_components_serial_number(
            self, fw_cmp_mock, fw_cmp_list, chassis_mock, manager_mock,
            system_mock, log_mock):
        """Test NIC component retrieval uses serial number for HPE systems.

        HPE systems can have NetworkAdapter IDs that change after reboot,
        so we use the SerialNumber when available for stable identification.
        """
        create_list = [
            {'component': 'bios', 'current_version': 'v1.0.0',
             'vendor': None, 'model': None, 'serial_number': None},
            {'component': 'bmc', 'current_version': 'v1.0.0',
             'vendor': None, 'model': 'BMC Model', 'serial_number': None},
            {'component': 'nic:SN12345', 'current_version': '1.2.3',
             'vendor': 'Acme', 'model': 'NIC-A', 'serial_number': 'SN12345'},
            {'component': 'nic:NIC2', 'current_version': '1.2.4',
             'vendor': 'Acme', 'model': 'NIC-B', 'serial_number': None},
        ]
        fw_cmp_list.sync_firmware_components.return_value = (
            create_list, [], []
        )

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            manager_mock.return_value.firmware_version = "v1.0.0"
            manager_mock.return_value.model = "BMC Model"
            system_mock.return_value.bios_version = "v1.0.0"

            # First NIC: has serial number - should use serial number
            netadp_ctrl1 = mock.MagicMock()
            netadp_ctrl1.firmware_package_version = "1.2.3"
            netadp1 = mock.MagicMock()
            netadp1.identity = 'NIC1'
            netadp1.serial_number = 'SN12345'
            netadp1.manufacturer = 'Acme'
            netadp1.model = 'NIC-A'
            netadp1.controllers = [netadp_ctrl1]

            # Second NIC: no serial number - should fall back to identity
            netadp_ctrl2 = mock.MagicMock()
            netadp_ctrl2.firmware_package_version = "1.2.4"
            netadp2 = mock.MagicMock()
            netadp2.identity = 'NIC2'
            netadp2.serial_number = None
            netadp2.manufacturer = 'Acme'
            netadp2.model = 'NIC-B'
            netadp2.controllers = [netadp_ctrl2]

            net_adapters = mock.MagicMock()
            net_adapters.get_members.return_value = [netadp1, netadp2]
            chassis_mock.return_value.network_adapters = net_adapters

            task.driver.firmware.cache_firmware_components(task)

            # Verify components include serial number for first NIC
            fw_cmp_list.sync_firmware_components.assert_called_once_with(
                task.context, task.node.id,
                [{'component': 'bios', 'current_version': 'v1.0.0',
                  'vendor': None, 'model': None, 'serial_number': None},
                 {'component': 'bmc', 'current_version': 'v1.0.0',
                  'vendor': None, 'model': 'BMC Model', 'serial_number': None},
                 {'component': 'nic:SN12345', 'current_version': '1.2.3',
                  'vendor': 'Acme', 'model': 'NIC-A',
                  'serial_number': 'SN12345'},
                 {'component': 'nic:NIC2', 'current_version': '1.2.4',
                  'vendor': 'Acme', 'model': 'NIC-B', 'serial_number': None}])

            # Verify debug log was called for serial number usage
            log_mock.debug.assert_any_call(
                'Using SerialNumber %(serial_number)s for '
                'NetworkAdapter %(net_adp_id)s',
                {'serial_number': 'SN12345', 'net_adp_id': 'NIC1'})


    @mock.patch.object(redfish_utils, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, '_get_connection', autospec=True)
    def test_missing_updateservice(self, conn_mock, log_mock):
        settings = [{'component': 'bmc', 'url': 'http://upfwbmc/v2.0.0'}]
        conn_mock.side_effect = sushy.exceptions.MissingAttributeError(
            attribute='UpdateService', resource='redfish/v1')
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            error_msg = ('The attribute UpdateService is missing from the '
                         'resource redfish/v1')
            self.assertRaisesRegex(
                exception.RedfishError, error_msg,
                task.driver.firmware.update,
                task, settings)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_missing_simple_update_action(self, get_systems_collection_mock,
                                          update_service_mock, log_mock):
        settings = [{'component': 'bmc', 'url': 'http://upfwbmc/v2.0.0'}]
        update_service = update_service_mock.return_value
        update_service.simple_update.side_effect = \
            sushy.exceptions.MissingAttributeError(
                attribute='#UpdateService.SimpleUpdate',
                resource='redfish/v1/UpdateService')
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:

            self.assertRaises(
                exception.RedfishError,
                task.driver.firmware.update,
                task, settings)
            expected_err_msg = (
                'The attribute #UpdateService.SimpleUpdate is missing '
                'from the resource redfish/v1/UpdateService')
            log_mock.error.assert_called_once_with(
                'The attribute #UpdateService.SimpleUpdate is missing '
                'on node %(node)s. Error: %(error)s',
                {'node': self.node.uuid, 'error': expected_err_msg})

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def _test_invalid_settings(self, log_mock):
        step = self.node.clean_step
        settings = step['argsinfo'].get('settings', None)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaises(
                exception.InvalidParameterValue,
                task.driver.firmware.update,
                task, settings)
            log_mock.debug.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def _test_invalid_settings_service(self, log_mock):
        step = self.node.service_step
        settings = step['argsinfo'].get('settings', None)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaises(
                exception.InvalidParameterValue,
                task.driver.firmware.update,
                task, settings)
            log_mock.debug.assert_not_called()

    def test_invalid_component_in_settings(self):
        argsinfo = {'settings': [
            {'component': 'something', 'url': 'https://nic-update/v1.1.0'}
        ]}
        self.node.clean_step = {'priority': 100, 'interface': 'firmware',
                                'step': 'update',
                                'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings()

    def test_invalid_component_in_settings_service(self):
        argsinfo = {'settings': [
            {'component': 'something', 'url': 'https://nic-update/v1.1.0'}
        ]}
        self.node.service_step = {'priority': 100, 'interface': 'firmware',
                                  'step': 'update',
                                  'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings_service()

    def test_missing_required_field_in_settings(self):
        argsinfo = {'settings': [
            {'url': 'https://nic-update/v1.1.0'},
            {'component': "bmc"}
        ]}
        self.node.clean_step = {'priority': 100, 'interface': 'firmware',
                                'step': 'update',
                                'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings()

    def test_missing_required_field_in_settings_service(self):
        argsinfo = {'settings': [
            {'url': 'https://nic-update/v1.1.0'},
            {'component': "bmc"}
        ]}
        self.node.service_step = {'priority': 100, 'interface': 'firmware',
                                  'step': 'update',
                                  'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings_service()

    def test_empty_settings(self):
        argsinfo = {'settings': []}
        self.node.clean_step = {'priority': 100, 'interface': 'firmware',
                                'step': 'update',
                                'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings()

    def test_empty_settings_service(self):
        argsinfo = {'settings': []}
        self.node.service_step = {'priority': 100, 'interface': 'firmware',
                                  'step': 'update',
                                  'argsinfo': argsinfo}
        self.node.save()
        self._test_invalid_settings_service()

    @mock.patch.object(time, 'sleep', lambda seconds: None)
    def _generate_new_driver_internal_info(self, components=[], invalid=False,
                                           add_wait=False, wait=1):
        bmc_component = {'component': 'bmc', 'url': 'https://bmc/v1.0.1'}
        bios_component = {'component': 'bios', 'url': 'https://bios/v1.0.1'}
        if add_wait:
            wait_start_time = timeutils.utcnow() -\
                datetime.timedelta(minutes=1)
            bmc_component['wait_start_time'] = wait_start_time.isoformat()
            bios_component['wait_start_time'] = wait_start_time.isoformat()
            bmc_component['wait'] = wait
            bios_component['wait'] = wait

        self.node.clean_step = {'priority': 100, 'interface': 'bios',
                                'step': 'apply_configuration',
                                'argsinfo': {'settings': []}}

        updates = []
        if 'bmc' in components:
            self.node.clean_step['argsinfo']['settings'].append(
                bmc_component)
            bmc_component['task_monitor'] = '/task/1'
            updates.append(bmc_component)
        if 'bios' in components:
            self.node.clean_step['argsinfo']['settings'].append(
                bios_component)
            bios_component['task_monitor'] = '/task/2'
            updates.append(bios_component)
        self._set_first_phase(updates)

        if invalid:
            self.node.provision_state = states.CLEANING
            self.node.driver_internal_info = {'something': 'else'}
        else:
            self.node.provision_state = states.CLEANING
            self.node.driver_internal_info = {
                redfish_fw.FW_UPDATE: _record(updates),
            }
        self.node.save()

    def _set_started_at(self, ago):
        """Make the recorded firmware update step start ``ago`` ago."""
        record = dict(self.node.driver_internal_info[redfish_fw.FW_UPDATE],
                      started_at=(timeutils.utcnow() - ago).isoformat())
        self.node.set_driver_internal_info(redfish_fw.FW_UPDATE, record)
        self.node.save()

    @staticmethod
    def _set_first_phase(updates):
        """Put the submitted first update in the phase it starts in."""
        if updates:
            updates[0]['phase'] = (
                redfish_fw.PHASE_BMC_UPDATING
                if updates[0]['component'] == 'bmc'
                else redfish_fw.PHASE_STAGING)

    def _generate_new_driver_internal_info_service(self, components=[],
                                                   invalid=False,
                                                   add_wait=False, wait=1):
        bmc_component = {'component': 'bmc', 'url': 'https://bmc/v1.0.1'}
        bios_component = {'component': 'bios', 'url': 'https://bios/v1.0.1'}
        if add_wait:
            wait_start_time = timeutils.utcnow() -\
                datetime.timedelta(minutes=1)
            bmc_component['wait_start_time'] = wait_start_time.isoformat()
            bios_component['wait_start_time'] = wait_start_time.isoformat()
            bmc_component['wait'] = wait
            bios_component['wait'] = wait

        self.node.service_step = {'priority': 100, 'interface': 'bios',
                                  'step': 'apply_configuration',
                                  'argsinfo': {'settings': []}}

        updates = []
        if 'bmc' in components:
            self.node.service_step['argsinfo']['settings'].append(
                bmc_component)
            bmc_component['task_monitor'] = '/task/1'
            updates.append(bmc_component)
        if 'bios' in components:
            self.node.service_step['argsinfo']['settings'].append(
                bios_component)
            bios_component['task_monitor'] = '/task/2'
            updates.append(bios_component)
        self._set_first_phase(updates)

        if invalid:
            self.node.provision_state = states.SERVICING
            self.node.driver_internal_info = {'something': 'else'}
        else:
            self.node.provision_state = states.SERVICING
            self.node.driver_internal_info = {
                redfish_fw.FW_UPDATE: _record(updates),
            }
        self.node.save()

    @mock.patch.object(task_manager, 'acquire', autospec=True)
    def _test__query_methods(self, acquire_mock):
        firmware = redfish_fw.RedfishFirmware()
        mock_manager = mock.Mock()
        node_list = [(self.node.uuid, 'redfish', '',
                      self.node.driver_internal_info)]
        mock_manager.iter_nodes.return_value = node_list
        task = mock.Mock(node=self.node,
                         driver=mock.Mock(firmware=firmware))
        acquire_mock.return_value = mock.MagicMock(
            __enter__=mock.MagicMock(return_value=task))

        firmware._check_node_redfish_firmware_update = mock.Mock()
        firmware._clear_updates = mock.Mock()

        # _query_update_status
        firmware._query_update_status(mock_manager, self.context)
        if not _updates(self.node):
            firmware._check_node_redfish_firmware_update.assert_not_called()
        else:
            firmware._check_node_redfish_firmware_update.\
                assert_called_once_with(task)

        # _query_update_failed
        firmware._query_update_failed(mock_manager, self.context)
        if not _updates(self.node):
            firmware._clear_updates.assert_not_called()
        else:
            firmware._clear_updates.assert_called_once_with(self.node)

    def test_redfish_fw_updates(self):
        self._generate_new_driver_internal_info(['bmc'])
        self._test__query_methods()

    def test_redfish_fw_updates_empty(self):
        self._generate_new_driver_internal_info(invalid=True)
        self._test__query_methods()

    def _test__check_node_redfish_firmware_update(self):
        firmware = redfish_fw.RedfishFirmware()
        firmware._continue_updates = mock.Mock()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.upgrade_lock = mock.Mock()
            task.process_event = mock.Mock()
            firmware._check_node_redfish_firmware_update(task)
            return task, firmware

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test_check_calls_touch_provisioning(self, mock_task_monitor,
                                            mock_get_update_service):
        """Test _check_node_redfish_firmware_update calls touch_provisioning.

        This prevents heartbeat timeouts for firmware updates that don't
        require the ramdisk agent (requires_ramdisk=False). By calling
        touch_provisioning on each poll, we keep provision_updated_at fresh.
        """
        self._generate_new_driver_internal_info(['bmc'])

        # Mock task still in progress
        mock_task_monitor.return_value.is_processing = True
        mock_task_monitor.return_value.get_task.return_value.task_state = \
            sushy.TASK_STATE_RUNNING

        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            with mock.patch.object(task.node, 'touch_provisioning',
                                   autospec=True) as mock_touch:
                firmware._check_node_redfish_firmware_update(task)

                # Verify touch_provisioning was called
                mock_touch.assert_called_once_with()

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_skips_touch_provisioning_on_conn_error(
            self, mock_get_update_service):
        """Test touch_provisioning is NOT called when BMC connection fails.

        When the BMC is unresponsive, we should NOT update
        provision_updated_at. This ensures the process eventually times
        out if the BMC never recovers, rather than being kept alive.
        """
        self._generate_new_driver_internal_info(['bmc'])

        # Mock connection error
        mock_get_update_service.side_effect = exception.RedfishConnectionError(
            'Connection failed')

        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            with mock.patch.object(task.node, 'touch_provisioning',
                                   autospec=True) as mock_touch:
                firmware._check_node_redfish_firmware_update(task)

                # Verify touch_provisioning was NOT called on connection error
                mock_touch.assert_not_called()

    @mock.patch.object(redfish_fw.manager_utils, 'cleaning_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_overall_timeout_exceeded(self, mock_get_update_service,
                                            mock_error_handler):
        """Test firmware update fails when overall timeout is exceeded.

        This ensures firmware updates don't run indefinitely - if the
        overall timeout is exceeded, the update should fail with an error.
        Uses clean_step so the error is routed to cleaning_error_handler,
        verifying the fix where _check_overall_timeout unconditionally
        called servicing_error_handler.
        """
        self._generate_new_driver_internal_info(['bmc'])
        self.node.provision_state = states.CLEANING

        # Set start time to 3 hours ago (exceeds 2 hour default timeout)
        self._set_started_at(datetime.timedelta(hours=3))

        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            firmware._check_node_redfish_firmware_update(task)

            # Verify error handler was called with timeout message
            mock_error_handler.assert_called_once()
            call_args = mock_error_handler.call_args
            self.assertIn('exceeded', call_args[0][1].lower())
            self.assertIn('timeout', call_args[0][1].lower())

            # Verify the firmware update info was cleaned up
            task.node.refresh()
            self.assertNotIn(redfish_fw.FW_UPDATE,
                             task.node.driver_internal_info)

    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test_check_overall_timeout_not_exceeded(self, mock_task_monitor,
                                                mock_get_update_service):
        """Test firmware update continues when timeout not exceeded."""
        self._generate_new_driver_internal_info(['bmc'])

        # Set start time to 1 hour ago (within 2 hour default timeout)
        self._set_started_at(datetime.timedelta(hours=1))

        # Mock task still in progress
        mock_task_monitor.return_value.is_processing = True
        mock_task_monitor.return_value.get_task.return_value.task_state = \
            sushy.TASK_STATE_RUNNING

        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            with mock.patch.object(task.node, 'touch_provisioning',
                                   autospec=True) as mock_touch:
                firmware._check_node_redfish_firmware_update(task)

                # Verify touch_provisioning was called (update continues)
                mock_touch.assert_called_once_with()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_conn_error(self, get_us_mock, log_mock):
        self._generate_new_driver_internal_info(['bmc'])
        get_us_mock.side_effect = exception.RedfishConnectionError('Error')
        try:
            self._test__check_node_redfish_firmware_update()
        except exception.RedfishError as e:
            exception_error = e.kwargs.get('error')

            warning_calls = [
                mock.call('Unable to communicate with firmware update '
                          'service on node %(node)s. Will try again on '
                          'the next poll. Error: %(error)s',
                          {'node': self.node.uuid,
                           'error': exception_error})
            ]
            log_mock.warning.assert_has_calls(warning_calls)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_check_bmc_version',
                       autospec=True)
    def test_check_update_wait_elapsed(self, check_mock, tm_mock,
                                       get_us_mock, log_mock):
        # The BMC update waited for its wait, and its task completed
        tm_mock.return_value.get_task.return_value = mock.Mock(
            task_state=sushy.TASK_STATE_COMPLETED,
            task_status=sushy.HEALTH_OK, messages=[])
        self._generate_new_driver_internal_info(['bmc'], add_wait=True)

        task, interface = self._test__check_node_redfish_firmware_update()

        check_mock.assert_called_once_with(
            interface, task, get_us_mock.return_value, mock.ANY, mock.ANY)
        fw_upd = check_mock.call_args[0][4]
        self.assertEqual(redfish_fw.PHASE_BMC_CHECKING, fw_upd['phase'])

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_check_bmc_version',
                       autospec=True)
    def test_check_update_bmc_wait_not_elapsed(self, check_mock,
                                               get_us_mock, log_mock):
        self._generate_new_driver_internal_info(['bmc'], add_wait=True,
                                                wait=600)

        self._test__check_node_redfish_firmware_update()

        check_mock.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_check_update_still_waiting(self, get_us_mock, log_mock):
        # An applied update holds for its wait before the next one
        self._generate_new_driver_internal_info(
            ['bios'], add_wait=True, wait=600)
        updates = _updates(self.node)
        updates[0]['phase'] = redfish_fw.PHASE_WAITING
        self.node.set_driver_internal_info(redfish_fw.FW_UPDATE,
                                           _record(updates))
        self.node.save()

        _, interface = self._test__check_node_redfish_firmware_update()
        interface._continue_updates.assert_not_called()

        # Once the wait is over, the updates continue
        self._generate_new_driver_internal_info(
            ['bios'], add_wait=True, wait=1)
        updates = _updates(self.node)
        updates[0]['phase'] = redfish_fw.PHASE_WAITING
        self.node.set_driver_internal_info(redfish_fw.FW_UPDATE,
                                           _record(updates))
        self.node.save()

        task, interface = self._test__check_node_redfish_firmware_update()
        interface._continue_updates.assert_called_once_with(
            task, get_us_mock.return_value,
            [{'component': 'bios', 'url': 'https://bios/v1.0.1',
              'task_monitor': '/task/2', 'phase': redfish_fw.PHASE_WAITING}])

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_check_bmc_version',
                       autospec=True)
    def test_check_update_task_monitor_not_found_bmc(self, check_mock,
                                                     tm_mock, get_us_mock,
                                                     log_mock):
        tm_mock.side_effect = exception.RedfishError()
        self._generate_new_driver_internal_info(['bmc'])

        task, interface = self._test__check_node_redfish_firmware_update()

        # The BMC version tells whether the update took effect
        fw_upd = {'component': 'bmc', 'url': 'https://bmc/v1.0.1',
                  'task_monitor': '/task/1',
                  'phase': redfish_fw.PHASE_BMC_CHECKING}
        check_mock.assert_called_once_with(
            interface, task, get_us_mock.return_value, [fw_upd], fw_upd)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_update_in_progress(self, tm_mock, get_us_mock, log_mock):
        tm_mock.return_value.is_processing = True
        task_mock = mock.Mock()
        task_mock.task_state = sushy.TASK_STATE_RUNNING
        tm_mock.return_value.get_task.return_value = task_mock

        self._generate_new_driver_internal_info(['bmc'])

        _, interface = self._test__check_node_redfish_firmware_update()
        debug_calls = [
            mock.call('Firmware update in progress for node %(node)s, '
                      'firmware %(firmware_image)s.',
                      {'node': self.node.uuid,
                       'firmware_image': 'https://bmc/v1.0.1'})]

        log_mock.debug.assert_has_calls(debug_calls)

        interface._continue_updates.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_update_task_state(self, tm_mock, get_us_mock, log_mock):
        """Test task with is_processing=False but still in active state.

        Some BMCs (particularly HPE iLO) may return is_processing=False
        while the task is still in RUNNING, STARTING, or PENDING state.
        The update should continue polling and not be treated as complete.
        """
        self._generate_new_driver_internal_info(['bmc'])

        # Test each of the three active states
        for task_state in [sushy.TASK_STATE_RUNNING,
                           sushy.TASK_STATE_STARTING,
                           sushy.TASK_STATE_PENDING]:
            log_mock.reset_mock()

            tm_mock.return_value.is_processing = False
            mock_task = tm_mock.return_value.get_task.return_value
            mock_task.task_state = task_state
            mock_task.task_status = sushy.HEALTH_OK

            _, interface = self._test__check_node_redfish_firmware_update()

            # Verify the new debug log message
            debug_calls = [
                mock.call('Firmware update in progress for node %(node)s, '
                          'firmware %(firmware_image)s.',
                          {'node': self.node.uuid,
                           'firmware_image': 'https://bmc/v1.0.1'})]

            log_mock.debug.assert_has_calls(debug_calls)
            interface._continue_updates.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_update_task_state_new(self, tm_mock, get_us_mock,
                                          log_mock):
        """Test task in NEW state is treated as in-progress, not terminal.

        Dell iDRAC returns TaskState.NEW immediately after a SimpleUpdate
        request before the task transitions to STARTING. This must not be
        treated as a terminal state, otherwise the firmware update is
        incorrectly declared as failed.
        """
        self._generate_new_driver_internal_info(['bmc'])

        tm_mock.return_value.is_processing = False
        mock_task = tm_mock.return_value.get_task.return_value
        mock_task.task_state = sushy.TASK_STATE_NEW
        mock_task.task_status = sushy.HEALTH_OK

        _, interface = self._test__check_node_redfish_firmware_update()

        debug_calls = [
            mock.call('Firmware update in progress for node %(node)s, '
                      'firmware %(firmware_image)s.',
                      {'node': self.node.uuid,
                       'firmware_image': 'https://bmc/v1.0.1'})]

        log_mock.debug.assert_has_calls(debug_calls)
        interface._continue_updates.assert_not_called()

    @mock.patch.object(manager_utils, 'cleaning_error_handler', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_fail(self, tm_mock, get_us_mock,
                                              cleaning_error_handler_mock):
        mock_sushy_task = mock.Mock()
        mock_sushy_task.task_state = 'exception'
        mock_message_unparsed = mock.Mock()
        mock_message_unparsed.message = None
        message_mock = mock.Mock()
        message_mock.message = 'Firmware upgrade failed'
        messages = mock.MagicMock(return_value=[[mock_message_unparsed],
                                                [message_mock],
                                                [message_mock]])
        mock_sushy_task.messages = messages
        mock_task_monitor = mock.Mock()
        mock_task_monitor.is_processing = False
        mock_task_monitor.get_task.return_value = mock_sushy_task
        tm_mock.return_value = mock_task_monitor
        self._generate_new_driver_internal_info(['bmc'])

        task, interface = self._test__check_node_redfish_firmware_update()

        task.upgrade_lock.assert_called_once_with()
        cleaning_error_handler_mock.assert_called_once()
        interface._continue_updates.assert_not_called()

    @mock.patch.object(manager_utils, 'servicing_error_handler',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_fail_servicing(
            self, tm_mock,
            get_us_mock,
            servicing_error_handler_mock):

        mock_sushy_task = mock.Mock()
        mock_sushy_task.task_state = 'exception'
        mock_message_unparsed = mock.Mock()
        mock_message_unparsed.message = None
        message_mock = mock.Mock()
        message_mock.message = 'Firmware upgrade failed'
        messages = mock.MagicMock(return_value=[[mock_message_unparsed],
                                                [message_mock],
                                                [message_mock]])
        mock_sushy_task.messages = messages
        mock_task_monitor = mock.Mock()
        mock_task_monitor.is_processing = False
        mock_task_monitor.get_task.return_value = mock_sushy_task
        tm_mock.return_value = mock_task_monitor
        self._generate_new_driver_internal_info_service(['bmc'])

        task, interface = self._test__check_node_redfish_firmware_update()

        task.upgrade_lock.assert_called_once_with()
        servicing_error_handler_mock.assert_called_once()
        interface._continue_updates.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_check_bmc_version',
                       autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    @mock.patch.object(redfish_utils, 'get_task_monitor', autospec=True)
    def test__check_node_firmware_update_done(self, tm_mock, get_us_mock,
                                              log_mock,
                                              bmc_completion_mock):
        task_mock = mock.Mock()
        task_mock.task_state = sushy.TASK_STATE_COMPLETED
        task_mock.task_status = sushy.HEALTH_OK
        message_mock = mock.Mock()
        message_mock.message = 'Firmware update done'
        task_mock.messages = [message_mock]
        mock_task_monitor = mock.Mock()
        mock_task_monitor.is_processing = False
        mock_task_monitor.get_task.return_value = task_mock
        tm_mock.return_value = mock_task_monitor
        self._generate_new_driver_internal_info(['bmc'])

        task, interface = self._test__check_node_redfish_firmware_update()
        task.upgrade_lock.assert_called_once_with()
        debug_calls = [
            mock.call('Redfish task completed for node %(node)s, '
                      'firmware %(firmware_image)s: %(messages)s.',
                      {'node': self.node.uuid,
                       'firmware_image': 'https://bmc/v1.0.1',
                       'messages': 'Firmware update done'})]

        log_mock.debug.assert_has_calls(debug_calls)
        # The BMC version tells whether the update took effect
        fw_upd = {'component': 'bmc', 'url': 'https://bmc/v1.0.1',
                  'task_monitor': '/task/1',
                  'phase': redfish_fw.PHASE_BMC_CHECKING}
        bmc_completion_mock.assert_called_once_with(
            interface, task, get_us_mock.return_value, [fw_upd], fw_upd)

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    def test__stage_firmware_file_https(self, stage_mock, dwl_tmp_mock):
        CONF.set_override('firmware_source', 'local', 'redfish')
        firmware_update = {'url': 'https://test1', 'component': 'bmc'}
        node = mock.Mock()
        dwl_tmp_mock.return_value = '/tmp/test1'
        stage_mock.return_value = ('http://staged/test1', 'http')

        firmware = redfish_fw.RedfishFirmware()

        staged_url, needs_cleanup = firmware._stage_firmware_file(
            node, firmware_update)

        self.assertEqual(staged_url, 'http://staged/test1')
        self.assertEqual(needs_cleanup, 'http')
        dwl_tmp_mock.assert_called_with(node, 'https://test1')
        stage_mock.assert_called_with(node, 'local', '/tmp/test1')

    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    @mock.patch.object(firmware_utils, 'get_swift_temp_url', autospec=True)
    def test__stage_firmware_file_swift(
            self, get_swift_tmp_url_mock, stage_mock, dwl_tmp_mock):
        CONF.set_override('firmware_source', 'swift', 'redfish')
        firmware_update = {'url': 'swift://container/bios.exe',
                           'component': 'bios'}
        node = mock.Mock()
        get_swift_tmp_url_mock.return_value = 'http://temp'

        firmware = redfish_fw.RedfishFirmware()

        staged_url, needs_cleanup = firmware._stage_firmware_file(
            node, firmware_update)

        self.assertEqual(staged_url, 'http://temp')
        self.assertIsNone(needs_cleanup)
        dwl_tmp_mock.assert_not_called()
        stage_mock.assert_not_called()

    @mock.patch.object(firmware_utils, 'cleanup', autospec=True)
    @mock.patch.object(firmware_utils, 'download_to_temp', autospec=True)
    @mock.patch.object(firmware_utils, 'stage', autospec=True)
    def test__stage_firmware_file_error(self, stage_mock, dwl_tmp_mock,
                                        cleanup_mock):
        CONF.set_override('firmware_source', 'local', 'redfish')
        node = mock.Mock()
        firmware_update = {'url': 'https://test1', 'component': 'bmc'}
        dwl_tmp_mock.return_value = '/tmp/test1'
        stage_mock.side_effect = exception.IronicException

        firmware = redfish_fw.RedfishFirmware()
        self.assertRaises(exception.IronicException,
                          firmware._stage_firmware_file, node,
                          firmware_update)
        dwl_tmp_mock.assert_called_with(node, 'https://test1')
        stage_mock.assert_called_with(node, 'local', '/tmp/test1')
        cleanup_mock.assert_called_with(node)

    def _test_continue_updates(self):

        update_service_mock = mock.Mock()
        firmware = redfish_fw.RedfishFirmware()

        updates = _updates(self.node)

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            firmware._continue_updates(
                task,
                update_service_mock,
                updates
            )
            return task

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test_continue_update_waitting(self, log_mock):
        self._generate_new_driver_internal_info(['bmc', 'bios'],
                                                add_wait=True, wait=120)
        self._test_continue_updates()
        debug_call = [
            mock.call('Waiting at %(time)s for %(seconds)s seconds '
                      'after %(component)s firmware update %(url)s '
                      'on node %(node)s',
                      {'time': mock.ANY, 'seconds': 120,
                       'component': 'bmc', 'url': 'https://bmc/v1.0.1',
                       'node': self.node.uuid})
        ]
        log_mock.debug.assert_has_calls(debug_call)

    # NOTE(zigo): cache_firmware_components makes real HTTP calls to the
    # node's fake BMC address, which may hang for longer than the per-test
    # 60s SIGALRM timeout in restricted-network build environments
    # (see Debian bug #1149372).
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    def test_continue_updates_last(self, cond_resume_clean_mock, log_mock,
                                   validate_mock,
                                   cache_firmware_components_mock):
        self._generate_new_driver_internal_info(['bmc'])
        task = self._test_continue_updates()

        cond_resume_clean_mock.assert_called_once_with(task)
        # Verify BMC validation was called before resuming conductor
        validate_mock.assert_called_once()
        # Verify firmware components were refreshed before resuming conductor
        cache_firmware_components_mock.assert_called_once()

        info_call = [
            mock.call('Firmware updates completed for node %(node)s',
                      {'node': self.node.uuid})
        ]
        log_mock.info.assert_has_calls(info_call)

    # NOTE(zigo): cache_firmware_components makes real HTTP calls to the
    # node's fake BMC address, which may hang for longer than the per-test
    # 60s SIGALRM timeout in restricted-network build environments
    # (see Debian bug #1149372).
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_service',
                       autospec=True)
    def test_continue_updates_last_service(self, cond_resume_service_mock,
                                           log_mock, validate_mock,
                                           cache_firmware_components_mock):
        self._generate_new_driver_internal_info_service(['bmc'])
        task = self._test_continue_updates()

        cond_resume_service_mock.assert_called_once_with(task)
        # Verify BMC validation was called before resuming conductor
        validate_mock.assert_called_once()
        # Verify firmware components were refreshed before resuming conductor
        cache_firmware_components_mock.assert_called_once()

        info_call = [
            mock.call('Firmware updates completed for node %(node)s',
                      {'node': self.node.uuid})
        ]
        log_mock.info.assert_has_calls(info_call)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_continue_updates_more_updates(self, get_system_collection_mock,
                                           node_power_action_mock,
                                           log_mock,
                                           validate_mock):
        cfg.CONF.set_override('firmware_update_wait_unresponsive_bmc', 0,
                              'redfish')
        self._generate_new_driver_internal_info(['bmc', 'bios'])

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock

        firmware = redfish_fw.RedfishFirmware()
        updates = _updates(self.node)

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.node.save = mock.Mock()

            firmware._continue_updates(task, update_service_mock, updates)

            debug_calls = [
                mock.call('Applying new firmware %(url)s for '
                          '%(component)s on node %(node_uuid)s',
                          {'url': 'https://bios/v1.0.1', 'component': 'bios',
                           'node_uuid': self.node.uuid})
            ]
            log_mock.debug.assert_has_calls(debug_calls)
            self.assertEqual(
                [{'component': 'bios', 'url': 'https://bios/v1.0.1',
                  'task_monitor': '/task/2', 'power_timeout': 300,
                  'phase': redfish_fw.PHASE_STAGING}],
                _updates(task.node))
            update_service_mock.simple_update.assert_called_once_with(
                'https://bios/v1.0.1')
            # node.save() is called twice:
            # 1. When setting the async step flags for staging
            # 2. In _continue_updates after _execute_firmware_update returns
            self.assertEqual(task.node.save.call_count, 2)
            # Verify BMC validation was called before continuing to next update
            validate_mock.assert_called_once_with(firmware, task.node)
            # The next update is staged first; its reboot comes later.
            node_power_action_mock.assert_not_called()

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    @mock.patch.object(time, 'sleep', lambda seconds: None)
    def test__execute_firmware_update_no_targets(self,
                                                 get_system_collection_mock,
                                                 system_mock):
        self._generate_new_driver_internal_info(['bios'])
        with open('ironic/tests/json_samples/'
                  'systems_collection_single.json') as f:
            response_obj = json.load(f)
        system_collection_mock = mock.MagicMock()
        system_collection_mock.get_members.return_value = response_obj[
            'Members']
        get_system_collection_mock.return_value = system_collection_mock

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock
        firmware = redfish_fw.RedfishFirmware()

        settings = [{'component': 'bios', 'url': 'https://bios/v1.0.1'}]
        firmware._execute_firmware_update(self.node, update_service_mock,
                                          settings)
        update_service_mock.simple_update.assert_called_once_with(
            'https://bios/v1.0.1')

    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    @mock.patch.object(time, 'sleep', lambda seconds: None)
    def test__execute_firmware_update_targets(self,
                                              get_system_collection_mock,
                                              system_mock):
        self._generate_new_driver_internal_info(['bios'])
        with open('ironic/tests/json_samples/'
                  'systems_collection_dual.json') as f:
            response_obj = json.load(f)
        system_collection_mock = mock.MagicMock()
        system_collection_mock.members_identities = response_obj[
            'Members']
        get_system_collection_mock.return_value = system_collection_mock

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock
        firmware = redfish_fw.RedfishFirmware()

        settings = [{'component': 'bios', 'url': 'https://bios/v1.0.1'}]
        firmware._execute_firmware_update(self.node, update_service_mock,
                                          settings)
        update_service_mock.simple_update.assert_called_once_with(
            'https://bios/v1.0.1', targets=[mock.ANY])

    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test__execute_firmware_update_unresponsive_bmc(self,
                                                       get_sys_collec_mock,
                                                       system_mock,
                                                       manager_mock,
                                                       set_async_mock):
        cfg.CONF.set_override('firmware_update_wait_unresponsive_bmc', 1,
                              'redfish')
        self._generate_new_driver_internal_info(['bmc'])
        with open(
            'ironic/tests/json_samples/systems_collection_single.json'
        ) as f:
            resp_obj = json.load(f)
        system_collection_mock = mock.MagicMock()
        system_collection_mock.get_members.return_value = resp_obj['Members']
        get_sys_collec_mock.return_value = system_collection_mock

        # Mock BMC version reading for setup
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        manager_mock.return_value = mock_manager

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock
        firmware = redfish_fw.RedfishFirmware()
        settings = [{'component': 'bmc', 'url': 'https://bmc/v1.2.3'}]
        firmware._execute_firmware_update(self.node, update_service_mock,
                                          settings)
        update_service_mock.simple_update.assert_called_once_with(
            'https://bmc/v1.2.3')
        # Verify BMC monitoring setup was called (internally by _execute)
        set_async_mock.assert_called_once_with(
            self.node, reboot=False, polling=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test__execute_firmware_update_unresponsive_bmc_node_override(
            self, get_sys_collec_mock, system_mock, manager_mock,
            set_async_mock):
        self._generate_new_driver_internal_info(['bmc'])
        # Set a specific value for firmware_update_unresponsive_bmc_wait for
        # the node
        with mock.patch('time.sleep', lambda x: None):
            d_info = self.node.driver_info.copy()
            d_info['firmware_update_unresponsive_bmc_wait'] = 1
            self.node.driver_info = d_info
            self.node.save()
        self.assertNotEqual(
            CONF.redfish.firmware_update_wait_unresponsive_bmc,
            self.node.driver_info.get('firmware_update_unresponsive_bmc_wait')
        )
        with open(
            'ironic/tests/json_samples/systems_collection_single.json'
        ) as f:
            resp_obj = json.load(f)
        system_collection_mock = mock.MagicMock()
        system_collection_mock.get_members.return_value = resp_obj['Members']
        get_sys_collec_mock.return_value = system_collection_mock

        # Mock BMC version reading for setup
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        manager_mock.return_value = mock_manager

        task_monitor_mock = mock.Mock()
        task_monitor_mock.task_monitor_uri = '/task/2'
        update_service_mock = mock.Mock()
        update_service_mock.simple_update.return_value = task_monitor_mock
        firmware = redfish_fw.RedfishFirmware()
        settings = [{'component': 'bmc', 'url': 'https://bmc/v1.2.3'}]
        firmware._execute_firmware_update(self.node, update_service_mock,
                                          settings)
        update_service_mock.simple_update.assert_called_once_with(
            'https://bmc/v1.2.3')
        # Verify BMC monitoring setup was called (internally by _execute)
        set_async_mock.assert_called_once_with(
            self.node, reboot=False, polling=True)

    def test__validate_resources_stability_success(self):
        """Test successful BMC resource validation with consecutive success."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep',
                                   autospec=True) as sleep_mock:

                # Mock successful resource responses
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                net_adapters = chassis_mock.return_value.network_adapters
                net_adapters.get_members.return_value = []

                # Mock time progression to simulate consecutive successes
                time_mock.side_effect = [0, 1, 2, 3]  # 3 successful attempts

                # Should complete successfully after 3 consecutive successes
                firmware._validate_resources_stability(task.node)

                # Verify all resources were checked 3 times (required success)
                self.assertEqual(system_mock.call_count, 3)
                self.assertEqual(manager_mock.call_count, 3)
                self.assertEqual(chassis_mock.call_count, 3)

                # Verify sleep was called between validation attempts
                expected_calls = [mock.call(
                    CONF.redfish.firmware_update_validation_interval)] * 2
                sleep_mock.assert_has_calls(expected_calls)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    def test__validate_resources_stability_disabled(self, system_mock,
                                                    log_mock):
        self.config(firmware_update_required_successes=0,
                    firmware_update_resource_validation_timeout=0,
                    group='redfish')
        firmware = redfish_fw.RedfishFirmware()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            firmware._validate_resources_stability(task.node)

        system_mock.assert_not_called()
        log_mock.info.assert_called_once_with(
            mock.ANY, {'reasons': 'required_successes=0, '
                                  'validation_timeout=0',
                       'node': self.node.uuid})
        # The message formats with its arguments
        msg, args = log_mock.info.call_args[0]
        self.assertIn('required_successes=0', msg % args)

    def test__validate_resources_stability_timeout(self):
        """Test BMC resource validation timeout when not achieved."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True), \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock system always failing
                system_mock.side_effect = exception.RedfishConnectionError(
                    'timeout')

                # Mock time progression to exceed timeout
                time_mock.side_effect = [0, 500]

                # Should raise RedfishError due to timeout
                self.assertRaises(exception.RedfishError,
                                  firmware._validate_resources_stability,
                                  task.node)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test__validate_resources_stability_intermittent_failures(
            self, mock_log):
        """Test BMC resource validation with intermittent failures."""
        cfg.CONF.set_override('firmware_update_required_successes', 3,
                              'redfish')
        cfg.CONF.set_override('firmware_update_validation_interval', 10,
                              'redfish')

        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock intermittent failures: success, success, fail,
                # success, success, success
                # When system_mock raises exception, other calls are not made
                call_count = 0

                def system_side_effect(*args):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 3:  # Third call fails
                        raise exception.RedfishConnectionError('error')
                    return mock.Mock()

                system_mock.side_effect = system_side_effect
                manager_mock.return_value = mock.Mock()
                net_adapters = chassis_mock.return_value.network_adapters
                net_adapters.get_members.return_value = []

                # Mock time progression (6 attempts total)
                time_mock.side_effect = [0, 10, 20, 30, 40, 50, 60]

                # Should eventually succeed after counter reset
                firmware._validate_resources_stability(task.node)

                # Verify all 6 attempts were made for system
                self.assertEqual(system_mock.call_count, 6)
                # Manager and chassis called only 5 times (not on failed)
                self.assertEqual(manager_mock.call_count, 5)
                self.assertEqual(chassis_mock.call_count, 5)

                # Verify verbose logging about BMC recovery was called
                expected_log_call = mock.call(
                    'BMC resource validation failed for node %(node)s: '
                    '%(error)s. This may indicate the BMC is still '
                    'restarting or recovering from firmware update.',
                    {'node': task.node.uuid, 'error': mock.ANY})
                mock_log.debug.assert_has_calls([expected_log_call])

    def test__validate_resources_stability_manager_failure(self):
        """Test BMC resource validation when Manager resource fails."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock:

                # Mock system success, manager failure
                system_mock.return_value = mock.Mock()
                manager_mock.side_effect = exception.RedfishError(
                    'manager error')

                # Mock time progression to exceed timeout
                time_mock.side_effect = [0, 500]

                # Should raise RedfishError due to timeout
                self.assertRaises(exception.RedfishError,
                                  firmware._validate_resources_stability,
                                  task.node)

    def test__validate_resources_stability_network_adapters_failure(self):
        """Test validation when NetworkAdapters resource fails."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock:

                # Mock system and manager success, NetworkAdapters failure
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                chassis_mock.side_effect = exception.RedfishError(
                    'chassis error')

                # Mock time progression to exceed timeout
                time_mock.side_effect = [0, 500]

                # Should raise RedfishError due to timeout
                self.assertRaises(exception.RedfishError,
                                  firmware._validate_resources_stability,
                                  task.node)

    def test__validate_resources_stability_custom_config(self):
        """Test BMC resource validation with custom configuration values."""
        cfg.CONF.set_override('firmware_update_required_successes', 5,
                              'redfish')
        cfg.CONF.set_override('firmware_update_validation_interval', 5,
                              'redfish')

        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep',
                                   autospec=True) as sleep_mock:

                # Mock successful resource responses
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                net_adapters = chassis_mock.return_value.network_adapters
                net_adapters.get_members.return_value = []

                # Mock time progression (5 successful attempts)
                time_mock.side_effect = [0, 5, 10, 15, 20, 25]

                # Should complete successfully after 5 consecutive successes
                firmware._validate_resources_stability(task.node)

                # Verify all resources checked 5 times (custom required)
                self.assertEqual(system_mock.call_count, 5)
                self.assertEqual(manager_mock.call_count, 5)
                self.assertEqual(chassis_mock.call_count, 5)

                # Verify sleep was called with custom interval
                expected_calls = [mock.call(5)] * 4  # 4 sleeps between 5
                sleep_mock.assert_has_calls(expected_calls)

    def test__validate_resources_stability_network_adapters_none(self):
        """Test validation succeeds when network_adapters is None."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock successful resource responses but network_adapters None
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                chassis_mock.return_value.network_adapters = None

                # Mock time progression to simulate consecutive successes
                time_mock.side_effect = [0, 1, 2, 3]

                # Should complete successfully (None network_adapters is OK)
                firmware._validate_resources_stability(task.node)

                # Verify all resources were checked 3 times
                self.assertEqual(system_mock.call_count, 3)
                self.assertEqual(manager_mock.call_count, 3)
                self.assertEqual(chassis_mock.call_count, 3)

    def test__validate_resources_stability_network_adapters_missing_attr(self):
        """Test validation succeeds when network_adapters is missing."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(redfish_utils, 'get_manager',
                                   autospec=True) as manager_mock, \
                 mock.patch.object(redfish_utils, 'get_chassis',
                                   autospec=True) as chassis_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock successful resource responses
                system_mock.return_value = mock.Mock()
                manager_mock.return_value = mock.Mock()
                # network_adapters raises MissingAttributeError
                type(chassis_mock.return_value).network_adapters = (
                    mock.PropertyMock(
                        side_effect=sushy.exceptions.MissingAttributeError))

                # Mock time progression to simulate consecutive successes
                time_mock.side_effect = [0, 1, 2, 3]

                # Should complete successfully (missing network_adapters is OK)
                firmware._validate_resources_stability(task.node)

                # Verify all resources were checked 3 times
                self.assertEqual(system_mock.call_count, 3)
                self.assertEqual(manager_mock.call_count, 3)
                self.assertEqual(chassis_mock.call_count, 3)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test__validate_resources_stability_badrequest_error(self, mock_log):
        """Test BMC resource validation handles BadRequestError correctly."""
        firmware = redfish_fw.RedfishFirmware()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            with mock.patch.object(redfish_utils, 'get_system',
                                   autospec=True) as system_mock, \
                 mock.patch.object(time, 'time', autospec=True) as time_mock, \
                 mock.patch.object(time, 'sleep', autospec=True):

                # Mock BadRequestError from sushy with proper arguments
                mock_response = mock.Mock()
                mock_response.status_code = 400
                system_mock.side_effect = sushy.exceptions.BadRequestError(
                    'http://test', mock_response, mock_response)

                # Mock time progression: start at 0, try once at 10, timeout
                # at 500, this allows at least one loop iteration to trigger
                # the exception
                time_mock.side_effect = [0, 10, 500]

                # Should raise RedfishError due to timeout
                self.assertRaises(exception.RedfishError,
                                  firmware._validate_resources_stability,
                                  task.node)

                # Verify verbose logging about BMC recovery was called
                expected_log_call = mock.call(
                    'BMC resource validation failed for node %(node)s: '
                    '%(error)s. This may indicate the BMC is still '
                    'restarting or recovering from firmware update.',
                    {'node': task.node.uuid, 'error': mock.ANY})
                mock_log.debug.assert_has_calls([expected_log_call])

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_uses_configured_timeout(self, mock_get_update_service,
                                                mock_submit,
                                                mock_set_async_flags,
                                                mock_get_system,
                                                mock_get_manager):
        """Test BMC firmware update sets up version checking."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0'}]

        # Mock system
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system

        # Mock BMC version reading
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        # add task_monitor to the side effect
        mock_submit.side_effect = self._mock_submit_side_effect

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # BMC uses version checking, not immediate reboot
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            # Verify BMC version check tracking is set up
            fw_updates = _updates(task.node) or []
            self.assertEqual(1, len(fw_updates))
            self.assertIn('bmc_check_start_time', fw_updates[0])
            self.assertIn(redfish_fw.BMC_VERSION_BEFORE, fw_updates[0])
            self.assertEqual(redfish_fw.PHASE_BMC_UPDATING,
                             fw_updates[0]['phase'])
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_uses_bmc_constant(self, mock_get_update_service,
                                          mock_submit,
                                          mock_set_async_flags,
                                          mock_get_system,
                                          mock_get_manager):
        """Test BMC firmware update detection works with BMC constant."""
        settings = [{'component': redfish_utils.BMC,
                     'url': 'http://bmc/v1.0.0'}]

        # Mock system
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system

        # Mock BMC version reading
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        # add task_monitor to the side effect
        mock_submit.side_effect = self._mock_submit_side_effect

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # BMC uses version checking, not immediate reboot
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            # Verify BMC version check tracking is set up
            fw_updates = _updates(task.node) or []
            self.assertEqual(1, len(fw_updates))
            self.assertIn('bmc_check_start_time', fw_updates[0])
            self.assertIn(redfish_fw.BMC_VERSION_BEFORE, fw_updates[0])
            self.assertEqual(redfish_fw.PHASE_BMC_UPDATING,
                             fw_updates[0]['phase'])
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_mixed_components_with_bmc(self, mock_get_update_service,
                                              mock_submit,
                                              mock_set_async_flags):
        """Test mixed component update with BIOS and BMC."""
        settings = [
            {'component': 'bios', 'url': 'http://bios/v1.0.0', 'wait': 120},
            {'component': 'bmc', 'url': 'http://bmc/v1.0.0', 'wait': 60}
        ]

        # add task_monitor to the side effect
        mock_submit.side_effect = self._mock_submit_side_effect

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # Only the first component (BIOS) is submitted, for staging
            mock_submit.assert_called_once()
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            fw_updates = _updates(task.node)
            self.assertEqual(2, len(fw_updates))
            self.assertEqual(redfish_fw.PHASE_STAGING,
                             fw_updates[0]['phase'])
            self.assertNotIn('phase', fw_updates[1])
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_with_explicit_wait(self, mock_get_update_service,
                                           mock_submit,
                                           mock_get_system,
                                           mock_get_manager,
                                           mock_set_async_flags):
        """Test BMC update with explicit wait."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'wait': 90}]

        # Mock system
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system

        # Mock BMC version reading
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        # add task_monitor to the side effect
        mock_submit.side_effect = self._mock_submit_side_effect

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # BMC uses version checking, not immediate reboot
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            # Verify wait time is stored
            info = task.node.driver_internal_info
            fw_updates = info[redfish_fw.FW_UPDATE]['updates']
            self.assertEqual(90, fw_updates[0]['wait'])
            self.assertEqual(states.SERVICEWAIT, result)

    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_bmc_no_immediate_reboot(self, mock_get_update_service,
                                            mock_submit,
                                            mock_get_system,
                                            mock_get_manager,
                                            mock_set_async_flags):
        """Test BMC firmware update does not set immediate reboot."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0'}]

        # Mock system
        mock_system = mock.Mock()
        mock_get_system.return_value = mock_system

        # Mock BMC version reading
        mock_manager = mock.Mock()
        mock_manager.firmware_version = '1.0.0'
        mock_get_manager.return_value = mock_manager

        # add task_monitor to the side effect
        mock_submit.side_effect = self._mock_submit_side_effect

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.service_step = {'step': 'update',
                                      'interface': 'firmware'}
            result = task.driver.firmware.update(task, settings)

            # Verify reboot=False for BMC updates
            mock_set_async_flags.assert_called_once_with(
                task.node,
                reboot=False,
                polling=True
            )
            # Verify we return wait state to keep step active
            self.assertEqual(states.SERVICEWAIT, result)

            # Verify BMC version check tracking is set up
            fw_updates = _updates(task.node) or []
            self.assertEqual(1, len(fw_updates))
            self.assertIn('bmc_check_start_time', fw_updates[0])
            self.assertIn(redfish_fw.BMC_VERSION_BEFORE, fw_updates[0])
            self.assertEqual(redfish_fw.PHASE_BMC_UPDATING,
                             fw_updates[0]['phase'])

    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(deploy_utils, 'set_async_step_flags', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_submit_simple_update',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_update_non_bmc_stages_without_reboot(
            self, mock_get_update_service, mock_submit,
            mock_set_async_flags, mock_power_action):
        """BIOS and NIC updates are staged before any reboot."""
        mock_submit.side_effect = self._mock_submit_side_effect

        for component in ('bios', 'nic:BCM57414'):
            mock_set_async_flags.reset_mock()
            settings = [{'component': component, 'url': 'http://fw/v1.0.0'}]
            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=False) as task:
                task.node.service_step = {'step': 'update',
                                          'interface': 'firmware'}
                result = task.driver.firmware.update(task, settings)

                mock_set_async_flags.assert_called_once_with(
                    task.node, reboot=False, polling=True)
                fw_updates = _updates(task.node)
                self.assertEqual(redfish_fw.PHASE_STAGING,
                                 fw_updates[0]['phase'])
                self.assertEqual(states.SERVICEWAIT, result)

        mock_power_action.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_bmc_finished',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    def test_bmc_version_check_timeout(self, mock_get_bmc_version,
                                       mock_finished):
        """After the check timeout, the BMC version decides the reboot."""
        start = (timeutils.utcnow()
                 - datetime.timedelta(seconds=301)).isoformat()
        cases = [
            # BMC unresponsive: whether it took effect is unknown
            (None, True),
            # BMC version unchanged: the update was a no-op
            ('1.0.0', False),
        ]
        for version, reboot in cases:
            mock_finished.reset_mock()
            settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                         'task_monitor': '/tasks/1',
                         'phase': redfish_fw.PHASE_BMC_CHECKING,
                         'wait': 30, 'wait_start_time': start,
                         redfish_fw.BMC_VERSION_BEFORE: '1.0.0',
                         redfish_fw.BMC_CHECK_START: start}]
            mock_get_bmc_version.return_value = version
            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=False) as task:
                task.node.set_driver_internal_info(redfish_fw.FW_UPDATE,
                                                   _record(settings))
                firmware = redfish_fw.RedfishFirmware()
                firmware._check_bmc_version(task, mock.sentinel.service,
                                            settings, settings[0])

                mock_finished.assert_called_once_with(
                    firmware, task, mock.sentinel.service, settings,
                    settings[0], reboot=reboot)
                self.assertNotIn('wait', settings[0])

    @mock.patch.object(redfish_fw.RedfishFirmware, '_bmc_finished',
                       autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    def test_bmc_version_changed(self, mock_get_bmc_version, mock_finished):
        """A changed BMC version reboots if more components follow.

        With grouping, the components that follow are staged without it.
        """
        bmc = {'component': 'bmc', 'url': 'http://bmc/v1.0.0',
               'task_monitor': '/tasks/1',
               'phase': redfish_fw.PHASE_BMC_CHECKING,
               redfish_fw.BMC_VERSION_BEFORE: '1.0.0',
               redfish_fw.BMC_CHECK_START: timeutils.utcnow().isoformat()}
        nic = {'component': 'nic:BCM57414', 'url': 'http://nic/v1.0.0'}
        mock_get_bmc_version.return_value = '2.0.0'
        for settings, grouping, reboot in (([dict(bmc), nic], False, True),
                                           ([dict(bmc), nic], True, False),
                                           ([dict(bmc)], False, False)):
            mock_finished.reset_mock()
            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=False) as task:
                task.node.set_driver_internal_info(
                    redfish_fw.FW_UPDATE, _record(settings, grouping))
                firmware = redfish_fw.RedfishFirmware()
                firmware._check_bmc_version(task, mock.sentinel.service,
                                            settings, settings[0])

                mock_finished.assert_called_once_with(
                    firmware, task, mock.sentinel.service, settings,
                    settings[0], reboot=reboot)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    def test_bmc_finished_with_reboot_applies_before_resume(
            self, mock_power_action, mock_resume_clean, validate_mock):
        """A BMC update needing a reboot is applied before the step ends."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'task_monitor': '/tasks/1', 'power_timeout': 300,
                     'phase': redfish_fw.PHASE_BMC_CHECKING}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.clean_step = {'step': 'update', 'interface': 'firmware'}
            task.node.set_driver_internal_info(redfish_fw.FW_UPDATE,
                                               _record(settings))

            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._bmc_finished(
                task, mock.sentinel.service, settings, settings[0],
                reboot=True)

            # The reboot applies the update, which is then polled
            mock_power_action.assert_called_once_with(task, states.REBOOT,
                                                      300)
            self.assertEqual(redfish_fw.PHASE_APPLYING,
                             _updates(task.node)[0]['phase'])

            # The BMC is validated before its host reboot; the step
            # resumes only once the applied update is confirmed.
            validate_mock.assert_called_once_with(firmware_interface,
                                                  task.node)
            mock_resume_clean.assert_not_called()

    # NOTE(zigo): cache_firmware_components makes real HTTP calls to the
    # node's fake BMC address, which may hang for longer than the per-test
    # 60s SIGALRM timeout in restricted-network build environments
    # (see Debian bug #1149372).
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(manager_utils, 'node_power_action', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_clear_updates',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_update_service', autospec=True)
    def test_final_update_without_reboot_flag_no_reboot(
            self, mock_get_update_service, mock_clear_updates,
            mock_power_action, mock_resume_clean, validate_mock,
            cache_firmware_components_mock):
        """Test final firmware update without reboot flag skips reboot."""
        settings = [{'component': 'bmc', 'url': 'http://bmc/v1.0.0',
                     'task_monitor': '/tasks/1'}]

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            # Set up node as if in cleaning
            task.node.clean_step = {'step': 'update', 'interface': 'firmware'}

            task.node.set_driver_internal_info(redfish_fw.FW_UPDATE,
                                               _record(settings))

            # Call _continue_updates with last firmware
            firmware_interface = redfish_fw.RedfishFirmware()
            firmware_interface._continue_updates(
                task, mock_get_update_service.return_value, settings)

            # Verify reboot was NOT triggered
            mock_power_action.assert_not_called()

            # Verify BMC validation was called before resuming conductor
            validate_mock.assert_called_once()
            # Verify firmware components were refreshed before resuming
            cache_firmware_components_mock.assert_called_once()

            # Verify resume clean was still called
            mock_resume_clean.assert_called_once_with(task)



class RedfishFirmwareStageThenRebootTestCase(db_base.DbTestCase):
    """Tests for staging BIOS and NIC firmware, then rebooting to apply it."""

    BIOS_TM = '/redfish/v1/TaskService/TaskMonitors/1'
    DELL_TM = '/redfish/v1/TaskService/TaskMonitors/JID_839968767020'

    def setUp(self):
        super().setUp()
        self.config(enabled_hardware_types=['redfish'],
                    enabled_power_interfaces=['redfish'],
                    enabled_boot_interfaces=['redfish-virtual-media'],
                    enabled_management_interfaces=['redfish'],
                    enabled_firmware_interfaces=['redfish'])
        self.config(firmware_update_starting_wait=30, group='redfish')
        self.node = obj_utils.create_test_node(
            self.context, driver='redfish', driver_info=INFO_DICT,
            provision_state=states.CLEANWAIT,
            clean_step={'step': 'update', 'interface': 'firmware'})
        self.firmware = redfish_fw.RedfishFirmware()

        self.now = datetime.datetime(2026, 1, 1, 12, 0, 0)

        def utcnow(with_timezone=False):
            if with_timezone:
                return self.now.replace(tzinfo=datetime.timezone.utc)
            return self.now

        utcnow_patcher = mock.patch.object(timeutils, 'utcnow', autospec=True,
                                           side_effect=utcnow)
        utcnow_patcher.start()
        self.addCleanup(utcnow_patcher.stop)

        patchers = {
            'update_service': mock.patch.object(
                redfish_utils, 'get_update_service', autospec=True),
            'task_monitor': mock.patch.object(
                redfish_utils, 'get_task_monitor', autospec=True),
            'power': mock.patch.object(
                manager_utils, 'node_power_action', autospec=True),
            'clean_error': mock.patch.object(
                manager_utils, 'cleaning_error_handler', autospec=True),
            'system': mock.patch.object(
                redfish_utils, 'get_system', autospec=True),
        }
        self.mocks = {}
        for name, patcher in patchers.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        # The previous boot reached OSRunning; a reboot resets the host.
        self._resets = 0
        self._set_boot(sushy.BootProgressStates.OS_RUNNING, reset_time='T0')

    def _advance(self, seconds):
        self.now += datetime.timedelta(seconds=seconds)

    def _set_boot(self, state, reset_time=None):
        """Set what the System reports about the host's boot.

        :param state: the BootProgress LastState, or None for a BMC that
            reports no BootProgress.
        :param reset_time: the LastResetTime, or None if not reported.
        """
        system = self.mocks['system'].return_value
        system.boot_progress = (mock.Mock(last_state=state)
                                if state is not None else None)
        system.json = ({'LastResetTime': reset_time}
                       if reset_time is not None else {})
        self.mocks['system'].side_effect = None

    def _reset_host(self, state=sushy.BootProgressStates.OS_RUNNING):
        """The host resets: a new LastResetTime, and the given state."""
        self._resets += 1
        self._set_boot(state, reset_time='T%d' % self._resets)

    def _verify(self, **fields):
        verify = {'reboot_time': self.now.isoformat(),
                  'before': {'state': 'OSRunning', 'reset_time': 'T0'},
                  'jids': [], 'new_boot': False, 'lc': None,
                  'os_boot_seen': None, 'waiting': None, 'passed': False}
        verify.update(fields)
        return verify

    def _applying(self, component='bios', task_monitor=BIOS_TM, **verify):
        """An update rebooted to apply it at the current time."""
        return self._update(component, phase=redfish_fw.PHASE_APPLYING,
                            task_monitor=task_monitor,
                            verify=self._verify(**verify))


    def _set_updates(self, *updates, grouping=False, **info):
        self.node.driver_internal_info = dict(
            info, **{redfish_fw.FW_UPDATE: _record(list(updates), grouping)})
        self.node.save()

    @staticmethod
    def _update(component='bios', phase=redfish_fw.PHASE_STAGING,
                task_monitor=BIOS_TM, **fields):
        fw_upd = {'component': component,
                  'url': 'http://fw/%s.bin' % component.replace(':', '_'),
                  'task_monitor': task_monitor, 'power_timeout': 300}
        if phase:
            fw_upd['phase'] = phase
        fw_upd.update(fields)
        return fw_upd

    def _set_task(self, state, status=sushy.HEALTH_OK, message=None):
        sushy_task = mock.Mock(task_state=state, task_status=status)
        sushy_task.messages = ([mock.Mock(message=message)] if message
                               else [])
        self.mocks['task_monitor'].return_value.get_task.return_value = (
            sushy_task)
        self.mocks['task_monitor'].side_effect = None

    def _task_gone(self):
        self.mocks['task_monitor'].side_effect = exception.RedfishError(
            error='Task monitor not found')

    def _poll(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.firmware._check_node_redfish_firmware_update(task)
        self.node.refresh()
        return task

    def _updates(self):
        return _updates(self.node)

    def _assert_rebooted_to_apply(self, task):
        self.mocks['power'].assert_called_once_with(task, states.REBOOT, 300)
        fw_upd = self._updates()[0]
        self.assertEqual(redfish_fw.PHASE_APPLYING, fw_upd['phase'])
        self.assertNotIn(redfish_fw.STARTING_SINCE, fw_upd)
        self.assertTrue(self.node.driver_internal_info['cleaning_reboot'])
        self.assertTrue(self.node.driver_internal_info['cleaning_polling'])
        self.mocks['clean_error'].assert_not_called()

    def _assert_waiting(self, phase=redfish_fw.PHASE_STAGING):
        self.mocks['power'].assert_not_called()
        self.mocks['clean_error'].assert_not_called()
        self.assertEqual(phase, self._updates()[0]['phase'])

    def _assert_failed(self, message):
        self.mocks['power'].assert_not_called()
        self.mocks['clean_error'].assert_called_once_with(
            mock.ANY, mock.ANY, traceback=mock.ANY)
        self.assertIn(message, self.mocks['clean_error'].call_args[0][1])
        self.assertIsNone(self._updates())

    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_execute_firmware_update_stages_non_bmc(self, collection_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        update_service = mock.Mock()
        update_service.simple_update.return_value.task_monitor_uri = (
            self.BIOS_TM)

        for component in ('bios', 'nic:NIC.Integrated.1'):
            settings = [{'component': component, 'url': 'http://fw/1.bin'}]
            self._set_updates(*settings)
            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=False) as task:
                self.firmware._execute_firmware_update(
                    task.node, update_service, settings)

                info = task.node.driver_internal_info
                self.assertEqual(
                    [{'component': component, 'url': 'http://fw/1.bin',
                      'task_monitor': self.BIOS_TM, 'power_timeout': 300,
                      'phase': redfish_fw.PHASE_STAGING}],
                    info[redfish_fw.FW_UPDATE]['updates'])
                self.assertFalse(info['cleaning_reboot'])
                self.assertTrue(info['cleaning_polling'])

        self.mocks['power'].assert_not_called()

    def test_staging_task_in_progress(self):
        self._set_updates(self._update())
        for state in (sushy.TASK_STATE_NEW, sushy.TASK_STATE_RUNNING):
            self._set_task(state)
            self._poll()
            self._assert_waiting()
            self.assertNotIn(redfish_fw.STARTING_SINCE, self._updates()[0])

    def test_staging_task_waiting_to_start(self):
        for state in (sushy.TASK_STATE_STARTING, sushy.TASK_STATE_PENDING):
            self.mocks['power'].reset_mock()
            self._set_updates(self._update())
            self._set_task(state)

            # First sighting is recorded, then the task must stay there
            self._poll()
            self._assert_waiting()
            self.assertEqual(self.now.isoformat(),
                             self._updates()[0][redfish_fw.STARTING_SINCE])

            self._advance(29)
            self._poll()
            self._assert_waiting()

            self._advance(1)
            task = self._poll()
            self._assert_rebooted_to_apply(task)

    def test_staging_task_starting_wait_restarts(self):
        self._set_updates(self._update())
        self._set_task(sushy.TASK_STATE_STARTING)
        self._poll()
        self._advance(20)
        # Leaving Starting forgets the earlier sighting
        self._set_task(sushy.TASK_STATE_RUNNING)
        self._poll()
        self.assertNotIn(redfish_fw.STARTING_SINCE, self._updates()[0])

        self._set_task(sushy.TASK_STATE_PENDING)
        self._poll()
        self._advance(20)
        self._poll()
        self._assert_waiting()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_continue_updates_next_submission_fails(self, collection_mock,
                                                    validate_mock):
        collection_mock.return_value.members_identities = ['1']
        self._set_updates(
            self._update('bmc', phase=None),
            {'component': 'nic:NIC.1', 'url': 'http://fw/nic.bin'})
        update_service = mock.Mock()
        update_service.simple_update.side_effect = (
            sushy.exceptions.ConnectionError(url='http://bmc', error='reset'))

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.firmware._continue_updates(task, update_service,
                                            self._updates())

        self.node.refresh()
        self.mocks['clean_error'].assert_called_once()
        self.assertIn('nic:NIC.1',
                      self.mocks['clean_error'].call_args[0][1])
        self.assertIsNone(self._updates())

    def test_staging_task_starting_no_wait(self):
        self.config(firmware_update_starting_wait=0, group='redfish')
        self._set_updates(self._update('nic:NIC.1'))
        self._set_task(sushy.TASK_STATE_STARTING)

        task = self._poll()

        self._assert_rebooted_to_apply(task)

    def test_staging_task_completed(self):
        for status in (sushy.HEALTH_OK, sushy.HEALTH_WARNING):
            self.mocks['power'].reset_mock()
            # A task that had been waiting in Starting completes
            self._set_updates(self._update(
                starting_since=self.now.isoformat()))
            self._set_task(sushy.TASK_STATE_COMPLETED, status)

            task = self._poll()

            self._assert_rebooted_to_apply(task)

    def test_staging_task_failed(self):
        for state, status in ((sushy.TASK_STATE_COMPLETED,
                               sushy.HEALTH_CRITICAL),
                              (sushy.TASK_STATE_EXCEPTION,
                               sushy.HEALTH_CRITICAL)):
            self.mocks['clean_error'].reset_mock()
            self._set_updates(self._update())
            self._set_task(state, status, message='Image download failed')

            self._poll()

            self._assert_failed('Image download failed')

    def test_staging_task_unreadable(self):
        self._set_updates(self._update())
        self.mocks['task_monitor'].side_effect = (
            exception.RedfishConnectionError(node=self.node.uuid,
                                             error='timeout'))
        self._poll()
        self._assert_waiting()

        self.mocks['task_monitor'].side_effect = None
        self.mocks['task_monitor'].return_value.get_task.side_effect = (
            sushy.exceptions.ConnectionError(url='http://bmc', error='boom'))
        self._poll()
        self._assert_waiting()

    @mock.patch.object(redfish_fw.drac_fw, 'check_scheduled_idrac_job',
                       autospec=True)
    def test_staging_task_gone_not_dell(self, job_mock):
        self._set_updates(self._update())
        self._task_gone()

        task = self._poll()

        self._assert_rebooted_to_apply(task)
        job_mock.assert_not_called()

    def test_staging_task_not_found(self):
        self._set_updates(self._update())
        self.mocks['task_monitor'].return_value.get_task.side_effect = (
            sushy.exceptions.ResourceNotFoundError(
                method='GET', url='http://bmc/task',
                response=mock.Mock(status_code=404)))

        task = self._poll()

        self._assert_rebooted_to_apply(task)

    def _dell(self):
        self.node.properties = dict(self.node.properties, vendor='Dell Inc.')
        self.node.save()

    @mock.patch.object(redfish_fw.drac_fw, 'check_scheduled_idrac_job',
                       autospec=True)
    def test_staging_task_gone_dell(self, job_mock):
        self._dell()
        self._task_gone()

        for has_job in (True, None):
            self.mocks['power'].reset_mock()
            job_mock.reset_mock()
            job_mock.return_value = has_job
            self._set_updates(self._update(task_monitor=self.DELL_TM))

            task = self._poll()

            job_mock.assert_called_once_with(task, mock.ANY)
            self.assertEqual(self.DELL_TM,
                             job_mock.call_args[0][1]['task_monitor'])
            self._assert_rebooted_to_apply(task)

    @mock.patch.object(redfish_fw.drac_fw, 'check_scheduled_idrac_job',
                       autospec=True)
    def test_staging_task_gone_dell_no_scheduled_job(self, job_mock):
        self._dell()
        self._set_updates(self._update(task_monitor=self.DELL_TM))
        self._task_gone()
        job_mock.return_value = False

        self._poll()

        self._assert_failed('no scheduled job was found')

    @mock.patch.object(redfish_fw.drac_fw, 'check_scheduled_idrac_job',
                       autospec=True)
    def test_staging_task_gone_dell_job_unreadable(self, job_mock):
        self._dell()
        self._set_updates(self._update(task_monitor=self.DELL_TM))
        self._task_gone()
        job_mock.side_effect = sushy.exceptions.ConnectionError(
            url='http://bmc', error='boom')

        self._poll()

        self._assert_waiting()

    def test_reboot_to_apply_persists_phase_first(self):
        self._set_updates(self._update(starting_since=self.now.isoformat()))
        self._set_task(sushy.TASK_STATE_COMPLETED)

        def check_persisted(task, *args, **kwargs):
            # A conductor restart during the reboot must not reboot again
            node = objects.Node.get(self.context, task.node.uuid)
            fw_upd = _updates(node)[0]
            self.assertEqual(redfish_fw.PHASE_APPLYING, fw_upd['phase'])
            self.assertNotIn(redfish_fw.STARTING_SINCE, fw_upd)
            self.assertTrue(node.driver_internal_info['cleaning_reboot'])

        self.mocks['power'].side_effect = check_persisted

        task = self._poll()

        self._assert_rebooted_to_apply(task)

    def test_reboot_to_apply_power_failure(self):
        self._set_updates(self._update())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self.mocks['power'].side_effect = exception.PowerStateFailure(
            pstate=states.REBOOT)

        self._poll()

        self.mocks['power'].assert_called_once()
        self.mocks['power'].reset_mock()
        self._assert_failed('Failed to reboot node')

    def test_reboot_to_apply_records_verify(self):
        self._set_updates(self._update(task_monitor=self.DELL_TM))
        self._set_task(sushy.TASK_STATE_COMPLETED)

        def reboot(task, *args, **kwargs):
            # What the gates need is persisted before the reboot
            node = objects.Node.get(self.context, task.node.uuid)
            fw_upd = _updates(node)[0]
            self.assertEqual(self._verify(jids=['JID_839968767020']),
                             fw_upd[redfish_fw.VERIFY])
            self._advance(20)

        self.mocks['power'].side_effect = reboot

        task = self._poll()

        self._assert_rebooted_to_apply(task)
        # The gates' delays run from the end of the reboot
        self.assertEqual(
            self._verify(jids=['JID_839968767020']),
            self._updates()[0][redfish_fw.VERIFY])

    def test_reboot_to_apply_boot_state_unreadable(self):
        self._set_updates(self._update())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self.mocks['system'].side_effect = exception.RedfishConnectionError(
            node=self.node.uuid, error='timeout')

        task = self._poll()

        self._assert_rebooted_to_apply(task)
        self.assertEqual(self._verify(before=None),
                         self._updates()[0][redfish_fw.VERIFY])

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_task_in_progress(self, continue_mock):
        self._set_updates(self._applying())
        self._reset_host()
        for state in (sushy.TASK_STATE_RUNNING, sushy.TASK_STATE_STARTING):
            self._set_task(state)
            self._poll()
            self._assert_waiting(redfish_fw.PHASE_APPLYING)

        self.mocks['task_monitor'].side_effect = (
            exception.RedfishConnectionError(node=self.node.uuid,
                                             error='timeout'))
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        continue_mock.assert_not_called()
        self.assertFalse(self._updates()[0][redfish_fw.VERIFY]['passed'])

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_task_done(self, continue_mock):
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._reset_host()

        task = self._poll()

        expected = [self._applying(new_boot=True, passed=True,
                                   lc='unavailable')]
        continue_mock.assert_called_once_with(
            self.firmware, task, self.mocks['update_service'].return_value,
            expected)
        self.assertEqual(expected, self._updates())

        # A task the BMC removed after the reboot does not hold the update
        continue_mock.reset_mock()
        self._set_updates(self._applying())
        self._task_gone()
        self._poll()
        continue_mock.assert_called_once()
        self.mocks['power'].assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_waiting_logged_when_it_changes(self, continue_mock,
                                                     log_mock):
        def waits(log):
            return [c[0][1]['reason'] for c in log.call_args_list
                    if 'not verified yet' in c[0][0]]

        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_RUNNING)
        self._reset_host(sushy.BootProgressStates.MEMORY)
        self._poll()
        running = 'the bios task is still Running'
        self.assertEqual([running], waits(log_mock.info))
        self.assertEqual(
            running, self._updates()[0][redfish_fw.VERIFY]['waiting'])

        # The same wait again is only logged for debugging
        self._poll()
        self.assertEqual([running], waits(log_mock.info))
        self.assertEqual([running], waits(log_mock.debug))

        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._poll()
        self.assertEqual(
            [running, 'BootProgress is MemoryInitializationStarted'],
            waits(log_mock.info))
        continue_mock.assert_not_called()

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_logs_each_check(self, continue_mock, log_mock):
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._reset_host()
        self._poll()
        continue_mock.assert_called_once()
        messages = [c[0][0] % c[0][1] for c in log_mock.info.call_args_list]
        self.assertEqual(
            ['Node %s reached BootProgress OSRunning after the reboot to '
             'apply firmware.' % self.node.uuid,
             'bios firmware update applied on node %s.' % self.node.uuid],
            messages[-2:])

    def test_applying_waiting_for_reset(self):
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        # The state of the boot before the reboot
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        self.assertEqual(
            'BootProgress is OSRunning, but the node has not been seen to '
            'reset since the reboot',
            self._updates()[0][redfish_fw.VERIFY]['waiting'])

    def test_applying_task_failed(self):
        self._set_updates(self._applying())
        self._reset_host()
        self._set_task(sushy.TASK_STATE_EXCEPTION, sushy.HEALTH_CRITICAL,
                       message='Flash failed')

        self._poll()

        self._assert_failed('Flash failed')

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_verified_skips_gates(self, continue_mock):
        self._set_updates(self._applying(passed=True))
        error = exception.RedfishConnectionError(node=self.node.uuid,
                                                 error='timeout')
        self.mocks['system'].side_effect = error
        self.mocks['task_monitor'].side_effect = error

        task = self._poll()

        continue_mock.assert_called_once_with(
            self.firmware, task, self.mocks['update_service'].return_value,
            self._updates())
        self.mocks['system'].assert_not_called()
        self.mocks['task_monitor'].assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_boot_state_trusted_after_delay(self, continue_mock):
        # The BMC keeps reporting OSRunning and the LastResetTime of the
        # boot before the reboot: nothing shows that the host reset.
        self.config(firmware_update_boot_check_delay=600, group='redfish')
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)

        self._advance(599)
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        continue_mock.assert_not_called()
        self.assertFalse(self._updates()[0][redfish_fw.VERIFY]['new_boot'])

        self._advance(1)
        self._poll()
        continue_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_reset_seen_allows_target_before_delay(
            self, continue_mock):
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)

        # No LastResetTime, but LastState went back to a POST state
        self._set_boot(sushy.BootProgressStates.MEMORY)
        self._advance(30)
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        self.assertTrue(self._updates()[0][redfish_fw.VERIFY]['new_boot'])

        self._set_boot(sushy.BootProgressStates.HARDWARE_COMPLETE)
        self._advance(30)
        self._poll()
        continue_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_no_boot_progress_held_for_delay(self, continue_mock):
        # A BMC that reports neither BootProgress nor LastResetTime
        self.config(firmware_update_boot_check_delay=600, group='redfish')
        self._set_boot(None)
        self._set_updates(self._applying(
            before={'state': None, 'reset_time': None}))
        self._set_task(sushy.TASK_STATE_COMPLETED)

        self._advance(599)
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        continue_mock.assert_not_called()

        self._advance(1)
        self._poll()
        continue_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_no_boot_progress_reset_seen_held_for_delay(
            self, continue_mock):
        # A reset only shows that POST started, not that it is over
        self.config(firmware_update_boot_check_delay=600, group='redfish')
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._reset_host(None)

        self._poll()
        verify = self._updates()[0][redfish_fw.VERIFY]
        self.assertTrue(verify['new_boot'])
        self.assertEqual('unavailable', verify['lc'])
        self._assert_waiting(redfish_fw.PHASE_APPLYING)

        self._advance(599)
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        continue_mock.assert_not_called()

        self._advance(1)
        self._poll()
        continue_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.drac_fw, 'check_lc_jobs', autospec=True)
    def test_applying_no_boot_progress_lc_done(self, lc_jobs_mock,
                                               continue_mock):
        self._set_updates(self._applying(task_monitor=self.DELL_TM,
                                         jids=['JID_839968767020']))
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._reset_host(None)
        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.DONE, None)

        self._poll()

        continue_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'check_boot_progress', autospec=True)
    def test_applying_boot_progress_unreadable(self, check_mock,
                                               continue_mock):
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._reset_host()
        check_mock.side_effect = sushy.exceptions.ConnectionError(
            url='http://bmc', error='boom')

        self._poll()

        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        continue_mock.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.drac_fw, 'check_lc_jobs', autospec=True)
    def test_applying_dell_lc_jobs(self, lc_jobs_mock, continue_mock):
        jids = ['JID_839968767020']
        self._set_updates(self._applying(task_monitor=self.DELL_TM,
                                         jids=jids))
        self._set_task(sushy.TASK_STATE_COMPLETED)
        # No BootProgress: the finished jobs confirm the update
        self._set_boot(None)

        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.RUNNING, jids[0])
        task = self._poll()
        lc_jobs_mock.assert_called_once_with(task, jids)
        self._assert_waiting(redfish_fw.PHASE_APPLYING)

        for error in (exception.RedfishError(error='boom'),
                      sushy.exceptions.ConnectionError(url='http://bmc',
                                                       error='boom')):
            lc_jobs_mock.side_effect = error
            self._poll()
            self._assert_waiting(redfish_fw.PHASE_APPLYING)
            self.assertIsNone(self._updates()[0][redfish_fw.VERIFY]['lc'])
        continue_mock.assert_not_called()

        lc_jobs_mock.side_effect = None
        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.DONE, None)
        self._poll()
        continue_mock.assert_called_once()
        verify = self._updates()[0][redfish_fw.VERIFY]
        self.assertEqual('done', verify['lc'])
        self.assertTrue(verify['passed'])

    @mock.patch.object(redfish_fw.drac_fw, 'check_lc_jobs', autospec=True)
    def test_applying_dell_lc_job_failed(self, lc_jobs_mock):
        self._set_updates(self._applying(task_monitor=self.DELL_TM,
                                         jids=['JID_839968767020']))
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._reset_host()
        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.ERROR,
                                     'JID_839968767020: flash failed')

        self._poll()

        self._assert_failed('did not apply the firmware: '
                            'JID_839968767020: flash failed')

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.drac_fw, 'check_lc_jobs', autospec=True)
    def test_applying_dell_lc_unavailable(self, lc_jobs_mock,
                                          continue_mock):
        self._set_updates(self._applying(task_monitor=self.DELL_TM,
                                         jids=['JID_839968767020']))
        self._set_task(sushy.TASK_STATE_COMPLETED)
        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.UNAVAILABLE,
                                     'no OEM job collection')

        # Nothing more to learn from the jobs; BootProgress still gates
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        self.assertEqual('unavailable',
                         self._updates()[0][redfish_fw.VERIFY]['lc'])

        self._reset_host()
        self._poll()
        lc_jobs_mock.assert_called_once()
        continue_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    @mock.patch.object(redfish_fw.drac_fw, 'check_lc_jobs', autospec=True)
    def test_applying_no_lc_job(self, lc_jobs_mock, continue_mock):
        # The task monitor names no LC job: nothing to check
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._reset_host()

        self._poll()

        lc_jobs_mock.assert_not_called()
        continue_mock.assert_called_once()
        self.assertEqual('unavailable',
                         self._updates()[0][redfish_fw.VERIFY]['lc'])

    def test_applying_verify_timeout(self):
        self.config(firmware_update_post_reboot_verify_timeout=1800,
                    group='redfish')
        self._set_updates(self._applying())
        self._reset_host()
        self._set_task(sushy.TASK_STATE_RUNNING)

        self._advance(1799)
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)

        self._advance(1)
        self._poll()
        self._assert_failed('could not be verified within 1800 seconds')
        self.assertIn('the bios task is still',
                      self.mocks['clean_error'].call_args[0][1])

    def test_applying_verify_timeout_bmc_unreachable(self):
        self._set_updates(self._applying())
        error = exception.RedfishConnectionError(node=self.node.uuid,
                                                 error='timeout')
        for name in ('update_service', 'system', 'task_monitor'):
            self.mocks[name].side_effect = error

        self._advance(1799)
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)

        self._advance(1)
        self._poll()
        self._assert_failed('the bios task could not be read')

    def test_applying_verify_timeout_disabled(self):
        self.config(firmware_update_post_reboot_verify_timeout=0,
                    firmware_update_overall_timeout=0, group='redfish')
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_RUNNING)

        self._advance(100000)
        self._poll()

        self._assert_waiting(redfish_fw.PHASE_APPLYING)

    def _set_servicing(self):
        self.node.provision_state = states.SERVICEWAIT
        self.node.clean_step = {}
        self.node.service_step = {'step': 'update', 'interface': 'firmware'}
        self.node.save()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_servicing_waits_for_os_running(self, continue_mock):
        self._set_servicing()
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)

        self._reset_host(sushy.BootProgressStates.OS_BOOT_STARTED)
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)

        self._reset_host(sushy.BootProgressStates.OS_RUNNING)
        self._poll()
        continue_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_servicing_proceeds_without_os_running(
            self, continue_mock):
        self.config(firmware_update_os_running_timeout=300,
                    firmware_update_boot_check_delay=600, group='redfish')
        self._set_servicing()
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)

        # Past POST, but maybe the previous boot: the wait does not start
        self._set_boot(sushy.BootProgressStates.HARDWARE_COMPLETE,
                       reset_time='T0')
        self._poll()
        self.assertIsNone(
            self._updates()[0][redfish_fw.VERIFY]['os_boot_seen'])

        # The host has reset, and its BMC never gets past POST complete
        self._advance(60)
        self._reset_host(sushy.BootProgressStates.HARDWARE_COMPLETE)
        self._poll()
        self.assertEqual(
            self.now.isoformat(),
            self._updates()[0][redfish_fw.VERIFY]['os_boot_seen'])

        self._advance(299)
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        continue_mock.assert_not_called()

        self._advance(1)
        self._poll()
        continue_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware, '_continue_updates',
                       autospec=True)
    def test_applying_without_update_service(self, continue_mock):
        self._set_updates(self._applying())
        self._set_task(sushy.TASK_STATE_COMPLETED)
        self._reset_host()
        self.mocks['update_service'].side_effect = (
            exception.RedfishConnectionError(node=self.node.uuid,
                                             error='timeout'))

        task = self._poll()

        continue_mock.assert_called_once_with(self.firmware, task, None,
                                              mock.ANY)

    def test_staging_without_update_service(self):
        self._set_updates(self._update())
        self.mocks['update_service'].side_effect = (
            exception.RedfishConnectionError(node=self.node.uuid,
                                             error='timeout'))

        self._poll()

        self.mocks['task_monitor'].assert_not_called()
        self._assert_waiting()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_next_update_waits_for_update_service(self, collection_mock,
                                                  validate_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        nic = {'component': 'nic:NIC.1', 'url': 'http://fw/nic.bin'}
        self._set_updates(self._applying(passed=True), nic)
        self.mocks['update_service'].side_effect = (
            exception.RedfishConnectionError(node=self.node.uuid,
                                             error='timeout'))

        self._poll()

        self.assertEqual([self._applying(passed=True), nic], self._updates())

        # The update service is back: the next update is submitted
        self.mocks['update_service'].side_effect = None
        update_service = self.mocks['update_service'].return_value
        update_service.simple_update.return_value.task_monitor_uri = (
            '/tasks/nic')

        self._poll()

        update_service.simple_update.assert_called_once_with(
            'http://fw/nic.bin')
        self.assertEqual([redfish_fw.PHASE_STAGING],
                         [u['phase'] for u in self._updates()])
        self.assertEqual('nic:NIC.1', self._updates()[0]['component'])
        self.mocks['power'].assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_get_current_bmc_version', autospec=True)
    def test_bmc_reboot_requested_applies_then_continues(
            self, version_mock, collection_mock, validate_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        update_service = self.mocks['update_service'].return_value
        update_service.simple_update.return_value.task_monitor_uri = (
            self.BIOS_TM)
        # The BMC version check timed out without telling whether the
        # update took effect, so the node must be rebooted
        version_mock.return_value = None
        bmc = self._update(
            'bmc', phase=redfish_fw.PHASE_BMC_CHECKING,
            task_monitor='/bmc/task',
            **{redfish_fw.BMC_VERSION_BEFORE: '1.0',
               redfish_fw.BMC_CHECK_START: (
                   self.now - datetime.timedelta(seconds=301)).isoformat()})
        bios = {'component': 'bios', 'url': 'http://fw/bios.bin'}
        self._set_updates(bmc, bios)

        task = self._poll()

        self._assert_rebooted_to_apply(task)
        self.assertEqual('bmc', self._updates()[0]['component'])
        self.assertEqual(self._verify(),
                         self._updates()[0][redfish_fw.VERIFY])
        update_service.simple_update.assert_not_called()
        # The BMC is validated before the reboot, not after it
        validate_mock.assert_called_once()

        # Once the reboot applied it, the next update is staged
        self.mocks['power'].reset_mock()
        self._task_gone()
        self._reset_host()
        self._poll()

        self.mocks['power'].assert_not_called()
        validate_mock.assert_called_once()
        update_service.simple_update.assert_called_once_with(
            'http://fw/bios.bin')
        self.assertEqual(1, len(self._updates()))
        self.assertEqual('bios', self._updates()[0]['component'])
        self.assertEqual(redfish_fw.PHASE_STAGING, self._updates()[0]['phase'])

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_stability_validated_only_after_bmc_update(
            self, collection_mock, resume_mock, validate_mock, cache_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        update_service = self.mocks['update_service'].return_value
        update_service.simple_update.return_value.task_monitor_uri = (
            '/tasks/next')
        for component, validated in (('bios', False), ('nic:NIC.1', False),
                                     ('bmc', True)):
            for more in (False, True):
                validate_mock.reset_mock()
                resume_mock.reset_mock()
                settings = [self._update(component, phase=None)]
                if more:
                    settings.append({'component': 'nic:NIC.2',
                                     'url': 'http://fw/next.bin'})
                self._set_updates(*settings)

                with task_manager.acquire(self.context, self.node.uuid,
                                          shared=False) as task:
                    self.firmware._continue_updates(
                        task, update_service, _updates(task.node))
                self.node.refresh()

                self.assertEqual(validated, validate_mock.called,
                                 (component, more))
                if more:
                    self.assertEqual(['nic:NIC.2'],
                                     [u['component'] for u in self._updates()])
                    resume_mock.assert_not_called()
                else:
                    resume_mock.assert_called_once()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    def test_stability_not_validated_after_verified_reboot(
            self, resume_mock, validate_mock, cache_mock):
        # A BMC update applied by a host reboot was validated before the
        # reboot; the verified reboot itself needs no further validation.
        self._task_gone()
        for component in ('bios', 'nic:NIC.1', 'bmc'):
            validate_mock.reset_mock()
            resume_mock.reset_mock()
            self._set_updates(self._applying(component))
            self._reset_host()

            self._poll()

            resume_mock.assert_called_once()
            validate_mock.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_stability_validation_failure_fails_step(self, collection_mock,
                                                     validate_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        validate_mock.side_effect = exception.RedfishError(
            error='BMC resources failed to stabilize')
        update_service = self.mocks['update_service'].return_value
        nxt = {'component': 'nic:NIC.1', 'url': 'http://fw/nic.bin'}
        bmc = functools.partial(self._update, 'bmc',
                                phase=redfish_fw.PHASE_BMC_CHECKING)
        cases = [
            # last component, next component, reboot needed
            ([bmc()], False),
            ([bmc(), nxt], False),
            ([bmc(), nxt], True),
        ]
        for settings, reboot in cases:
            self.mocks['clean_error'].reset_mock()
            self._set_updates(*settings)

            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=False) as task:
                settings = _updates(task.node)
                self.firmware._bmc_finished(
                    task, update_service, settings, settings[0], reboot)
            self.node.refresh()

            self._assert_failed('BMC resources failed to stabilize')
        update_service.simple_update.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    def test_bmc_version_changed_reboots_to_apply(self, manager_mock,
                                                  validate_mock):
        # More components follow: the node is rebooted, and the reboot
        # verified, before the next one is submitted.
        manager_mock.return_value.firmware_version = '2.0'
        bmc = self._update('bmc', phase=redfish_fw.PHASE_BMC_CHECKING,
                           task_monitor='/bmc/task',
                           **{redfish_fw.BMC_VERSION_BEFORE: '1.0'})
        bios = {'component': 'bios', 'url': 'http://fw/bios.bin'}
        self._set_updates(bmc, bios)

        task = self._poll()

        self._assert_rebooted_to_apply(task)
        fw_upd = self._updates()[0]
        self.assertEqual('bmc', fw_upd['component'])
        self.assertIn(redfish_fw.VERIFY, fw_upd)
        self.assertEqual(2, len(self._updates()))
        validate_mock.assert_called_once()

    def test_check_routes_by_phase(self):
        update_service = self.mocks['update_service'].return_value
        cases = [
            (redfish_fw.PHASE_STAGING, 'bios', '_poll_staging'),
            (redfish_fw.PHASE_STAGED, 'bios', '_poll_staging'),
            (redfish_fw.PHASE_APPLYING, 'nic:NIC.1', '_poll_applying'),
            (redfish_fw.PHASE_APPLYING, 'bmc', '_poll_applying'),
            (redfish_fw.PHASE_WAITING, 'bios', '_poll_waiting'),
            (redfish_fw.PHASE_BMC_UPDATING, 'bmc', '_poll_bmc_updating'),
            (redfish_fw.PHASE_BMC_CHECKING, 'bmc', '_poll_bmc_checking'),
        ]
        self.assertEqual({phase for phase, _c, _h in cases},
                         set(redfish_fw._PHASE_HANDLERS))
        for phase, component, handler in cases:
            fw_upd = self._update(component, phase=phase)
            self._set_updates(fw_upd)
            with mock.patch.object(redfish_fw.RedfishFirmware, handler,
                                   autospec=True) as handler_mock:
                task = self._poll()

            handler_mock.assert_called_once_with(
                self.firmware, task, update_service, [fw_upd], fw_upd)

    def test_check_legacy_non_bmc_update_fails(self):
        for component in ('bios', 'nic:NIC.1'):
            self.mocks['clean_error'].reset_mock()
            # Started by an older Ironic: no phase
            self._set_updates(self._update(component, phase=None,
                                           bios_reboot_triggered=True))

            self._poll()

            self._assert_failed('cannot be resumed')
            self.mocks['task_monitor'].assert_not_called()

    def test_grouped_submission_interrupted_fails(self):
        # The conductor stopped after the first update was staged, while
        # submitting the next: the next one's task is unknown.
        self._grouped(
            self._update('bios', phase=redfish_fw.PHASE_STAGED),
            self._update('nic:NIC.1', phase=None, task_monitor=None))

        self._poll()

        self._assert_failed('conductor stopped while submitting')
        self.mocks['task_monitor'].assert_not_called()

    def test_check_update_from_earlier_version_fails(self):
        # Recorded by an earlier version: even an update the node was
        # rebooted to apply cannot be followed, so the step fails, and the
        # old entries are removed.
        legacy = {
            'redfish_fw_updates': [self._update(
                'bios', phase=None, bios_reboot_triggered=True,
                verify=self._verify())],
            'redfish_fw_update_start_time': self.now.isoformat(),
            'firmware_reboot_requested': True,
        }
        self.node.driver_internal_info = legacy
        self.node.save()

        self._poll()

        self._assert_failed('cannot be resumed')
        for key in legacy:
            self.assertNotIn(key, self.node.driver_internal_info)

    @mock.patch.object(redfish_fw.RedfishFirmware, '_execute_firmware_update',
                       autospec=True)
    def test_update_records_step(self, execute_mock):
        settings = [{'component': 'bios', 'url': 'http://bios/v2'}]
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.firmware.update(task, settings,
                                 allow_grouping_reboots=True)
        self.node.refresh()
        self.assertEqual(
            {'version': redfish_fw.RECORD_VERSION,
             'started_at': self.now.isoformat(), 'grouping': True,
             'updates': settings},
            self.node.driver_internal_info[redfish_fw.FW_UPDATE])

    @mock.patch.object(redfish_fw.RedfishFirmware, '_execute_firmware_update',
                       autospec=True)
    def test_update_submit_failure_leaves_no_record(self, execute_mock):
        execute_mock.side_effect = exception.RedfishError(error='boom')
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.assertRaises(
                exception.RedfishError, self.firmware.update, task,
                [{'component': 'bios', 'url': 'http://bios/v2'}])
        self.node.refresh()
        self.assertNotIn(redfish_fw.FW_UPDATE, self.node.driver_internal_info)

    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test_set_phase_logs_changes(self, log_mock):
        fw_upd = {'component': 'bios'}
        self.firmware._set_phase(self.node, fw_upd, redfish_fw.PHASE_STAGING)
        self.firmware._set_phase(self.node, fw_upd, redfish_fw.PHASE_STAGING)
        self.firmware._set_phase(self.node, fw_upd, redfish_fw.PHASE_STAGED)

        self.assertEqual(redfish_fw.PHASE_STAGED, fw_upd['phase'])
        # Logged on entering the first phase and on changing it, not when
        # the phase stays the same.
        self.assertEqual(
            [{'component': 'bios', 'node': self.node.uuid,
              'phase': 'staging'},
             {'component': 'bios', 'node': self.node.uuid,
              'previous': 'staging', 'phase': 'staged'}],
            [c[0][1] for c in log_mock.info.call_args_list])

    def test_has_update(self):
        for info, expected in (
                ({}, False),
                ({redfish_fw.FW_UPDATE: _record([self._update()])}, True),
                # Left by an earlier version, to be failed
                ({'redfish_fw_updates': [self._update()]}, True)):
            self.node.driver_internal_info = info
            self.assertEqual(expected, redfish_fw._has_update(self.node))

    @mock.patch.object(redfish_fw.RedfishFirmware, '_execute_firmware_update',
                       autospec=True)
    def test_update_clears_stale_fields(self, execute_mock):
        self.node.set_driver_internal_info('firmware_reboot_requested', True)
        self.node.set_driver_internal_info('bmc_fw_version_before_update',
                                           '1.0')
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid) as task:
            self.firmware.update(
                task, [{'component': 'bios', 'url': 'http://bios/v2'}])
            info = task.node.driver_internal_info
            self.assertNotIn('firmware_reboot_requested', info)
            self.assertNotIn('bmc_fw_version_before_update', info)

    def _fake_tasks(self):
        """Serve sushy tasks by task monitor URI from a dict of states.

        A URI missing from the dict, or mapped to None, is a task the BMC
        no longer has.
        """
        tasks = {}

        def get_task_monitor(node, uri):
            if tasks.get(uri) is None:
                raise exception.RedfishError(error='%s not found' % uri)
            sushy_task = mock.Mock(task_state=tasks[uri],
                                   task_status=sushy.HEALTH_OK, messages=[])
            return mock.Mock(is_processing=False,
                             get_task=mock.Mock(return_value=sushy_task))

        self.mocks['task_monitor'].side_effect = get_task_monitor
        return tasks

    def _reset_on_reboot(self, state=sushy.BootProgressStates.OS_RUNNING):
        """Each reboot of the node resets its host to the given state."""
        self.mocks['power'].side_effect = (
            lambda *args, **kwargs: self._reset_host(state))

    def _submits(self, *names):
        """SimpleUpdate returns /tasks/<name> for each of names in turn."""
        update_service = self.mocks['update_service'].return_value
        update_service.simple_update.side_effect = [
            mock.Mock(task_monitor_uri='/tasks/%s' % name) for name in names]
        return update_service

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_update_bios_nic_bmc_end_to_end(
            self, collection_mock, manager_mock, resume_mock,
            validate_mock, cache_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        manager_mock.return_value.firmware_version = '1.0'
        update_service = self._submits('bios', 'nic', 'bmc')
        tasks = self._fake_tasks()
        self._reset_on_reboot()

        settings = [{'component': 'bios', 'url': 'http://fw/bios.bin'},
                    {'component': 'nic:NIC.1', 'url': 'http://fw/nic.bin'},
                    {'component': 'bmc', 'url': 'http://fw/bmc.bin'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertEqual(states.CLEANWAIT,
                             self.firmware.update(task, settings))
        self.node.refresh()

        # BIOS: staged, rebooted once, verified
        tasks['/tasks/bios'] = sushy.TASK_STATE_RUNNING
        self._poll()
        tasks['/tasks/bios'] = sushy.TASK_STATE_COMPLETED
        self._poll()
        self.assertEqual(1, self.mocks['power'].call_count)
        self._poll()
        self.assertEqual('nic:NIC.1', self._updates()[0]['component'])

        # NIC: waits in Starting until staged, rebooted once, verified
        tasks['/tasks/nic'] = sushy.TASK_STATE_STARTING
        self._poll()
        self._advance(30)
        self._poll()
        self.assertEqual(2, self.mocks['power'].call_count)
        tasks['/tasks/nic'] = sushy.TASK_STATE_RUNNING
        self._poll()
        self.assertEqual('nic:NIC.1', self._updates()[0]['component'])
        tasks['/tasks/nic'] = None
        self._poll()
        self.assertEqual('bmc', self._updates()[0]['component'])

        # BMC: applied by the BMC itself, confirmed by its version
        tasks['/tasks/bmc'] = sushy.TASK_STATE_COMPLETED
        self._advance(CONF.redfish.firmware_update_reboot_delay)
        manager_mock.return_value.firmware_version = '2.0'
        self._poll()

        self.assertEqual([mock.call(mock.ANY, states.REBOOT, 300)] * 2,
                         self.mocks['power'].call_args_list)
        self.assertEqual(3, update_service.simple_update.call_count)
        # Only the BMC update can leave the BMC flapping
        validate_mock.assert_called_once()
        resume_mock.assert_called_once()
        cache_mock.assert_called_once()
        self.mocks['clean_error'].assert_not_called()
        self.assertNotIn(redfish_fw.FW_UPDATE, self.node.driver_internal_info)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(redfish_fw.drac_fw, 'check_lc_jobs', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_update_bios_dell_end_to_end(
            self, collection_mock, lc_jobs_mock, resume_mock,
            validate_mock, cache_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        jid = 'JID_839968767020'
        self._submits(jid)
        tasks = self._fake_tasks()
        # The host resets into POST, where the LC job flashes the BIOS
        self._reset_on_reboot(sushy.BootProgressStates.MEMORY)

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.firmware.update(
                task, [{'component': 'bios', 'url': 'http://fw/bios.bin'}])
        self.node.refresh()

        # Staged: the job waits for the reboot to run it
        tasks['/tasks/' + jid] = sushy.TASK_STATE_COMPLETED
        self._poll()
        self.mocks['power'].assert_called_once()
        self.assertEqual([jid],
                         self._updates()[0][redfish_fw.VERIFY]['jids'])

        # The job runs during POST
        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.RUNNING, jid)
        self._advance(60)
        self._poll()
        self._assert_waiting_after_reboot()
        lc_jobs_mock.assert_called_once_with(mock.ANY, [jid])

        # The job is done, but POST is not over
        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.DONE, None)
        self._advance(60)
        self._poll()
        self._assert_waiting_after_reboot()
        self.assertEqual('done', self._updates()[0][redfish_fw.VERIFY]['lc'])

        # POST complete: the step resumes
        self._set_boot(sushy.BootProgressStates.HARDWARE_COMPLETE,
                       reset_time='T1')
        self._advance(60)
        self._poll()

        self.assertEqual(2, lc_jobs_mock.call_count)
        self.mocks['power'].assert_called_once()
        validate_mock.assert_not_called()
        resume_mock.assert_called_once()
        self.mocks['clean_error'].assert_not_called()
        self.assertNotIn(redfish_fw.FW_UPDATE, self.node.driver_internal_info)

    def _assert_waiting_after_reboot(self):
        self.mocks['power'].assert_called_once()
        self.mocks['clean_error'].assert_not_called()
        self.assertEqual(redfish_fw.PHASE_APPLYING,
                         self._updates()[0]['phase'])

    # Grouping updates to share reboots

    @staticmethod
    def _tm(name):
        return '/tasks/%s' % name

    def _grouped(self, *updates):
        self._set_updates(*updates, grouping=True)

    @mock.patch.object(redfish_fw.RedfishFirmware, '_execute_firmware_update',
                       autospec=True)
    def test_update_grouping_validation(self, execute_mock):
        bios = {'component': 'bios', 'url': 'http://fw/bios.bin'}
        nic = {'component': 'nic:NIC.1', 'url': 'http://fw/nic.bin'}
        rejected = [
            ([bios], 'maybe'),
            ([bios], 2),
            ([bios], None),
            ([bios, dict(bios)], True),
            ([dict(nic, wait=60), bios], True),
        ]
        for settings, grouping in rejected:
            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=False) as task:
                self.assertRaises(exception.InvalidParameterValue,
                                  self.firmware.update, task, settings,
                                  allow_grouping_reboots=grouping)
        execute_mock.assert_not_called()

        accepted = [
            ([dict(nic, wait=60), bios], False),
            ([bios, nic, {'component': 'bmc', 'url': 'http://fw/bmc.bin',
                          'wait': 60}], True),
            ([bios, dict(bios)], False),
            ([bios, dict(bios)], 'false'),
            ([bios, nic], 'true'),
        ]
        for settings, grouping in accepted:
            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=False) as task:
                self.assertEqual(
                    states.CLEANWAIT,
                    self.firmware.update(task, settings,
                                         allow_grouping_reboots=grouping))
        self.assertEqual(len(accepted), execute_mock.call_count)

    @mock.patch.object(redfish_fw.RedfishFirmware, '_execute_firmware_update',
                       autospec=True)
    def test_update_grouping_flag(self, execute_mock):
        settings = [{'component': 'bios', 'url': 'http://fw/bios.bin'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.firmware.update(task, settings,
                                 allow_grouping_reboots='true')
        self.node.refresh()
        self.assertTrue(self.node.driver_internal_info[
            redfish_fw.FW_UPDATE]['grouping'])

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.firmware.update(task, settings)
        self.node.refresh()
        self.assertFalse(self.node.driver_internal_info[
            redfish_fw.FW_UPDATE]['grouping'])

    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_grouping_stages_updates_for_one_reboot(self, collection_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        update_service = self._submits('JID_2')
        tasks = self._fake_tasks()
        bmc = {'component': 'bmc', 'url': 'http://fw/bmc.bin'}
        self._grouped(self._update(task_monitor=self._tm('JID_1')),
                      {'component': 'nic:NIC.1', 'url': 'http://fw/nic.bin'},
                      bmc)

        # BIOS staged: the NIC update is submitted to share its reboot
        tasks[self._tm('JID_1')] = sushy.TASK_STATE_COMPLETED
        self._poll()
        update_service.simple_update.assert_called_once_with(
            'http://fw/nic.bin')
        self.assertEqual([redfish_fw.PHASE_STAGED, redfish_fw.PHASE_STAGING,
                          None],
                         [u.get('phase') for u in self._updates()])
        self.mocks['power'].assert_not_called()

        # The NIC is polled next, while the BIOS update stays staged
        tasks[self._tm('JID_2')] = sushy.TASK_STATE_RUNNING
        self._poll()
        self.assertEqual(redfish_fw.PHASE_STAGING,
                         self._updates()[1]['phase'])
        self.mocks['power'].assert_not_called()

        # NIC staged: the BMC is next, so one reboot applies both
        tasks[self._tm('JID_2')] = sushy.TASK_STATE_COMPLETED
        task = self._poll()

        self.mocks['power'].assert_called_once_with(task, states.REBOOT, 300)
        updates = self._updates()
        self.assertEqual([redfish_fw.PHASE_APPLYING,
                          redfish_fw.PHASE_APPLYING, None],
                         [u.get('phase') for u in updates])
        self.assertEqual(self._verify(jids=['JID_1', 'JID_2']),
                         updates[0][redfish_fw.VERIFY])
        self.assertNotIn(redfish_fw.VERIFY, updates[1])
        self.assertEqual(1, update_service.simple_update.call_count)

    def test_grouping_last_update_reboots_group(self):
        # The last update closes the group; a task that disappeared while
        # staging counts as staged.
        self._grouped(self._update(phase=redfish_fw.PHASE_STAGED),
                      self._update('nic:NIC.1', task_monitor='/tasks/nic'))
        self._task_gone()

        self._poll()

        self.mocks['power'].assert_called_once()
        self.assertEqual([redfish_fw.PHASE_APPLYING] * 2,
                         [u['phase'] for u in self._updates()])
        update_service = self.mocks['update_service'].return_value
        update_service.simple_update.assert_not_called()

    def test_without_grouping_one_reboot_per_update(self):
        self._set_updates(self._update(),
                          {'component': 'nic:NIC.1',
                           'url': 'http://fw/nic.bin'})
        self._set_task(sushy.TASK_STATE_COMPLETED)

        task = self._poll()

        self._assert_rebooted_to_apply(task)
        self.assertNotIn('phase', self._updates()[1])
        update_service = self.mocks['update_service'].return_value
        update_service.simple_update.assert_not_called()

    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_grouping_submit_failure_names_staged(self, collection_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        update_service = self.mocks['update_service'].return_value
        errors = [
            sushy.exceptions.HTTPError(
                method='POST', url='http://bmc/update',
                response=mock.Mock(status_code=400, json=mock.Mock(
                    return_value={}))),
            sushy.exceptions.ConnectionError(url='http://bmc/update',
                                             error='timeout'),
        ]
        for error in errors:
            update_service.simple_update.reset_mock()
            update_service.simple_update.side_effect = error
            self.mocks['clean_error'].reset_mock()
            self._grouped(
                self._update(phase=redfish_fw.PHASE_STAGED),
                self._update('nic:NIC.1', task_monitor='/tasks/nic'),
                {'component': 'nic:NIC.2', 'url': 'http://fw/nic2.bin'})
            self._set_task(sushy.TASK_STATE_COMPLETED)

            self._poll()

            update_service.simple_update.assert_called_once_with(
                'http://fw/nic2.bin')
            # Not even a rejected request lifts the hold: firmware is
            # staged
            self._assert_failed('Failed to submit nic:NIC.2 firmware')
            self.assertIn('Firmware for bios, nic:NIC.1 is already staged',
                          self.mocks['clean_error'].call_args[0][1])

    def test_grouping_staging_failure_names_staged(self):
        self._grouped(self._update(phase=redfish_fw.PHASE_STAGED),
                      self._update('nic:NIC.1', task_monitor='/tasks/nic'))
        self._set_task(sushy.TASK_STATE_EXCEPTION, sushy.HEALTH_CRITICAL,
                       message='Image download failed')

        self._poll()

        self._assert_failed('Image download failed')
        self.assertIn('Firmware for bios is already staged',
                      self.mocks['clean_error'].call_args[0][1])

    def _applying_group(self, *names, **verify):
        """Updates of a group just rebooted to apply them."""
        updates = [self._update(name.split('@')[0], task_monitor=self._tm(
            name.split('@')[-1]), phase=redfish_fw.PHASE_APPLYING)
            for name in names]
        updates[0][redfish_fw.VERIFY] = self._verify(**verify)
        return updates

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_grouped_applying_waits_for_every_task(
            self, collection_mock, manager_mock, validate_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        manager_mock.return_value.firmware_version = '1.0'
        update_service = self._submits('bmc')
        tasks = self._fake_tasks()
        self._grouped(*self._applying_group('bios', 'nic:NIC.1@nic'),
                      {'component': 'bmc', 'url': 'http://fw/bmc.bin'})
        self._reset_host()

        tasks['/tasks/bios'] = sushy.TASK_STATE_COMPLETED
        tasks['/tasks/nic'] = sushy.TASK_STATE_RUNNING
        self._poll()
        self._assert_waiting(redfish_fw.PHASE_APPLYING)
        update_service.simple_update.assert_not_called()

        # Every task done: the whole group is done, the BMC is next
        tasks['/tasks/nic'] = None
        self._poll()

        update_service.simple_update.assert_called_once_with(
            'http://fw/bmc.bin')
        self.assertEqual(['bmc'], [u['component'] for u in self._updates()])
        validate_mock.assert_not_called()
        self.mocks['power'].assert_not_called()

    def test_grouped_applying_task_failed(self):
        self._grouped(*self._applying_group('bios', 'nic:NIC.1@nic'))
        self._reset_host()
        sushy_tasks = {
            '/tasks/bios': mock.Mock(task_state=sushy.TASK_STATE_COMPLETED,
                                     task_status=sushy.HEALTH_OK,
                                     messages=[]),
            '/tasks/nic': mock.Mock(task_state=sushy.TASK_STATE_EXCEPTION,
                                    task_status=sushy.HEALTH_CRITICAL,
                                    messages=[mock.Mock(
                                        message='NIC flash failed')]),
        }
        self.mocks['task_monitor'].side_effect = lambda node, uri: mock.Mock(
            get_task=mock.Mock(return_value=sushy_tasks[uri]))

        self._poll()

        self._assert_failed('NIC flash failed')

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(redfish_fw.drac_fw, 'check_lc_jobs', autospec=True)
    def test_grouped_applying_dell_lc_jobs(self, lc_jobs_mock, resume_mock,
                                           cache_mock):
        self._task_gone()
        self._grouped(*self._applying_group(
            'bios@JID_1', 'nic:NIC.1@JID_2', jids=['JID_1', 'JID_2']))
        self._reset_host()

        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.RUNNING, 'JID_2')
        task = self._poll()
        lc_jobs_mock.assert_called_once_with(task, ['JID_1', 'JID_2'])
        self._assert_waiting(redfish_fw.PHASE_APPLYING)

        lc_jobs_mock.return_value = (drac_fw.LCJobStatus.ERROR,
                                     'JID_2: failed')
        self._poll()
        self._assert_failed('JID_2: failed')
        self.assertIn('bios, nic:NIC.1',
                      self.mocks['clean_error'].call_args[0][1])
        resume_mock.assert_not_called()

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(redfish_fw, 'LOG', autospec=True)
    def test_grouped_last_group_resumes(self, log_mock, resume_mock,
                                        validate_mock, cache_mock):
        tasks = self._fake_tasks()
        self._grouped(*self._applying_group('bios', 'nic:NIC.1@nic'))
        self._reset_host()
        tasks['/tasks/bios'] = sushy.TASK_STATE_COMPLETED

        self._poll()

        messages = [c[0][0] % c[0][1] for c in log_mock.info.call_args_list]
        self.assertIn('The nic:NIC.1 firmware update task is gone from node '
                      '%s after the reboot to apply it.' % self.node.uuid,
                      messages)
        self.assertEqual(
            ['bios, nic:NIC.1 firmware update applied on node %s.'
             % self.node.uuid],
            [m for m in messages if 'applied on node' in m])
        resume_mock.assert_called_once()
        validate_mock.assert_not_called()
        self.assertNotIn(redfish_fw.FW_UPDATE, self.node.driver_internal_info)

    @mock.patch.object(redfish_fw.RedfishFirmware,
                       'cache_firmware_components', autospec=True)
    @mock.patch.object(redfish_fw.RedfishFirmware,
                       '_validate_resources_stability', autospec=True)
    @mock.patch.object(manager_utils, 'notify_conductor_resume_clean',
                       autospec=True)
    @mock.patch.object(redfish_utils, 'get_manager', autospec=True)
    @mock.patch.object(redfish_utils, 'get_system_collection', autospec=True)
    def test_update_grouped_end_to_end(
            self, collection_mock, manager_mock, resume_mock,
            validate_mock, cache_mock):
        collection_mock.return_value.members_identities = ['/Systems/1']
        manager_mock.return_value.firmware_version = '1.0'
        names = ('bios', 'nic1', 'bmc', 'nic2', 'nic3')
        update_service = self._submits(*names)
        tasks = self._fake_tasks()
        self._reset_on_reboot()

        settings = [{'component': 'bios', 'url': 'http://fw/bios.bin'},
                    {'component': 'nic:NIC.1', 'url': 'http://fw/nic1.bin'},
                    {'component': 'bmc', 'url': 'http://fw/bmc.bin'},
                    {'component': 'nic:NIC.2', 'url': 'http://fw/nic2.bin'},
                    {'component': 'nic:NIC.3', 'url': 'http://fw/nic3.bin'}]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.firmware.update(task, settings, allow_grouping_reboots=True)
        self.node.refresh()

        def stage_and_poll(name):
            tasks['/tasks/' + name] = sushy.TASK_STATE_COMPLETED
            self._poll()

        # BIOS and NIC.1 staged, then applied by one reboot
        stage_and_poll('bios')
        stage_and_poll('nic1')
        self.assertEqual(1, self.mocks['power'].call_count)
        self._poll()
        self.assertEqual('bmc', self._updates()[0]['component'])

        # The BMC applies its own update without a reboot; once it is
        # stable, NIC.2 is submitted
        tasks['/tasks/bmc'] = sushy.TASK_STATE_COMPLETED
        self._advance(CONF.redfish.firmware_update_reboot_delay)
        manager_mock.return_value.firmware_version = '2.0'
        self._poll()
        self.assertEqual(1, self.mocks['power'].call_count)
        self.assertEqual('nic:NIC.2', self._updates()[0]['component'])
        validate_mock.assert_called_once()

        # NIC.2 and NIC.3 staged, then applied by one reboot
        stage_and_poll('nic2')
        stage_and_poll('nic3')
        self.assertEqual(2, self.mocks['power'].call_count)
        self._poll()

        self.assertEqual([mock.call(mock.ANY, states.REBOOT, 300)] * 2,
                         self.mocks['power'].call_args_list)
        self.assertEqual(5, update_service.simple_update.call_count)
        validate_mock.assert_called_once()
        resume_mock.assert_called_once()
        self.mocks['clean_error'].assert_not_called()
        self.assertNotIn(redfish_fw.FW_UPDATE, self.node.driver_internal_info)
