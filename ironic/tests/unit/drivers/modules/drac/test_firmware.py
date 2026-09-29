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

from types import SimpleNamespace
from unittest import mock

import sushy

from ironic.common import exception
from ironic.drivers.modules.drac import firmware
from ironic.drivers.modules.redfish import utils
from ironic.tests import base


def job(identity, state, job_type='FirmwareUpdate'):
    return {'id': identity, 'state': state, 'type': job_type,
            'message': 'LC message'}


class DellFirmwareTestCase(base.TestCase):

    def setUp(self):
        super().setUp()
        self.task = SimpleNamespace(node=SimpleNamespace(uuid='node'))

    @mock.patch.object(utils, 'get_system', autospec=True)
    def test_old_sushy_reads_all_states(self, get_system):
        connector = mock.Mock(spec=['get'])
        connector.get.return_value.json.return_value = {'Members': [
            {'Id': 'JID_1', 'JobState': 'CompletedWithErrors',
             'JobType': 'FirmwareUpdate', 'MessageId': 'RED001'}]}
        collection = SimpleNamespace(path='/Jobs', _conn=connector)
        manager = mock.Mock(spec=['get_oem_extension'])
        manager.get_oem_extension.return_value.job_collection = collection
        get_system.return_value.managers = [manager]
        self.assertEqual([{'id': 'JID_1', 'state': 'CompletedWithErrors',
                           'type': 'FirmwareUpdate', 'message': 'RED001'}],
                         firmware.get_jobs(self.task))
        connector.get.assert_called_once_with('/Jobs?$expand=.($levels=1)')

    def test_new_sushy_job_objects(self):
        collection = mock.Mock(spec=['get_jobs'])
        collection.get_jobs.return_value = [SimpleNamespace(
            identity='JID_1', job_state='Completed', job_type='FirmwareUpdate',
            message=None, message_id='RED002')]
        self.assertEqual([{'id': 'JID_1', 'state': 'Completed',
                           'type': 'FirmwareUpdate', 'message': 'RED002'}],
                         firmware._read_jobs(collection))

    @mock.patch.object(firmware, '_get_dell_job_collection', autospec=True)
    def test_noncallable_get_jobs_keeps_full_failure_detection(
            self, get_collection):
        connector = mock.Mock(spec=['get'])
        connector.get.return_value.json.return_value = {'Members': [
            {'Id': 'JID_1', 'JobState': 'Failed', 'JobType': 'FirmwareUpdate',
             'Message': 'Flash failed'}]}
        get_collection.return_value = SimpleNamespace(
            path='/Jobs', _conn=connector, get_jobs=None)
        status, detail = firmware.check_lc_jobs(self.task, ['JID_1'])
        self.assertEqual(firmware.LC_JOBS_ERROR, status)
        self.assertIn('Flash failed', detail)
        connector.get.assert_called_once_with('/Jobs?$expand=.($levels=1)')

    @mock.patch.object(firmware, '_get_dell_job_collection', autospec=True)
    def test_native_get_jobs_failure_does_not_fall_back(self, get_collection):
        collection = mock.Mock(spec=['get_jobs', '_conn'])
        collection.get_jobs.side_effect = exception.RedfishConnectionError(
            node='node', error='temporarily unavailable')
        get_collection.return_value = collection
        status, detail = firmware.check_lc_jobs(self.task, ['JID_1'])
        self.assertEqual(firmware.LC_JOBS_RUNNING, status)
        self.assertIn('temporarily unavailable', detail)
        collection._conn.get.assert_not_called()

    @mock.patch.object(utils, 'get_system', autospec=True)
    def test_no_extension_is_unsupported(self, get_system):
        manager = mock.Mock(spec=['get_oem_extension'])
        manager.get_oem_extension.side_effect = (
            sushy.exceptions.OEMExtensionNotFoundError())
        get_system.return_value.managers = [manager]
        self.assertIsNone(firmware.get_jobs(self.task))

    @mock.patch.object(utils, 'get_system', autospec=True)
    def test_transport_error_is_not_unsupported(self, get_system):
        get_system.side_effect = exception.RedfishConnectionError(
            node='node', error='down')
        status, detail = firmware.check_lc_jobs(self.task, ['JID_1'])
        self.assertEqual(firmware.LC_JOBS_RUNNING, status)
        self.assertIn('down', detail)

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_multiple_jobs_and_late_job_failure(self, get_jobs):
        historical = job('old', 'Failed')
        get_jobs.return_value = [historical]
        tracking = firmware.snapshot_lc_jobs(self.task)
        get_jobs.return_value = [historical, job('JID_1', 'Scheduled')]
        self.assertEqual((firmware.LC_JOBS_RUNNING, 'JID_1: Scheduled'),
                         firmware.check_lc_jobs(
                             self.task, ['JID_1'], tracking))
        get_jobs.return_value = [historical, job('JID_1', 'Completed'),
                                 job('JID_2', 'Running')]
        self.assertEqual((firmware.LC_JOBS_RUNNING, 'JID_2: Running'),
                         firmware.check_lc_jobs(
                             self.task, ['JID_1'], tracking))
        get_jobs.return_value = [historical, job('JID_1', 'Completed'),
                                 job('JID_2', 'CompletedWithErrors')]
        status, detail = firmware.check_lc_jobs(self.task, ['JID_1'], tracking)
        self.assertEqual(firmware.LC_JOBS_ERROR, status)
        self.assertIn('JID_2: CompletedWithErrors - LC message', detail)
        self.assertNotIn('old', detail)

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_unknown_and_reboot_pending_states(self, get_jobs):
        for state in ('Downloading', 'RebootPending', 'UserIntervention',
                      'Paused', 'NewFutureState'):
            with self.subTest(state=state):
                get_jobs.return_value = [job('JID_1', state)]
                self.assertEqual(firmware.LC_JOBS_RUNNING,
                                 firmware.check_lc_jobs(
                                     self.task, ['JID_1'])[0])

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_missing_job_cannot_complete(self, get_jobs):
        get_jobs.return_value = [job('JID_1', 'Running')]
        tracking = firmware.snapshot_lc_jobs(self.task)
        firmware.check_lc_jobs(self.task, ['JID_1'], tracking)
        get_jobs.return_value = []
        status, detail = firmware.check_lc_jobs(self.task, ['JID_1'], tracking)
        self.assertEqual(firmware.LC_JOBS_RUNNING, status)
        self.assertIn('missing', detail)

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_success_survives_purging(self, get_jobs):
        tracking = {'baseline': [], 'jobs': {}}
        get_jobs.return_value = [job('JID_1', 'Completed')]
        self.assertEqual(firmware.LC_JOBS_DONE, firmware.check_lc_jobs(
            self.task, ['JID_1'], tracking)[0])
        get_jobs.return_value = []
        self.assertEqual(firmware.LC_JOBS_DONE, firmware.check_lc_jobs(
            self.task, ['JID_1'], tracking)[0])

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_capability_cannot_disappear(self, get_jobs):
        get_jobs.return_value = []
        tracking = firmware.snapshot_lc_jobs(self.task)
        get_jobs.return_value = None
        self.assertEqual(firmware.LC_JOBS_RUNNING, firmware.check_lc_jobs(
            self.task, ['JID_1'], tracking)[0])

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_unrelated_active_firmware_blocks_completion(self, get_jobs):
        get_jobs.return_value = [job('other', 'Running')]
        tracking = firmware.snapshot_lc_jobs(self.task)
        get_jobs.return_value.append(job('JID_1', 'Completed'))
        self.assertEqual((firmware.LC_JOBS_RUNNING, 'other: Running'),
                         firmware.check_lc_jobs(
                             self.task, ['JID_1'], tracking))

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_staging_requires_explicit_armed_or_completed_job(self, get_jobs):
        for state in ('Downloading', 'Running', 'Scheduled', 'Completed',
                      'Failed'):
            with self.subTest(state=state):
                get_jobs.return_value = [job('JID_1', state)]
                expected = {
                    'Scheduled': firmware.LC_JOBS_STAGED,
                    'Completed': firmware.LC_JOBS_DONE,
                    'Failed': firmware.LC_JOBS_ERROR,
                }.get(state, firmware.LC_JOBS_RUNNING)
                self.assertEqual(expected, firmware.check_staged_job(
                    self.task, 'JID_1')[0])

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_empty_jids_do_not_skip_supported_required_check(self, get_jobs):
        get_jobs.return_value = []
        tracking = firmware.snapshot_lc_jobs(self.task)
        self.assertEqual(firmware.LC_JOBS_RUNNING, firmware.check_lc_jobs(
            self.task, [], tracking, required=True)[0])

    @mock.patch.object(firmware, 'get_jobs', autospec=True)
    def test_newly_available_capability_does_not_import_historical_errors(
            self, get_jobs):
        get_jobs.return_value = None
        tracking = firmware.snapshot_lc_jobs(self.task)
        get_jobs.return_value = [job('old', 'Failed'),
                                 job('JID_1', 'Scheduled')]
        status, detail = firmware.check_lc_jobs(self.task, ['JID_1'], tracking)
        self.assertEqual(firmware.LC_JOBS_RUNNING, status)
        self.assertNotIn('old', detail)

    @mock.patch.object(firmware, '_get_dell_job_collection', autospec=True)
    def test_malformed_jobs_are_not_unsupported(self, get_collection):
        connector = mock.Mock(spec=['get'])
        connector.get.return_value.json.side_effect = ValueError('not JSON')
        get_collection.return_value = SimpleNamespace(
            path='/Jobs', _conn=connector)
        status, detail = firmware.check_lc_jobs(self.task, ['JID_1'])
        self.assertEqual(firmware.LC_JOBS_RUNNING, status)
        self.assertIn('Malformed', detail)
