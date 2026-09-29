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

"""Dell firmware staging and application outcomes, including older sushy."""

import sushy
from sushy.oem.dell import constants as dell_constants

from ironic.common import exception
from ironic.drivers.modules.redfish import utils as redfish_utils

LC_JOBS_DONE = 'done'
LC_JOBS_RUNNING = 'running'
LC_JOBS_ERROR = 'error'
LC_JOBS_UNAVAILABLE = 'unavailable'
LC_JOBS_STAGED = 'staged'

FAILED_JOB_STATES = getattr(
    dell_constants, 'FAILED_JOB_STATES',
    ('CompletedWithErrors', 'Failed', 'RebootFailed'))
STAGED_JOB_STATES = ('Scheduled', 'RebootPending', 'UserIntervention')


def _get_dell_job_collection(task):
    """Only an absent OEM capability, not a failed read, is unsupported."""
    system = redfish_utils.get_system(task.node)
    for manager in system.managers:
        try:
            return manager.get_oem_extension('Dell').job_collection
        except sushy.exceptions.OEMExtensionNotFoundError:
            continue
    return None


def _read_jobs(collection):
    """Read full outcomes rather than the incomplete unfinished-job view."""
    get_jobs = getattr(collection, 'get_jobs', None)
    if callable(get_jobs):
        return [{'id': job.identity, 'state': job.job_state,
                 'type': job.job_type,
                 'message': job.message or job.message_id}
                for job in get_jobs()]
    # Older supported sushy has no get_jobs(), and get_unfinished_jobs() drops
    # both failures and several nonterminal states. Use its expanded endpoint
    # without discarding any outcomes. Never fall back after a failed GET.
    response = collection._conn.get(
        collection.path + '?$expand=.($levels=1)')
    return [{'id': job['Id'], 'state': job.get('JobState'),
             'type': job.get('JobType'),
             'message': job.get('Message') or job.get('MessageId')}
            for job in response.json()['Members']]


def get_jobs(task):
    collection = _get_dell_job_collection(task)
    try:
        return _read_jobs(collection) if collection is not None else None
    except (ValueError, KeyError, TypeError) as exc:
        raise exception.RedfishError(
            error='Malformed Dell job response: %s' % exc)


def snapshot_lc_jobs(task):
    """Record pre-submission identities to exclude historical errors."""
    jobs = get_jobs(task)
    return {'supported': jobs is not None,
            'baseline': [job['id'] for job in jobs or []], 'jobs': {}}


def check_lc_jobs(task, jids, tracking=None, required=False,
                  allow_staged=False):
    """Check expected jobs and newly discovered firmware jobs.

    The optional tracking dictionary is mutated for the caller to persist in
    its state object. Explicit successful results survive job purging; missing
    or unknown results never mean success. Setting required also requires a
    discovered job when the task monitor did not expose a JID.
    allow_staged is only for deciding whether an apply reset can be issued;
    armed jobs are never accepted as successfully applied after that reset.
    """
    jids = list(jids)
    if not jids and tracking is None and not required:
        return LC_JOBS_DONE, None
    try:
        jobs = get_jobs(task)
    except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
        return LC_JOBS_RUNNING, str(exc)
    if jobs is None:
        if tracking and tracking.get('supported'):
            return LC_JOBS_RUNNING, 'Previously supported LC jobs unavailable'
        return LC_JOBS_UNAVAILABLE, 'Dell job capability is not available'
    if tracking is None:
        tracking = {'baseline': [job['id'] for job in jobs], 'jobs': {}}
    elif tracking.get('supported') is False:
        # Capability discovery is not an empty historical job collection.
        tracking['baseline'] = [job['id'] for job in jobs]
    tracking['supported'] = True
    tracked = tracking['jobs']
    for jid in jids:
        tracked.setdefault(jid, None)

    messages = {}
    present = set()
    for job in jobs:
        identity, state = job['id'], job['state']
        firmware = not job['type'] or 'firmware' in job['type'].lower()
        active = state != 'Completed' and state not in FAILED_JOB_STATES
        if (identity not in tracked and not (firmware and (
                active or identity not in tracking['baseline']))):
            continue
        present.add(identity)
        tracked[identity] = state
        messages[identity] = job['message']

    failed = [identity for identity, state in tracked.items()
              if state in FAILED_JOB_STATES]
    if failed:
        return LC_JOBS_ERROR, '; '.join('%s: %s - %s' % (
            identity, tracked[identity],
            messages.get(identity) or 'no message')
            for identity in failed)
    pending = [identity for identity, state in tracked.items()
               if state != 'Completed'
               and not (allow_staged and state in STAGED_JOB_STATES)]
    if pending:
        return LC_JOBS_RUNNING, ', '.join('%s: %s' % (
            identity, tracked[identity] if identity in present else 'missing')
            for identity in pending)
    if required and not tracked:
        return LC_JOBS_RUNNING, 'No matching firmware jobs found'
    return LC_JOBS_DONE, None


