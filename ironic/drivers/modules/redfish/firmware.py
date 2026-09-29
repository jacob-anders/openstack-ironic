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

from urllib.parse import urlparse

from oslo_log import log
from oslo_utils import timeutils
import sushy

from ironic.common import async_steps
from ironic.common import exception
from ironic.common.i18n import _
from ironic.common import metrics_utils
from ironic.common import states
from ironic.conductor import periodics
from ironic.conductor import utils as manager_utils
from ironic.conf import CONF
from ironic.drivers import base
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.drac import firmware as drac_fw
from ironic.drivers.modules.redfish import firmware_utils
from ironic.drivers.modules.redfish import utils as redfish_utils
from ironic import objects

LOG = log.getLogger(__name__)

METRICS = metrics_utils.get_metrics_logger(__name__)

# The single driver_internal_info entry holding the whole state of an
# in-progress firmware update. Its value is a versioned dict:
#
#   version:     schema version of this dict (STATE_VERSION)
#   state:       the current state, one of the STATE_* constants below
#   entered_at:  ISO time the current state was entered
#   started_at:  ISO time the firmware.update step started; the reference
#                point for CONF.redfish.firmware_update_overall_timeout
#   settings:    the firmware update dicts still to apply. Each entry
#                carries 'component', 'url', and, once submitted,
#                'task_monitor', 'submitted', 'staged', and 'power_timeout'.
#                'synchronous' and 'task_success' retain positive outcomes
#                independently of monitor presence; 'version_changed' records
#                BMC version evidence once its completion gate passes.
#                Dell entries also carry correlated job IDs. The transient
#                jobs_before baseline is discarded once staging is verified.
#   cleanup:     staging back-ends to clean up, as understood by
#                firmware_utils.cleanup()
#   grouping:    whether adjacent non-BMC updates share one reboot
#   segment:     the components being applied together, as
#                {'length': n, 'current': i or None, 'batched': bool};
#                'current' is the index being staged, 'batched' tells a
#                consolidated non-BMC segment from a single BMC one. Its
#                started_at anchors the fixed deadline; jobs tracks Dell
#                outcomes and poll_index bounds task reads per invocation.
#   reboot_time: ISO time the apply reboot was issued; the reference
#                point for the boot check delay and for
#                CONF.redfish.firmware_update_post_reboot_verify_timeout
#   bmc:         BMC version-check bookkeeping, or None. See
#                _start_bmc_segment for the field meanings
#   verify:      post-reboot verify state, or None. See _build_verify
FIRMWARE_UPDATE_STATE = 'redfish_fw_update'
STATE_VERSION = 2

# A SimpleUpdate for settings[segment['current']] is in flight. Poll its
# task until the BMC reports it staged, then submit the next component of
# the segment or issue the consolidated apply reboot.
STATE_STAGING = 'staging'
# The apply reboot has been issued. Anchors the time it was issued,
# which the verify gates measure from; the next poll starts polling the
# segment's tasks.
STATE_REBOOTING = 'rebooting'
# Poll every task monitor of the segment until all are terminal.
STATE_APPLYING = 'applying'
# Dell Lifecycle Controller job gate.
STATE_VERIFYING_APPLY = 'verifying_apply'
# BootProgress gate; success enters inventory verification. Supported boot
# checks cannot expire into success. The OS-running deadline is an additional
# bound for servicing nodes that have completed POST.
STATE_VERIFYING_BOOT = 'verifying_boot'
# A BMC component is being applied: the BMC does not reboot the host, so
# its completion is detected by watching the reported BMC version.
STATE_WAITING_BMC = 'waiting_bmc'
# An old conductor's bookkeeping cannot establish which images were applied.
STATE_RECOVERY_REQUIRED = 'recovery_required'
STATE_VALIDATING_BMC = 'validating_bmc'
STATE_VERIFYING_INVENTORY = 'verifying_inventory'
STATE_STARTING = 'starting'

# Declared state machine. A move not listed here is a programming error.
# None is the state of a node with no update in progress, so it is the
# only source for the two states an update can start in.
_TRANSITIONS = {
    None: frozenset({STATE_STARTING, STATE_STAGING, STATE_WAITING_BMC,
                     STATE_RECOVERY_REQUIRED}),
    STATE_STARTING: frozenset({STATE_STAGING, STATE_WAITING_BMC}),
    STATE_STAGING: frozenset({STATE_REBOOTING}),
    STATE_REBOOTING: frozenset({STATE_APPLYING}),
    STATE_APPLYING: frozenset({STATE_VERIFYING_APPLY}),
    STATE_VERIFYING_APPLY: frozenset({STATE_VERIFYING_BOOT}),
    STATE_VERIFYING_BOOT: frozenset({STATE_VERIFYING_INVENTORY}),
    STATE_WAITING_BMC: frozenset({STATE_VALIDATING_BMC}),
    STATE_VALIDATING_BMC: frozenset({STATE_REBOOTING,
                                    STATE_VERIFYING_INVENTORY}),
    STATE_VERIFYING_INVENTORY: frozenset({STATE_STARTING}),
    STATE_RECOVERY_REQUIRED: frozenset(),
}

# States entered after the apply reboot has been issued. Nothing is
# "staged and pending" on the BMC any more once the node has been told to
# reboot: the components are being applied, not waiting to be.
_REBOOTED_STATES = frozenset({STATE_REBOOTING, STATE_APPLYING,
                              STATE_VERIFYING_APPLY, STATE_VERIFYING_BOOT,
                              STATE_VERIFYING_INVENTORY})

_STATE_HANDLERS = {
    STATE_STAGING: '_handle_staging',
    STATE_REBOOTING: '_handle_rebooting',
    STATE_APPLYING: '_handle_applying',
    STATE_VERIFYING_APPLY: '_handle_verifying_apply',
    STATE_VERIFYING_BOOT: '_handle_verifying_boot',
    STATE_WAITING_BMC: '_handle_waiting_bmc',
    STATE_RECOVERY_REQUIRED: '_handle_recovery_required',
    STATE_VALIDATING_BMC: '_handle_validating_bmc',
    STATE_VERIFYING_INVENTORY: '_handle_verifying_inventory',
    STATE_STARTING: '_handle_starting',
}

# Values for the 'lc' and 'boot' gates within the verify state dict.
_VERIFY_PENDING = 'pending'
_VERIFY_PASSED = 'passed'
_VERIFY_SKIPPED = 'skipped'

# driver_internal_info entry naming the staging back-ends to clean up.
# Not part of the state object: firmware_utils.cleanup() and the Redfish
# management interface both read this key directly, so the value tracked
# in the state object is placed here only for the duration of a cleanup.
STAGED_CLEANUP = 'firmware_cleanup'

# driver_internal_info entries used before the state object existed.
# Deleted once by _migrate_legacy_state; never written again.
LEGACY_UPDATES = 'redfish_fw_updates'
LEGACY_START_TIME = 'redfish_fw_update_start_time'
LEGACY_BMC_VERSION = 'bmc_fw_version_before_update'
LEGACY_REBOOT_REQUESTED = 'firmware_reboot_requested'
LEGACY_BATCHED_UPDATE = 'firmware_batched_update'
LEGACY_BATCH_SUBMITTED = 'firmware_batch_submitted'
LEGACY_BATCH_CURRENT_INDEX = 'firmware_batch_current_index'
LEGACY_BATCH_VERIFY = 'firmware_batch_verify'
LEGACY_ALLOW_GROUPING = 'firmware_allow_grouping'
LEGACY_KEYS = (LEGACY_UPDATES, LEGACY_START_TIME, STAGED_CLEANUP,
               LEGACY_BMC_VERSION, LEGACY_REBOOT_REQUESTED,
               LEGACY_BATCHED_UPDATE, LEGACY_BATCH_SUBMITTED,
               LEGACY_BATCH_CURRENT_INDEX, LEGACY_BATCH_VERIFY,
               LEGACY_ALLOW_GROUPING)


def _verify_phase_timeout(state):
    """Timeout of the post-reboot verify phase.

    :param state: the state object (unused; the phase timeout is a
        configuration value, not a per-node one).
    :returns: the configured timeout in seconds.
    """
    return CONF.redfish.firmware_update_post_reboot_verify_timeout


def _os_running_timeout(state):
    """Bounded wait for OSRunning once OSBootStarted has been reported.

    :param state: the state object (unused).
    :returns: the timeout in seconds.
    """
    return CONF.redfish.firmware_update_os_running_timeout


def _boot_check_delay(state):
    """How long a target BootProgress state stays untrusted.

    :param state: the state object (unused).
    :returns: the delay in seconds.
    """
    return CONF.redfish.firmware_update_boot_check_delay


def _bmc_wait_interval(state):
    """Length of the wait currently running for a BMC component.

    :param state: the state object.
    :returns: the wait in seconds, or None when no wait is configured.
    """
    settings = state.get('settings') or [{}]
    return settings[0].get('wait')


# Per-state deadlines, as (path to the ISO timestamp the state is
# measured from, callable returning the limit in seconds). Every state is
# additionally bounded by the fixed segment deadline and overall timeout.
_STATE_TIMEOUTS = {
    STATE_REBOOTING: ('reboot_time', _verify_phase_timeout),
    STATE_APPLYING: ('reboot_time', _verify_phase_timeout),
    STATE_VERIFYING_APPLY: ('reboot_time', _verify_phase_timeout),
    STATE_VERIFYING_BOOT: ('reboot_time', _verify_phase_timeout),
    STATE_WAITING_BMC: ('bmc.wait_start', _bmc_wait_interval),
}

# Two further deadlines of verifying_boot, declared in the same shape
# but outside _STATE_TIMEOUTS, which holds the one deadline bounding a
# state as a whole: verifying_boot is bounded there by the verify phase,
# and these run alongside it for two narrower cases. The first, from a
# different anchor, is a servicing node that finished POST but was never
# reported OSRunning. The second, from the same anchor, is how long the
# gate declines to trust a target state it cannot tell apart from the
# previous boot, and how long a node whose BMC reports no BootProgress
# at all is held. Both are read through :meth:`_deadline_elapsed`.
_OS_RUNNING_DEADLINE = ('verify.os_boot_started_at', _os_running_timeout)
_BOOT_CHECK_DELAY = ('reboot_time', _boot_check_delay)


def _state_anchor(state, path):
    """Read a timestamp out of the state object by dotted path.

    :param state: the state object.
    :param path: a dotted path such as ``'reboot_time'``,
        ``'bmc.wait_start'`` or ``'verify.os_boot_started_at'``.
    :returns: the ISO timestamp string, or None when not set.
    """
    value = state
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _leading_batchable_run(settings, max_size=None):
    """Length of the leading run of adjacent non-BMC components.

    A reboot is shared by a maximal run of adjacent non-BMC components.
    This function returns the length of that leading run, optionally
    capped to max_size (used for batch-of-1 when grouping is disabled).

    :param settings: list of firmware update dicts
    :param max_size: optional upper bound on the returned run length
    :returns: int — number of components in the leading batchable run
    """
    for i, s in enumerate(settings):
        if max_size is not None and i >= max_size:
            return max_size
        component = s.get('component', '')
        if redfish_utils.get_component_type(component) == redfish_utils.BMC:
            return i
    if max_size is not None:
        return min(len(settings), max_size)
    return len(settings)


