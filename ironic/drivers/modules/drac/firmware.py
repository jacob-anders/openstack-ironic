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

import enum

from oslo_log import log as logging
import sushy

from ironic.common import exception
from ironic.drivers.modules.redfish import utils as redfish_utils

LOG = logging.getLogger(__name__)

# Prefix of a Lifecycle Controller job identity (JID).
_JID_PREFIX = 'JID_'


class LCJobStatus(enum.Enum):
    """Combined status of a set of Lifecycle Controller jobs."""

    DONE = 'done'
    """Every job finished successfully."""

    RUNNING = 'running'
    """At least one job has not finished yet."""

    ERROR = 'error'
    """At least one tracked job failed, even if a sibling is still running."""

    UNAVAILABLE = 'unavailable'
    """The node offers no Dell OEM job collection to check."""

    STAGED = 'staged'


# Compatibility aliases used by the Redfish firmware state machine.
LC_JOBS_DONE = LCJobStatus.DONE
LC_JOBS_RUNNING = LCJobStatus.RUNNING
LC_JOBS_ERROR = LCJobStatus.ERROR
LC_JOBS_UNAVAILABLE = LCJobStatus.UNAVAILABLE
LC_JOBS_STAGED = LCJobStatus.STAGED

_FAILED_JOB_STATES = frozenset({
    'CompletedWithErrors', 'Failed', 'RebootFailed',
})
_STAGED_JOB_STATES = frozenset({
    'Scheduled', 'RebootPending', 'UserIntervention',
})


def jid_from_task_monitor(task_monitor_uri):
    """Get the Lifecycle Controller job ID from a task monitor URI.

    iDRAC names a firmware update task monitor after the job that
    performs the update: ``/redfish/v1/TaskService/TaskMonitors/JID_...``

    :param task_monitor_uri: a task monitor URI, or None.
    :returns: the JID, or None if the URI does not end in one.
    """
    if not task_monitor_uri:
        return None
    jid = task_monitor_uri.rstrip('/').rsplit('/', 1)[-1]
    return jid if jid.startswith(_JID_PREFIX) else None


def _get_dell_job_collection(task):
    """Get the Dell OEM Lifecycle Controller job collection for a node.

    :param task: a TaskManager instance
    :returns: a sushy DellJobCollection instance, or None if the node
        has no manager offering the Dell OEM extension.
    :raises: RedfishError or RedfishConnectionError if the system cannot
        be read.
    :raises: sushy.exceptions.SushyError if the managers or the job
        collection cannot be read, including MissingAttributeError when
        the system does not link its managers.
    """
    node = task.node
    system = redfish_utils.get_system(node)
    for manager in system.managers:
        try:
            manager_oem = manager.get_oem_extension('Dell')
        except sushy.exceptions.OEMExtensionNotFoundError:
            LOG.debug('Dell OEM extension is not available for '
                      'manager %(manager)s of node %(node)s',
                      {'manager': manager.identity, 'node': node.uuid})
            continue
        return manager_oem.job_collection

    return None


def _read_jobs(job_collection):
    """Read complete Dell job outcomes using public or compatible APIs."""
    get_jobs = getattr(job_collection, 'get_jobs', None)
    if callable(get_jobs):
        return [{'id': job.identity, 'state': job.job_state,
                 'type': job.job_type,
                 'message': job.message or job.message_id}
                for job in get_jobs()]

    # Older supported sushy releases expose only unfinished jobs. Their
    # expanded collection response retains terminal outcomes needed to
    # distinguish a completed update from a failed or still-running one.
    connector = getattr(job_collection, '_conn', None)
    path = getattr(job_collection, 'path', None)
    if (not callable(getattr(connector, 'get', None))
            or not isinstance(path, str)):
        raise exception.RedfishError(error=(
            'The installed sushy has no compatible full Dell job reader'))
    response = connector.get(path + '?$expand=.($levels=1)')
    return [{'id': job['Id'], 'state': job.get('JobState'),
             'type': job.get('JobType'),
             'message': job.get('Message') or job.get('MessageId')}
            for job in response.json()['Members']]


