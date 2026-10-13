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

import time
from urllib.parse import urlparse

from oslo_log import log
from oslo_utils import strutils
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

# Temporary field names stored in node.driver_internal_info
BMC_FW_VERSION_BEFORE_UPDATE = 'bmc_fw_version_before_update'
GROUPING = 'redfish_fw_update_grouping'
FIRMWARE_REBOOT_REQUESTED = 'firmware_reboot_requested'

# Temporary field names stored in fw_upd/current_update settings dict
BMC_UPDATE_COMPLETED = 'bmc_update_completed'
# Where an update applied by rebooting the node stands: BIOS and NIC
# updates, and BMC updates that need a host reboot to take effect.
PHASE = 'phase'
# The BMC is staging the update for the next reboot.
PHASE_STAGING = 'staging'
# Staged, waiting for later updates to share its reboot.
PHASE_STAGED = 'staged'
# The node has been rebooted to apply the staged update.
PHASE_APPLYING = 'applying'
# When the staging task was first seen waiting in Starting or Pending.
STARTING_SINCE = 'starting_since'
# What is known about the reboot applying the update; see _reboot_to_apply.
VERIFY = 'verify'

# Redfish task states of a task that has not finished.
_TASK_IN_PROGRESS = (sushy.TASK_STATE_NEW, sushy.TASK_STATE_RUNNING,
                     sushy.TASK_STATE_STARTING, sushy.TASK_STATE_PENDING)


def _name(value):
    """The Redfish name of a sushy enum value, for messages."""
    return getattr(value, 'value', value)