class RedfishFirmware(base.FirmwareInterface):

    _FW_SETTINGS_ARGSINFO = {
        'settings': {
            'description': (
                'A list of dicts with firmware components to be updated. '
                'A non-BMC wait is a minimum post-application settling '
                'interval; a grouped segment uses the largest requested '
                'interval. BMC wait controls version verification.'
            ),
            'required': True
        },
        'allow_grouping_reboots': {
            'description': (
                'Boolean. When True, adjacent non-BMC firmware updates '
                'share a single consolidated host reboot instead of '
                'rebooting after each component. BMC entries segment the '
                'list into independent phases. Ironic does not reorder '
                'the settings list. Duplicate components within a non-BMC '
                'segment are rejected. Defaults to False.'
            ),
            'required': False
        }
    }

    def _segment_run_length(self, state):
        """Leading batchable run length, respecting the grouping mode.

        :param state: the state object
        :returns: int — capped to 1 when grouping is disabled
        """
        settings = state.get('settings') or []
        if state.get('grouping'):
            return _leading_batchable_run(settings)
        return _leading_batchable_run(settings, max_size=1)

    def _staged_pending(self, state, exclude=None):
        """Components the BMC accepted but has not yet applied.

        Derived, not stored: 'task_monitor' is set only on a successful
        SimpleUpdate, and the states in _REBOOTED_STATES are exactly
        those entered once the consolidated reboot has been issued.

        :param state: the state object
        :param exclude: a settings dict to omit (the one that just failed)
        :returns: list of component name strings, possibly empty
        """
        if state.get('state') in _REBOOTED_STATES:
            return []
        settings = state.get('settings') or []
        run_length = self._segment_run_length(state)
        return [s.get('component', '') for s in settings[:run_length]
                if s.get('task_monitor') and s is not exclude]

    def _staged_pending_note(self, node, state, exclude=None):
        """Operator-facing suffix naming components still armed on the BMC.

        :param node: the Ironic node object
        :param state: the state object
        :param exclude: a settings dict to omit (the one that just failed)
        :returns: a string to append to an error message, or '' if nothing
            is currently staged and pending
        """
        pending = self._staged_pending(state, exclude)
        if not pending:
            return ''
        LOG.warning('Firmware update failed for node %(node)s with '
                    'components already staged on the BMC: %(components)s. '
                    'These remain scheduled to apply on the next host boot.',
                    {'node': node.uuid, 'components': ', '.join(pending)})
        return _(' Components already staged on the BMC and still scheduled '
                 'to apply on the next host boot from any source: '
                 '%(components)s. Power-cycling this node will apply them, '
                 'and retrying firmware.update will stage them a second '
                 'time.') % {'components': ', '.join(pending)}

    # --- state object plumbing -------------------------------------

    def _persist(self, node, state):
        """Write the state object back to the node.

        The only place, besides :meth:`_transition`, that a handler may
        persist from.

        :param node: the Ironic node object
        :param state: the state object
        """
        node.set_driver_internal_info(FIRMWARE_UPDATE_STATE, state)
        node.set_driver_internal_info(
            async_steps.FIRMWARE_UPDATE_IN_PROGRESS, True)
        node.save()

    def _transition(self, task, state, new_state):
        """Move the update to ``new_state`` and persist it.

        :param task: a TaskManager instance
        :param state: the state object (mutated in place)
        :param new_state: the state to move to
        :raises: exception.InvalidFirmwareUpdateState if the move is not
            declared in ``_TRANSITIONS``
        """
        old_state = state.get('state')
        if new_state not in _TRANSITIONS.get(old_state, frozenset()):
            raise exception.InvalidFirmwareUpdateState(
                node=task.node.uuid, old=old_state, new=new_state)
        LOG.info('Node %(node)s: firmware update state %(old)s -> %(new)s',
                 {'node': task.node.uuid, 'old': old_state,
                  'new': new_state})
        state['state'] = new_state
        state['entered_at'] = str(timeutils.utcnow().isoformat())
        self._persist(task.node, state)

    def _deadline_elapsed(self, state, anchor_path, limit_fn):
        """Time spent against a declared deadline.

        :param state: the state object
        :param anchor_path: dotted path of the deadline's timestamp
        :param limit_fn: callable returning the limit in seconds
        :returns: a tuple ``(elapsed, limit)``. ``elapsed`` is a
            timedelta since the anchor, or None when it is not set.
            ``limit`` is the deadline in seconds.
        """
        limit = limit_fn(state)
        anchor = _state_anchor(state, anchor_path)
        if anchor is None:
            return None, limit
        return timeutils.utcnow(True) - timeutils.parse_isotime(anchor), limit

    def _state_elapsed(self, state):
        """Time spent against the current state's deadline.

        :param state: the state object
        :returns: a tuple ``(elapsed, limit)``. ``elapsed`` is a
            timedelta since the state's reference timestamp, or None
            when the state has no deadline or the timestamp is not set.
            ``limit`` is the deadline in seconds, or None.
        """
        anchor_path, limit_fn = _STATE_TIMEOUTS.get(
            state.get('state'), (None, None))
        if anchor_path is None:
            return None, None
        return self._deadline_elapsed(state, anchor_path, limit_fn)

    def _state_timed_out(self, state):
        """Whether the current state has exceeded its deadline.

        A limit of zero or less means unbounded.

        :param state: the state object
        :returns: True if the deadline has passed, False otherwise
        """
        elapsed, limit = self._state_elapsed(state)
        if elapsed is None or not limit or limit <= 0:
            return False
        return elapsed.total_seconds() >= limit

    def _build_verify(self, jids, new_boot_observed=False):
        """Build the post-reboot verify state for a segment.

        :param jids: list of Dell LC job id (JID) strings covered by
            this verify phase.
        :param new_boot_observed: whether BootProgress was seen leaving
            its pre-reboot value, proving the node reset.
        :returns: a new verify state dict. The phase's reference
            timestamp is the state object's ``reboot_time``;
            ``os_boot_started_at`` is per-reboot bookkeeping, anchoring
            the bounded wait for OSRunning once POST has finished.
        """
        return {
            'jids': list(jids),
            'lc': _VERIFY_PENDING,
            'boot': _VERIFY_PENDING,
            'new_boot_observed': new_boot_observed,
            'os_boot_started_at': None,
        }

    def _fail(self, task, state, msg, exclude=None, note=True,
              traceback=False):
        """Fail the firmware update step and discard its state.

        :param task: a TaskManager instance
        :param state: the state object
        :param msg: the error message; the staged-pending-components
            note is appended to it when applicable
        :param exclude: a settings dict to omit from that note (the one
            that just failed)
        :param note: whether the staged-pending-components note applies
            to this failure at all
        :param traceback: whether to include a traceback in the step
            error
        """
        if note:
            msg += self._staged_pending_note(task.node, state, exclude)
        try:
            self._report_step_error(task, msg, traceback=traceback)
        finally:
            self._clear_updates(task.node)

    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        properties = redfish_utils.COMMON_PROPERTIES.copy()
        properties['firmware_update_boot_progress'] = _(
            'Boot progress policy: auto requires reported readiness; '
            'limited uses firmware_update_boot_check_delay for hardware known '
            'to report only intermediate boot stages. Neither policy skips '
            'supported firmware job or reset checks.')
        properties['firmware_update_bios_pending_reset'] = _(
            'BIOS reset policy: auto waits for Pending and Running tasks; '
            'compatibility permits an ungrouped BIOS task in Starting, '
            'Pending or Running to receive its apply reset after the staging '
            'wait. Use compatibility only for platforms documented to keep '
            'these task states until reset, rather than while flashing.')
        return properties

    def validate(self, task):
        """Validates the driver information needed by the redfish driver.

        :param task: a TaskManager instance containing the node to act on.
        :raises: InvalidParameterValue on malformed parameter(s)
        :raises: MissingParameterValue on missing parameter(s)
        """
        redfish_utils.parse_driver_info(task.node)
        self._validate_boot_policy(task.node)

    def _validate_boot_policy(self, node):
        policy = node.driver_info.get('firmware_update_boot_progress', 'auto')
        if policy not in ('auto', 'limited'):
            raise exception.InvalidParameterValue(_(
                'firmware_update_boot_progress must be auto or limited'))
        bios_policy = node.driver_info.get(
            'firmware_update_bios_pending_reset', 'auto')
        if bios_policy not in ('auto', 'compatibility'):
            raise exception.InvalidParameterValue(_(
                'firmware_update_bios_pending_reset must be auto or '
                'compatibility'))

    @METRICS.timer('RedfishFirmware.cache_firmware_components')
    def cache_firmware_components(self, task, required_components=(),
                                  known_supported_types=()):
        """Store or update Firmware Components on the given node.

        This method stores Firmware Components to the firmware_information
        table during 'cleaning' operation. It will also update the timestamp
        of each Firmware Component.

        :param task: a TaskManager instance.
        :param required_components: component names whose supported inventory
            must be readable before updating the cache. Ordinary discovery
            remains best effort; update verification retries transient errors.
        :param known_supported_types: previously observed capabilities that
            must not be reclassified as unsupported during recovery.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support getting Firmware Components from bare metal.
        """

        node_id = task.node.id
        settings = []
        required_types = {redfish_utils.get_component_type(component)
                          for component in required_components}
        unsupported_types = set()
        # NOTE(iurygregory): currently we will only retrieve BIOS and BMC
        # firmware information through the redfish system and manager.

        system = redfish_utils.get_system(task.node)

        if system.bios_version:
            bios_fw = {'component': redfish_utils.BIOS,
                       'current_version': system.bios_version,
                       'vendor': None, 'model': None, 'serial_number': None}
            settings.append(bios_fw)
        else:
            LOG.debug('Could not retrieve BiosVersion in node %(node_uuid)s '
                      'system %(system)s', {'node_uuid': task.node.uuid,
                                            'system': system.identity})

        # NOTE(iurygregory): normally we only relay on the System to
        # perform actions, but to retrieve the BMC Firmware we need to
        # access the Manager.
        try:
            manager = redfish_utils.get_manager(task.node, system)
            if manager.firmware_version:
                bmc_fw = {'component': redfish_utils.BMC,
                          'current_version': manager.firmware_version,
                          'vendor': None,
                          'model': manager.model,
                          'serial_number': None}
                settings.append(bmc_fw)
            else:
                LOG.debug('Could not retrieve FirmwareVersion in node '
                          '%(node_uuid)s manager %(manager)s',
                          {'node_uuid': task.node.uuid,
                           'manager': manager.identity})
        except (exception.RedfishError, sushy.exceptions.SushyError):
            if redfish_utils.BMC in required_types:
                raise
            LOG.warning('No manager available to retrieve Firmware '
                        'from the bmc of node %s', task.node.uuid)

        nic_components = None
        try:
            if redfish_utils.NIC in required_types:
                nic_components = self.retrieve_nic_components(
                    task, system, strict=True)
            else:
                nic_components = self.retrieve_nic_components(task, system)
        except exception.UnsupportedDriverExtension:
            if redfish_utils.NIC in known_supported_types:
                raise exception.RedfishError(error=_(
                    'Previously supported NetworkAdapters inventory is '
                    'temporarily unavailable'))
            unsupported_types.add(redfish_utils.NIC)
            LOG.warning('NIC firmware inventory is unsupported on node %s',
                        task.node.uuid)
        except (exception.RedfishError,
                sushy.exceptions.BadRequestError,
                sushy.exceptions.MissingAttributeError) as e:
            if redfish_utils.NIC in required_types:
                raise
            # NOTE(janders) if an exception is raised, log a warning
            # with exception details. This is important for HP hardware
            # which at the time of writing this are known to return 400
            # responses to GET NetworkAdapters while OS isn't fully booted
            LOG.warning('Unable to access NetworkAdapters on node '
                        '%(node_uuid)s, Error: %(error)s',
                        {'node_uuid': task.node.uuid, 'error': e})
        # NOTE(janders) if no exception is raised but no NICs are returned,
        # state that clearly but in a lower severity message
        if nic_components == []:
            LOG.debug('Could not retrieve Firmware Package Version from '
                      'NetworkAdapters on node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
        elif nic_components:
            settings.extend(nic_components)

        available = {entry['component'] for entry in settings}
        missing = [component for component in required_components
                   if component not in available
                   and redfish_utils.get_component_type(component)
                   not in unsupported_types]
        if missing:
            raise exception.RedfishError(error=_(
                'Firmware inventory is not yet available for: %s')
                % ', '.join(missing))

        if not settings:
            error_msg = (_('Cannot retrieve firmware for node %s: no '
                           'supported components') % task.node.uuid)
            LOG.error(error_msg)
            raise exception.UnsupportedDriverExtension(error_msg)

        create_list, update_list, nochange_list = (
            objects.FirmwareComponentList.sync_firmware_components(
                task.context, node_id, settings))

        if create_list:
            for new_fw in create_list:
                new_fw_cmp = objects.FirmwareComponent(
                    task.context,
                    node_id=node_id,
                    component=new_fw['component'],
                    current_version=new_fw['current_version'],
                    vendor=new_fw.get('vendor'),
                    model=new_fw.get('model'),
                    serial_number=new_fw.get('serial_number'),
                )
                new_fw_cmp.create()
        if update_list:
            for up_fw in update_list:
                up_fw_cmp = objects.FirmwareComponent.get(
                    task.context,
                    node_id=node_id,
                    name=up_fw['component']
                )
                if up_fw_cmp.current_version != up_fw.get('current_version'):
                    up_fw_cmp.last_version_flashed = up_fw.get(
                        'current_version')
                    up_fw_cmp.current_version = up_fw.get('current_version')
                up_fw_cmp.vendor = up_fw.get('vendor')
                up_fw_cmp.model = up_fw.get('model')
                up_fw_cmp.serial_number = up_fw.get('serial_number')
                up_fw_cmp.save()

    def retrieve_nic_components(self, task, system, strict=False):
        """Helper function to retrieve all NICs components on a given node.

        :param task: a TaskManager instance.
        :param system: a Redfish System object
        :param strict: propagate read failures and explicitly report absent
            NetworkAdapters capability for update verification.
        :returns: a list of NIC components
        """
        nic_list = []
        try:
            chassis = redfish_utils.get_chassis(task.node, system)
        except exception.RedfishError:
            if strict:
                raise
            LOG.debug('No chassis available to retrieve NetworkAdapters '
                      'firmware information on node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
            return nic_list

        try:
            network_adapters = chassis.network_adapters
            if network_adapters is None:
                if strict:
                    raise exception.UnsupportedDriverExtension(
                        'NetworkAdapters is not supported')
                LOG.debug('NetworkAdapters not available on chassis for '
                          'node %(node_uuid)s',
                          {'node_uuid': task.node.uuid})
                return nic_list
        except sushy.exceptions.MissingAttributeError:
            if strict:
                raise exception.UnsupportedDriverExtension(
                    'NetworkAdapters is not supported')
            LOG.debug('NetworkAdapters not available on chassis for '
                      'node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
            return nic_list

        adapters = network_adapters.get_members()
        for net_adp in adapters:
            for net_adp_ctrl in net_adp.controllers:
                fw_pkg_v = net_adp_ctrl.firmware_package_version
                if not fw_pkg_v:
                    continue

                if net_adp.serial_number:
                    net_adp_id = net_adp.serial_number
                    LOG.debug('Using SerialNumber %(serial_number)s for '
                              'NetworkAdapter %(net_adp_id)s',
                              {'serial_number': net_adp.serial_number,
                               'net_adp_id': net_adp.identity})
                else:
                    net_adp_id = net_adp.identity
                    LOG.debug('Using Identity %(identity)s for '
                              'NetworkAdapter %(net_adp_id)s',
                              {'identity': net_adp.identity,
                               'net_adp_id': net_adp.identity})

                net_adp_fw = {
                    'component': (
                        redfish_utils.NIC_COMPONENT_PREFIX
                        + net_adp_id),
                    'current_version': fw_pkg_v,
                    'vendor': net_adp.manufacturer,
                    'model': net_adp.model,
                    'serial_number': net_adp.serial_number,
                }
                nic_list.append(net_adp_fw)

        return nic_list

    @METRICS.timer('RedfishFirmware.update')
    @base.deploy_step(priority=0, abortable=False,
                      argsinfo=_FW_SETTINGS_ARGSINFO)
    @base.clean_step(priority=0, abortable=False,
                     argsinfo=_FW_SETTINGS_ARGSINFO,
                     requires_ramdisk=True)
    @base.service_step(priority=0, abortable=False,
                       argsinfo=_FW_SETTINGS_ARGSINFO,
                       requires_ramdisk=False)
    def update(self, task, settings, allow_grouping_reboots=False):
        """Update the Firmware on the node using the settings for components.

        :param task: a TaskManager instance.
        :param settings: a list of dictionaries, each dictionary contains the
            component name and the url that will be used to update the
            firmware.
        :param allow_grouping_reboots: Boolean. When True, non-BMC firmware
            updates are batched into a single host reboot. Defaults to False.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support update via the interface.
        :raises: InvalidParameterValue, if validation of the settings fails.
        :raises: MissingParamterValue, if some required parameters are
            missing.
        :returns: states.CLEANWAIT if Firmware update with the settings is in
            progress asynchronously of None if it is complete.
        """
        firmware_utils.validate_firmware_interface_update_args(settings)
        self._validate_boot_policy(task.node)
        if not isinstance(allow_grouping_reboots, bool):
            raise exception.InvalidParameterValue(
                _('allow_grouping_reboots must be a boolean, '
                  'got %s') % type(allow_grouping_reboots).__name__)
        if allow_grouping_reboots:
            seen = set()
            for s in settings:
                comp = s.get('component', '')
                if redfish_utils.get_component_type(comp) == redfish_utils.BMC:
                    seen.clear()
                    continue
                if comp in seen:
                    raise exception.InvalidParameterValue(
                        _("component '%(comp)s' appears more than once; "
                          "batched updates require distinct components. "
                          "Use separate firmware.update steps, or omit "
                          "allow_grouping_reboots, for staged or sequential "
                          "updates of the same component.") % {'comp': comp})
                seen.add(comp)

        node = task.node
        update_service = redfish_utils.get_update_service(node)

        LOG.debug('Updating Firmware on node %(node_uuid)s with settings '
                  '%(settings)s, allow_grouping_reboots=%(group)s',
                  {'node_uuid': node.uuid, 'settings': settings,
                   'group': allow_grouping_reboots})

        state = {
            'version': STATE_VERSION,
            'state': None,
            'entered_at': None,
            'started_at': str(timeutils.utcnow().isoformat()),
            'settings': settings,
            'cleanup': None,
            'grouping': allow_grouping_reboots,
            'segment': None,
            'reboot_time': None,
            'bmc': None,
            'verify': None,
            'inventory_supported': sorted({
                redfish_utils.get_component_type(component.component)
                for component in objects.FirmwareComponentList.get_by_node_id(
                    task.context, node.id)
                if redfish_utils.get_component_type(component.component)}),
        }
        self._transition(task, state, STATE_STARTING)
        if self._nic_inventory_ready(task, state):
            self._start_next_segment(task, state, update_service)
        else:
            deploy_utils.set_async_step_flags(node, reboot=False, polling=True)
            self._persist(node, state)
        return async_steps.get_return_state(node)

    def _nic_inventory_ready(self, task, state):
        """Wait for HPE NIC visibility before arming any image in its batch."""
        run = state['settings'][:self._segment_run_length(state)]
        if not any(redfish_utils.get_component_type(entry['component'])
                   == redfish_utils.NIC for entry in run):
            return True
        vendor = redfish_utils.get_system_vendor(task.node).lower()
        if (not ({'hp', 'hpe'} & set(vendor.split()))
                and 'hewlett' not in vendor):
            return True
        error = ''
        try:
            system = redfish_utils.get_system(task.node)
            chassis = redfish_utils.get_chassis(task.node, system)
            adapters = chassis.network_adapters
            if adapters is not None and adapters.get_members():
                supported = state.setdefault('inventory_supported', [])
                if redfish_utils.NIC not in supported:
                    supported.append(redfish_utils.NIC)
                return True
        except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
            error = str(exc)
        state['last_error'] = (
            'HPE NetworkAdapters are not visible for NIC staging. Keep the '
            'instance OS running during servicing, or boot IPA before '
            'day-0 firmware updates. ' + error)
        return False

    def _start_next_segment(self, task, state, update_service):
        """Start the next firmware update segment.

        Starts either a consolidated non-BMC segment or a single BMC one
        for the first component(s) left in ``state['settings']``.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        """
        if self._segment_run_length(state) > 0:
            self._start_batched_segment(task, state, update_service)
        else:
            self._start_bmc_segment(task, state, update_service)

    def _start_bmc_segment(self, task, state, update_service):
        """Submit a BMC component and start watching its version.

        BMC updates do not immediately reboot the host. Task/job outcomes and
        the reported version establish completion before resource recovery.
        An unchanged version requires positive task or job evidence.

        The ``bmc`` entry of the state object records ``version_before``
        (the version reported before the update), ``wait_start`` (the
        reference point of the wait currently running, refreshed on each
        version-check poll), ``check_start`` (the reference point of the
        overall version-check timeout), ``checking`` (whether the wait
        that is running is a version-check poll rather than the initial
        update wait) and ``reboot_requested`` (whether the segment must
        reboot the host to apply the update).

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        """
        node = task.node
        fw_upd = state['settings'][0]
        state['segment'] = {'length': 1, 'current': None, 'batched': False,
                            'started_at': (state.get('entered_at')
                                           or timeutils.utcnow().isoformat())}
        self._snapshot_segment_jobs(task, state)
        state['reboot_time'] = None
        state['verify'] = None
        bmc = {'version_before': None, 'wait_start': None,
               'check_start': None, 'checking': False,
               'reboot_requested': False}

        try:
            system = redfish_utils.get_system(node)
            manager = redfish_utils.get_manager(node, system)
            current_bmc_version = manager.firmware_version
            bmc['version_before'] = current_bmc_version
            LOG.debug('BMC version before update for node %(node)s: '
                      '%(version)s',
                      {'node': node.uuid, 'version': current_bmc_version})
        except (exception.RedfishError, sushy.exceptions.SushyError) as e:
            LOG.warning('Could not read BMC version before update for '
                        'node %(node)s: %(error)s',
                        {'node': node.uuid, 'error': e})

        LOG.info('BMC firmware update for node %(node)s. '
                 'Monitoring BMC version instead of immediate reboot.',
                 {'node': node.uuid})

        wait_interval = fw_upd.get('wait')
        if wait_interval is None:
            wait_interval = CONF.redfish.firmware_update_reboot_delay
        fw_upd['wait'] = wait_interval
        start_time = str(timeutils.utcnow().isoformat())
        bmc['wait_start'] = start_time
        bmc['check_start'] = start_time
        bmc['check_timeout'] = wait_interval
        state['bmc'] = bmc

        deploy_utils.set_async_step_flags(
            node,
            reboot=False,
            polling=True
        )
        self._transition(task, state, STATE_WAITING_BMC)
        self._submit_simple_update(node, state, update_service, fw_upd)
        self._persist(node, state)

    def _get_current_bmc_version(self, node):
        """Get current BMC firmware version.

        Note: BMC may be temporarily unresponsive after firmware update.
        Expected exceptions (timeouts, connection refused, HTTP errors) are
        caught and logged, returning None to indicate version unavailable.

        :param node: the Ironic node object
        :returns: Current BMC firmware version string, or None if BMC
                  is unresponsive/inaccessible
        """
        try:
            system = redfish_utils.get_system(node)
            manager = redfish_utils.get_manager(node, system)
            return manager.firmware_version
        except (exception.RedfishError,
                exception.RedfishConnectionError,
                sushy.exceptions.SushyError) as e:
            # BMC unresponsiveness is expected after firmware update
            # (timeouts, connection refused, HTTP 4xx/5xx errors)
            LOG.debug('BMC temporarily unresponsive for node %(node)s: '
                      '%(error)s', {'node': node.uuid, 'error': e})
            return None

    def _bmc_update_completion(self, task, state, update_service):
        """Verify the BMC version before entering asynchronous recovery."""
        node = task.node
        bmc = state['bmc']
        current_update = state['settings'][0]
        if self._run_lc_job_gate(task, state, False):
            return None
        current_version = self._get_current_bmc_version(node)
        version_before = bmc.get('version_before')
        changed = (current_version is not None and version_before is not None
                   and current_version != version_before)
        check_start_time = bmc.get('check_start')
        elapsed = (timeutils.utcnow(True)
                   - timeutils.parse_isotime(check_start_time)
                   if check_start_time else None)
        timeout = bmc.get('check_timeout',
                          CONF.redfish.firmware_update_reboot_delay)
        positive = self._positive_task_outcome(current_update)
        tracking = state['segment'].get('jobs') or {}
        jobs = tracking.get('jobs', {})
        positive = positive or (bool(jobs) and all(
            outcome == 'Completed' for outcome in jobs.values()))
        if changed or (positive and elapsed is not None
                       and elapsed.total_seconds() >= timeout):
            current_update['version_changed'] = changed
            # A same-version reinstall needs a successful task/job, never just
            # a timer. Any following component needs the BMC handoff reset.
            bmc['reboot_requested'] = (
                len(state['settings']) > 1
                or current_version is None or version_before is None)
            current_update.pop('wait', None)
            bmc['wait_start'] = None
            self._continue_after_bmc(task, state, update_service)
            return None
        current_update['wait'] = (
            CONF.redfish.firmware_update_bmc_version_check_interval)
        state['last_error'] = 'BMC update has no verified completion yet'
        bmc['wait_start'] = timeutils.utcnow().isoformat()
        bmc['checking'] = True
        self._persist(node, state)
        return None

    def _start_bmc_apply_reboot(self, task, state, fw_upd, set_flags=True):
        """Reboot the host to apply a BMC segment, and enter ``rebooting``.

        The BMC's own task is already terminal by the time this runs, so
        the segment is recorded with no task monitors left to poll: the
        ``applying`` state passes straight through to the verify states,
        which are shared with the batched machine.

        :param task: a TaskManager instance
        :param state: the state object
        :param fw_upd: the BMC settings dict whose update is being
            applied
        :param set_flags: whether the async step flags still need to be
            set for a reboot (they are already set when the reboot was
            requested by an expired version check)
        """
        node = task.node
        if not self._can_reboot_segment(task, state):
            return
        state['verify'] = self._build_verify(self._segment_jids(state))
        fw_upd.pop('task_monitor', None)
        self._prepare_reboot_observation(node, state)
        state['bmc']['reboot_requested'] = False
        if set_flags:
            deploy_utils.set_async_step_flags(node, reboot=True, polling=True)
        self._transition(task, state, STATE_REBOOTING)
        manager_utils.node_power_action(task, states.REBOOT)

    def _submit_simple_update(self, node, state, update_service, fw_upd):
        """Submit a SimpleUpdate request and track cleanup.

        Handles systems-collection targeting, firmware file staging,
        the SimpleUpdate call, and cleanup tracking.

        :param node: the node that will have a firmware update executed.
        :param state: the state object; its ``cleanup`` list is extended
            when the firmware file had to be staged.
        :param update_service: the sushy firmware update service.
        :param fw_upd: single firmware update settings dict (mutated
            in-place: task_monitor and power_timeout are added).
        :returns: task_monitor_uri string
        """
        fw_upd['power_timeout'] = CONF.redfish.firmware_update_reboot_delay

        try:
            systems_collection = redfish_utils.get_system_collection(node)
        except exception.RedfishError as e:
            LOG.error('Failed getting Redfish Systems Collection'
                      ' for node %(node)s. Error %(error)s',
                      {'node': node.uuid, 'error': e})
            raise exception.RedfishError(error=e)
        count = len(systems_collection.members_identities)
        # NOTE(janders) if we see more than one System on the BMC, assume that
        # we need to explicitly specify Target parameter when calling
        # SimpleUpdate. This is needed for compatibility with sushy-tools
        # in automated testing using VMs.
        if count > 1:
            target = node.driver_info.get('redfish_system_id')
            targets = [target]
        else:
            targets = None

        component_url, cleanup = self._stage_firmware_file(
            node, fw_upd, state=state)

        if cleanup:
            fw_clean = state.setdefault('cleanup', None) or []
            if cleanup not in fw_clean:
                fw_clean.append(cleanup)
            state['cleanup'] = fw_clean
            self._persist(node, state)

        LOG.debug('Applying new firmware %(url)s for %(component)s on node '
                  '%(node_uuid)s',
                  {'url': fw_upd['url'], 'component': fw_upd['component'],
                   'node_uuid': node.uuid})
        try:
            if (state.get('grouping')
                    and redfish_utils.get_component_type(fw_upd['component'])
                    != redfish_utils.BMC):
                task_monitor = firmware_utils.simple_update_on_reset(
                    update_service, component_url, targets=targets)
            elif targets is not None:
                task_monitor = update_service.simple_update(component_url,
                                                            targets=targets)
            else:
                task_monitor = update_service.simple_update(component_url)
        except sushy.exceptions.MissingAttributeError as e:
            LOG.error('The attribute #UpdateService.SimpleUpdate is missing '
                      'on node %(node)s. Error: %(error)s',
                      {'node': node.uuid, 'error': e.message})
            raise exception.RedfishError(error=e)

        fw_upd['task_monitor'] = task_monitor.task_monitor_uri
        fw_upd['submitted'] = True
        fw_upd['synchronous'] = not bool(task_monitor.task_monitor_uri)
        jid = self._jid_from_task_monitor(task_monitor.task_monitor_uri)
        if jid:
            fw_upd['jids'] = [jid]

        return task_monitor.task_monitor_uri

    def _jid_from_task_monitor(self, task_monitor):
        """Extract the Dell LC job id (JID) from a task monitor URI.

        Assumes the JID is the last path segment of the URI, an iDRAC
        convention: /redfish/v1/TaskService/TaskMonitors/JID_...

        :param task_monitor: a task monitor URI string, or '' or None.
        :returns: the JID (the URI's last path segment), or '' if a
            JID cannot be derived.
        """
        jid = (task_monitor.rstrip('/').rsplit('/', 1)[-1]
               if task_monitor else '')
        return jid if jid.startswith('JID_') else ''

    def _snapshot_segment_jobs(self, task, state):
        tracking = (drac_fw.snapshot_lc_jobs(task)
                    if redfish_utils.is_dell_node(task.node) else None)
        state['segment']['jobs'] = tracking
        if tracking is not None:
            state['settings'][0]['jobs_before'] = list(tracking['baseline'])

    def _segment_jids(self, state):
        jids = []
        for entry in state['settings'][:state['segment']['length']]:
            jids.extend(entry.get('jids', []))
            jid = self._jid_from_task_monitor(entry.get('task_monitor'))
            if jid:
                jids.append(jid)
        return list(dict.fromkeys(jids))

    def _can_reboot_segment(self, task, state):
        if not redfish_utils.is_dell_node(task.node):
            return True
        status, detail = drac_fw.check_lc_jobs(
            task, self._segment_jids(state), state['segment'].get('jobs'),
            required=True, allow_staged=state['segment']['batched'])
        state['last_error'] = detail
        if status == drac_fw.LC_JOBS_ERROR:
            raise exception.FirmwareUpdateFailed(error=detail)
        return status in (drac_fw.LC_JOBS_DONE, drac_fw.LC_JOBS_UNAVAILABLE)

    def _submit_one_batched_component(self, node, state, update_service,
                                      idx):
        """Submit a single SimpleUpdate for one component in a batch.

        :param node: the node object
        :param state: the state object
        :param update_service: the sushy firmware update service
        :param idx: index into the state's settings for the component
            to submit
        :raises: RedfishError if SimpleUpdate submission fails
        """
        settings = state['settings']
        fw_upd = settings[idx]
        component = fw_upd.get('component', '')
        LOG.debug('Batched submission %(idx)d/%(total)d: staging '
                  '%(component)s from %(url)s for node %(node)s',
                  {'idx': idx + 1, 'total': len(settings),
                   'component': component, 'url': fw_upd['url'],
                   'node': node.uuid})
        try:
            self._submit_simple_update(node, state, update_service, fw_upd)
        except Exception as e:
            LOG.error('Batched firmware submission failed at component '
                      '%(component)s (%(idx)d/%(total)d) for node '
                      '%(node)s. Error: %(error)s. No consolidated '
                      'reboot will be issued.',
                      {'component': component, 'idx': idx + 1,
                       'total': len(settings), 'node': node.uuid,
                       'error': e})
            raise

    def _start_batched_segment(self, task, state, update_service):
        """Submit the first non-BMC firmware update and start staging polling.

        Submits SimpleUpdate for the leading run of non-BMC components in
        the settings and sets up async polling. The periodic poller will
        monitor staging progress and submit subsequent components one at
        a time, only triggering a consolidated reboot after all are
        staged. BMC entries and any trailing components are left in the
        settings for later processing via _start_next_segment.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        :raises: RedfishError if the SimpleUpdate submission fails
        """
        node = task.node
        settings = state['settings']

        run_length = self._segment_run_length(state)
        LOG.info('Batching %(batch)d of %(total)d components for node '
                 '%(node)s; remaining components will be processed '
                 'in subsequent segments.',
                 {'batch': run_length, 'total': len(settings),
                  'node': node.uuid})

        state['segment'] = {'length': run_length, 'current': 0,
                            'batched': True,
                            'started_at': (state.get('entered_at')
                                           or timeutils.utcnow().isoformat())}
        self._snapshot_segment_jobs(task, state)
        state['reboot_time'] = None
        state['bmc'] = None
        state['verify'] = None

        deploy_utils.set_async_step_flags(
            node,
            reboot=False,
            polling=True
        )
        self._transition(task, state, STATE_STAGING)
        self._submit_one_batched_component(node, state, update_service, 0)
        self._persist(node, state)

        LOG.info('Submitted component 1/%(count)d for node %(node)s. '
                 'Polling for staging completion before submitting next.',
                 {'count': run_length, 'node': node.uuid})

    def _validate_resources_stability(self, node):
        """Take one BMC recovery sample; leave waiting to the periodic."""
        timeout = CONF.redfish.firmware_update_resource_validation_timeout
        required_successes = CONF.redfish.firmware_update_required_successes
        validation_interval = CONF.redfish.firmware_update_validation_interval
        if not timeout or not required_successes:
            return True
        state = node.driver_internal_info[FIRMWARE_UPDATE_STATE]
        bmc = state['bmc']
        validation = bmc.setdefault('validation', {
            'started_at': timeutils.utcnow().isoformat(), 'successes': 0})
        if validation['successes'] >= required_successes:
            return True
        if self._seconds_since(validation['started_at']) >= timeout:
            raise exception.FirmwareUpdateFailed(error=_(
                'BMC resources failed to stabilize within %(timeout)s '
                'seconds; last error: %(error)s') % {
                    'timeout': timeout, 'error': validation.get('error')})
        if (validation.get('checked_at') and self._seconds_since(
                validation['checked_at']) < validation_interval):
            return False
        validation['checked_at'] = timeutils.utcnow().isoformat()
        try:
            system = redfish_utils.get_system(node)
            redfish_utils.get_manager(node, system)
            chassis = redfish_utils.get_chassis(node, system)
            try:
                adapters = chassis.network_adapters
                if adapters is not None:
                    adapters.get_members()
            except sushy.exceptions.MissingAttributeError:
                pass
            validation['successes'] += 1
        except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
            validation['successes'] = 0
            validation['error'] = str(exc)
            state['last_error'] = str(exc)
        self._persist(node, state)
        return validation['successes'] >= required_successes

    def _seconds_since(self, timestamp):
        return (timeutils.utcnow(True)
                - timeutils.parse_isotime(timestamp)).total_seconds()

    def _boot_observation(self, node):
        """Read standard markers, including fields missing from older sushy."""
        data = redfish_utils.get_system(node).json
        progress = data.get('BootProgress') or {}
        return {'reset': data.get('LastResetTime'),
                'state': progress.get('LastState'),
                'state_time': progress.get('LastStateTime'),
                'power': data.get('PowerState')}

    def _prepare_reboot_observation(self, node, state):
        before = self._boot_observation(node)
        verify = state['verify']
        verify['before'] = before
        verify['progress_supported'] = before['state'] not in (
            None, 'None', 'OEM')
        state['reboot_time'] = timeutils.utcnow().isoformat()

    def _sample_reboot(self, node, state):
        """Accumulate reset evidence; failed reads prove nothing."""
        observed = self._boot_observation(node)
        verify = state['verify']
        before = verify.get('before') or {}
        if observed['power'] in ('Off', 'PoweringOff', 'PoweringOn'):
            verify['new_boot_observed'] = True
        for marker in ('reset', 'state_time'):
            if (before.get(marker) and observed.get(marker)
                    and before[marker] != observed[marker]):
                verify['new_boot_observed'] = True
        if observed['state'] not in (None, 'None', 'OEM'):
            verify['progress_supported'] = True
            if before.get('state') and before['state'] != observed['state']:
                verify['new_boot_observed'] = True
        verify['observed'] = observed
        return observed

    def _report_step_error(self, task, error_msg, traceback=True):
        """Route a step error to the correct error handler.

        :param task: a TaskManager instance
        :param error_msg: the error message string
        :param traceback: whether to include traceback (default True)
        """
        task.node.set_driver_internal_info(
            async_steps.FIRMWARE_UPDATE_IN_PROGRESS, True)
        if task.node.clean_step:
            manager_utils.cleaning_error_handler(
                task, error_msg, traceback=traceback)
        elif task.node.service_step:
            manager_utils.servicing_error_handler(
                task, error_msg, traceback=traceback)
        elif task.node.deploy_step:
            manager_utils.deploying_error_handler(
                task, error_msg, traceback=traceback)
        else:
            LOG.error('No step type set on node %(node)s when attempting '
                      'to report firmware update error: %(error)s',
                      {'node': task.node.uuid, 'error': error_msg})
            node = task.node
            node.maintenance = True
            node.maintenance_reason = error_msg
            manager_utils.node_history_record(
                node, event=error_msg, error=True)
            node.save()

    def _resume_step(self, task):
        """Notify the conductor to resume the current step.

        :param task: a TaskManager instance
        """
        if task.node.clean_step:
            manager_utils.notify_conductor_resume_clean(task)
        elif task.node.service_step:
            manager_utils.notify_conductor_resume_service(task)
        elif task.node.deploy_step:
            manager_utils.notify_conductor_resume_deploy(task)

    def _continue_after_bmc(self, task, state, update_service):
        """Keep the verified BMC segment until its resources have recovered."""
        self._transition(task, state, STATE_VALIDATING_BMC)

    def _handle_validating_bmc(self, task, state, update_service):
        if self._run_lc_job_gate(task, state, False):
            return None
        if not self._validate_resources_stability(task.node):
            return None
        if state['bmc'].get('reboot_requested'):
            self._start_bmc_apply_reboot(task, state, state['settings'][0])
            return None
        return STATE_VERIFYING_INVENTORY

    def _handle_starting(self, task, state, update_service):
        if not self._nic_inventory_ready(task, state):
            return None
        try:
            self._start_next_segment(task, state, update_service)
        except Exception as exc:
            self._fail(task, state, _(
                'Failed to start the next firmware segment on node '
                '%(node)s: %(error)s') % {
                    'node': task.node.uuid, 'error': exc}, traceback=True)
        return None

    def _handle_verifying_inventory(self, task, state, update_service):
        # Late LC jobs still block completion while inventory settles.
        if self._run_lc_job_gate(task, state, self._state_timed_out(state)):
            state['entered_at'] = timeutils.utcnow().isoformat()
            return None
        if state.get('verify'):
            if self._run_boot_progress_gate(task, state, False):
                return None
        settings = state['settings'][:state['segment']['length']]
        wait = max([CONF.redfish.firmware_update_inventory_wait]
                   + [entry.get('wait', 0) for entry in settings])
        if self._seconds_since(state['entered_at']) < wait:
            return None
        inventory_options = {}
        if state.get('inventory_supported'):
            inventory_options['known_supported_types'] = (
                state['inventory_supported'])
        try:
            self.cache_firmware_components(
                task, required_components=[entry['component']
                                           for entry in settings],
                **inventory_options)
        except exception.UnsupportedDriverExtension:
            LOG.warning('Firmware inventory is unsupported on node %s',
                        task.node.uuid)
        return self._finish_segment(task, state)

    def _cleanup_staged(self, node, state=None):
        """Remove the firmware files staged for this update.

        ``firmware_utils.cleanup`` reads the staging back-ends from the
        ``firmware_cleanup`` driver_internal_info entry, which is shared
        with the Redfish management interface, so the list tracked in the
        state object is placed there only for the duration of the call.

        :param node: the node to clean up for
        :param state: the state object, or None when there is none (an
            update that failed while staging its very first file)
        """
        cleanup = (state or {}).get('cleanup')
        if not cleanup:
            firmware_utils.cleanup(node)
            return

        info = node.driver_internal_info
        had_entry = STAGED_CLEANUP in info
        previous = info.get(STAGED_CLEANUP)
        node.set_driver_internal_info(STAGED_CLEANUP, cleanup)
        try:
            firmware_utils.cleanup(node)
        finally:
            if had_entry:
                node.set_driver_internal_info(STAGED_CLEANUP, previous)
            else:
                node.del_driver_internal_info(STAGED_CLEANUP)

    def _clear_updates(self, node):
        """Clears firmware updates artifacts

        Clears the firmware update state from driver_internal_info and
        any files that were staged.

        Note that the caller must have an exclusive lock on the node.

        :param node: the node to clear the firmware updates from
        """
        state = node.driver_internal_info.get(FIRMWARE_UPDATE_STATE)
        try:
            self._cleanup_staged(node, state)
        except exception.IronicException:
            LOG.exception('Unable to remove staged firmware for node %s',
                          node.uuid)
        node.del_driver_internal_info(FIRMWARE_UPDATE_STATE)
        node.del_driver_internal_info(async_steps.FIRMWARE_UPDATE_IN_PROGRESS)
        # A node whose update was started before the upgrade to the state
        # object, and which failed before the periodic could migrate it,
        # still carries the old entries.
        for key in LEGACY_KEYS:
            node.del_driver_internal_info(key)
        node.save()

    def _migrate_legacy_state(self, node):
        """Fold pre-state-object driver_internal_info into the state.

        A conductor upgraded in the middle of a firmware update finds the
        node described by the old constellation of driver_internal_info
        entries. Which components of that update had already been staged
        on the BMC cannot be recovered from them reliably, so the update
        is moved into ``recovery_required``. A booted host cannot prove that
        every requested component was applied. Fail without a power action or
        another submission, retaining the component identities in the error.

        :param node: the Ironic node object
        :returns: True if a migration was performed, False otherwise
        """
        info = node.driver_internal_info
        if info.get(FIRMWARE_UPDATE_STATE):
            return False
        settings = info.get(LEGACY_UPDATES)
        if not settings:
            return False

        now = str(timeutils.utcnow().isoformat())
        state = {
            'version': STATE_VERSION,
            'state': STATE_RECOVERY_REQUIRED,
            'entered_at': now,
            'started_at': info.get(LEGACY_START_TIME) or now,
            'settings': settings,
            'cleanup': info.get(STAGED_CLEANUP),
            'grouping': False,
            'segment': {'length': len(settings), 'current': None,
                        'batched': False},
            'reboot_time': None,
            'bmc': None,
            'verify': None,
        }

        LOG.warning('A firmware update was in flight on node %(node)s '
                    'across the upgrade to the %(key)s '
                    'driver_internal_info entry. It is not resumed where '
                    'it left off: operator recovery is required because '
                    'application of all requested images cannot be proven.',
                    {'node': node.uuid, 'key': FIRMWARE_UPDATE_STATE})
        node.set_driver_internal_info(FIRMWARE_UPDATE_STATE, state)
        for key in LEGACY_KEYS:
            node.del_driver_internal_info(key)
        node.save()
        return True

    def _handle_recovery_required(self, task, state, update_service):
        components = ', '.join(entry.get('component', 'unknown')
                               for entry in state.get('settings', []))
        self._fail(task, state, _(
            'Cannot safely resume a legacy firmware update for %(components)s '
            'on node %(node)s. Inspect pending BMC tasks/jobs, remove staged '
            'images if appropriate, and retry the update after recovery.') % {
                'components': components, 'node': task.node.uuid}, note=False)

    @METRICS.timer('RedfishFirmware._query_update_failed')
    @periodics.node_periodic(
        purpose='checking if async update of firmware component failed',
        spacing=CONF.redfish.firmware_update_fail_interval,
        filters={'reserved': False, 'provision_state_in': [states.CLEANFAIL,
                 states.DEPLOYFAIL, states.SERVICEFAIL], 'maintenance': True},
        predicate_extra_fields=['driver_internal_info'],
        predicate=lambda n: (n.driver_internal_info.get(FIRMWARE_UPDATE_STATE)
                             or n.driver_internal_info.get(LEGACY_UPDATES)),
    )
    def _query_update_failed(self, task, manager, context):

        """Periodic job to check for failed firmware updates."""
        task.upgrade_lock()
        if (task.node.provision_state not in (
                states.CLEANFAIL, states.DEPLOYFAIL, states.SERVICEFAIL)
                or not task.node.maintenance):
            return
        # A firmware update failed. Discard any remaining firmware
        # updates so when the user takes the node out of
        # maintenance mode, pending firmware updates do not
        # automatically continue.
        LOG.error('Update firmware failed for node %(node)s. '
                  'Discarding remaining firmware updates.',
                  {'node': task.node.uuid})

        self._clear_updates(task.node)

    @METRICS.timer('RedfishFirmware._query_update_status')
    @periodics.node_periodic(
        purpose='checking async update of firmware component',
        spacing=CONF.redfish.firmware_update_status_interval,
        filters={'reserved': False, 'provision_state_in': [states.CLEANWAIT,
                 states.DEPLOYWAIT, states.SERVICEWAIT]},
        predicate_extra_fields=['driver_internal_info'],
        predicate=lambda n: (n.driver_internal_info.get(FIRMWARE_UPDATE_STATE)
                             or n.driver_internal_info.get(LEGACY_UPDATES)),
    )
    def _query_update_status(self, task, manager, context):
        """Periodic job to check firmware update tasks."""
        # The database selection can be stale by the time the lock is acquired.
        task.upgrade_lock()
        if task.node.provision_state not in (
                states.CLEANWAIT, states.DEPLOYWAIT, states.SERVICEWAIT):
            return
        self._check_node_redfish_firmware_update(task)

    def _task_messages(self, sushy_task):
        """Collect the human-readable messages of a Redfish task.

        :param sushy_task: the sushy task object
        :returns: a list of message strings, possibly empty
        """
        messages = []
        if sushy_task.messages and not sushy_task.messages[0].message:
            sushy_task.parse_messages()

        if sushy_task.messages is not None:
            for m in sushy_task.messages:
                msg = m.message
                if not msg or msg.lower() in ['unknown', 'unknown error']:
                    msg = m.message_id
                if msg:
                    messages.append(msg)
        return messages

    def _handle_task_completion(self, task, state, sushy_task, messages,
                                update_service):
        """Handle firmware update task completion.

        :param task: a TaskManager instance
        :param state: the state object
        :param sushy_task: the sushy task object
        :param messages: list of task messages
        :param update_service: the sushy firmware update service
        """
        node = task.node
        current_update = state['settings'][0]

        if (sushy_task.task_state == sushy.TASK_STATE_COMPLETED
                and sushy_task.task_status in
                [sushy.HEALTH_OK, sushy.HEALTH_WARNING]):
            current_update['task_success'] = True
            LOG.debug('Redfish task completed for node %(node)s, '
                      'firmware %(firmware_image)s: %(messages)s.',
                      {'node': node.uuid,
                       'firmware_image': current_update['url'],
                       'messages': ", ".join(messages)})

            component = current_update.get('component', '')
            component_type = redfish_utils.get_component_type(component)

            if component_type == redfish_utils.BMC:
                self._bmc_update_completion(task, state, update_service)
            else:
                self._continue_after_bmc(task, state, update_service)
        else:
            error_msg = (_('Firmware update failed for node %(node)s, '
                           'firmware %(firmware_image)s. '
                           'Error: %(errors)s') %
                         {'node': node.uuid,
                          'firmware_image': current_update['url'],
                          'errors': ",  ".join(messages)})

            self._fail(task, state, error_msg, note=False, traceback=True)

    def _bmc_wait_completed(self, task, state, update_service):
        """Poll task outcomes before accepting version-based completion."""
        return self._poll_bmc_update_task(task, state, update_service)

    def _check_overall_timeout(self, task):
        """Check if firmware update has exceeded overall timeout.

        :param task: A TaskManager instance
        :returns: True if timeout exceeded and error was handled,
                  False otherwise
        """
        node = task.node
        overall_timeout = CONF.redfish.firmware_update_overall_timeout
        if overall_timeout <= 0:
            return False

        state = node.driver_internal_info.get(FIRMWARE_UPDATE_STATE) or {}
        start_time_str = state.get('started_at')
        if not start_time_str:
            return False

        start_time = timeutils.parse_isotime(start_time_str)
        elapsed = timeutils.utcnow(True) - start_time
        if elapsed.total_seconds() < overall_timeout:
            return False

        msg = (_('Firmware update on node %(node)s has exceeded '
                 'the overall timeout of %(timeout)s seconds. '
                 'Elapsed time: %(elapsed)s seconds.')
               % {'node': node.uuid,
                  'timeout': overall_timeout,
                  'elapsed': int(elapsed.total_seconds())})
        LOG.error(msg)
        task.upgrade_lock()
        self._fail(task, state, msg)
        return True

    def _run_lc_job_gate(self, task, state, timed_out):
        """Run the Dell LC job gate.

        Skipped (treated as passed) on non-Dell nodes, and on Dell nodes
        whose job collection is unavailable. Otherwise the gate holds
        the phase until the LC job(s) applying the firmware are
        terminal.

        :param task: a TaskManager instance.
        :param state: the state object.
        :param timed_out: whether the verify phase timeout has elapsed.
        :returns: True if the caller should stop and return (the step
            failed, or another poll is needed); False if the phase
            should proceed to the BootProgress gate.
        """
        node = task.node
        verify = state.get('verify')
        if not redfish_utils.is_dell_node(node):
            if verify is not None:
                verify['lc'] = _VERIFY_SKIPPED
            return False

        timeout = CONF.redfish.firmware_update_post_reboot_verify_timeout
        jids = list(dict.fromkeys(self._segment_jids(state)
                                  + (verify['jids'] if verify else [])))
        status, detail = drac_fw.check_lc_jobs(
            task, jids, state['segment'].get('jobs'), required=True)
        state['last_error'] = detail
        if status == drac_fw.LC_JOBS_ERROR:
            msg = (_('Firmware update on node %(node)s failed: the '
                     'Dell Lifecycle Controller reported an error '
                     'applying firmware: %(detail)s')
                   % {'node': node.uuid, 'detail': detail})
            LOG.error(msg)
            self._fail(task, state, msg)
            return True
        if status == drac_fw.LC_JOBS_RUNNING:
            if timed_out:
                msg = (_('Firmware update on node %(node)s was '
                         'rebooted to apply firmware, but the Dell '
                         'Lifecycle Controller job(s) %(jids)s did '
                         'not finish within %(timeout)s seconds. '
                         'The node may still be applying firmware '
                         'during POST and must not be power-cycled '
                         'until the Lifecycle Controller job '
                         'finishes.')
                       % {'node': node.uuid,
                           'jids': ', '.join(jids),
                          'timeout': timeout})
                LOG.error(msg)
                self._fail(task, state, msg)
                return True
            LOG.debug('Dell Lifecycle Controller job(s) %(jids)s '
                      'still running for node %(node)s. Will check '
                      'again on next poll.',
                      {'jids': jids, 'node': node.uuid})
            self._persist(node, state)
            return True
        if status == drac_fw.LC_JOBS_UNAVAILABLE:
            LOG.warning('Cannot verify Dell Lifecycle Controller '
                        'job(s) for node %(node)s: %(detail)s. '
                        'Skipping the LC job gate.',
                        {'node': node.uuid, 'detail': detail})
            if verify is not None:
                verify['lc'] = _VERIFY_SKIPPED
        else:
            LOG.info('Dell Lifecycle Controller job(s) %(jids)s '
                     'finished for node %(node)s.',
                     {'jids': jids, 'node': node.uuid})
            if verify is not None:
                verify['lc'] = _VERIFY_PASSED
        return False

    def _os_running_wait_elapsed(self, state):
        """Whether the bounded wait for OSRunning is over.

        Measured from the first observation of a booted-OS state, not
        from the reboot: the node may have spent most of the phase
        applying firmware during POST before the OS started at all.
        Records that first observation as the deadline's anchor.

        :param state: the state object, whose verify state is updated
            in place with ``os_boot_started_at`` on the first call.
        :returns: True if the caller should stop waiting for OSRunning
            and proceed, False if it should keep polling.
        """
        elapsed, limit = self._deadline_elapsed(state,
                                                *_OS_RUNNING_DEADLINE)
        if limit <= 0:
            return True
        if elapsed is None:
            state['verify']['os_boot_started_at'] = str(
                timeutils.utcnow().isoformat())
            return False
        return elapsed.total_seconds() >= limit

    def _run_boot_progress_gate(self, task, state, timed_out):
        """Require new-boot readiness; only absent telemetry uses a timer."""
        node = task.node
        verify = state['verify']
        observed = self._sample_reboot(node, state)
        state['last_error'] = 'boot: %s, power: %s' % (
            observed['state'], observed['power'])
        if timed_out:
            raise exception.FirmwareUpdateFailed(error=_(
                'Timed out waiting for firmware reboot recovery on node '
                '%(node)s; %(status)s') % {
                    'node': node.uuid, 'status': state['last_error']})
        elapsed = self._seconds_since(state['reboot_time'])
        if (observed['power'] != 'On'
                or elapsed < CONF.redfish.firmware_update_reboot_min_wait):
            return True
        limited = node.driver_info.get(
            'firmware_update_boot_progress', 'auto') == 'limited'
        if limited or not verify.get('progress_supported'):
            before = verify.get('before') or {}
            if ((before.get('reset') or before.get('state_time'))
                    and not verify['new_boot_observed']):
                return True
            if elapsed < CONF.redfish.firmware_update_boot_check_delay:
                return True
            verify['boot'] = _VERIFY_SKIPPED
            return False
        targets = {target.value for target in
                   redfish_utils.get_boot_progress_targets(node)}
        if (verify['new_boot_observed'] and observed['state'] in targets):
            verify['boot'] = _VERIFY_PASSED
            return False
        if (node.service_step and verify['new_boot_observed']
                and observed['state'] in {
                    item.value for item in
                    redfish_utils.BOOT_PROGRESS_POST_COMPLETE}
                and CONF.redfish.firmware_update_os_running_timeout > 0
                and self._os_running_wait_elapsed(state)):
            raise exception.FirmwareUpdateFailed(error=_(
                'The host completed POST but did not report OSRunning within '
                'firmware_update_os_running_timeout. Configure limited boot '
                'progress only if this platform is known not to report it.'))
        return True

    def _finish_segment(self, task, state):
        """Complete the current segment and hand off if more remain.

        Drops the completed segment's components from the settings. If
        more components remain, hands off to :meth:`_start_next_segment`
        for the next segment (which might be a BMC update or another
        batch). Inventory has already been verified by the caller. Persist
        the next segment's start as a declared phase before submitting it.

        :param task: a TaskManager instance
        :param state: the state object
        """
        node = task.node
        settings = state['settings']
        segment = state['segment'] or {}
        run_length = segment.get('length', 0)

        if segment.get('batched'):
            LOG.info('Batch segment of %(count)d components completed for '
                     'node %(node)s.',
                     {'count': run_length, 'node': node.uuid})

        del settings[:run_length]
        state['segment'] = None
        state['reboot_time'] = None
        state['verify'] = None
        state['bmc'] = None

        if settings:
            LOG.info('%(remaining)d components remaining for node %(node)s. '
                     'Continuing with next segment.',
                     {'remaining': len(settings), 'node': node.uuid})
            return STATE_STARTING

        self._clear_updates(node)
        LOG.info('Firmware updates completed for node %(node)s',
                 {'node': node.uuid})
        self._resume_step(task)
        return None

    def _poll_bmc_update_task(self, task, state, update_service):
        """Poll the Redfish task of a BMC component being applied.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        :returns: None; this either stays in ``waiting_bmc`` or moves the
            update on through a nested call
        """
        node = task.node
        current_update = state['settings'][0]

        if not current_update.get('task_monitor'):
            if current_update.get('submitted'):
                return self._bmc_update_completion(task, state, update_service)
            raise exception.FirmwareUpdateFailed(error=_(
                'BMC submission outcome is unknown; inspect the BMC jobs '
                'before retrying the update.'))

        try:
            task_monitor = redfish_utils.get_task_monitor(
                node, current_update['task_monitor'])
        except exception.RedfishConnectionError as e:
            # If the BMC firmware is being updated, the BMC will be
            # unavailable for some amount of time.
            LOG.warning('Unable to communicate with task monitor service '
                        'on node %(node)s. Will try again on the next poll. '
                        'Error: %(error)s',
                        {'node': node.uuid, 'error': e})
            return None
        except (exception.RedfishTaskMonitorNotFound,
                sushy.exceptions.ResourceNotFoundError):
            LOG.warning('Firmware task disappeared for node %(node)s, '
                        'firmware %(firmware_image)s; checking completion '
                        'evidence.',
                        {'node': node.uuid,
                         'firmware_image': current_update['url']})
            self._bmc_update_completion(task, state, update_service)
            return None

        try:
            # The last response does not necessarily contain a Task,
            # so get it
            sushy_task = task_monitor.get_task()
            task_state = sushy_task.task_state
        except sushy.exceptions.ResourceNotFoundError:
            return self._bmc_update_completion(task, state, update_service)
        except Exception as e:
            LOG.warning('Unable to get task for node %(node)s: %(error)s. '
                        'Will retry on next poll.',
                        {'node': node.uuid, 'error': e})
            return None

        # Check if task is in a terminal state (completed, failed, etc.)
        # If so, proceed directly to completion handling
        if task_state not in [sushy.TASK_STATE_NEW,
                              sushy.TASK_STATE_RUNNING,
                              sushy.TASK_STATE_STARTING,
                              sushy.TASK_STATE_PENDING]:
            # Task is done (COMPLETED, EXCEPTION, KILLED, CANCELLED, etc.)
            # Parse messages and handle completion
            LOG.debug('Firmware update task in terminal state %(state)s '
                      'for node %(node)s',
                      {'state': task_state, 'node': node.uuid})

            # Only parse the messages if the BMC did not return parsed
            # messages
            messages = self._task_messages(sushy_task)

            self._handle_task_completion(task, state, sushy_task, messages,
                                         update_service)
            return None

        LOG.debug('Firmware update in progress for node %(node)s, '
                  'firmware %(firmware_image)s.',
                  {'node': node.uuid,
                   'firmware_image': current_update['url']})
        return None

    @METRICS.timer('RedfishFirmware._check_node_redfish_firmware_update')
    def _check_node_redfish_firmware_update(self, task):
        """Check the progress of running firmware update on a node.

        Loads the state object, enforces the overall timeout, and then
        runs the handler of the current state. A handler returns the
        state to move to next, which is validated and applied by
        :meth:`_transition` before that state's handler runs in turn, or
        None when the update stays where it is until the next poll.

        Per-state deadlines are declared in ``_STATE_TIMEOUTS`` and read
        by the handlers through :meth:`_state_elapsed` and
        :meth:`_state_timed_out`: unlike the overall timeout, expiry
        does not mean the same thing in every state (a gate that must
        now fail, a wait that is up), so each handler acts on its own
        deadline.

        :param task: a TaskManager instance
        """
        # Upgrade the lock to ensure we are using the latest info from
        # the node.
        task.upgrade_lock()
        node = task.node

        self._migrate_legacy_state(node)

        # Check overall timeout for firmware update operation
        if self._check_overall_timeout(task):
            return

        state = node.driver_internal_info.get(FIRMWARE_UPDATE_STATE)
        if not state:
            return
        if (state.get('version') != STATE_VERSION
                or state.get('state') not in _STATE_HANDLERS):
            self._fail(task, state, _(
                'Unsupported firmware update state or schema version on node '
                '%s. Inspect the BMC before recovering the update.')
                % node.uuid, note=False)
            return
        if state['state'] == STATE_RECOVERY_REQUIRED:
            self._handle_recovery_required(task, state, None)
            return

        segment = state.get('segment') or {}
        anchor = segment.get('started_at') or state['entered_at']
        segment_expired = (self._seconds_since(anchor)
                           >= CONF.redfish.firmware_update_apply_timeout)
        reboot_expired = (state['state'] in _REBOOTED_STATES
                          and self._state_timed_out(state))
        if segment_expired or reboot_expired:
            components = ', '.join(
                entry['component'] for entry in
                state['settings'][:segment.get('length', 1)])
            self._fail(task, state, _(
                'Firmware update timed out in state %(phase)s for '
                '%(components)s on node %(node)s. Last status: %(status)s; '
                'LC jobs: %(jobs)s. Check firmware_update_apply_timeout and '
                'firmware_update_post_reboot_verify_timeout before retrying.')
                % {'phase': state['state'], 'components': components,
                   'node': node.uuid, 'status': state.get('last_error'),
                   'jobs': (state.get('verify') or {}).get('jids', [])})
            return

        # The driver's fixed deadlines own these waits, including outages.
        # Agent callback timeouts must not race application monitoring.
        node.touch_provisioning()
        if state.get('verify') and state.get('reboot_time'):
            try:
                self._sample_reboot(node, state)
            except (exception.RedfishError,
                    sushy.exceptions.SushyError) as exc:
                state['last_error'] = str(exc)

        try:
            update_service = redfish_utils.get_update_service(node)
        except (exception.RedfishError, sushy.exceptions.SushyError) as e:
            # If the BMC firmware is being updated, the BMC will be
            # unavailable for some amount of time.
            LOG.warning('Unable to communicate with firmware update service '
                        'on node %(node)s. Will try again on the next poll. '
                        'Error: %(error)s',
                        {'node': node.uuid, 'error': e})
            state['last_error'] = str(e)
            self._persist(node, state)
            return

        # Bounded because every declared transition moves forward, so a
        # single poll can visit each state at most once.
        for _step in range(len(_STATE_HANDLERS)):
            handler = getattr(self, _STATE_HANDLERS[state['state']])
            try:
                new_state = handler(task, state, update_service)
            except exception.FirmwareUpdateFailed as exc:
                self._fail(task, state, str(exc))
                return
            except (exception.RedfishError,
                    sushy.exceptions.SushyError) as exc:
                state['last_error'] = str(exc)
                LOG.warning('Cannot monitor firmware update on node '
                            '%(node)s in state %(state)s: %(error)s. '
                            'Retrying on the next poll.',
                            {'node': node.uuid, 'state': state['state'],
                             'error': exc})
                self._persist(node, state)
                return
            if new_state is None:
                if (node.driver_internal_info.get(FIRMWARE_UPDATE_STATE)
                        is state):
                    self._persist(node, state)
                return
            self._transition(task, state, new_state)

    def _handle_staging(self, task, state, update_service):
        """Poll the component being staged and advance when it is staged.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        :returns: the next state, or None to stay in ``staging``
        """
        node = task.node
        settings = state['settings']
        current_idx = state['segment']['current']
        fw_upd = settings[current_idx]
        component = fw_upd.get('component', '')
        monitor_uri = fw_upd.get('task_monitor')

        if not monitor_uri and not fw_upd.get('submitted'):
            self._fail(task, state, _(
                'No task monitor was recorded for component %(component)s '
                'on node %(node)s. Submission outcome is unknown; inspect '
                'the BMC jobs before retrying.') % {
                    'component': component, 'node': node.uuid})
            return None
        if fw_upd.get('staged'):
            return self._advance_staging(task, state)
        sushy_task = None
        if monitor_uri:
            try:
                monitor = redfish_utils.get_task_monitor(node, monitor_uri)
                sushy_task = monitor.get_task()
            except (exception.RedfishTaskMonitorNotFound,
                    sushy.exceptions.ResourceNotFoundError):
                LOG.warning('Staging task disappeared for %(component)s on '
                            'node %(node)s; checking available OEM evidence.',
                            {'component': component, 'node': node.uuid})
        if sushy_task is not None:
            active = (sushy.TASK_STATE_NEW, sushy.TASK_STATE_PENDING,
                      sushy.TASK_STATE_RUNNING, sushy.TASK_STATE_STARTING)
            successful = (sushy_task.task_state == sushy.TASK_STATE_COMPLETED
                          and sushy_task.task_status in (
                              sushy.HEALTH_OK, sushy.HEALTH_WARNING))
            if not successful and sushy_task.task_state not in active:
                self._fail_component(task, state, fw_upd, sushy_task)
                return None
            if successful:
                fw_upd['task_success'] = True
        if self._staging_ready(task, state, fw_upd, sushy_task):
            fw_upd['staged'] = True
            return self._advance_staging(task, state)
        return None

    def _staging_ready(self, task, state, entry, sushy_task):
        """Queue armed Dell jobs; otherwise wait for staging completion."""
        if redfish_utils.is_dell_node(task.node):
            tracking = state['segment'].get('jobs')
            if tracking is None:
                raise exception.FirmwareUpdateFailed(error=_(
                    'Dell job baseline is missing; recover the update before '
                    'submitting further firmware.'))
            status, detail = drac_fw.check_staged_update(task, entry, tracking)
            state['last_error'] = detail
            if status == drac_fw.LC_JOBS_ERROR:
                raise exception.FirmwareUpdateFailed(error=detail)
            if status != drac_fw.LC_JOBS_UNAVAILABLE:
                return status in (drac_fw.LC_JOBS_STAGED, drac_fw.LC_JOBS_DONE)
        if self._positive_task_outcome(entry):
            return True
        if sushy_task is None:
            state['last_error'] = (
                'Unknown staging outcome for %s: task disappeared before '
                'success was observed' % entry['component'])
            return False
        bios_compatibility = (
            redfish_utils.get_component_type(entry['component'])
            == redfish_utils.BIOS
            and task.node.driver_info.get('firmware_update_bios_pending_reset')
            == 'compatibility')
        if (not state.get('grouping')
                and (sushy_task.task_state == sushy.TASK_STATE_STARTING
                     or (bios_compatibility and sushy_task.task_state in (
                         sushy.TASK_STATE_PENDING,
                         sushy.TASK_STATE_RUNNING)))):
            # Preserve single-component updates that need a reset to start.
            # Starting alone is insufficient evidence to queue another image.
            started = entry.setdefault('starting_at',
                                       timeutils.utcnow().isoformat())
            return (self._seconds_since(started)
                    >= CONF.redfish.firmware_update_nic_starting_wait)
        return False

    def _positive_task_outcome(self, entry):
        """A missing monitor alone is never evidence of a successful update."""
        return entry.get('synchronous') or entry.get('task_success')

    def _recover_task_outcome(self, task, state, entry):
        if self._positive_task_outcome(entry):
            return True
        if (redfish_utils.get_component_type(entry['component'])
                == redfish_utils.BMC and entry.get('version_changed')):
            return True
        if redfish_utils.is_dell_node(task.node):
            status, detail = drac_fw.check_lc_jobs(
                task, entry.get('jids', []), state['segment'].get('jobs'),
                required=True)
            state['last_error'] = detail
            if status == drac_fw.LC_JOBS_ERROR:
                raise exception.FirmwareUpdateFailed(error=detail)
            return status == drac_fw.LC_JOBS_DONE
        state['last_error'] = (
            'Unknown application outcome for %s: task disappeared before '
            'success was observed' % entry['component'])
        return False

    def _advance_staging(self, task, state):
        """Submit the next component, or reboot once all are staged.

        :param task: a TaskManager instance
        :param state: the state object
        :returns: None; the consolidated reboot enters ``rebooting``
            itself so that the poll ends with the reboot issued
        """
        node = task.node
        settings = state['settings']
        segment = state['segment']
        next_idx = segment['current'] + 1
        run_length = segment['length']

        if next_idx >= run_length:
            self._start_batched_reboot(task, state)
            return None

        try:
            update_service = redfish_utils.get_update_service(node)
        except exception.RedfishError as e:
            error_msg = (
                _('Failed to get update service for node %(node)s '
                  'while advancing batch: %(error)s')
                % {'node': node.uuid, 'error': e})
            LOG.error(error_msg)
            self._fail(task, state, error_msg, traceback=True)
            return None

        try:
            # Persist intent before the POST. A lost response must not cause
            # the same image to be submitted again on conductor restart.
            if segment.get('jobs') is not None:
                before = drac_fw.snapshot_lc_jobs(task)
                if segment['jobs']['supported'] and not before['supported']:
                    raise exception.RedfishError(error=_(
                        'Previously supported Dell jobs are unavailable'))
                settings[next_idx]['jobs_before'] = before['baseline']
            segment['current'] = next_idx
            self._persist(node, state)
            self._submit_one_batched_component(
                node, state, update_service, next_idx)
        except Exception as e:
            error_msg = (
                _('Batched firmware submission failed at component '
                  '%(component)s (%(idx)d/%(total)d) for node '
                  '%(node)s. Error: %(error)s')
                % {'component': settings[next_idx].get('component', ''),
                   'idx': next_idx + 1, 'total': run_length,
                   'node': node.uuid, 'error': e})
            LOG.error(error_msg)
            self._fail(task, state, error_msg, traceback=True)
            return None

        segment['current'] = next_idx
        self._persist(node, state)
        LOG.info('Submitted component %(idx)d/%(total)d for node '
                 '%(node)s. Polling for staging completion.',
                 {'idx': next_idx + 1, 'total': run_length,
                  'node': node.uuid})
        return None

    def _start_batched_reboot(self, task, state):
        """Issue the consolidated apply reboot for a staged segment.

        :param task: a TaskManager instance
        :param state: the state object
        """
        node = task.node
        settings = state['settings']
        segment = state['segment']
        run_length = segment['length']
        if not self._can_reboot_segment(task, state):
            return

        # Gather the JID set of the whole segment before the
        # consolidated reboot: the applying state pops 'task_monitor'
        # from each entry as its task completes, so this is the only
        # opportunity to record them.
        jids = self._segment_jids(state)

        state['verify'] = self._build_verify(jids)
        self._prepare_reboot_observation(node, state)
        segment['current'] = None

        LOG.info('All %(count)d batch components staged for node '
                 '%(node)s. Triggering consolidated reboot.',
                 {'count': run_length, 'node': node.uuid})
        deploy_utils.set_async_step_flags(
            node, reboot=True, polling=True)
        self._transition(task, state, STATE_REBOOTING)
        power_timeout = settings[0].get('power_timeout', 0)
        manager_utils.node_power_action(task, states.REBOOT,
                                        power_timeout)

    def _handle_rebooting(self, task, state, update_service):
        """Start polling the segment's tasks after the apply reboot.

        The state is where the update sits between the reboot being
        issued and the next poll: it anchors ``reboot_time``, which the
        gates measure from, and records the transition.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        :returns: ``applying``
        """
        return STATE_APPLYING

    def _handle_applying(self, task, state, update_service):
        """Poll every task monitor of the segment until all are terminal.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        :returns: ``verifying_apply`` once every task is terminal, None
            to stay
        """
        node = task.node
        settings = state['settings']
        run_length = state['segment']['length']
        completed = 0
        still_running = 0
        polled = 0
        offset = state['segment'].get('poll_index', 0)
        indices = list(range(offset, run_length)) + list(range(offset))

        for index in indices:
            fw_upd = settings[index]
            monitor_uri = fw_upd.get('task_monitor')
            if not monitor_uri:
                if self._recover_task_outcome(task, state, fw_upd):
                    completed += 1
                continue
            if polled >= CONF.redfish.firmware_update_tasks_per_poll:
                still_running += 1
                continue
            polled += 1
            state['segment']['poll_index'] = (index + 1) % run_length

            try:
                task_monitor = redfish_utils.get_task_monitor(
                    node, monitor_uri)
                sushy_task = task_monitor.get_task()
            except exception.RedfishConnectionError as e:
                LOG.warning('Unable to reach task monitor for %(component)s '
                            'on node %(node)s: %(error)s. Will retry.',
                            {'component': fw_upd.get('component', ''),
                             'node': node.uuid, 'error': e})
                still_running += 1
                break
            except (exception.RedfishTaskMonitorNotFound,
                    sushy.exceptions.ResourceNotFoundError):
                if self._recover_task_outcome(task, state, fw_upd):
                    fw_upd.pop('task_monitor', None)
                    completed += 1
                continue
            except Exception as e:
                LOG.warning('Unable to get task for %(component)s on node '
                            '%(node)s: %(error)s. Will retry.',
                            {'component': fw_upd.get('component', ''),
                             'node': node.uuid, 'error': e})
                state['last_error'] = str(e)
                still_running += 1
                break

            # Active tasks must finish after the apply reset, even if a
            # compatibility policy authorized that reset during staging.
            if sushy_task.task_state in [sushy.TASK_STATE_NEW,
                                         sushy.TASK_STATE_RUNNING,
                                         sushy.TASK_STATE_STARTING,
                                         sushy.TASK_STATE_PENDING]:
                still_running += 1
                continue

            if (sushy_task.task_state == sushy.TASK_STATE_COMPLETED
                    and sushy_task.task_status in
                    [sushy.HEALTH_OK, sushy.HEALTH_WARNING]):
                fw_upd['task_success'] = True
                fw_upd.pop('task_monitor', None)
                completed += 1
                continue

            self._fail_component(task, state, fw_upd, sushy_task)
            return None

        LOG.debug('Batched firmware update progress for node %(node)s: '
                  '%(completed)d/%(total)d completed, '
                  '%(running)d still running',
                  {'node': node.uuid, 'completed': completed,
                   'total': run_length, 'running': still_running})

        if completed == run_length:
            # Every task in this segment is terminal. Do not finish
            # (cache firmware components and resume) until every
            # available gate -- LC job, BootProgress -- passes for the
            # whole segment.
            return STATE_VERIFYING_APPLY

        self._persist(node, state)
        return None

    def _handle_verifying_apply(self, task, state, update_service):
        """Run the Dell LC job gate.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        :returns: ``verifying_boot`` once the gate passes, None to stay
        """
        if state['verify'] is None:
            raise exception.FirmwareUpdateFailed(error=_(
                'Firmware application evidence is missing. Inspect BMC '
                'jobs before recovering this interrupted update.'))

        if self._run_lc_job_gate(task, state, self._state_timed_out(state)):
            return None
        return STATE_VERIFYING_BOOT

    def _handle_verifying_boot(self, task, state, update_service):
        """Run the BootProgress gate, then finish the segment.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        :returns: None; the segment either finishes the step or starts
            the next segment itself
        """
        if self._run_boot_progress_gate(task, state,
                                        self._state_timed_out(state)):
            return None
        return STATE_VERIFYING_INVENTORY

    def _handle_waiting_bmc(self, task, state, update_service):
        """Advance a BMC component by one poll.

        A BMC segment alternates between waiting (for the update itself,
        then between version checks) and polling: either the Redfish task
        of the update, or the reported BMC version.

        :param task: a TaskManager instance
        :param state: the state object
        :param update_service: the sushy firmware update service
        :returns: None; the segment moves itself on through nested calls
        """
        node = task.node
        current_update = state['settings'][0]
        if self._run_lc_job_gate(task, state, False):
            return None
        elapsed, wait_interval = self._state_elapsed(state)

        if elapsed is None or wait_interval is None:
            return self._poll_bmc_update_task(task, state, update_service)

        if elapsed.total_seconds() >= wait_interval:
            LOG.debug('Finished waiting after firmware update '
                      '%(firmware_image)s on node %(node)s. '
                      'Elapsed time: %(seconds)s seconds',
                      {'firmware_image': current_update['url'],
                       'node': node.uuid,
                       'seconds': elapsed.seconds})
            current_update.pop('wait', None)
            state['bmc']['wait_start'] = None

            return self._bmc_wait_completed(task, state, update_service)

        LOG.debug('Continuing to wait after firmware update '
                  '%(firmware_image)s on node %(node)s. '
                  'Elapsed time: %(seconds)s seconds',
                  {'firmware_image': current_update['url'],
                   'node': node.uuid,
                   'seconds': elapsed.seconds})
        return None

    def _fail_component(self, task, state, fw_upd, sushy_task):
        """Fail the step because one component's Redfish task failed.

        :param task: a TaskManager instance
        :param state: the state object
        :param fw_upd: the settings dict whose task failed
        :param sushy_task: the failed sushy task object
        """
        messages = []
        if sushy_task.messages:
            if not sushy_task.messages[0].message:
                sushy_task.parse_messages()
            for m in sushy_task.messages:
                msg = m.message
                if not msg or msg.lower() in ['unknown', 'unknown error']:
                    msg = m.message_id
                if msg:
                    messages.append(msg)

        error_msg = (
            _('Batched firmware update failed for component '
              '%(component)s on node %(node)s. Error: %(errors)s')
            % {'component': fw_upd.get('component', ''),
               'node': task.node.uuid,
               'errors': ', '.join(messages)})
        LOG.error(error_msg)
        self._fail(task, state, error_msg, exclude=fw_upd, traceback=True)

    def _stage_firmware_file(self, node, component_update, state=None):
        """Make the firmware image reachable by the BMC.

        :param node: the Ironic node object
        :param component_update: a single firmware update settings dict
        :param state: the state object, used only to clean up already
            staged files if this staging fails
        :returns: a tuple (url the BMC should fetch, the staging
            back-end to clean up afterwards or None)
        """
        try:
            url = component_update['url']
            name = component_update['component']
            parsed_url = urlparse(url)
            scheme = parsed_url.scheme.lower()
            source = (CONF.redfish.firmware_source).lower()

            # Keep it simple, in further processing TLS does not matter
            if scheme == 'https':
                scheme = 'http'

            # If source and scheme is HTTP, then no staging,
            # returning original location
            if scheme == 'http' and source == scheme:
                LOG.debug('For node %(node)s serving firmware for '
                          '%(component)s from original location %(url)s',
                          {'node': node.uuid, 'component': name, 'url': url})
                return url, None

            # If source and scheme is Swift, then not moving, but
            # returning Swift temp URL
            if scheme == 'swift' and source == scheme:
                temp_url = firmware_utils.get_swift_temp_url(parsed_url)
                LOG.debug('For node %(node)s serving original firmware at '
                          'for %(component)s at %(url)s via Swift temporary '
                          'url %(temp_url)s',
                          {'node': node.uuid, 'component': name, 'url': url,
                           'temp_url': temp_url})
                return temp_url, None

            # For remaining, download the image to temporary location
            temp_file = firmware_utils.download_to_temp(node, url)

            return firmware_utils.stage(node, source, temp_file)

        except exception.IronicException:
            self._cleanup_staged(node, state)
            raise