def check_staged_update(task, update, tracking):
    """Correlate staging even when the TaskMonitor URI is not an LC JID."""
    jobs = get_jobs(task)
    if jobs is None:
        if tracking.get('supported'):
            return LC_JOBS_RUNNING, 'Previously supported LC jobs unavailable'
        return LC_JOBS_UNAVAILABLE, 'Dell job capability is not available'
    if tracking.get('supported') is False:
        tracking['baseline'] = [job['id'] for job in jobs]
        update['jobs_before'] = list(tracking['baseline'])
    tracking['supported'] = True
    jids = update.get('jids', [])
    if not jids:
        uri = update.get('task_monitor') or ''
        jid = uri.rstrip('/').rsplit('/', 1)[-1]
        if jid.startswith('JID_'):
            jids = [jid]
        else:
            before = update.get('jobs_before', tracking['baseline'])
            jids = [job['id'] for job in jobs if job['id'] not in before
                    and (not job['type']
                         or 'firmware' in job['type'].lower())]
        update['jids'] = jids
    if not jids:
        return LC_JOBS_RUNNING, 'No LC job published for this component'
    by_id = {job['id']: job for job in jobs}
    ready = True
    for jid in jids:
        job = by_id.get(jid)
        if job is None:
            if tracking['jobs'].get(jid) != 'Completed':
                ready = False
            continue
        tracking['jobs'][jid] = job['state']
        if job['state'] in FAILED_JOB_STATES:
            return LC_JOBS_ERROR, '%s: %s - %s' % (
                jid, job['state'], job['message'])
        if (job['state'] not in STAGED_JOB_STATES
                and job['state'] != 'Completed'):
            ready = False
    if ready:
        update.pop('jobs_before', None)
        return LC_JOBS_STAGED, None
    return LC_JOBS_RUNNING, 'LC jobs still staging: %s' % ', '.join(jids)


def check_staged_job(task, jid):
    """Distinguish downloaded/armed firmware from downloading or flashing."""
    jobs = get_jobs(task)
    if jobs is None:
        return LC_JOBS_UNAVAILABLE, 'Dell job capability is not available'
    for job in jobs:
        if job['id'] != jid:
            continue
        detail = '%s: %s - %s' % (jid, job['state'], job['message'])
        if job['state'] in FAILED_JOB_STATES:
            return LC_JOBS_ERROR, detail
        if job['state'] == 'Completed':
            return LC_JOBS_DONE, detail
        if job['state'] in STAGED_JOB_STATES:
            return LC_JOBS_STAGED, detail
        return LC_JOBS_RUNNING, detail
    return LC_JOBS_RUNNING, '%s: missing' % jid


def check_scheduled_idrac_job(task, current_update):
    """Compatibility entry point for the BMC monitor-disappearance path."""
    uri = current_update.get('task_monitor', '')
    if not uri:
        return None
    status, _detail = check_staged_job(
        task, uri.rstrip('/').rsplit('/', 1)[-1])
    if status == LC_JOBS_UNAVAILABLE:
        return None
    return status in (LC_JOBS_STAGED, LC_JOBS_DONE)