class RedfishFirmware(base.FirmwareInterface):

    _FW_SETTINGS_ARGSINFO = {
        'settings': {
            'description': (
                'A list of dicts with firmware components to be updated'
            ),
            'required': True
        },
        'allow_grouping_reboots': {
            'description': (
                'Boolean. When True, adjacent firmware updates of components '
                'other than the BMC are staged together and applied by a '
                'single reboot of the node, instead of one reboot each. BMC '
                'updates are applied on their own. Components must not '
                'repeat, and only BMC components may set a wait. Defaults '
                'to False.'
            ),
            'required': False
        }
    }

    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        return redfish_utils.COMMON_PROPERTIES.copy()

    def validate(self, task):
        """Validates the driver information needed by the redfish driver.

        :param task: a TaskManager instance containing the node to act on.
        :raises: InvalidParameterValue on malformed parameter(s)
        :raises: MissingParameterValue on missing parameter(s)
        """
        redfish_utils.parse_driver_info(task.node)

    @METRICS.timer('RedfishFirmware.cache_firmware_components')
    def cache_firmware_components(self, task):
        """Store or update Firmware Components on the given node.

        This method stores Firmware Components to the firmware_information
        table during 'cleaning' operation. It will also update the timestamp
        of each Firmware Component.

        :param task: a TaskManager instance.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support getting Firmware Components from bare metal.
        """

        node_id = task.node.id
        settings = []
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
        except exception.RedfishError:
            LOG.warning('No manager available to retrieve Firmware '
                        'from the bmc of node %s', task.node.uuid)

        nic_components = None
        try:
            nic_components = self.retrieve_nic_components(task, system)
        except (exception.RedfishError,
                sushy.exceptions.BadRequestError,
                sushy.exceptions.MissingAttributeError) as e:
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

    def retrieve_nic_components(self, task, system):
        """Helper function to retrieve all NICs components on a given node.

        :param task: a TaskManager instance.
        :param system: a Redfish System object
        :returns: a list of NIC components
        """
        nic_list = []
        try:
            chassis = redfish_utils.get_chassis(task.node, system)
        except exception.RedfishError:
            LOG.debug('No chassis available to retrieve NetworkAdapters '
                      'firmware information on node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
            return nic_list

        try:
            network_adapters = chassis.network_adapters
            if network_adapters is None:
                LOG.debug('NetworkAdapters not available on chassis for '
                          'node %(node_uuid)s',
                          {'node_uuid': task.node.uuid})
                return nic_list
            adapters = network_adapters.get_members()
        except sushy.exceptions.MissingAttributeError:
            LOG.debug('NetworkAdapters not available on chassis for '
                      'node %(node_uuid)s',
                      {'node_uuid': task.node.uuid})
            return nic_list

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
        :param allow_grouping_reboots: whether adjacent updates of
            components other than the BMC share one reboot.
        :raises: UnsupportedDriverExtension, if the node's driver doesn't
            support update via the interface.
        :raises: InvalidParameterValue, if validation of the settings fails.
        :raises: MissingParamterValue, if some required parameters are
            missing.
        :returns: states.CLEANWAIT if Firmware update with the settings is in
            progress asynchronously of None if it is complete.
        """
        firmware_utils.validate_firmware_interface_update_args(settings)
        try:
            allow_grouping_reboots = strutils.bool_from_string(
                allow_grouping_reboots, strict=True)
        except ValueError as e:
            raise exception.InvalidParameterValue(
                _('allow_grouping_reboots must be a boolean: %s') % e)
        if allow_grouping_reboots:
            self._validate_grouping(settings)
        node = task.node
        update_service = redfish_utils.get_update_service(node)

        LOG.debug('Updating Firmware on node %(node_uuid)s with settings '
                  '%(settings)s, allow_grouping_reboots=%(grouping)s',
                  {'node_uuid': node.uuid, 'settings': settings,
                   'grouping': allow_grouping_reboots})
        if allow_grouping_reboots:
            node.set_driver_internal_info(GROUPING, True)
        else:
            node.del_driver_internal_info(GROUPING)
        # Clean any stale temp fields from previous steps.
        self._clean_temp_fields(node)
        self._execute_firmware_update(node, update_service, settings)

        # Store updated settings and start time for overall timeout tracking
        node.set_driver_internal_info('redfish_fw_updates', settings)
        node.set_driver_internal_info(
            'redfish_fw_update_start_time',
            timeutils.utcnow().isoformat())
        node.save()

        # Return wait state to keep the step active and let polling handle
        # the monitoring and eventual completion/reboot
        return async_steps.get_return_state(node)

    def _validate_grouping(self, settings):
        """Validate settings for updates that share reboots.

        :param settings: the firmware update settings.
        :raises: InvalidParameterValue if a component repeats, since its
            updates would be staged for the same reboot, or a component
            other than the BMC sets a wait, which has no place between
            updates sharing a reboot.
        """
        seen = set()
        for fw_upd in settings:
            component = fw_upd['component']
            if component in seen:
                raise exception.InvalidParameterValue(
                    _('Component %s appears more than once; with '
                      'allow_grouping_reboots each component may only be '
                      'updated once per step.') % component)
            seen.add(component)
            if (fw_upd.get('wait')
                    and redfish_utils.get_component_type(component)
                    != redfish_utils.BMC):
                raise exception.InvalidParameterValue(
                    _('Component %s sets a wait, which with '
                      'allow_grouping_reboots is only supported for the '
                      'BMC.') % component)

    def _clean_temp_fields(self, node):
        """Clean up temporary fields used during firmware update monitoring.

        This ensures no stale data interferes with new firmware updates.

        :param node: the Ironic node object
        """
        # BMC-related temp fields
        node.del_driver_internal_info(BMC_FW_VERSION_BEFORE_UPDATE)
        # General firmware temp fields
        node.del_driver_internal_info(FIRMWARE_REBOOT_REQUESTED)

    def _setup_bmc_update_monitoring(self, node, fw_upd):
        """Set up monitoring for BMC firmware update.

        BMC updates do not reboot immediately. Instead, we check the BMC
        version periodically. If the version changed, we continue without
        reboot. If timeout expires without version change, we trigger a reboot.

        :param node: the Ironic node object
        :param fw_upd: firmware update settings dict
        """
        # Record current BMC version before update
        try:
            system = redfish_utils.get_system(node)
            manager = redfish_utils.get_manager(node, system)
            current_bmc_version = manager.firmware_version
            node.set_driver_internal_info(
                BMC_FW_VERSION_BEFORE_UPDATE, current_bmc_version)
            LOG.debug('BMC version before update for node %(node)s: '
                      '%(version)s',
                      {'node': node.uuid, 'version': current_bmc_version})
        except Exception as e:
            LOG.warning('Could not read BMC version before update for '
                        'node %(node)s: %(error)s',
                        {'node': node.uuid, 'error': e})

        LOG.info('BMC firmware update for node %(node)s. '
                 'Monitoring BMC version instead of immediate reboot.',
                 {'node': node.uuid})

        # Use wait_interval or default reboot delay
        wait_interval = fw_upd.get('wait')
        if wait_interval is None:
            wait_interval = CONF.redfish.firmware_update_reboot_delay
        fw_upd['wait'] = wait_interval
        # Set wait_start_time for polling interval and bmc_check_start_time
        # for total timeout tracking (wait_start_time gets updated each poll)
        start_time = str(timeutils.utcnow().isoformat())
        fw_upd['wait_start_time'] = start_time
        fw_upd['bmc_check_start_time'] = start_time
        # Mark this as a BMC update so we can handle timeouts properly
        fw_upd['component_type'] = redfish_utils.BMC

        # BMC: Set async flags without immediate reboot
        deploy_utils.set_async_step_flags(
            node,
            reboot=False,
            polling=True
        )

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

    def _handle_bmc_update_completion(self, task, update_service,
                                      settings, current_update):
        """Handle BMC firmware update completion with version checking.

        For BMC updates, we don't reboot immediately. Instead, we check
        the BMC version periodically. If the version changed, we continue
        without reboot. If timeout expires without version change, we trigger
        a reboot.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        :param current_update: the current firmware update being processed
        """
        # Upgrade the lock to ensure we are using the latest info from
        # the node.
        task.upgrade_lock()
        node = task.node

        # Try to get current BMC version
        # Note: BMC may be unresponsive after firmware update - expected
        current_version = self._get_current_bmc_version(node)
        version_before = node.driver_internal_info.get(
            BMC_FW_VERSION_BEFORE_UPDATE)

        # If we can read the version and it changed, update is complete
        if (current_version is not None
                and version_before is not None
                and current_version != version_before):
            node.del_driver_internal_info(BMC_FW_VERSION_BEFORE_UPDATE)

            # Check if more components are pending updates after BMC update.
            # With grouping, they are staged without this reboot and applied
            # by the reboot of their group, as the BMC has already recovered.
            if (len(settings) > 1
                    and not node.driver_internal_info.get(GROUPING)):
                # Upgrade the lock to ensure we are using the latest info from
                # the node.
                task.upgrade_lock()
                # More components to update - trigger reboot before continuing
                # Some hardware can only execute NIC firmware updates after
                # the host reboots following the BMC firmware update.

                LOG.info('BMC firmware update complete for node %(node)s. '
                         'More components pending - triggering reboot before '
                         'continuing to next component.',
                         {'node': node.uuid})
                # This is required because we saw cases where NIC updates
                # were not being executed after the BMC update.
                node.set_driver_internal_info(FIRMWARE_REBOOT_REQUESTED, True)
                node.save()
                self._continue_updates(task, update_service, settings)
                return
            else:
                # Last component or grouping - no reboot needed
                # Servicing/Cleaning will trigger one after the last one.
                LOG.info('BMC firmware version for node %(node)s changed '
                         'from %(old)s to %(new)s.',
                         {'node': node.uuid, 'old': version_before,
                          'new': current_version})
                node.save()
                self._continue_updates(task, update_service, settings)
            return

        # Check if we've been checking for too long
        check_start_time = current_update.get('bmc_check_start_time')

        if check_start_time:
            check_start = timeutils.parse_isotime(check_start_time)
            elapsed_time = timeutils.utcnow(True) - check_start
            timeout = current_update.get(
                'wait', CONF.redfish.firmware_update_reboot_delay)
            if elapsed_time.seconds >= timeout:
                # Timeout: version didn't change or BMC unresponsive
                if (current_version is not None
                        and version_before is not None
                        and current_version == version_before):
                    # Version didn't change - skip reboot
                    LOG.info(
                        'BMC firmware version for node %(node)s did not '
                        'change (still %(version)s). Update appears to be '
                        'a no-op or does not require reboot. Continuing '
                        'without reboot.',
                        {'node': node.uuid, 'version': current_version})
                else:
                    # Version changed or we can't tell - reboot to apply
                    LOG.warning(
                        'BMC firmware version check timeout expired for '
                        'node %(node)s after %(elapsed)s seconds. '
                        'Will reboot to complete firmware update.',
                        {'node': node.uuid, 'elapsed': elapsed_time.seconds})
                    # Mark that reboot is needed
                    node.set_driver_internal_info(
                        FIRMWARE_REBOOT_REQUESTED, True)
                    # Enable reboot flag now that we're ready to reboot
                    deploy_utils.set_async_step_flags(
                        node,
                        reboot=True,
                        polling=True
                    )

                node.del_driver_internal_info(BMC_FW_VERSION_BEFORE_UPDATE)
                node.save()
                self._continue_updates(task, update_service, settings)
                return

        # Continue checking - set wait to check again
        wait_interval = (
            CONF.redfish.firmware_update_bmc_version_check_interval)
        current_update['wait'] = wait_interval
        current_update['wait_start_time'] = str(
            timeutils.utcnow().isoformat())
        current_update['bmc_version_checking'] = True
        node.set_driver_internal_info('redfish_fw_updates', settings)
        node.save()

        LOG.debug('BMC firmware version check continuing for node %(node)s. '
                  'Will check again in %(interval)s seconds.',
                  {'node': node.uuid, 'interval': wait_interval})

    def _submit_simple_update(self, node, update_service, fw_upd):
        """Submit a SimpleUpdate request and track cleanup.

        Handles systems-collection targeting, firmware file staging,
        the SimpleUpdate call, and cleanup tracking.

        :param node: the node that will have a firmware update executed.
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

        component_url, cleanup = self._stage_firmware_file(node, fw_upd)

        LOG.debug('Applying new firmware %(url)s for %(component)s on node '
                  '%(node_uuid)s',
                  {'url': fw_upd['url'], 'component': fw_upd['component'],
                   'node_uuid': node.uuid})
        try:
            if targets is not None:
                task_monitor = update_service.simple_update(component_url,
                                                            targets=targets)
            else:
                task_monitor = update_service.simple_update(component_url)
        except sushy.exceptions.MissingAttributeError as e:
            LOG.error('The attribute #UpdateService.SimpleUpdate is missing '
                      'on node %(node)s. Error: %(error)s',
                      {'node': node.uuid, 'error': e.message})
            raise exception.RedfishError(error=e)
        except sushy.exceptions.SushyError as e:
            LOG.error('SimpleUpdate failed for node %(node)s: %(error)s',
                      {'node': node.uuid, 'error': e})
            raise exception.RedfishError(error=e)

        fw_upd['task_monitor'] = task_monitor.task_monitor_uri

        if cleanup:
            fw_clean = node.driver_internal_info.get('firmware_cleanup')
            if not fw_clean:
                fw_clean = [cleanup]
            elif cleanup not in fw_clean:
                fw_clean.append(cleanup)
            node.set_driver_internal_info('firmware_cleanup', fw_clean)

        return task_monitor.task_monitor_uri

    def _execute_firmware_update(self, node, update_service, settings):
        """Executes the next firmware update to the node

        Submits the first firmware update in the settings list to the node.
        A BMC update is then followed through the BMC firmware version. Any
        other update is staged by the BMC and applied by rebooting the
        node.

        :param node: the node that will have a firmware update executed.
        :param update_service: the sushy firmware update service.
        :param settings: remaining settings for firmware update that needs
            to be executed.
        """
        fw_upd = settings[0]
        self._submit_simple_update(node, update_service, fw_upd)
        node.set_driver_internal_info('redfish_fw_updates', settings)

        component_type = redfish_utils.get_component_type(
            fw_upd['component'])
        if component_type == redfish_utils.BMC:
            self._setup_bmc_update_monitoring(node, fw_upd)
            return

        LOG.info('%(component)s firmware update submitted for node '
                 '%(node)s. Waiting for the BMC to stage it before rebooting '
                 'the node to apply it.',
                 {'component': fw_upd['component'], 'node': node.uuid})
        fw_upd[PHASE] = PHASE_STAGING
        deploy_utils.set_async_step_flags(node, reboot=False, polling=True)

    def _validate_resources_stability(self, node):
        """Validate that BMC resources are consistently available.

        This covers a BMC firmware update, after which the BMC's own web
        service can answer and then drop out again while the new image
        comes up.

        Requires consecutive successful responses from System, Manager,
        and NetworkAdapters resources before considering them stable.
        The number of required successes is configured via
        CONF.redfish.firmware_update_required_successes.
        Timeout is configured via
        CONF.redfish.firmware_update_resource_validation_timeout.

        :param node: the Ironic node object
        :raises: RedfishError if resources don't stabilize within timeout
        """
        timeout = CONF.redfish.firmware_update_resource_validation_timeout
        required_successes = CONF.redfish.firmware_update_required_successes
        validation_interval = CONF.redfish.firmware_update_validation_interval

        # Skip validation if validation is disabled via configuration
        if required_successes == 0 or timeout == 0:
            reasons = []
            if required_successes == 0:
                reasons.append('required_successes=0')
            if timeout == 0:
                reasons.append('validation_timeout=0')

            LOG.info('BMC resource validation disabled (%(reasons)s) for '
                     'node %(node)s',
                     {'reasons': ', '.join(reasons), 'node': node.uuid})
            return

        LOG.debug('Starting resource stability validation for node %(node)s '
                  '(timeout: %(timeout)s seconds, '
                  'required_successes: %(required)s, '
                  'validation_interval: %(interval)s seconds)',
                  {'node': node.uuid, 'timeout': timeout,
                   'required': required_successes,
                   'interval': validation_interval})

        start_time = time.time()
        end_time = start_time + timeout
        consecutive_successes = 0
        last_exc = None

        while time.time() < end_time:
            try:
                # Test System resource
                system = redfish_utils.get_system(node)

                # Test Manager resource
                redfish_utils.get_manager(node, system)

                # Test Chassis and NetworkAdapters resource (if available)
                # Some systems may not have NetworkAdapters, which is valid
                chassis = redfish_utils.get_chassis(node, system)
                try:
                    network_adapters = chassis.network_adapters
                    if network_adapters is not None:
                        network_adapters.get_members()
                except sushy.exceptions.MissingAttributeError:
                    # NetworkAdapters not available is acceptable
                    pass

                # All resources successful
                consecutive_successes += 1
                LOG.debug('Resource validation success %(count)d/%(required)d '
                          'for node %(node)s',
                          {'count': consecutive_successes,
                           'required': required_successes,
                           'node': node.uuid})

                if consecutive_successes >= required_successes:
                    LOG.info('All tested Redfish resources stable and '
                             ' available for node %(node)s',
                             {'node': node.uuid})
                    return

            except (exception.RedfishError,
                    exception.RedfishConnectionError,
                    sushy.exceptions.SushyError) as e:
                LOG.debug('BMC resource validation failed for node %(node)s: '
                          '%(error)s. This may indicate the BMC is still '
                          'restarting or recovering from firmware update.',
                          {'node': node.uuid, 'error': e})
                # Resource not available yet, reset counter
                if consecutive_successes > 0:
                    LOG.debug('Resource validation interrupted for node '
                              '%(node)s, resetting success counter '
                              '(error: %(error)s)',
                              {'node': node.uuid, 'error': e})
                consecutive_successes = 0
                last_exc = e

            # Wait before next validation attempt
            time.sleep(validation_interval)
        # Timeout reached without achieving stability
        error_msg = _('BMC resources failed to stabilize within '
                      '%(timeout)s seconds for node %(node)s') % {
            'timeout': timeout, 'node': node.uuid}
        if last_exc:
            error_msg += _(', last error: %(error)s') % {'error': last_exc}
        LOG.error(error_msg)
        raise exception.RedfishError(error=error_msg)

    def _report_step_error(self, task, error_msg, traceback=True):
        """Route a step error to the correct error handler.

        :param task: a TaskManager instance
        :param error_msg: the error message string
        :param traceback: whether to include traceback (default True)
        """
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

    def _continue_updates(self, task, update_service, settings):
        """Continues processing the firmware updates

        Called once the current firmware update has finished. Waits for
        its configured ``wait`` and reboots the node if the update still
        needs a reboot to take effect. A BMC firmware update can leave the
        BMC's web service flapping, so after one the resources are
        validated as stable before the next update is submitted or the
        step resumes; updates to other components leave the BMC running
        and need no such wait.

        Note that the caller must have an exclusive lock on the node.

        :param task: a TaskManager instance containing the node to act on.
        :param update_service: the sushy firmware update service
        :param settings: the remaining firmware updates to apply
        """
        node = task.node
        fw_upd = settings[0]

        wait_interval = fw_upd.get('wait')
        if wait_interval:
            time_now = str(timeutils.utcnow().isoformat())
            fw_upd['wait_start_time'] = time_now

            LOG.debug('Waiting at %(time)s for %(seconds)s seconds after '
                      '%(component)s firmware update %(url)s '
                      'on node %(node)s',
                      {'time': time_now,
                       'seconds': wait_interval,
                       'component': fw_upd['component'],
                       'url': fw_upd['url'],
                       'node': node.uuid})

            node.set_driver_internal_info('redfish_fw_updates', settings)
            node.save()
            return

        if node.driver_internal_info.get(FIRMWARE_REBOOT_REQUESTED):
            # A BMC update that could not be confirmed is applied by
            # rebooting the node, like any other staged update.
            if not self._bmc_stable(task, fw_upd):
                return
            node.del_driver_internal_info(FIRMWARE_REBOOT_REQUESTED)
            LOG.info('Rebooting node %(node)s to apply %(component)s '
                     'firmware.',
                     {'node': node.uuid, 'component': fw_upd['component']})
            self._reboot_to_apply(task, settings)
            return

        count = self._applying_count(settings)
        if len(settings) == count:
            if not self._bmc_stable(task, fw_upd):
                return
            self._clear_updates(node)

            LOG.info('Firmware updates completed for node %(node)s',
                     {'node': node.uuid})

            try:
                self.cache_firmware_components(task)
            except Exception as e:
                LOG.warning('Failed to refresh firmware components for node '
                            '%(node)s after firmware update: %(error)s',
                            {'node': node.uuid, 'error': e})

            self._resume_step(task)

        else:
            if update_service is None:
                try:
                    update_service = redfish_utils.get_update_service(node)
                except exception.RedfishConnectionError as e:
                    LOG.warning('Unable to communicate with firmware update '
                                'service on node %(node)s to submit the next '
                                'update. Will try again on the next poll. '
                                'Error: %(error)s',
                                {'node': node.uuid, 'error': e})
                    return

            if not self._bmc_stable(task, fw_upd):
                return

            del settings[:count]
            try:
                self._execute_firmware_update(node, update_service, settings)
            except exception.IronicException as e:
                self._fail(task, _(
                    'Failed to submit %(component)s firmware for node '
                    '%(node)s: %(error)s') % {
                        'component': settings[0]['component'],
                        'node': node.uuid, 'error': e})
                return
            node.save()

    def _bmc_stable(self, task, fw_upd):
        """Validate BMC resources after a BMC firmware update.

        Validated once, before the update continues or, if the BMC update
        needs one, before the host reboot applying it. Failing validation
        fails the step.

        :param task: a TaskManager instance
        :param fw_upd: the firmware update that has finished
        :returns: True if the update may continue, False if the step has
            been failed.
        """
        if (redfish_utils.get_component_type(fw_upd['component'])
                != redfish_utils.BMC or VERIFY in fw_upd):
            return True
        LOG.info('Validating BMC responsiveness after the BMC firmware '
                 'update of node %(node)s', {'node': task.node.uuid})
        try:
            self._validate_resources_stability(task.node)
        except exception.RedfishError as e:
            self._fail(task, str(e))
            return False
        return True

    def _clear_updates(self, node):
        """Clears firmware updates artifacts

        Clears firmware updates from driver_internal_info and any files
        that were staged.

        Note that the caller must have an exclusive lock on the node.

        :param node: the node to clear the firmware updates from
        """
        firmware_utils.cleanup(node)
        node.del_driver_internal_info('redfish_fw_updates')
        node.del_driver_internal_info('redfish_fw_update_start_time')
        node.del_driver_internal_info('firmware_cleanup')
        node.del_driver_internal_info(GROUPING)
        # Clean all temporary fields used during firmware update monitoring
        self._clean_temp_fields(node)
        node.save()

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

        """Periodic job to check for failed firmware updates."""
        # A firmware update failed. Discard any remaining firmware
        # updates so when the user takes the node out of
        # maintenance mode, pending firmware updates do not
        # automatically continue.
        LOG.error('Update firmware failed for node %(node)s. '
                  'Discarding remaining firmware updates.',
                  {'node': task.node.uuid})

        task.upgrade_lock()
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
        """Periodic job to check firmware update tasks."""
        self._check_node_redfish_firmware_update(task)

    def _handle_task_completion(self, task, sushy_task, messages,
                                update_service, settings, current_update):
        """Handle BMC firmware update task completion.

        :param task: a TaskManager instance
        :param sushy_task: the sushy task object
        :param messages: list of task messages
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        :param current_update: the current firmware update being processed
        """
        node = task.node

        if self._task_succeeded(sushy_task):
            LOG.debug('Redfish task completed for node %(node)s, '
                      'firmware %(firmware_image)s: %(messages)s.',
                      {'node': node.uuid,
                       'firmware_image': current_update['url'],
                       'messages': ", ".join(messages)})
            # BMC: Start version checking instead of immediate reboot
            self._handle_bmc_update_completion(
                task, update_service, settings, current_update)
        else:
            self._fail_task(task, current_update, sushy_task)

    def _task_succeeded(self, sushy_task):
        """Whether a terminal Redfish task finished successfully."""
        return (sushy_task.task_state == sushy.TASK_STATE_COMPLETED
                and sushy_task.task_status in [sushy.HEALTH_OK,
                                               sushy.HEALTH_WARNING])

    def _task_messages(self, sushy_task):
        """Collect the messages of a Redfish task.

        :param sushy_task: the sushy task object
        :returns: a list of message strings
        """
        # Only parse the messages if the BMC did not return parsed
        # messages
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

    def _fail(self, task, error_msg, traceback=False):
        """Fail the step and discard the remaining firmware updates.

        :param task: a TaskManager instance
        :param error_msg: the error message
        :param traceback: whether to include a traceback
        """
        updates = (task.node.driver_internal_info.get('redfish_fw_updates')
                   or [])
        staged = [u['component'] for u in updates
                  if u.get(PHASE) == PHASE_STAGED]
        if staged:
            error_msg += _(' Firmware for %s is already staged on the BMC '
                           'and will be applied on the next reboot of the '
                           'node.') % ', '.join(staged)
        self._report_step_error(task, error_msg, traceback=traceback)
        self._clear_updates(task.node)

    def _fail_task(self, task, fw_upd, sushy_task):
        """Fail the step because a firmware update task failed.

        :param task: a TaskManager instance
        :param fw_upd: the firmware update whose task failed
        :param sushy_task: the failed sushy task object
        """
        error_msg = (_('Firmware update failed for node %(node)s, '
                       'firmware %(firmware_image)s. '
                       'Error: %(errors)s') %
                     {'node': task.node.uuid,
                      'firmware_image': fw_upd['url'],
                      'errors': ",  ".join(self._task_messages(sushy_task))})
        self._fail(task, error_msg, traceback=True)

    def _read_task(self, node, fw_upd):
        """Read the Redfish task of a firmware update.

        :param node: an Ironic node object
        :param fw_upd: the firmware update whose task to read
        :returns: the sushy task, or None if it cannot be read now and
            should be read again on the next poll.
        :raises: RedfishError if the BMC no longer has the task.
        """
        try:
            task_monitor = redfish_utils.get_task_monitor(
                node, fw_upd['task_monitor'])
        except exception.RedfishConnectionError as e:
            LOG.warning('Unable to communicate with task monitor service '
                        'on node %(node)s. Will try again on the next poll. '
                        'Error: %(error)s',
                        {'node': node.uuid, 'error': e})
            return None

        try:
            # The last response does not necessarily contain a Task,
            # so get it
            return task_monitor.get_task()
        except sushy.exceptions.ResourceNotFoundError as e:
            raise exception.RedfishError(error=e)
        except Exception as e:
            LOG.warning('Unable to get task for node %(node)s: %(error)s. '
                        'Will retry on next poll.',
                        {'node': node.uuid, 'error': e})
            return None

    def _poll_staging(self, task, update_service, settings):
        """Poll a firmware update the BMC is staging.

        Firmware for components other than the BMC is applied while the
        node reboots. Once the BMC has staged it -- its task has completed,
        or waits in Starting or Pending for the reboot to run it -- the
        node is rebooted to apply it.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        """
        node = task.node
        idx = next((i for i, u in enumerate(settings)
                    if u.get(PHASE) == PHASE_STAGING), None)
        if idx is None:
            # The conductor stopped while submitting the next update of a
            # group, so its task, if the BMC created one, is unknown.
            self._fail_unrecognized_update(task)
            return
        fw_upd = settings[idx]
        try:
            sushy_task = self._read_task(node, fw_upd)
        except exception.RedfishError:
            self._staging_task_gone(task, update_service, settings, idx)
            return
        if sushy_task is None:
            return

        task_state = sushy_task.task_state
        if task_state in (sushy.TASK_STATE_STARTING,
                          sushy.TASK_STATE_PENDING):
            # A task waiting for the reboot to run it reports Starting or
            # Pending, but some BMCs report Starting briefly before
            # running the update themselves, so act once the state lasts.
            if not self._waited_in_starting(node, settings, fw_upd):
                return
        elif task_state in _TASK_IN_PROGRESS:
            if fw_upd.pop(STARTING_SINCE, None):
                node.set_driver_internal_info('redfish_fw_updates', settings)
                node.save()
            LOG.debug('%(component)s firmware update on node %(node)s is '
                      'still being staged (task state %(state)s).',
                      {'component': fw_upd['component'], 'node': node.uuid,
                       'state': task_state})
            return
        elif not self._task_succeeded(sushy_task):
            self._fail_task(task, fw_upd, sushy_task)
            return

        LOG.info('%(component)s firmware is staged on node %(node)s (task '
                 'state %(state)s).',
                 {'component': fw_upd['component'], 'node': node.uuid,
                  'state': task_state})
        self._staged(task, update_service, settings, idx)

    def _staged(self, task, update_service, settings, idx):
        """Continue once an update has been staged.

        With grouping allowed, the next update shares the reboot if it is
        not for the BMC, so it is submitted for staging in turn. Otherwise
        the node is rebooted to apply every staged update.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        :param idx: the index of the update just staged
        """
        node = task.node
        settings[idx].pop(STARTING_SINCE, None)
        settings[idx][PHASE] = PHASE_STAGED
        nxt = idx + 1
        if (node.driver_internal_info.get(GROUPING)
                and nxt < len(settings)
                and redfish_utils.get_component_type(
                    settings[nxt]['component']) != redfish_utils.BMC):
            LOG.info('Staging %(component)s firmware on node %(node)s to '
                     'apply it with the reboot of the updates staged '
                     'before it.',
                     {'component': settings[nxt]['component'],
                      'node': node.uuid})
            try:
                self._submit_simple_update(node, update_service,
                                           settings[nxt])
            except exception.IronicException as e:
                self._fail(task, _(
                    'Failed to submit %(component)s firmware for node '
                    '%(node)s: %(error)s.') % {
                        'component': settings[nxt]['component'],
                        'node': node.uuid, 'error': e})
                return
            settings[nxt][PHASE] = PHASE_STAGING
            self._save_updates(node, settings)
            return

        LOG.info('Rebooting node %(node)s to apply %(components)s '
                 'firmware.',
                 {'node': node.uuid,
                  'components': ', '.join(u['component']
                                          for u in settings[:nxt])})
        self._reboot_to_apply(task, settings, nxt)

    def _waited_in_starting(self, node, settings, fw_upd):
        """Whether a staging task has waited in Starting long enough.

        :param node: an Ironic node object
        :param settings: firmware update settings
        :param fw_upd: the firmware update being staged
        :returns: True once the task has been seen waiting for
            ``[redfish]firmware_update_starting_wait`` seconds.
        """
        since = fw_upd.get(STARTING_SINCE)
        if since is None:
            since = str(timeutils.utcnow().isoformat())
            fw_upd[STARTING_SINCE] = since
            node.set_driver_internal_info('redfish_fw_updates', settings)
            node.save()
        elapsed = timeutils.utcnow(True) - timeutils.parse_isotime(since)
        wait = CONF.redfish.firmware_update_starting_wait
        if elapsed.total_seconds() < wait:
            LOG.debug('%(component)s firmware update task on node %(node)s '
                      'is waiting to start. Treating it as staged once it '
                      'has waited %(wait)s seconds.',
                      {'component': fw_upd['component'], 'node': node.uuid,
                       'wait': wait})
            return False
        return True

    def _check_bmc_scheduled_firmware_update(self, task, current_update):
        """Check if the BMC has a scheduled job for this firmware update.

        Delegates to vendor-specific helpers when available.
        Currently only Dell iDRAC is supported via
        ``drac.firmware.check_scheduled_idrac_job``.

        :param task: a TaskManager instance
        :param current_update: the current firmware update being processed
        :returns: True if a matching scheduled job was found, False if
            no matching job exists, None if this check is not supported
        :raises: RedfishError or sushy.exceptions.SushyError if the BMC
            jobs cannot be read
        """
        vendor = task.node.properties.get('vendor', '')
        if vendor and 'Dell' in vendor.split():
            return drac_fw.check_scheduled_idrac_job(task, current_update)
        return None

    def _staging_task_gone(self, task, update_service, settings, idx):
        """Handle a staging task that the BMC no longer has.

        The task can disappear once the update is staged, or after its
        download failed. Where the BMC's job queue can be checked, a
        scheduled job tells the two apart; elsewhere the update is
        assumed to be staged, and the reboot applies it if it is.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        :param idx: the index of the update being staged
        """
        node = task.node
        fw_upd = settings[idx]
        try:
            has_job = self._check_bmc_scheduled_firmware_update(task, fw_upd)
        except (exception.RedfishError, sushy.exceptions.SushyError) as e:
            LOG.warning('Unable to check the BMC for a scheduled firmware '
                        'job on node %(node)s: %(error)s. Will try again on '
                        'the next poll.', {'node': node.uuid, 'error': e})
            return

        if has_job is None:
            LOG.info('The %(component)s firmware update task disappeared '
                     'from node %(node)s while staging. Assuming the '
                     'firmware was staged.',
                     {'component': fw_upd['component'], 'node': node.uuid})
        elif not has_job:
            self._fail(task, _(
                'Firmware update failed for node %(node)s, firmware '
                '%(firmware_image)s. The BMC task disappeared and no '
                'scheduled job was found, indicating the firmware download '
                'or staging failed.') % {'node': node.uuid,
                                         'firmware_image': fw_upd['url']})
            return
        else:
            LOG.info('The %(component)s firmware update task disappeared '
                     'from node %(node)s while staging. Scheduled job '
                     'confirmed.',
                     {'component': fw_upd['component'], 'node': node.uuid})
        self._staged(task, update_service, settings, idx)

    def _observe_boot(self, node):
        """Read the markers that show whether the node's host has reset.

        :param node: the Ironic node object.
        :returns: an observation from
            :func:`redfish_utils.get_boot_observation`, or None if the
            System cannot be read.
        """
        try:
            system = redfish_utils.get_system(node)
            return redfish_utils.get_boot_observation(system)
        except (exception.RedfishError, sushy.exceptions.SushyError) as e:
            LOG.warning('Could not read the boot state of node %(node)s '
                        'before rebooting it to apply firmware: %(error)s',
                        {'node': node.uuid, 'error': e})
            return None

    def _reboot_to_apply(self, task, settings, count=1):
        """Reboot the node to apply the staged firmware update.

        The update enters the applying phase before the reboot is
        requested, so that a conductor restart in between never requests
        the reboot a second time. The phase records what the post-reboot
        gates need under ``VERIFY``: when the reboot was requested, the
        boot markers read before it, and the Dell Lifecycle Controller jobs
        applying the update.

        :param task: a TaskManager instance
        :param settings: firmware update settings; the first ``count``
            entries are the updates applied by this reboot.
        :param count: how many updates the reboot applies.
        """
        node = task.node
        fw_upd = settings[0]
        jids = []
        for upd in settings[:count]:
            jid = drac_fw.jid_from_task_monitor(upd.get('task_monitor'))
            if jid:
                jids.append(jid)
            upd.pop(STARTING_SINCE, None)
            upd[PHASE] = PHASE_APPLYING
        fw_upd[VERIFY] = {
            'reboot_time': str(timeutils.utcnow().isoformat()),
            'before': self._observe_boot(node),
            'jids': jids,
            'new_boot': False,
            'lc': None,
            'os_boot_seen': None,
            'waiting': None,
            'passed': False,
        }
        deploy_utils.set_async_step_flags(node, reboot=True, polling=True)
        self._save_updates(node, settings)

        try:
            manager_utils.node_power_action(task, states.REBOOT,
                                            fw_upd.get('power_timeout', 0))
        except exception.IronicException as e:
            self._fail(task, _(
                'Failed to reboot node %(node)s to apply %(component)s '
                'firmware: %(error)s. Inspect the node and its BMC before '
                'retrying.') % {'node': node.uuid,
                                'component': fw_upd['component'],
                                'error': e})
            return

        # The gates' delays run from the reboot itself.
        fw_upd[VERIFY]['reboot_time'] = str(timeutils.utcnow().isoformat())
        self._save_updates(node, settings)

    def _save_updates(self, node, settings):
        """Persist the firmware updates of a node.

        :param node: the Ironic node object.
        :param settings: firmware update settings.
        """
        node.set_driver_internal_info('redfish_fw_updates', settings)
        node.save()

    def _applying_count(self, settings):
        """How many updates, at the head of settings, one reboot applies.

        :param settings: firmware update settings.
        :returns: the number of leading updates in the applying phase, and
            at least 1.
        """
        count = 0
        for fw_upd in settings:
            if fw_upd.get(PHASE) != PHASE_APPLYING:
                break
            count += 1
        return max(count, 1)

    def _poll_applying(self, task, update_service, settings, fw_upd):
        """Poll a firmware update applied by rebooting the node.

        A finished Redfish task does not prove that the firmware has been
        applied: firmware staged for the reboot is flashed during POST, and
        a node powered off or a step resumed then can interrupt it. So the
        update only continues once its task has finished and every check
        the platform offers has passed:

        * Dell: the Lifecycle Controller jobs applying it have finished.
        * BootProgress shows the node booted past POST: to OSRunning while
          servicing, so the firmware versions read next are current.

        All of it is bounded by
        ``[redfish]firmware_update_post_reboot_verify_timeout``.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service, or None
            if it cannot be reached
        :param settings: firmware update settings
        :param fw_upd: the firmware update being applied, ``settings[0]``
        """
        node = task.node
        verify = fw_upd[VERIFY]
        timeout = CONF.redfish.firmware_update_post_reboot_verify_timeout
        applying = settings[:self._applying_count(settings)]
        components = ', '.join(u['component'] for u in applying)
        if not verify['passed']:
            waiting = verify.get('waiting')
            try:
                applied = self._verify_applied(task, applying, verify)
            except exception.IronicException as e:
                self._fail(task, str(e))
                return
            if not applied:
                if self._verify_timed_out(verify):
                    self._fail(task, _(
                        'Firmware update of %(component)s on node %(node)s '
                        'could not be verified within %(timeout)s seconds of '
                        'the reboot applying it: %(reason)s. The firmware may '
                        'still be applying; check the node and its BMC '
                        'before power-cycling it.') % {
                            'component': components,
                            'node': node.uuid, 'reason': verify['waiting'],
                            'timeout': timeout})
                else:
                    # Log what is awaited when it changes, not on every
                    # poll.
                    log = (LOG.debug if verify['waiting'] == waiting
                           else LOG.info)
                    log('Firmware update of %(component)s on node %(node)s '
                        'is not verified yet: %(reason)s.',
                        {'component': components, 'node': node.uuid,
                         'reason': verify['waiting']})
                    self._save_updates(node, settings)
                return
            LOG.info('%(component)s firmware update applied on node '
                     '%(node)s.', {'component': components, 'node': node.uuid})
            verify['passed'] = True
            self._save_updates(node, settings)

        self._continue_updates(task, update_service, settings)

    def _verify_timed_out(self, verify):
        """Whether the post-reboot verify timeout has elapsed.

        :param verify: the ``VERIFY`` state of the update.
        """
        timeout = CONF.redfish.firmware_update_post_reboot_verify_timeout
        if timeout <= 0:
            return False
        elapsed = timeutils.utcnow(True) - timeutils.parse_isotime(
            verify['reboot_time'])
        return elapsed.total_seconds() >= timeout

    def _since_reboot(self, verify):
        """Seconds since the reboot applying the update."""
        return (timeutils.utcnow(True) - timeutils.parse_isotime(
            verify['reboot_time'])).total_seconds()

    def _verify_applied(self, task, applying, verify):
        """Check whether rebooted updates have been applied.

        Updates ``verify`` with what this poll has seen.

        :param task: a TaskManager instance
        :param applying: the firmware updates the reboot applies
        :param verify: their ``VERIFY`` state
        :returns: True if the updates have been applied. Otherwise False,
            and ``verify['waiting']`` says what is still awaited.
        :raises: IronicException if an update has failed.
        """
        node = task.node
        system = self._detect_reset(node, verify)

        # A failed task is only reported once no task or Lifecycle
        # Controller job of the reboot is still in progress, so that the
        # step does not end while another update is still being applied.
        failed = None
        for fw_upd in applying:
            try:
                sushy_task = self._read_task(node, fw_upd)
            except exception.RedfishError:
                LOG.info('The %(component)s firmware update task is gone '
                         'from node %(node)s after the reboot to apply it.',
                         {'component': fw_upd['component'],
                          'node': node.uuid})
                continue
            if sushy_task is None:
                verify['waiting'] = _('the %s task could not be '
                                      'read') % fw_upd['component']
                return False
            if sushy_task.task_state in _TASK_IN_PROGRESS:
                verify['waiting'] = _(
                    'the %(component)s task is still %(state)s') % {
                        'component': fw_upd['component'],
                        'state': _name(sushy_task.task_state)}
                return False
            if failed is None and not self._task_succeeded(sushy_task):
                failed = (fw_upd, sushy_task)
        failure = None
        if failed:
            fw_upd, sushy_task = failed
            failure = exception.IronicException(_(
                'Firmware update failed for node %(node)s, firmware '
                '%(firmware_image)s. Error: %(errors)s') % {
                    'node': node.uuid,
                    'firmware_image': fw_upd['url'],
                    'errors': ',  '.join(
                        self._task_messages(sushy_task))})

        if verify['lc'] is None:
            if verify['jids']:
                try:
                    status, detail = drac_fw.check_lc_jobs(task,
                                                           verify['jids'])
                except (exception.RedfishError,
                        sushy.exceptions.SushyError) as e:
                    verify['waiting'] = _('its Lifecycle Controller jobs '
                                          'could not be read: %s') % e
                    return False
            else:
                status = drac_fw.LCJobStatus.UNAVAILABLE
            if status == drac_fw.LCJobStatus.RUNNING:
                verify['waiting'] = _('Lifecycle Controller jobs %s are '
                                      'still running') % detail
                return False
            if failure:
                raise failure
            if status == drac_fw.LCJobStatus.ERROR:
                raise exception.IronicException(_(
                    'Firmware update of %(component)s on node %(node)s '
                    'failed: the Dell Lifecycle Controller did not apply '
                    'the firmware: %(detail)s') % {
                        'component': ', '.join(u['component']
                                               for u in applying),
                        'node': node.uuid, 'detail': detail})
            if status == drac_fw.LCJobStatus.DONE:
                LOG.info('Dell Lifecycle Controller jobs %(jids)s '
                         'finished on node %(node)s.',
                         {'jids': ', '.join(verify['jids']),
                          'node': node.uuid})
            verify['lc'] = status.value

        if failure:
            raise failure
        if system is None:
            verify['waiting'] = _('its System could not be read')
            return False
        return self._check_boot(node, system, verify)

    def _detect_reset(self, node, verify):
        """Read the node's System and check whether the host has reset.

        :param node: an Ironic node object.
        :param verify: the ``VERIFY`` state, updated in place once the
            host is seen to have reset since the reboot was requested.
        :returns: the sushy System, or None if it could not be read.
        """
        try:
            system = redfish_utils.get_system(node)
            observation = redfish_utils.get_boot_observation(system)
        except (exception.RedfishError, sushy.exceptions.SushyError) as e:
            LOG.warning('Unable to read the boot state of node %(node)s: '
                        '%(error)s. Will check again on the next poll.',
                        {'node': node.uuid, 'error': e})
            return None

        if (not verify['new_boot']
                and redfish_utils.boot_changed_since(verify['before'],
                                                     observation)):
            LOG.info('Node %(node)s has reset since it was rebooted to '
                     'apply firmware: %(before)s, now %(now)s.',
                     {'node': node.uuid, 'before': verify['before'],
                      'now': observation})
            verify['new_boot'] = True
        return system

    def _check_boot(self, node, system, verify):
        """Check that the node has booted past POST since the reboot.

        :param node: an Ironic node object.
        :param system: the sushy System read in this poll.
        :param verify: the ``VERIFY`` state, updated in place.
        :returns: True if the boot gate has passed. Otherwise False, and
            ``verify['waiting']`` says what is still awaited.
        """
        check_delay = CONF.redfish.firmware_update_boot_check_delay
        targets = redfish_utils.get_boot_progress_targets(node)
        try:
            status, last_state = redfish_utils.check_boot_progress(
                node, system, targets, reboot_time=verify['reboot_time'],
                check_delay=check_delay, new_boot_observed=verify['new_boot'])
        except sushy.exceptions.SushyError as e:
            verify['waiting'] = _('its BootProgress could not be read: '
                                  '%s') % e
            return False

        if status == redfish_utils.BOOT_PROGRESS_PASSED:
            LOG.info('Node %(node)s reached BootProgress %(state)s after '
                     'the reboot to apply firmware.',
                     {'node': node.uuid, 'state': _name(last_state)})
            return True

        delay_passed = self._since_reboot(verify) >= check_delay

        if status == redfish_utils.BOOT_PROGRESS_UNAVAILABLE:
            # Nothing says when POST, and any firmware flashed during it,
            # is over, unless a Lifecycle Controller job applying the
            # update finished: a reset only shows POST has started.
            # Otherwise only the delay protects it.
            if (verify['lc'] == drac_fw.LCJobStatus.DONE.value
                    or delay_passed):
                LOG.info('Node %(node)s reports no BootProgress, so its '
                         'boot after the reboot to apply firmware was not '
                         'confirmed.', {'node': node.uuid})
                return True
            verify['waiting'] = _(
                'the node reports no BootProgress, so '
                '[redfish]firmware_update_boot_check_delay must pass')
            return False

        if ((verify['new_boot'] or delay_passed)
                and last_state in redfish_utils.BOOT_PROGRESS_POST_COMPLETE):
            # POST is over, but servicing waits for OSRunning, which some
            # BMCs never report. Bound that wait from the end of POST.
            if verify['os_boot_seen'] is None:
                verify['os_boot_seen'] = str(timeutils.utcnow().isoformat())
            waited = (timeutils.utcnow(True) - timeutils.parse_isotime(
                verify['os_boot_seen'])).total_seconds()
            timeout = CONF.redfish.firmware_update_os_running_timeout
            if waited >= timeout:
                LOG.warning('Node %(node)s reported BootProgress %(state)s '
                            'but not OSRunning within the configured wait. '
                            'Not every BMC reports OSRunning, so '
                            'proceeding.',
                            {'node': node.uuid, 'state': _name(last_state)})
                return True
            verify['waiting'] = _(
                'BootProgress is %(state)s; waiting up to %(timeout)s '
                'seconds for OSRunning') % {
                    'state': _name(last_state), 'timeout': timeout}
            return False
        if last_state in targets:
            # Until the host is seen to reset, a target state may still
            # describe the boot before the reboot.
            verify['waiting'] = _(
                'BootProgress is %s, but the node has not been seen to '
                'reset since the reboot') % _name(last_state)
            return False
        verify['waiting'] = _('BootProgress is %s') % _name(last_state)
        return False

    def _fail_unrecognized_update(self, task):
        """Fail an update whose progress was not recorded.

        It was started by a version without update phases, or the
        conductor stopped while submitting it, after the BMC may have
        accepted it but before its task was recorded. Such an update cannot
        be followed safely, and the BMC may still hold its firmware.

        :param task: a TaskManager instance
        """
        node = task.node
        self._fail(task, _(
            'The firmware update of node %(node)s cannot be resumed: it was '
            'started by an earlier version of Ironic, or the conductor '
            'stopped while submitting it. Check the firmware jobs on its '
            'BMC, then run the update again.') % {
                'node': node.uuid})

    def _handle_wait_completion(self, task, update_service, settings,
                                current_update):
        """Handle firmware update wait completion.

        :param task: a TaskManager instance
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        :param current_update: the current firmware update being processed
        """
        # Upgrade lock at the start since we may modify driver_internal_info
        task.upgrade_lock()
        node = task.node

        # Check if this is BMC version checking
        if current_update.get('bmc_version_checking'):
            current_update.pop('bmc_version_checking', None)
            node.set_driver_internal_info(
                'redfish_fw_updates', settings)
            node.save()
            # Continue BMC version checking
            self._handle_bmc_update_completion(
                task, update_service, settings, current_update)
        elif current_update.get('component_type') == redfish_utils.BMC:
            # BMC update wait expired - check if task is still running
            # before transitioning to version checking
            task_still_running = False
            try:
                task_monitor = redfish_utils.get_task_monitor(
                    node, current_update['task_monitor'])
                if task_monitor.is_processing:
                    task_still_running = True
                    LOG.debug('BMC firmware update wait expired but task '
                              ' still processing for node %(node)s. '
                              'Continuing to monitor task completion.',
                              {'node': node.uuid})
            except exception.RedfishConnectionError as e:
                LOG.debug('Unable to communicate with task monitor for node '
                          '%(node)s during wait completion: %(error)s. '
                          'BMC may be resetting, will transition to version '
                          'checking.', {'node': node.uuid, 'error': e})
            except exception.RedfishError as e:
                LOG.debug('Task monitor unavailable for node %(node)s: '
                          '%(error)s. Task may have completed, transitioning '
                          'to version checking.',
                          {'node': node.uuid, 'error': e})

            if task_still_running:
                # Task is still running, continue to monitor task completion
                # Don't transition to version checking yet.
                node.set_driver_internal_info('redfish_fw_updates', settings)
                node.save()
                return

            # Task completed, deleted or BMC unavailable
            # Transition to version checking
            LOG.info('BMC firmware update wait expired for node %(node)s. '
                     'Task completed or unavailable. Transitioning to version '
                     'checking mode.',
                     {'node': node.uuid})
            self._handle_bmc_update_completion(
                task, update_service, settings, current_update)
        else:
            # Regular wait completion
            self._continue_updates(task, update_service, settings)

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

        start_time_str = node.driver_internal_info.get(
            'redfish_fw_update_start_time')
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
        self._clear_updates(node)
        self._report_step_error(task, msg, traceback=False)
        return True

    def _handle_firmware_update_task(self, task, node, current_update,
                                     update_service, settings):
        """Handle the BMC firmware update task monitoring and completion.

        :param task: a TaskManager instance
        :param node: an Ironic node object
        :param current_update: the current firmware update being processed
        :param update_service: the sushy firmware update service
        :param settings: firmware update settings
        """
        try:
            sushy_task = self._read_task(node, current_update)
        except exception.RedfishError:
            # The BMC deleted the Task before we could query it
            LOG.warning('Firmware update completed for node %(node)s, '
                        'firmware %(firmware_image)s, but success of the '
                        'update is unknown.  Assuming update was successful.',
                        {'node': node.uuid,
                         'firmware_image': current_update['url']})
            self._continue_updates(task, update_service, settings)
            return
        if sushy_task is None:
            return

        if sushy_task.task_state in _TASK_IN_PROGRESS:
            LOG.debug('Firmware update in progress for node %(node)s, '
                      'firmware %(firmware_image)s.',
                      {'node': node.uuid,
                       'firmware_image': current_update['url']})
            return

        LOG.debug('Firmware update task in terminal state %(state)s '
                  'for node %(node)s',
                  {'state': sushy_task.task_state, 'node': node.uuid})
        self._handle_task_completion(task, sushy_task,
                                     self._task_messages(sushy_task),
                                     update_service, settings,
                                     current_update)

    @METRICS.timer('RedfishFirmware._check_node_redfish_firmware_update')
    def _check_node_redfish_firmware_update(self, task):
        """Check the progress of running firmware update on a node."""
        # Upgrade the lock to ensure we are using the latest info from
        # the node.
        task.upgrade_lock()
        node = task.node

        # Check overall timeout for firmware update operation
        if self._check_overall_timeout(task):
            return

        settings = node.driver_internal_info['redfish_fw_updates']
        current_update = settings[0]

        try:
            update_service = redfish_utils.get_update_service(node)
        except exception.RedfishConnectionError as e:
            # If the BMC firmware is being updated, the BMC will be
            # unavailable for some amount of time.
            LOG.warning('Unable to communicate with firmware update service '
                        'on node %(node)s. Will try again on the next poll. '
                        'Error: %(error)s',
                        {'node': node.uuid, 'error': e})
            # Verifying an applied update does not need the service, and
            # must still time out while the BMC is unreachable.
            if current_update.get(PHASE) != PHASE_APPLYING:
                return
            update_service = None

        # An update started by an earlier version, whose BMC update
        # completed and node rebooted: continue with the next component.
        if current_update.get(BMC_UPDATE_COMPLETED):
            LOG.info('BMC firmware update completed and node %(node)s has '
                     'rebooted. Continuing with next component.',
                     {'node': node.uuid})
            current_update.pop(BMC_UPDATE_COMPLETED, None)
            node.set_driver_internal_info('redfish_fw_updates', settings)
            node.save()

            self._continue_updates(task, update_service, settings)
            return

        # Touch provisioning to indicate progress is being monitored.
        # This prevents heartbeat timeout from triggering for steps that
        # don't require the ramdisk agent (requires_ramdisk=False).
        # Note: Only touch after successful BMC communication to ensure
        # the process eventually times out if the BMC is unresponsive,
        # unless an applied update is being verified, which has its own
        # timeout.
        node.touch_provisioning()

        wait_start_time = current_update.get('wait_start_time')
        if wait_start_time:
            wait_start = timeutils.parse_isotime(wait_start_time)

            elapsed_time = timeutils.utcnow(True) - wait_start
            if elapsed_time.seconds >= current_update['wait']:
                LOG.debug('Finished waiting after firmware update '
                          '%(firmware_image)s on node %(node)s. '
                          'Elapsed time: %(seconds)s seconds',
                          {'firmware_image': current_update['url'],
                           'node': node.uuid,
                           'seconds': elapsed_time.seconds})
                current_update.pop('wait', None)
                current_update.pop('wait_start_time', None)

                # Handle wait completion
                self._handle_wait_completion(
                    task, update_service, settings, current_update)
            else:
                LOG.debug('Continuing to wait after firmware update '
                          '%(firmware_image)s on node %(node)s. '
                          'Elapsed time: %(seconds)s seconds',
                          {'firmware_image': current_update['url'],
                           'node': node.uuid,
                           'seconds': elapsed_time.seconds})

            return

        phase = current_update.get(PHASE)
        if phase in (PHASE_STAGING, PHASE_STAGED):
            self._poll_staging(task, update_service, settings)
        elif phase == PHASE_APPLYING or VERIFY in current_update:
            # An update the node was rebooted to apply by an earlier
            # version records the same VERIFY state, without a phase.
            self._poll_applying(task, update_service, settings,
                                current_update)
        elif (redfish_utils.get_component_type(current_update['component'])
                != redfish_utils.BMC):
            self._fail_unrecognized_update(task)
        else:
            self._handle_firmware_update_task(
                task, node, current_update, update_service, settings)

    def _stage_firmware_file(self, node, component_update):

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
            firmware_utils.cleanup(node)
            raise
