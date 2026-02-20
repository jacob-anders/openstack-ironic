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

"""Tests for DRAC BIOS interface"""

from unittest import mock

from ironic.common import exception
from ironic.common import states
from ironic.conductor import task_manager
from ironic.drivers.modules.drac import bios as drac_bios
from ironic.drivers.modules.drac import utils as drac_utils
from ironic.tests.unit.drivers.modules.drac import utils as test_utils
from ironic.tests.unit.objects import utils as obj_utils


INFO_DICT = test_utils.INFO_DICT


class DracRedfishBIOSTestCase(test_utils.BaseDracTest):

    def setUp(self):
        super(DracRedfishBIOSTestCase, self).setUp()
        self.node = obj_utils.create_test_node(
            self.context, driver='idrac', driver_info=INFO_DICT)

    @mock.patch.object(drac_utils, 'execute_oem_manager_method',
                       autospec=True)
    def test_pre_configuration_no_pending_jobs(self, mock_oem):
        mock_oem.return_value = []
        data = [{'name': 'ProcTurboMode', 'value': 'Disabled'}]
        self.node.provision_state = states.SERVICING
        self.node.save()
        bios_iface = drac_bios.DracRedfishBIOS()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            result = bios_iface.pre_configuration(task, data)
            self.assertIsNone(result)
            mock_oem.assert_called_once()

    @mock.patch.object(drac_utils, 'execute_oem_manager_method',
                       autospec=True)
    def test_pre_configuration_pending_jobs_raises(self, mock_oem):
        mock_job = mock.Mock()
        mock_job.identity = 'JID_123456789'
        mock_oem.return_value = [mock_job]
        data = [{'name': 'ProcTurboMode', 'value': 'Disabled'}]
        self.node.provision_state = states.SERVICING
        self.node.save()
        bios_iface = drac_bios.DracRedfishBIOS()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError,
                'unfinished iDRAC configuration jobs.*JID_123456789',
                bios_iface.pre_configuration, task, data)

    @mock.patch.object(drac_utils, 'execute_oem_manager_method',
                       autospec=True)
    def test_pre_configuration_oem_failure_continues(self, mock_oem):
        mock_oem.side_effect = exception.RedfishError(
            error='OEM extension not found')
        data = [{'name': 'ProcTurboMode', 'value': 'Disabled'}]
        self.node.provision_state = states.SERVICING
        self.node.save()
        bios_iface = drac_bios.DracRedfishBIOS()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            result = bios_iface.pre_configuration(task, data)
            self.assertIsNone(result)

    @mock.patch.object(drac_utils, 'execute_oem_manager_method',
                       autospec=True)
    def test_pre_configuration_multiple_pending_jobs(self, mock_oem):
        job1 = mock.Mock()
        job1.identity = 'JID_111'
        job2 = mock.Mock()
        job2.identity = 'JID_222'
        mock_oem.return_value = [job1, job2]
        data = [{'name': 'ProcTurboMode', 'value': 'Disabled'}]
        self.node.provision_state = states.SERVICING
        self.node.save()
        bios_iface = drac_bios.DracRedfishBIOS()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaisesRegex(
                exception.RedfishError,
                'JID_111.*JID_222',
                bios_iface.pre_configuration, task, data)
