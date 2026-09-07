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
Dell iDRAC firmware update utilities.

Provides Dell-specific helpers used by the generic Redfish firmware
interface when running on iDRAC hardware.
"""

from oslo_log import log as logging

from ironic.drivers.modules.redfish import utils as redfish_utils

LOG = logging.getLogger(__name__)

LC_JOBS_DONE = 'done'
LC_JOBS_RUNNING = 'running'
LC_JOBS_ERROR = 'error'
LC_JOBS_UNAVAILABLE = 'unavailable'


def _get_dell_job_collection(task):
    """Get the Dell OEM Lifecycle Controller job collection for a node.

    :param task: a TaskManager instance
    :returns: a sushy DellJobCollection instance, or None if it could
        not be obtained (no system/managers, or the Dell OEM extension
        is not available)
    """
    node = task.node
    try:
        system = redfish_utils.get_system(node)
    except Exception as e:
        LOG.warning('Cannot get system for Dell OEM job check on '
                    'node %(node)s: %(error)s. Falling back to '
                    'assuming firmware staging succeeded.',
                    {'node': node.uuid, 'error': e})
        return None

    if not system.managers:
        return None

    for manager in system.managers:
        try:
            manager_oem = manager.get_oem_extension('Dell')
        except Exception as e:
            LOG.warning('Dell OEM extension not found on iDRAC node '
                        '%(node)s: %(error)s. Falling back to '
                        'assuming firmware staging succeeded.',
                        {'node': node.uuid, 'error': e})
            return None

        return manager_oem.job_collection

    return None


def check_scheduled_idrac_job(task, current_update):
    """Check Dell iDRAC for a scheduled Lifecycle Controller job.

    Used when the Redfish task of a firmware update disappeared before
    it could be observed: a job that is still scheduled or running means
    the firmware was staged and a reboot will apply it, while a missing
    or finished job means the download or staging did not leave anything
    to apply.

    Assumes the JID is the last path segment of the task monitor URI, an
    iDRAC convention: /redfish/v1/TaskService/TaskMonitors/JID_...

    :param task: a TaskManager instance
    :param current_update: the current firmware update being processed
    :returns: True if the job is still scheduled or running, False if it
        has finished (successfully or not) or does not exist, None if
        the Dell OEM job collection is not available
    """
    node = task.node
    task_monitor_uri = current_update.get('task_monitor', '')
    jid = (task_monitor_uri.rsplit('/', 1)[-1]
           if task_monitor_uri else '')

    if not jid:
        return None

    status, detail = check_lc_jobs(task, [jid])
    if status == LC_JOBS_UNAVAILABLE:
        return None
    if status == LC_JOBS_RUNNING:
        LOG.info(
            'Dell iDRAC: found scheduled LC job %(jid)s '
            'for node %(node)s, firmware staging succeeded.',
            {'jid': jid, 'node': node.uuid})
        return True
    if status == LC_JOBS_ERROR:
        LOG.warning(
            'Dell iDRAC: LC job %(jid)s for node %(node)s ended in '
            'error: %(detail)s',
            {'jid': jid, 'node': node.uuid, 'detail': detail})
    else:
        LOG.debug(
            'Dell iDRAC: LC job %(jid)s is not in the unfinished '
            'job list for node %(node)s.',
            {'jid': jid, 'node': node.uuid})
    return False


def _check_lc_jobs_fallback(task, job_collection, jids):
    """Classify LC jobs using only the unfinished-jobs view.

    Used when the installed sushy has no DellJobCollection.get_jobs(),
    or a call to it failed, so no per-job error detection is possible.

    :param task: a TaskManager instance
    :param job_collection: a sushy DellJobCollection instance
    :param jids: a list of JIDs to check
    :returns: a tuple (status, detail), status is one of LC_JOBS_DONE,
        LC_JOBS_RUNNING, LC_JOBS_UNAVAILABLE
    """
    node = task.node
    try:
        unfinished = job_collection.get_unfinished_jobs()
    except Exception as e:
        LOG.warning('Failed to query Dell iDRAC unfinished jobs for '
                    'node %(node)s: %(error)s.',
                    {'node': node.uuid, 'error': e})
        return LC_JOBS_UNAVAILABLE, str(e)

    still_running = [jid for jid in jids if jid in unfinished]
    LOG.debug('Dell LC jobs %(jids)s checked via unfinished-jobs '
              'fallback for node %(node)s; still running: '
              '%(running)s.',
              {'jids': jids, 'node': node.uuid,
               'running': still_running})
    if still_running:
        return LC_JOBS_RUNNING, ', '.join(still_running)

    return LC_JOBS_DONE, None


def _classify_dell_jobs(jids, jobs):
    """Classify DellJob objects into failed/running/done for `jids`.

    The outcome of a job is taken from sushy's DellJob.is_failed and
    DellJob.is_finished, so the set of terminal and failed Lifecycle
    Controller job states is maintained in sushy alone.

    :param jids: a list of JIDs to check
    :param jobs: a list of sushy DellJob objects, as returned by
        DellJobCollection.get_jobs()
    :returns: a tuple (status, detail), status is one of LC_JOBS_DONE,
        LC_JOBS_RUNNING, LC_JOBS_ERROR
    """
    jobs_by_id = {job.identity: job for job in jobs}

    failed = []
    running = []
    for jid in jids:
        job = jobs_by_id.get(jid)
        if job is None:
            # No longer present in the job collection; treat as
            # finished.
            continue

        if job.is_failed:
            failed.append((jid, job.job_state, job.message))
        elif job.is_finished:
            continue
        else:
            running.append(jid)

    if failed:
        detail = '; '.join(
            '%s: %s - %s' % (jid, job_state, message or 'unknown error')
            for jid, job_state, message in failed)
        return LC_JOBS_ERROR, detail

    if running:
        return LC_JOBS_RUNNING, ', '.join(running)

    return LC_JOBS_DONE, None


def check_lc_jobs(task, jids):
    """Check the status of one or more Dell Lifecycle Controller jobs.

    Multi-JID generalization of check_scheduled_idrac_job for
    reporting completion/error status of already-scheduled LC jobs.

    :param task: a TaskManager instance
    :param jids: an iterable of Dell LC job IDs (JIDs) to check
    :returns: a tuple (status, detail).  status is one of
        LC_JOBS_DONE, LC_JOBS_RUNNING, LC_JOBS_ERROR,
        LC_JOBS_UNAVAILABLE.  detail is None for LC_JOBS_DONE, a
        comma-separated list of still-running JIDs for
        LC_JOBS_RUNNING, a description of the failed job(s) for
        LC_JOBS_ERROR, or a reason string for LC_JOBS_UNAVAILABLE
    """
    node = task.node
    jids = list(jids)
    if not jids:
        return LC_JOBS_DONE, None

    job_collection = _get_dell_job_collection(task)
    if job_collection is None:
        reason = ('Dell OEM Lifecycle Controller job collection is '
                  'not available for node %s' % node.uuid)
        LOG.warning('Cannot check Dell LC jobs %(jids)s for node '
                    '%(node)s: OEM job collection unavailable.',
                    {'jids': jids, 'node': node.uuid})
        return LC_JOBS_UNAVAILABLE, reason

    get_jobs = getattr(job_collection, 'get_jobs', None)
    if not callable(get_jobs):
        LOG.warning('Sushy DellJobCollection.get_jobs() is not '
                    'available for node %(node)s; per-job LC error '
                    'detection needs a newer sushy release. Falling '
                    'back to the unfinished-jobs check.',
                    {'node': node.uuid})
        return _check_lc_jobs_fallback(task, job_collection, jids)

    try:
        jobs = get_jobs(job_ids=jids)
    except Exception as e:
        LOG.warning('Failed to fetch Dell LC jobs %(jids)s for node '
                    '%(node)s: %(error)s. Falling back to the '
                    'unfinished-jobs check.',
                    {'jids': jids, 'node': node.uuid, 'error': e})
        return _check_lc_jobs_fallback(task, job_collection, jids)

    return _classify_dell_jobs(jids, jobs)