def get_jobs(task):
    """Read complete Dell job outcomes through sushy's public API.

    The minimum sushy version for this interface provides
    ``DellJobCollection.get_jobs``. Missing OEM capability is represented by
    ``None``; transport, parsing, and resource-read failures are raised so a
    caller can retry rather than treating them as unsupported hardware.

    :param task: a TaskManager instance
    :returns: a list of normalized job dictionaries, or None if the Dell OEM
        job collection is not available
    :raises: RedfishError or sushy.exceptions.SushyError when the collection
        cannot be read
    """
    job_collection = _get_dell_job_collection(task)
    if job_collection is None:
        return None

    try:
        jobs = _read_jobs(job_collection)
        for job in jobs:
            if not isinstance(job['id'], str) or not job['id']:
                raise ValueError('Job Id must be a nonempty string')
            for field in ('state', 'type', 'message'):
                if job[field] is not None and not isinstance(job[field], str):
                    raise ValueError('Job %s must be a string or null' % field)
        return jobs
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise exception.RedfishError(
            error='Malformed Dell job response: %s' % exc)


def snapshot_lc_jobs(task):
    """Record pre-submission job IDs so historical failures do not taint us.

    :param task: a TaskManager instance
    :returns: a mutable tracking dictionary to persist with the firmware
        segment
    """
    jobs = get_jobs(task)
    return {'supported': jobs is not None,
            'baseline': [job['id'] for job in jobs or []], 'jobs': {}}


def check_scheduled_idrac_job(task, current_update):
    """Check Dell iDRAC for a scheduled Lifecycle Controller job.

    Used when the Redfish task of a firmware update disappeared before
    it could be observed: a job that is still scheduled or running means
    the firmware was staged and a reboot will apply it, while a missing
    or finished job means the download or staging did not leave anything
    to apply.

    :param task: a TaskManager instance
    :param current_update: the current firmware update being processed
    :returns: True if the job is still scheduled or running, False if it
        has finished (successfully or not) or does not exist, None if
        the task monitor does not name a job or the Dell OEM job
        collection is not available
    :raises: RedfishError, RedfishConnectionError or
        sushy.exceptions.SushyError if the job collection cannot be read;
        the job may still exist, so the caller should check again later.
    """
    node = task.node
    jid = jid_from_task_monitor(current_update.get('task_monitor'))
    if not jid:
        LOG.warning('Task monitor %(uri)s of node %(node)s does not name a '
                    'Lifecycle Controller job, so the job cannot be '
                    'checked.',
                    {'uri': current_update.get('task_monitor'),
                     'node': node.uuid})
        return None

    status, detail = check_lc_jobs(task, [jid])
    if status == LCJobStatus.UNAVAILABLE:
        return None
    if status == LCJobStatus.RUNNING:
        LOG.debug('Found scheduled LC job %(jid)s for node %(node)s, '
                  'firmware staging succeeded.',
                  {'jid': jid, 'node': node.uuid})
        return True
    if status == LCJobStatus.ERROR:
        LOG.warning('LC job %(jid)s for node %(node)s did not leave '
                    'firmware to apply: %(detail)s',
                    {'jid': jid, 'node': node.uuid, 'detail': detail})
    else:
        LOG.debug('LC job %(jid)s for node %(node)s has already '
                  'finished.', {'jid': jid, 'node': node.uuid})
    return False


