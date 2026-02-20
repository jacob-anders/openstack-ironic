#
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

"""
DRAC BIOS configuration specific methods
"""

from oslo_log import log

from ironic.common import exception
from ironic.common.i18n import _
from ironic.drivers.modules.drac import utils as drac_utils
from ironic.drivers.modules.redfish import bios as redfish_bios

LOG = log.getLogger(__name__)


class DracRedfishBIOS(redfish_bios.RedfishBIOS):
    """iDRAC Redfish interface for BIOS settings-related actions.

    Extends the generic Redfish BIOS interface with Dell iDRAC-specific
    checks, such as verifying that no pending configuration jobs exist
    before applying BIOS settings.
    """
    # NOTE(cardoe): deprecated in favor of plain Redfish
    supported = False

    def pre_configuration(self, task, settings):
        """Check for pending iDRAC jobs before applying BIOS settings.

        Dell iDRAC supports only one outstanding configuration job at a
        time.  If another job is pending (e.g. from a boot sequence
        change, virtual media configuration, or firmware update), the
        BIOS set_attributes call will fail.  This hook detects that
        condition early and raises a clear error.

        :param task: a TaskManager instance containing the node to act on.
        :param settings: a list of BIOS settings to be applied.
        :returns: a wait state if a reboot was triggered by the parent
            hook, otherwise None.
        :raises: RedfishError if unfinished iDRAC configuration jobs
            exist.
        """
        result = super().pre_configuration(task, settings)
        if result is not None:
            return result

        self._check_unfinished_jobs(task)
        return None

    def _check_unfinished_jobs(self, task):
        """Fail if unfinished iDRAC configuration jobs exist.

        :param task: a TaskManager instance.
        :raises: RedfishError if unfinished jobs are found.
        """
        try:
            unfinished = drac_utils.execute_oem_manager_method(
                task, 'get unfinished jobs',
                lambda m: m.job_collection.get_unfinished_jobs())
        except exception.RedfishError:
            LOG.warning('Unable to query iDRAC job queue for node %(node)s. '
                        'Proceeding with BIOS configuration and relying on '
                        'the BMC to reject conflicting requests.',
                        {'node': task.node.uuid})
            return

        if unfinished:
            job_ids = ', '.join(
                str(getattr(j, 'identity', j)) for j in unfinished)
            error_msg = (
                _('Cannot apply BIOS settings on node %(node)s because '
                  'there are unfinished iDRAC configuration jobs: '
                  '%(jobs)s. Dell iDRAC supports only one outstanding '
                  'configuration job at a time. Clear the job queue or '
                  'wait for the pending jobs to complete before retrying.')
                % {'node': task.node.uuid, 'jobs': job_ids})
            LOG.error(error_msg)
            raise exception.RedfishError(error=error_msg)
