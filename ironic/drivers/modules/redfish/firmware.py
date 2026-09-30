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

# Keep the existing sequential queue. Only its first entry can be submitted.
# Its completion evidence and recovery timestamps live with that component;
# subsequent entries remain untouched until verification permits continuation.
_VERIFY_PENDING = 'pending'
_VERIFY_PASSED = 'passed'
_VERIFY_SKIPPED = 'skipped'


class RedfishFirmware(base.FirmwareInterface):

    _FW_SETTINGS_ARGSINFO = {
        'settings': {
            'description': (
                'A list of dicts with firmware components to be updated. '
                'A non-BMC wait is a minimum post-application settling '
                'interval. BMC wait controls version verification.'
            ),
            'required': True
        }
    }

    def get_properties(self):
        """Return the properties of the interface."""
        properties = redfish_utils.COMMON_PROPERTIES.copy()
        properties['firmware_update_boot_progress'] = _(
            'Boot progress policy: auto requires reported readiness; '
            'limited uses firmware_update_boot_check_delay for hardware known '
            'to report only intermediate boot stages. Neither policy skips '
            'supported firmware job or reset checks.')
        properties['firmware_update_bios_pending_reset'] = _(
            'BIOS reset policy: auto waits for Pending and Running tasks; '
            'compatibility permits a BIOS task in Starting, Pending or '
            'Running to receive its apply reset after the staging wait. Use '
            'compatibility only for platforms documented to keep these task '
            'states until reset, rather than while flashing.')
        return properties

    def validate(self, task):
        """Validate driver information needed by the Redfish driver."""
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
                                  known_supported_types=(), nic_bindings=None):
        """Store or update Firmware Components on the given node.

        :param task: a TaskManager instance.
        :param required_components: component names whose supported inventory
            must be readable before updating the cache. Ordinary discovery
            remains best effort; update verification retries transient errors.
        :param known_supported_types: previously observed capabilities that
            must not be reclassified as unsupported during recovery.
        :param nic_bindings: requested NIC aliases mapped to adapter identities
            captured before submission. Serial uniqueness is invalidated in
            place if recovery exposes duplicates, for the caller to persist.
            Identity metadata is not cached in the firmware component table.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support getting Firmware Components from bare metal.
        """
        node_id = task.node.id
        settings = []
        required_types = {redfish_utils.get_component_type(component)
                          for component in required_components}
        unsupported_types = set()
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
        nic_identities = []
        try:
            if redfish_utils.NIC in required_types:
                nic_identities, nic_components = self._read_nic_inventory(
                    task, system, strict=True)
            else:
                nic_components = self.retrieve_nic_components(task, system)
        except exception.UnsupportedDriverExtension:
            if redfish_utils.NIC in known_supported_types or nic_bindings:
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
            LOG.warning('Unable to access NetworkAdapters on node '
                        '%(node_uuid)s, Error: %(error)s',
                        {'node_uuid': task.node.uuid, 'error': e})
        if nic_components == []:
            LOG.debug('Could not retrieve Firmware Package Version from '
                      'NetworkAdapters on node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
        elif nic_components:
            settings.extend({key: value for key, value in entry.items()
                             if key != '_identity'}
                            for entry in nic_components)

        self._verify_inventory(settings, nic_components or [],
                               required_components, nic_bindings or {},
                               unsupported_types, nic_identities)

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

    def _verify_inventory(self, settings, nics, required, bindings,
                          unsupported, identities):
        """Check requested availability before publishing a partial cache."""
        available = {entry['component'] for entry in settings}
        if any(redfish_utils.get_component_type(component) == redfish_utils.NIC
               for component in required):
            available -= {entry['component'] for entry in nics}
            available.update(self._available_nic_components(
                nics, identities, required, bindings))
        missing = [component for component in required
                   if component not in available
                   and redfish_utils.get_component_type(component)
                   not in unsupported]
        if missing:
            raise exception.RedfishError(error=_(
                'Firmware inventory is not yet available for: %s')
                % ', '.join(missing))

    def _nic_identity(self, adapter):
        return {'id': adapter.identity,
                'serial': adapter.serial_number or None,
                'uri': getattr(adapter, 'path', None)}

    def _nic_aliases(self, identity):
        return {redfish_utils.NIC_COMPONENT_PREFIX + value
                for value in (identity['id'], identity['serial']) if value}

    def _same_nic(self, expected, actual):
        if expected.get('serial'):
            if expected['serial'] != actual['serial']:
                return False
            if (expected.get('serial_unique', True)
                    and actual.get('serial_unique', False)):
                return True
        if expected.get('uri'):
            return expected['uri'] == actual['uri']
        return expected['id'] == actual['id']

    def _available_nic_components(self, inventory, identities, required,
                                  bindings):
        """Resolve identity against all adapters, then check its version."""
        available = set()
        readable = [entry['_identity'] for entry in inventory]
        for requested in required:
            binding = bindings.get(requested)
            if binding and binding.get('serial') and any(
                    identity['serial'] == binding['serial']
                    and not identity['serial_unique']
                    for identity in identities):
                # Once duplicates are observed, a later partial enumeration
                # cannot restore serial-only trust for this in-flight update.
                binding['serial_unique'] = False
            matches = [identity for identity in identities
                       if (self._same_nic(binding, identity) if binding
                           else requested in self._nic_aliases(identity))]
            if len(matches) == 1 and matches[0] in readable:
                available.add(requested)
        return available

    def retrieve_nic_components(self, task, system, strict=False,
                                include_identifiers=False):
        """Read NIC inventory, propagating failures when strict."""
        _identities, components = self._read_nic_inventory(
            task, system, strict)
        if include_identifiers:
            return components
        return [{key: value for key, value in entry.items()
                 if key != '_identity'} for entry in components]

    def _read_nic_inventory(self, task, system, strict=False):
        """Return all adapter identities and their version-bearing entries.

        Identity and serial uniqueness use the complete enumeration, including
        adapters with no controllers or readable versions. Those adapters must
        not disappear from alias resolution during firmware recovery.
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
            return [], nic_list
        try:
            network_adapters = chassis.network_adapters
            if network_adapters is None:
                if strict:
                    raise exception.UnsupportedDriverExtension(
                        'NetworkAdapters is not supported')
                LOG.debug('NetworkAdapters not available on chassis for '
                          'node %(node_uuid)s',
                          {'node_uuid': task.node.uuid})
                return [], nic_list
        except sushy.exceptions.MissingAttributeError:
            if strict:
                raise exception.UnsupportedDriverExtension(
                    'NetworkAdapters is not supported')
            LOG.debug('NetworkAdapters not available on chassis for '
                      'node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
            return [], nic_list

        adapters = network_adapters.get_members()
        serials = [adapter.serial_number for adapter in adapters]
        identities = []
        for net_adp in adapters:
            identity = self._nic_identity(net_adp)
            identity['serial_unique'] = bool(identity['serial']) and (
                serials.count(identity['serial']) == 1)
            identities.append(identity)
            for net_adp_ctrl in net_adp.controllers:
                fw_pkg_v = net_adp_ctrl.firmware_package_version
                if not fw_pkg_v:
                    continue
                if identity['serial_unique']:
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
                component = {
                    'component': (redfish_utils.NIC_COMPONENT_PREFIX
                                  + net_adp_id),
                    'current_version': fw_pkg_v,
                    'vendor': net_adp.manufacturer,
                    'model': net_adp.model,
                    'serial_number': net_adp.serial_number,
                }
                component['_identity'] = identity
                nic_list.append(component)
        return identities, nic_list

    @METRICS.timer('RedfishFirmware.update')
    @base.deploy_step(priority=0, abortable=False,
                      argsinfo=_FW_SETTINGS_ARGSINFO)
    @base.clean_step(priority=0, abortable=False,
                     argsinfo=_FW_SETTINGS_ARGSINFO,
                     requires_ramdisk=True)
    @base.service_step(priority=0, abortable=False,
                       argsinfo=_FW_SETTINGS_ARGSINFO,
                       requires_ramdisk=False)
    def update(self, task, settings):
        """Update firmware sequentially, verifying each component before next.

        :param task: a TaskManager instance.
        :param settings: component/url dictionaries in submission order.
        :returns: the asynchronous wait state for the current step.
        """
        firmware_utils.validate_firmware_interface_update_args(settings)
        self._validate_boot_policy(task.node)
        node = task.node
        update_service = redfish_utils.get_update_service(node)
        node.set_driver_internal_info('redfish_fw_update_start_time',
                                      timeutils.utcnow().isoformat())
        self._persist(node, settings)
        deploy_utils.set_async_step_flags(node, reboot=False, polling=True)
        self._start_component(task, update_service, settings)
        return async_steps.get_return_state(node)

    def _persist(self, node, settings):
        """Save the complete queue while the caller holds the node lock.

        Read-only polling changes are saved before the callback returns.
        Hardware-action intent must be complete and saved before the action;
        preparation failures must never publish a partial intent record.
        """
        node.set_driver_internal_info('redfish_fw_updates', settings)
        node.set_driver_internal_info(
            async_steps.FIRMWARE_UPDATE_IN_PROGRESS, True)
        node.save()

    def _system_vendor(self, node, update):
        """Retain discovery for this update; failed reads remain retryable."""
        if 'vendor' not in update:
            update['vendor'] = redfish_utils.get_system_vendor(node)
        return update['vendor']

    def _is_dell_node(self, node, update):
        return 'dell' in self._system_vendor(node, update).lower().split()

    def _start_component(self, task, update_service, settings):
        """Prepare only the head of the queue; persist intent before POST."""
        node = task.node
        update = settings[0]
        update.setdefault('started_at', timeutils.utcnow().isoformat())
        if 'inventory_supported' not in update:
            update['inventory_supported'] = sorted({
                redfish_utils.get_component_type(component.component)
                for component in objects.FirmwareComponentList.get_by_node_id(
                    task.context, node.id)
                if redfish_utils.get_component_type(component.component)})
        self._persist(node, settings)
        try:
            if not self._nic_inventory_ready(task, update):
                self._persist(node, settings)
                return
            tracking = (drac_fw.snapshot_lc_jobs(task)
                        if self._is_dell_node(node, update) else None)
            update['jobs'] = tracking
            if tracking is not None:
                update['jobs_before'] = list(tracking['baseline'])
            if (redfish_utils.get_component_type(update['component'])
                    == redfish_utils.BMC):
                wait = update.get('wait',
                                  CONF.redfish.firmware_update_reboot_delay)
                update['bmc'] = {
                    'version_before': self._get_current_bmc_version(node),
                    'check_start': timeutils.utcnow().isoformat(),
                    'check_timeout': wait,
                    'wait_start': timeutils.utcnow().isoformat(),
                    'checking': False,
                    'reboot_requested': False,
                }
                update['wait'] = wait
            deploy_utils.set_async_step_flags(node, reboot=False, polling=True)
            update['submission_started'] = True
            self._persist(node, settings)
            self._submit_simple_update(node, update_service, update, settings)
            self._persist(node, settings)
        except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
            update['last_error'] = str(exc)
            self._persist(node, settings)
            if update.get('submission_started'):
                raise

    def _nic_inventory_ready(self, task, update):
        """Bind a readable NIC's ID/serial aliases before its submission."""
        if (redfish_utils.get_component_type(update['component'])
                != redfish_utils.NIC):
            return True
        vendor = self._system_vendor(task.node, update).lower()
        hpe = bool({'hp', 'hpe'} & set(vendor.split()) or 'hewlett' in vendor)
        error = ''
        try:
            system = redfish_utils.get_system(task.node)
            chassis = redfish_utils.get_chassis(task.node, system)
            try:
                collection = chassis.network_adapters
            except sushy.exceptions.MissingAttributeError:
                collection = None
            if collection is None:
                if (not hpe and redfish_utils.NIC not in update.get(
                        'inventory_supported', [])):
                    return True
                adapters = []
            else:
                adapters = collection.get_members()
                supported = update.setdefault('inventory_supported', [])
                if redfish_utils.NIC not in supported:
                    supported.append(redfish_utils.NIC)
            if adapters:
                identities = [self._nic_identity(adapter)
                              for adapter in adapters]
                matches = [identity for identity in identities
                           if update['component']
                           in self._nic_aliases(identity)]
                if len(matches) != 1:
                    raise exception.InvalidParameterValue(_(
                        'NIC component %s does not identify exactly one '
                        'adapter in NetworkAdapters') % update['component'])
                identity = dict(matches[0])
                serial = identity['serial']
                identity['serial_unique'] = bool(serial) and sum(
                    item['serial'] == serial for item in identities) == 1
                update['nic_identity'] = identity
                return True
        except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
            error = str(exc)
        update['last_error'] = (
            'NetworkAdapters are not readable for NIC staging. ' + error)
        if hpe:
            update['last_error'] += (
                ' Keep the instance OS running during servicing, or boot IPA '
                'before day-0 firmware updates.')
        return False

    def _submit_simple_update(self, node, update_service, update, settings):
        """Submit the current image and retain positive response evidence."""
        update['power_timeout'] = CONF.redfish.firmware_update_reboot_delay
        systems = redfish_utils.get_system_collection(node)
        targets = ([node.driver_info.get('redfish_system_id')]
                   if len(systems.members_identities) > 1 else None)
        component_url, cleanup = self._stage_firmware_file(node, update)
        if cleanup:
            backends = node.driver_internal_info.get('firmware_cleanup') or []
            if cleanup not in backends:
                backends.append(cleanup)
            node.set_driver_internal_info('firmware_cleanup', backends)
            self._persist(node, settings)
        try:
            if targets is not None:
                monitor = update_service.simple_update(component_url,
                                                       targets=targets)
            else:
                monitor = update_service.simple_update(component_url)
        except sushy.exceptions.MissingAttributeError as exc:
            raise exception.RedfishError(error=exc)
        update['task_monitor'] = monitor.task_monitor_uri
        update['submitted'] = True
        update['synchronous'] = not bool(monitor.task_monitor_uri)
        jid = self._jid_from_task_monitor(monitor.task_monitor_uri)
        if jid:
            update['jids'] = [jid]
        return monitor.task_monitor_uri

    def _jid_from_task_monitor(self, task_monitor):
        """Extract a Dell LC job identity from its task-monitor URI."""
        jid = (task_monitor.rstrip('/').rsplit('/', 1)[-1]
               if task_monitor else '')
        return jid if jid.startswith('JID_') else ''

    def _positive_task_outcome(self, entry):
        """A missing monitor alone is never evidence of a successful update."""
        return entry.get('synchronous') or entry.get('task_success')

    def _task_messages(self, sushy_task):
        messages = []
        if sushy_task.messages and not sushy_task.messages[0].message:
            sushy_task.parse_messages()
        if sushy_task.messages is not None:
            for message in sushy_task.messages:
                text = message.message
                if not text or text.lower() in ['unknown', 'unknown error']:
                    text = message.message_id
                if text:
                    messages.append(text)
        return messages

    def _read_update_task(self, task, update):
        """Read an outcome without turning a disappeared task into success."""
        uri = update.get('task_monitor')
        if not uri:
            return None
        try:
            monitor = redfish_utils.get_task_monitor(task.node, uri)
            result = monitor.get_task()
        except (exception.RedfishTaskMonitorNotFound,
                sushy.exceptions.ResourceNotFoundError):
            return None
        except (TypeError, ValueError) as exc:
            # Decode/representation errors are retryable in every phase, but
            # never mean that the Task disappeared or completed successfully.
            raise exception.RedfishError(error=exc)
        successful = (result.task_state == sushy.TASK_STATE_COMPLETED
                      and result.task_status in (
                          sushy.HEALTH_OK, sushy.HEALTH_WARNING))
        active = (sushy.TASK_STATE_NEW, sushy.TASK_STATE_PENDING,
                  sushy.TASK_STATE_RUNNING, sushy.TASK_STATE_STARTING)
        if successful:
            update['task_success'] = True
        elif result.task_state not in active:
            raise exception.FirmwareUpdateFailed(error=_(
                'Firmware update failed for component %(component)s on '
                'node %(node)s: %(messages)s') % {
                    'component': update['component'], 'node': task.node.uuid,
                    'messages': ', '.join(self._task_messages(result))})
        return result

    def _staging_ready(self, task, update, sushy_task):
        """Check armed Dell jobs or the single-image compatibility policy."""
        if self._is_dell_node(task.node, update):
            tracking = update.get('jobs')
            if tracking is None:
                raise exception.FirmwareUpdateFailed(error=_(
                    'Dell job baseline is missing; recover the update before '
                    'submitting further firmware.'))
            status, detail = drac_fw.check_staged_update(
                task, update, tracking)
            update['last_error'] = detail
            if status == drac_fw.LC_JOBS_ERROR:
                raise exception.FirmwareUpdateFailed(error=detail)
            if status != drac_fw.LC_JOBS_UNAVAILABLE:
                return status in (drac_fw.LC_JOBS_STAGED, drac_fw.LC_JOBS_DONE)
        if self._positive_task_outcome(update):
            return True
        if sushy_task is None:
            update['last_error'] = (
                'Unknown staging outcome for %s: task disappeared before '
                'success was observed' % update['component'])
            return False
        bios_compatibility = (
            redfish_utils.get_component_type(update['component'])
            == redfish_utils.BIOS
            and task.node.driver_info.get('firmware_update_bios_pending_reset')
            == 'compatibility')
        if (sushy_task.task_state == sushy.TASK_STATE_STARTING
                or (bios_compatibility and sushy_task.task_state in (
                    sushy.TASK_STATE_PENDING, sushy.TASK_STATE_RUNNING))):
            started = update.setdefault('starting_at',
                                        timeutils.utcnow().isoformat())
            return (self._seconds_since(started)
                    >= CONF.redfish.firmware_update_nic_starting_wait)
        return False

    def _recover_task_outcome(self, task, update):
        if self._positive_task_outcome(update):
            return True
        if (redfish_utils.get_component_type(update['component'])
                == redfish_utils.BMC and update.get('version_changed')):
            return True
        if self._is_dell_node(task.node, update):
            status, detail = drac_fw.check_lc_jobs(
                task, update.get('jids', []), update.get('jobs'),
                required=True)
            update['last_error'] = detail
            if status == drac_fw.LC_JOBS_ERROR:
                raise exception.FirmwareUpdateFailed(error=detail)
            return status == drac_fw.LC_JOBS_DONE
        update['last_error'] = (
            'Unknown application outcome for %s: task disappeared before '
            'success was observed' % update['component'])
        return False

    def _run_lc_job_gate(self, task, update, allow_staged=False):
        """Require all known/late firmware jobs before proceeding."""
        verify = update.get('verify')
        if not self._is_dell_node(task.node, update):
            if verify is not None:
                verify['lc'] = _VERIFY_SKIPPED
            return False
        status, detail = drac_fw.check_lc_jobs(
            task, update.get('jids', []), update.get('jobs'),
            required=True, allow_staged=allow_staged)
        update['last_error'] = detail
        if status == drac_fw.LC_JOBS_ERROR:
            raise exception.FirmwareUpdateFailed(error=detail)
        if status == drac_fw.LC_JOBS_RUNNING:
            return True
        if (status == drac_fw.LC_JOBS_UNAVAILABLE
                and not update.get('lc_unavailable_logged')):
            LOG.warning('Cannot verify Dell LC jobs for %(component)s on '
                        'node %(node)s: %(detail)s',
                        {'component': update['component'],
                         'node': task.node.uuid, 'detail': detail})
            update['lc_unavailable_logged'] = True
        if verify is not None:
            verify['lc'] = (_VERIFY_SKIPPED
                            if status == drac_fw.LC_JOBS_UNAVAILABLE
                            else _VERIFY_PASSED)
        return False

    def _start_apply_reboot(self, task, settings):
        """Persist apply-reset intent for this component before issuing it."""
        update = settings[0]
        bmc = redfish_utils.get_component_type(update['component'])
        if self._run_lc_job_gate(task, update,
                                 allow_staged=bmc != redfish_utils.BMC):
            return
        prepared = {'verify': {
            'jids': list(update.get('jids', [])),
            'lc': _VERIFY_PENDING, 'boot': _VERIFY_PENDING,
            'new_boot_observed': False, 'os_boot_started_at': None,
        }}
        self._prepare_reboot_observation(task.node, prepared)
        # The presence of verify selects post-reset polling. Publish it only
        # after the baseline GET and timestamp preparation both succeeded.
        update.update(prepared)
        deploy_utils.set_async_step_flags(task.node, reboot=True, polling=True)
        self._persist(task.node, settings)
        if bmc == redfish_utils.BMC:
            manager_utils.node_power_action(task, states.REBOOT)
        else:
            manager_utils.node_power_action(task, states.REBOOT,
                                            update.get('power_timeout', 0))

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

    def _prepare_reboot_observation(self, node, update):
        before = self._boot_observation(node)
        verify = update['verify']
        verify['before'] = before
        verify['progress_supported'] = before['state'] not in (
            None, 'None', 'OEM')
        update['reboot_time'] = timeutils.utcnow().isoformat()

    def _sample_reboot(self, node, update):
        """Accumulate reset evidence; failed reads prove nothing."""
        observed = self._boot_observation(node)
        verify = update['verify']
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

    def _os_running_wait_elapsed(self, update):
        timeout = CONF.redfish.firmware_update_os_running_timeout
        if timeout <= 0:
            return True
        started = update['verify'].get('os_boot_started_at')
        if started is None:
            update['verify']['os_boot_started_at'] = (
                timeutils.utcnow().isoformat())
            return False
        return self._seconds_since(started) >= timeout

    def _run_boot_progress_gate(self, task, update, observed):
        """Require new-boot readiness; only absent telemetry uses a timer."""
        # None means this poll could not read boot telemetry. The previously
        # persisted observation is not a fresh response and cannot pass a gate.
        if observed is None:
            return True
        node = task.node
        verify = update['verify']
        update['last_error'] = 'boot: %s, power: %s' % (
            observed['state'], observed['power'])
        elapsed = self._seconds_since(update['reboot_time'])
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
                and self._os_running_wait_elapsed(update)):
            raise exception.FirmwareUpdateFailed(error=_(
                'The host completed POST but did not report OSRunning within '
                'firmware_update_os_running_timeout. Configure limited boot '
                'progress only if this platform is known not to report it.'))
        return True

    def _get_current_bmc_version(self, node):
        try:
            system = redfish_utils.get_system(node)
            manager = redfish_utils.get_manager(node, system)
            return manager.firmware_version
        except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
            LOG.debug('BMC temporarily unresponsive for node %(node)s: '
                      '%(error)s', {'node': node.uuid, 'error': exc})
            return None

    def _bmc_update_completion(self, task, update, settings):
        """Verify BMC completion before resource recovery or host handoff."""
        bmc = update['bmc']
        current_version = self._get_current_bmc_version(task.node)
        version_before = bmc.get('version_before')
        changed = (current_version is not None and version_before is not None
                   and current_version != version_before)
        positive = self._positive_task_outcome(update)
        jobs = (update.get('jobs') or {}).get('jobs', {})
        positive = positive or (bool(jobs) and all(
            outcome == 'Completed' for outcome in jobs.values()))
        if changed or (positive and self._seconds_since(bmc['check_start'])
                       >= bmc['check_timeout']):
            update['version_changed'] = changed
            bmc['reboot_requested'] = (
                len(settings) > 1
                or current_version is None or version_before is None)
            update.pop('wait', None)
            bmc['wait_start'] = None
            bmc['completed'] = True
            return
        update['wait'] = (
            CONF.redfish.firmware_update_bmc_version_check_interval)
        update['last_error'] = 'BMC update has no verified completion yet'
        bmc['wait_start'] = timeutils.utcnow().isoformat()
        bmc['checking'] = True

    def _validate_resources_stability(self, node):
        """Take one BMC recovery sample; leave waiting to the periodic."""
        timeout = CONF.redfish.firmware_update_resource_validation_timeout
        required_successes = CONF.redfish.firmware_update_required_successes
        validation_interval = CONF.redfish.firmware_update_validation_interval
        if not timeout or not required_successes:
            return True
        update = node.driver_internal_info['redfish_fw_updates'][0]
        validation = update['bmc'].setdefault('validation', {
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
            update['last_error'] = str(exc)
        return validation['successes'] >= required_successes

    def _continue_updates(self, task, update_service, settings, observed=None):
        """Refresh verified inventory before popping the current component."""
        update = settings[0]
        if self._run_lc_job_gate(task, update):
            if update.get('inventory_started_at'):
                update['inventory_started_at'] = timeutils.utcnow().isoformat()
            return
        if (update.get('verify')
                and self._run_boot_progress_gate(task, update, observed)):
            return
        started = update.setdefault('inventory_started_at',
                                    timeutils.utcnow().isoformat())
        wait = max(CONF.redfish.firmware_update_inventory_wait,
                   update.get('wait', 0))
        if self._seconds_since(started) < wait:
            return
        options = {}
        if update.get('inventory_supported'):
            options['known_supported_types'] = update['inventory_supported']
        if update.get('nic_identity'):
            options['nic_bindings'] = {
                update['component']: update['nic_identity']}
        try:
            self.cache_firmware_components(
                task, required_components=[update['component']], **options)
        except exception.UnsupportedDriverExtension:
            LOG.warning('Firmware inventory is unsupported on node %s',
                        task.node.uuid)
        settings.pop(0)
        if settings:
            if 'vendor' in update:
                settings[0]['vendor'] = update['vendor']
            self._persist(task.node, settings)
            self._start_component(task, update_service, settings)
        else:
            self._clear_updates(task.node)
            self._resume_step(task)

    def _poll_current_update(self, task, update_service, settings,
                             observed=None):
        """Advance only the current sequential component in this poll."""
        update = settings[0]
        if not update.get('submission_started'):
            self._start_component(task, update_service, settings)
            return
        if not update.get('submitted'):
            raise exception.FirmwareUpdateFailed(error=_(
                'Firmware submission outcome is unknown; inspect the BMC '
                'jobs before retrying the update.'))
        if update.get('verify') and not update.get('reboot_time'):
            # Older code persisted verify before its baseline GET. Without a
            # reboot_time it had not reached the persisted intent/power call.
            # Discard only that preparation, then prepare a fresh reset.
            update.pop('verify')
        if update.get('reboot_time') and not update.get('verify'):
            raise exception.FirmwareUpdateFailed(error=_(
                'Firmware reboot intent has lost its verification record. '
                'Inspect the BMC before recovering this update.'))
        if update.get('verify'):
            result = self._read_update_task(task, update)
            if result is not None:
                if result.task_state != sushy.TASK_STATE_COMPLETED:
                    return
                update.pop('task_monitor', None)
            if not self._recover_task_outcome(task, update):
                return
            self._continue_updates(task, update_service, settings, observed)
            return
        if update.get('bmc'):
            if self._run_lc_job_gate(task, update):
                return
            bmc = update['bmc']
            if bmc.get('completed'):
                if not self._validate_resources_stability(task.node):
                    return
                if bmc['reboot_requested']:
                    update.pop('task_monitor', None)
                    self._start_apply_reboot(task, settings)
                else:
                    self._continue_updates(task, update_service, settings)
                return
            if (bmc.get('wait_start')
                    and self._seconds_since(bmc['wait_start'])
                    < update.get('wait', 0)):
                return
            update.pop('wait', None)
            bmc['wait_start'] = None
            result = self._read_update_task(task, update)
            if result is not None and result.task_state in (
                    sushy.TASK_STATE_NEW, sushy.TASK_STATE_STARTING,
                    sushy.TASK_STATE_PENDING, sushy.TASK_STATE_RUNNING):
                return
            self._bmc_update_completion(task, update, settings)
            return
        result = self._read_update_task(task, update)
        if self._staging_ready(task, update, result):
            update['staged'] = True
            self._start_apply_reboot(task, settings)

    @METRICS.timer('RedfishFirmware._check_node_redfish_firmware_update')
    def _check_node_redfish_firmware_update(self, task):
        """Poll sequential updates, retaining the head until all gates pass."""
        task.upgrade_lock()
        try:
            self._poll_firmware_update(task)
        except Exception as exc:
            # A programming failure must neither starve later nodes in the
            # periodic scan nor silently become another transport retry.
            message = _(
                'Unexpected firmware monitoring error on node %(node)s: '
                '%(type)s: %(error)s') % {
                    'node': task.node.uuid, 'type': type(exc).__name__,
                    'error': exc}
            LOG.exception(message)
            self._fail_update(task, message)

    def _poll_firmware_update(self, task):
        """Advance this node and persist observations before returning."""
        node = task.node
        settings = node.driver_internal_info.get('redfish_fw_updates')
        if not settings:
            return
        update = settings[0]
        # An older entry may already have been submitted. Without its outcome
        # and reset bookkeeping, restarting it could duplicate an action.
        if not update.get('started_at'):
            self._fail_update(task, _(
                'Cannot safely resume an interrupted firmware update without '
                'application evidence. Inspect the BMC before retrying.'))
            return
        overall = CONF.redfish.firmware_update_overall_timeout
        started = node.driver_internal_info.get('redfish_fw_update_start_time')
        verify_timeout = (
            CONF.redfish.firmware_update_post_reboot_verify_timeout)
        reboot_expired = (
            update.get('reboot_time')
            and verify_timeout > 0
            and self._seconds_since(update['reboot_time']) >= verify_timeout)
        if ((overall > 0 and started
             and self._seconds_since(started) >= overall)
                or self._seconds_since(update['started_at'])
                >= CONF.redfish.firmware_update_apply_timeout
                or reboot_expired):
            self._fail_update(task, _(
                'Firmware update timed out for %(component)s on node '
                '%(node)s. Last status: %(status)s; LC jobs: %(jobs)s.') % {
                    'component': update['component'], 'node': node.uuid,
                    'status': update.get('last_error'),
                    'jobs': update.get('jids', [])})
            return
        node.touch_provisioning()
        observed = None
        if update.get('verify') and update.get('reboot_time'):
            try:
                observed = self._sample_reboot(node, update)
            except (exception.RedfishError,
                    sushy.exceptions.SushyError) as exc:
                update['last_error'] = str(exc)
        try:
            update_service = redfish_utils.get_update_service(node)
            self._poll_current_update(task, update_service, settings, observed)
        except exception.FirmwareUpdateFailed as exc:
            self._fail_update(task, str(exc))
        except (exception.RedfishError, sushy.exceptions.SushyError) as exc:
            update['last_error'] = str(exc)
            LOG.warning('Cannot monitor firmware update on node %(node)s: '
                        '%(error)s. Retrying on the next poll.',
                        {'node': node.uuid, 'error': exc})
        if node.driver_internal_info.get('redfish_fw_updates'):
            self._persist(node, settings)

    def _report_step_error(self, task, error_msg, traceback=True):
        """Route a step error to the correct power-preserving error handler."""
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
            node = task.node
            node.maintenance = True
            node.maintenance_reason = error_msg
            manager_utils.node_history_record(node, event=error_msg,
                                              error=True)
            node.save()

    def _fail_update(self, task, message):
        settings = task.node.driver_internal_info.get('redfish_fw_updates')
        if settings:
            jobs = drac_fw.describe_tracked_jobs(settings[0].get('jobs'))
            if jobs:
                message += _(' Tracked LC jobs: %s.') % jobs
        if settings and settings[0].get('submitted'):
            message += _(' The BMC accepted firmware for %s; inspect its jobs '
                         'before another boot or submission.') % (
                             settings[0]['component'])
        try:
            self._report_step_error(task, message, traceback=False)
        finally:
            # Retire the executable queue to prevent accidental resumption.
            # Maintenance and the error/history retain the failure and job
            # evidence; clearing the queue does not authorize a power action.
            self._clear_updates(task.node)

    def _clear_updates(self, node):
        try:
            firmware_utils.cleanup(node)
        except exception.IronicException:
            LOG.exception('Unable to remove staged firmware for node %s',
                          node.uuid)
        for key in ('redfish_fw_updates', 'redfish_fw_update_start_time',
                    'firmware_cleanup', 'bmc_fw_version_before_update',
                    'firmware_reboot_requested',
                    async_steps.FIRMWARE_UPDATE_IN_PROGRESS):
            node.del_driver_internal_info(key)
        node.save()

    def _resume_step(self, task):
        if task.node.clean_step:
            manager_utils.notify_conductor_resume_clean(task)
        elif task.node.service_step:
            manager_utils.notify_conductor_resume_service(task)
        elif task.node.deploy_step:
            manager_utils.notify_conductor_resume_deploy(task)

    @METRICS.timer('RedfishFirmware._query_update_failed')
    @periodics.node_periodic(
        purpose='checking if async update of firmware component failed',
        spacing=CONF.redfish.firmware_update_fail_interval,
        filters={'reserved': False, 'provision_state_in': [states.CLEANFAIL,
                 states.DEPLOYFAIL, states.SERVICEFAIL], 'maintenance': True},
        predicate_extra_fields=['driver_internal_info'],
        predicate=lambda n: n.driver_internal_info.get('redfish_fw_updates'),
    )
    def _query_update_failed(self, task, manager, context):
        task.upgrade_lock()
        if (task.node.provision_state not in (
                states.CLEANFAIL, states.DEPLOYFAIL, states.SERVICEFAIL)
                or not task.node.maintenance):
            return
        LOG.error('Update firmware failed for node %(node)s. Discarding '
                  'remaining firmware updates.', {'node': task.node.uuid})
        self._clear_updates(task.node)

    @METRICS.timer('RedfishFirmware._query_update_status')
    @periodics.node_periodic(
        purpose='checking async update of firmware component',
        spacing=CONF.redfish.firmware_update_status_interval,
        filters={'reserved': False, 'provision_state_in': [states.CLEANWAIT,
                 states.DEPLOYWAIT, states.SERVICEWAIT]},
        predicate_extra_fields=['driver_internal_info'],
        predicate=lambda n: n.driver_internal_info.get('redfish_fw_updates'),
    )
    def _query_update_status(self, task, manager, context):
        task.upgrade_lock()
        if task.node.provision_state not in (
                states.CLEANWAIT, states.DEPLOYWAIT, states.SERVICEWAIT):
            return
        self._check_node_redfish_firmware_update(task)

    def _stage_firmware_file(self, node, component_update):
        try:
            url = component_update['url']
            name = component_update['component']
            parsed_url = urlparse(url)
            scheme = parsed_url.scheme.lower()
            source = CONF.redfish.firmware_source.lower()
            if scheme == 'https':
                scheme = 'http'
            if scheme == 'http' and source == scheme:
                LOG.debug('For node %(node)s serving firmware for '
                          '%(component)s from original location %(url)s',
                          {'node': node.uuid, 'component': name, 'url': url})
                return url, None
            if scheme == 'swift' and source == scheme:
                temp_url = firmware_utils.get_swift_temp_url(parsed_url)
                LOG.debug('For node %(node)s serving original firmware at '
                          'for %(component)s at %(url)s via Swift temporary '
                          'url %(temp_url)s',
                          {'node': node.uuid, 'component': name, 'url': url,
                           'temp_url': temp_url})
                return temp_url, None
            temp_file = firmware_utils.download_to_temp(node, url)
            return firmware_utils.stage(node, source, temp_file)
        except exception.IronicException:
            firmware_utils.cleanup(node)
            raise