def _classify_dell_jobs(jids, jobs):
    """Classify DellJob objects into failed/running/done for `jids`.

    The outcome of a job is taken from sushy's DellJob.is_failed and
    DellJob.is_finished, so the set of terminal and failed Lifecycle
    Controller job states is maintained in sushy alone. A job missing
    from the collection has no outcome to report, so it counts as
    failed: it never counts as done.

    :param jids: a list of JIDs to check
    :param jobs: a list of sushy DellJob objects, as returned by
        DellJobCollection.get_jobs()
    :returns: a tuple (status, detail), status is one of
        LCJobStatus.DONE, LCJobStatus.RUNNING, LCJobStatus.ERROR
    """
    jobs_by_id = {job.identity: job for job in jobs}

    failed = []
    running = []
    for jid in jids:
        job = jobs_by_id.get(jid)
        if job is None:
            failed.append('%s: not found in the job queue' % jid)
        elif job.is_failed:
            failed.append('%s: %s - %s' % (
                jid, job.job_state, job.message or 'unknown error'))
        elif not job.is_finished:
            running.append(jid)

    # Report an error only once every job has stopped, so that nothing is
    # still being applied when the caller acts on the failure.
    if running:
        return LCJobStatus.RUNNING, ', '.join(running)

    if failed:
        return LCJobStatus.ERROR, '; '.join(failed)

    return LCJobStatus.DONE, None


def _check_tracked_lc_jobs(task, jids, tracking, required, allow_staged):
    """Check jobs while retaining known outcomes across task/job purging."""
    try:
        jobs = get_jobs(task)
    except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
        return LCJobStatus.RUNNING, str(exc)

    if jobs is None:
        if tracking and tracking.get('supported'):
            return LCJobStatus.RUNNING, (
                'Previously supported LC jobs are unavailable')
        return LCJobStatus.UNAVAILABLE, (
            'Dell Lifecycle Controller jobs are not available')

    if tracking is None:
        tracking = {'supported': True,
                    'baseline': [job['id'] for job in jobs], 'jobs': {}}
    elif tracking.get('supported') is False:
        # First successful read after an unavailable baseline: treat this
        # collection as historical, rather than attributing its old jobs to
        # the current update.
        tracking['baseline'] = [job['id'] for job in jobs]
    tracking['supported'] = True
    tracked = tracking.setdefault('jobs', {})
    baseline = set(tracking.get('baseline', []))
    for jid in jids:
        tracked.setdefault(jid, None)

    present = set()
    messages = {}
    for job in jobs:
        identity = job['id']
        state = job['state']
        job_type = job.get('type')
        failed_state = (job.get('failed', False)
                        or state in _FAILED_JOB_STATES)
        finished = (job.get('finished', False)
                    or state == 'Completed' or failed_state)
        firmware_job = (not job_type
                        or 'firmware' in job_type.lower())
        active = not finished and not failed_state
        if not (identity in tracked
                or (firmware_job and identity not in baseline)
                or (firmware_job and active)):
            continue
        present.add(identity)
        tracked[identity] = state
        messages[identity] = job.get('message')

    failed = [identity for identity, state in tracked.items()
              if state in _FAILED_JOB_STATES]
    if failed:
        details = []
        for identity, state in tracked.items():
            status = state or 'unknown'
            if (identity not in present and state not in _FAILED_JOB_STATES
                    and state != 'Completed'):
                status += ' (missing)'
            message = messages.get(identity)
            if message:
                status += ' - %s' % message
            details.append('%s: %s' % (identity, status))
        return LCJobStatus.ERROR, '; '.join(details)

    pending = [identity for identity, state in tracked.items()
               if state not in _FAILED_JOB_STATES and state != 'Completed'
               and not (allow_staged and identity in present
                        and state in _STAGED_JOB_STATES)]
    if pending:
        return LCJobStatus.RUNNING, ', '.join(
            '%s: %s' % (identity,
                        tracked[identity] if identity in present
                        else 'missing')
            for identity in pending)
    if required and not tracked:
        return LCJobStatus.RUNNING, 'No matching firmware jobs found'
    return LCJobStatus.DONE, None


def check_lc_jobs(task, jids, tracking=None, required=False,
                  allow_staged=False):
    """Check the status of one or more Dell Lifecycle Controller jobs.

    :param task: a TaskManager instance
    :param jids: an iterable of Dell LC job IDs (JIDs) to check
    :param tracking: optional mutable baseline/outcome record retained by the
        caller across polls
    :param required: require positive job evidence even if ``jids`` is empty
    :param allow_staged: treat armed jobs as sufficient only for the decision
        to issue an apply reboot
    :returns: a tuple (status, detail) with an LCJobStatus. detail is
        None for DONE, a comma-separated list of still-running JIDs for
        RUNNING, a description of the failed or missing job(s) for
        ERROR, or a reason string for UNAVAILABLE
    :raises: RedfishError, RedfishConnectionError or
        sushy.exceptions.SushyError if the jobs cannot be read. A read
        failure says nothing about the jobs, so callers should check
        again later rather than treat it as any status.
    """
    node = task.node
    jids = list(jids)
    if tracking is not None or required or allow_staged:
        return _check_tracked_lc_jobs(task, jids, tracking, required,
                                      allow_staged)
    if not jids:
        return LCJobStatus.DONE, None

    job_collection = _get_dell_job_collection(task)
    if job_collection is None:
        reason = ('Dell OEM Lifecycle Controller job collection is '
                  'not available for node %s' % node.uuid)
        LOG.warning('Cannot check LC jobs %(jids)s for node %(node)s: OEM '
                    'job collection unavailable.',
                    {'jids': jids, 'node': node.uuid})
        return LCJobStatus.UNAVAILABLE, reason

    return _classify_dell_jobs(jids, job_collection.get_jobs(job_ids=jids))


def check_staged_update(task, update, tracking):
    """Correlate a submitted image with its armed Dell LC job.

    :param task: a TaskManager instance
    :param update: the firmware setting being staged; mutated with matching
        JIDs as they become visible
    :param tracking: the segment's mutable job baseline/outcome record
    :returns: an ``(LCJobStatus, detail)`` tuple
    """
    try:
        jobs = get_jobs(task)
    except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
        return LCJobStatus.RUNNING, str(exc)
    if jobs is None:
        if tracking.get('supported'):
            return LCJobStatus.RUNNING, (
                'Previously supported LC jobs are unavailable')
        return LCJobStatus.UNAVAILABLE, (
            'Dell Lifecycle Controller jobs are not available')

    if tracking.get('supported') is False:
        tracking['baseline'] = [job['id'] for job in jobs]
        update['jobs_before'] = list(tracking['baseline'])
    tracking['supported'] = True
    jids = list(update.get('jids', []))
    if not jids:
        jid = jid_from_task_monitor(update.get('task_monitor'))
        if jid:
            jids = [jid]
        else:
            before = set(update.get('jobs_before', tracking['baseline']))
            jids = [job['id'] for job in jobs
                    if job['id'] not in before
                    and (not job['type']
                         or 'firmware' in job['type'].lower())]
        update['jids'] = jids
    if not jids:
        return LCJobStatus.RUNNING, 'No LC job published for this component'

    by_id = {job['id']: job for job in jobs}
    known = tracking.setdefault('jobs', {})
    ready = True
    for jid in jids:
        job = by_id.get(jid)
        if job is None:
            if known.get(jid) != 'Completed':
                ready = False
            continue
        state = job['state']
        known[jid] = state
        if job.get('failed', False) or state in _FAILED_JOB_STATES:
            return LCJobStatus.ERROR, '%s: %s - %s' % (
                jid, state, job.get('message') or 'no message')
        if state not in _STAGED_JOB_STATES and state != 'Completed':
            ready = False
    if ready:
        update.pop('jobs_before', None)
        return LC_JOBS_STAGED, None
    return LCJobStatus.RUNNING, 'LC jobs still staging: %s' % ', '.join(jids)


def describe_tracked_jobs(tracking):
    """Describe tracked job outcomes for recovery diagnostics."""
    jobs = (tracking or {}).get('jobs', {})
    return ', '.join('%s=%s' % (identity, state or 'unknown')
                     for identity, state in sorted(jobs.items()))
