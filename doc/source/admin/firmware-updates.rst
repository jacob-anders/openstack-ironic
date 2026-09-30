Firmware Updates
================

The firmware update step applies components sequentially in the requested order
during cleaning, deployment or servicing. By default each non-BMC component
receives its own apply reboot; adjacent components can optionally share a reboot.
BMC firmware uses version and resource-recovery checks and may
require a host handoff reboot before another component can be submitted.

The current segment is retained until its available task/job, host-recovery
and inventory checks pass. A failure stops the queue, preserves power and places
the node in maintenance. The executable queue is discarded so it cannot resume
automatically; diagnostics and tracked job outcomes remain in the node's error
and history. Inspect the BMC jobs before recovering and retrying an update.

Progress is stored in one versioned ``redfish_fw_update`` record in the node's
``driver_internal_info``. Its named phases cover staging, application, BMC
recovery, boot and inventory verification. Phase transitions and complete action
intent are persisted before submissions or resets; ambiguous responses do not
cause those hardware actions to be replayed after a conductor restart.

Legacy in-flight queues and unknown record versions enter maintenance for
operator recovery rather than being resumed without reliable application
evidence. Inspect BMC tasks and staged jobs before retrying such an update.

.. note:: Only :doc:`/admin/drivers/redfish` supports firmware updates
   currently.

The optional per-component ``wait`` has different uses. For a BMC update it
controls the initial/version-check wait, defaulting to
``[redfish]firmware_update_reboot_delay`` (300 seconds). For a non-BMC component
it sets a minimum post-application settling interval, with
``firmware_update_inventory_wait`` providing the lower bound. Elapsed time alone
does not replace a required firmware-job outcome.

Grouping host reboots
---------------------

Set the boolean ``allow_grouping_reboots`` in the step's ``args`` to ``true`` to
stage adjacent non-BMC images and apply them with one consolidated host reboot.
The default is ``false``. Grouping supports the same ``bmc``, ``bios`` and
``nic:<Id>`` component names; it does not reorder the requested settings.

The recommended order is BMC first, then BIOS and NICs. A BMC entry ends the
preceding non-BMC segment: ``[bios, nic:1, bmc, nic:2]`` has two non-BMC segments.
BMC recovery may also require its host handoff reboot, so one reboot per
non-BMC segment does not mean one reboot for the whole mixed request.
Duplicates within a non-BMC segment are rejected, including different ID/serial
aliases for the same NIC. Repeats across BMC boundaries or without grouping are
allowed. All requested NIC identities are bound before any image in their
segment is submitted.

.. code-block:: json

   [{
     "interface": "firmware",
     "step": "update",
     "args": {
       "allow_grouping_reboots": true,
       "settings": [
         {"component": "bmc", "url": "https://example.com/bmc.bin"},
         {"component": "bios", "url": "https://example.com/bios.exe"},
         {"component": "nic:NIC.Integrated.1-1-1",
          "url": "https://example.com/nic.zip"}
       ]
     }
   }]

Grouped ``SimpleUpdate`` requests explicitly request ``OnReset`` through the
standard ``@Redfish.OperationApplyTime`` action annotation. A rejected request
fails the step rather than being retried without deferred application. On Dell,
Ironic waits for an armed Lifecycle Controller job before staging another image;
on other platforms it waits for staging task completion. A generic ``Starting``
task or the BIOS compatibility policy does not authorize another grouped image.

Observed task success is retained across polls and conductor restarts. A task
disappearing before success leaves its outcome unknown; it needs supported OEM
evidence or fails at the segment deadline. A deferred ``OnReset`` response with
no monitor is also unknown, rather than synchronous staging success. Positive
OEM job evidence can resolve it; without that evidence, Ironic neither submits
another image nor resets the host. Before each additional image, Dell job checks
cover all submitted segment members, so an earlier failure stops staging.

The segment uses the largest requested non-BMC ``wait``, with
``firmware_update_inventory_wait`` as a lower bound. Budget for the whole batch's
staging, POST, flashing and recovery. ``firmware_update_tasks_per_poll`` limits
application monitoring to 16 task reads per invocation by default; subsequent
polls rotate through the remaining tasks.

Each poll persists the complete operation record, including per-image and Dell
job evidence. Very large batches increase database write volume and node-lock
time as well as staging and recovery duration. Prefer smaller steps when those
costs become significant; increasing a timeout alone does not reduce them.

If a later image fails staging, the whole step fails without a consolidated
reboot. Images already staged may still apply on the next boot from any source.
Inspect pending BMC Tasks/Jobs and remove queued images if appropriate before
aborting servicing and retrying. Failure handling preserves power even when
cleaning or servicing failure power-off options are enabled.

How it works
------------

The ``update`` step can be used via cleaning, servicing or deployment. It accepts
JSON in the following format::

    [{
        "interface": "firmware",
        "step": "update",
        "args": {
            "settings":[
                {
                    "component": "bmc",
                    "url": "<url_to_firmware_image1>",
                    "wait": <number_of_seconds_to_wait>
                },
                {
                    "component": "bios",
                    "url": "<url_to_firmware_image2>"
                },
                {
                    "component": "nic:AD0700",
                    "url": "<url_to_firmware_image3>"
                },
                {
                    "component": "nic:NIC.Slot.2",
                    "url": "<url_to_firmware_image4>"
                }
                ...
            ]
        }
    }]

The different attributes of the ``update`` step are as follows:

.. csv-table::
    :header: "Attribute", "Description"
    :widths: 30, 120

    "``interface``", "Interface of the step.  Must be ``firmware`` for firmware update"
    "``step``", "Name of the step.  Must be ``update`` for firmware update"
    "``args``", "Keyword-argument entry (<name>: <value>) being passed to the step"
    "``args.settings``", "Ordered list of dictionaries of firmware updates to be applied"
    "``args.allow_grouping_reboots``", "Optional boolean; share a reboot among adjacent non-BMC updates (default false)"

Each firmware image dictionary is of the form::

    {
      "component": "The desired component to have the firmware updated, supported components are listed below",
      "url": "<URL of firmware image file>",
      "wait": <Optional settling interval or BMC version-check wait in seconds>
    }

.. csv-table::
    :header: "Supported Components", "Description"
    :widths: 30, 120

    "bmc", "The BMC firmware"
    "bios", "The BIOS firmware"
    "nic:<NIC_REDFISH_ID>", "Since machines can have multiple NICs, we use **nic:** as prefix plus the **NIC_REDFISH_ID** to identify the NIC to update"

When NetworkAdapters inventory is readable, ``nic:<SerialNumber>`` is also
accepted if it identifies exactly one adapter. Ironic binds either form before
submission and verifies that same adapter after recovery. A unique serial can
identify the adapter if its Redfish ID changes. Duplicate serials require an
unambiguous Redfish ID; unknown or ambiguous names are rejected before the image
is submitted. Missing requested inventory remains a recovery failure, rather
than being satisfied by another adapter's firmware version.

The ``component`` and ``url`` arguments in the firmware image dictionary are
mandatory, while the ``wait`` argument is optional.

For ``url`` currently ``http``, ``https``, ``swift`` and ``file`` schemes are
supported.

Verification policies
---------------------

Dell Lifecycle Controller jobs must report successful application, including
late jobs discovered while inventory settles. A missing or unreadable job does
not establish success. If a job fails while another is still running, Ironic
fails the step in maintenance without powering off, rebooting or continuing to
another image. Retiring the Ironic queue does not cancel BMC-side jobs.

``driver_info/firmware_update_boot_progress`` defaults to ``auto``. Reported
readiness is required: ``OSRunning`` for servicing, with hardware initialization
completion, OS boot start or setup also accepted for cleaning/deployment.
For a platform documented to report only intermediate stages, explicitly set
``limited`` to use the configured boot-check delay. A stalled supported check is
not automatically classified as unavailable. Available job checks still apply.

``driver_info/firmware_update_bios_pending_reset`` defaults to ``auto``, which
waits for Pending or Running BIOS tasks. For hardware documented to retain these
states until reset, ``compatibility`` permits the apply reset after the staging
wait. This policy must not be used for tasks that are actively flashing.
Application and boot verification are still required after that reset.

For example, select a documented platform policy with:

.. code-block:: console

   $ baremetal node set <node> --driver-info firmware_update_boot_progress=limited
   $ baremetal node set <node> --driver-info firmware_update_bios_pending_reset=compatibility

Some platforms, notably HPE, expose NIC inventory only while an OS is running.
Keep the instance OS running during servicing, or boot IPA before day-0 NIC
updates. Ironic waits for supported inventory to become readable before staging.
For a grouped segment, this prerequisite holds the whole segment, including a
preceding BIOS image. Disabling the ramdisk does not remove this hardware
requirement; Ironic does not boot a tenant OS merely to satisfy it.

Firmware packages are vendor-specific. Ironic does not infer an expected version
from the filename or image contents, and no expected-version input is required.
It verifies available task/job outcomes and recovery, then refreshes inventory;
this is not an exact comparison against an image's intended version.
Same-version reinstalls are valid. An unchanged BMC version requires a positive
task or job result, rather than expiration of its version-check wait.

Timeout hierarchy
-----------------

The first applicable deadline to expire fails the update. All values below are
``[redfish]`` configuration options, in seconds:

* ``firmware_update_overall_timeout`` (7200) bounds the complete settings list.
  Zero disables only this overall limit.
* ``firmware_update_apply_timeout`` (1800) bounds one segment from preparation
  through staging, application and inventory recovery. A segment contains one
  component unless grouping is enabled. This deadline always remains finite.
* ``firmware_update_post_reboot_verify_timeout`` (1800) bounds all remaining
  verification from the apply-reset request, including inventory recovery and
  late LC jobs. Entering inventory does not restart or bypass it. Zero disables
  this narrower deadline while the segment limit remains enforced.
* ``firmware_update_os_running_timeout`` (300) bounds the servicing wait from
  observed POST completion to OSRunning. Zero uses the broader budgets.
* ``firmware_update_resource_validation_timeout`` (480) bounds BMC resource
  recovery, also within the segment and overall budgets.

``firmware_update_reboot_min_wait`` (60), ``firmware_update_boot_check_delay``
(600 for absent/limited telemetry) and ``firmware_update_inventory_wait`` (60)
are minimum waits, not permission to bypass supported checks. Include these and
any explicit per-component ``wait`` in the applicable deadline budgets. For
slow POST, increase the post-reboot and segment budgets together; for a slow
BMC update, size the segment and resource-recovery budgets for that hardware.

Reads occur on ``firmware_update_status_interval`` (60) polls. The BMC
``firmware_update_validation_interval`` (30) is a minimum between samples, not
a blocking sleep. At default settings, three consecutive successful samples
span at least 120 seconds from the first sample to the third. Failed samples
restart the success count, without extending the validation deadline.

The legacy ``firmware_update_bmc_timeout`` and
``firmware_update_wait_unresponsive_bmc`` options, and the per-node
``firmware_update_unresponsive_bmc_wait`` property, are deprecated and ignored by
this interface. They do not extend these budgets.
The deprecated ``firmware_update_reboot_watch_timeout`` is also ignored; reset
evidence is sampled asynchronously rather than in a blocking watch.

Applying updates
----------------

To perform a firmware update, first download the firmware to a web server,
Swift or filesystem that the Ironic conductor or BMC has network access to.
This could be the ironic conductor web server or another web server on the BMC
network. Using a web browser, curl, or similar tool on a server that has
network access to the BMC or Ironic conductor, try downloading the firmware to
verify that the URLs are correct and that the web server is configured
properly.

Next, construct the JSON for the firmware update step to be executed.
When launching the firmware update, the JSON may be specified on the command
line directly or in a file. The following example shows one step that
installs two firmware updates.

.. code-block:: json

    [{
        "interface": "firmware",
        "step": "update",
        "args": {
            "settings":[
                {
                    "component": "bmc",
                    "url": "http://192.0.2.10/BMC_4_22_00_00.EXE",
                    "wait": 300
                },
                {
                    "component": "bios",
                    "url": "https://192.0.2.10/BIOS_19.0.12_A00.EXE"
                },
                {
                    "component": "nic:AD0700",
                    "url": "http://192.0.2.10/NIC_19.0.12_AD0700.EXE"
                },
                {
                    "component": "nic:NIC.Slot.2",
                    "url": "http://192.0.2.10/NICSlot2_1.0.12.EXE"
                }
            ]
        }
    }]


It is also possible to use ``runbooks`` for firmware updates.

First, create a YAML file with the firmware update steps. For example, save the
following as ``firmware_runbook.yaml``:

.. code-block:: yaml

    - interface: firmware
      step: update
      args:
        settings:
          - component: bmc
            url: http://192.168.0.8:8080/ilo5278.bin
          - component: nic:AD0700
            url: http://192.168.0.8:8080/nic.bin
          - component: nic:NIC.Slot.2
            url: http://192.168.0.8:8080/nic.bin

Then create and use the runbook:

.. code-block:: console

    $ baremetal runbook create --name <RUNBOOK> --steps firmware_runbook.yaml
    $ baremetal node add trait <ironic_node_uuid> <RUNBOOK>
    $ baremetal node <clean or service>  <ironic_node_uuid> --runbook <RUNBOOK>

Finally, launch the firmware update step against the node. The
following example assumes the above JSON is in a file named
``update.json``:

.. code-block:: console

   $ baremetal node clean <ironic_node_uuid> --clean-steps update.json
   $ baremetal node service <ironic_node_uuid> --service-steps update.json

.. note::
   You can use the ``--disable-ramdisk`` flag to perform firmware updates
   without booting into the ramdisk, which saves reboots and speeds up the
   process:

   .. code-block:: console

      $ baremetal node clean <ironic_node_uuid> --disable-ramdisk --clean-steps update.json
      $ baremetal node service <ironic_node_uuid> --disable-ramdisk --service-steps update.json

In the following example, the JSON is specified directly on the command line:

.. code-block:: console

   $ baremetal node clean <ironic_node_uuid> --clean-steps \
       '[{"interface": "firmware", "step": "update", "args": {"settings":[{"component": "bmc", "url":"http://192.168.0.8:8080/ilo5278.bin"}]}}]'
   $ baremetal node clean <ironic_node_uuid> --clean-steps \
       '[{"interface": "firmware", "step": "update", "args": {"settings":[{"component": "bios", "url":"http://192.168.0.8:8080/bios.bin"}]}}]'
   $ baremetal node service <ironic_node_uuid> --service-steps \
       '[{"interface": "firmware", "step": "update", "args": {"settings":[{"component": "bmc", "url":"http://192.168.0.8:8080/ilo5278.bin"}]}}]'
   $ baremetal node service <ironic_node_uuid> --service-steps \
       '[{"interface": "firmware", "step": "update", "args": {"settings":[{"component": "bios", "url":"http://192.168.0.8:8080/bios.bin"}]}}]'
   $ baremetal node clean <ironic_node_uuid> --clean-steps \
       '[{"interface": "firmware", "step": "update", "args": {"settings":[{"component": "nic:AD0700", "url":"http://192.168.0.8:8080/nic.bin"}]}}]'
   $ baremetal node clean <ironic_node_uuid> --clean-steps \
       '[{"interface": "firmware", "step": "update", "args": {"settings":[{"component": "nic:NIC.Slot.2", "url":"http://192.168.0.8:8080/nic.bin"}]}}]'

.. note::
   For Dell machines, you must extract the firmimgFIT.d9 from the iDRAC.exe
   This can be done using the command ``7za e iDRAC_<VERSION>.exe``.

.. note::
   For HPE machines you must extract the ilo5_<version>.bin from the
   ilo5_<version>.fwpkg
   This can be done using the command ``7za e ilo<version>.fwpkg``.

How it works - via Management Interface
---------------------------------------

.. deprecated::
   The management interface for firmware updates is deprecated in favor of
   the firmware interface. Please use the firmware interface for new
   deployments.

The ``update_firmware`` cleaning step accepts JSON in the following format::

    [{
        "interface": "management",
        "step": "update_firmware",
        "args": {
            "firmware_images":[
                {
                    "url": "<url_to_firmware_image1>",
                    "checksum": "<checksum for image, uses SHA1, SHA256, or SHA512>",
                    "source": "<optional override source setting for image>",
                    "wait": <number_of_seconds_to_wait>
                },
                {
                    "url": "<url_to_firmware_image2>"
                },
                ...
            ]
        }
    }]

The different attributes of the ``update_firmware`` cleaning step are as follows:

.. csv-table::
    :header: "Attribute", "Description"
    :widths: 30, 120

    "``interface``", "Interface of the cleaning step.  Must be ``management`` for firmware update"
    "``step``", "Name of cleaning step.  Must be ``update_firmware`` for firmware update"
    "``args``", "Keyword-argument entry (<name>: <value>) being passed to the step"
    "``args.firmware_images``", "Ordered list of dictionaries of firmware images to be applied"

Each firmware image dictionary is of the form::

    {
      "url": "<URL of firmware image file>",
      "checksum": "<checksum for image, uses SHA1>",
      "source": "<Optional override source setting for image>",
      "wait": <Optional time in seconds to wait after applying update>
    }

The ``url`` and ``checksum`` arguments in the firmware image dictionary are
mandatory, while the ``source`` and ``wait`` arguments are optional.

For ``url`` currently ``http``, ``https``, ``swift`` and ``file`` schemes are
supported.

``source`` corresponds to :oslo.config:option:`redfish.firmware_source` and by
setting it here, it is possible to override global setting per firmware image
in clean step arguments.

.. note::
   At the present time, targets for the firmware update cannot be specified.
   In testing, the BMC applied the update to all applicable targets on the
   node. It is assumed that the BMC knows what components a given firmware
   image is applicable to.

To perform a firmware update, first download the firmware to a web server,
Swift or filesystem that the Ironic conductor or BMC has network access to.
This could be the ironic conductor web server or another web server on the BMC
network. Using a web browser, curl, or similar tool on a server that has
network access to the BMC or Ironic conductor, try downloading the firmware to
verify that the URLs are correct and that the web server is configured
properly.

Next, construct the JSON for the firmware update cleaning step to be executed.
When launching the firmware update, the JSON may be specified on the command
line directly or in a file. The following example shows one cleaning step that
installs four firmware updates. All except 3rd entry that has explicit
``source`` added, uses the setting from :oslo.config:option:`redfish.firmware_source`
to determine if and where to stage the files:

.. code-block:: json

    [{
        "interface": "management",
        "step": "update_firmware",
        "args": {
            "firmware_images":[
                {
                    "url": "http://192.0.2.10/BMC_4_22_00_00.EXE",
                    "checksum": "<sha1-checksum-of-the-file>",
                    "wait": 300
                },
                {
                    "url": "https://192.0.2.10/NIC_19.0.12_A00.EXE",
                    "checksum": "<sha1-checksum-of-the-file>"
                },
                {
                    "url": "file:///firmware_images/idrac/9/PERC_WN64_6.65.65.65_A00.EXE",
                    "checksum": "<sha1-checksum-of-the-file>",
                    "source": "http"
                },
                {
                    "url": "swift://firmware_container/BIOS_W8Y0W_WN64_2.1.7.EXE",
                    "checksum": "<sha1-checksum-of-the-file>"
                }
            ]
        }
    }]

Finally, launch the firmware update cleaning step against the node. The
following example assumes the above JSON is in a file named
``firmware_update.json``:

.. code-block:: console

   $ baremetal node clean <ironic_node_uuid> --clean-steps firmware_update.json

In the following example, the JSON is specified directly on the command line:

.. code-block:: console

   $ baremetal node clean <ironic_node_uuid> --clean-steps \
       '[{"interface": "management", "step": "update_firmware", "args": {"firmware_images":[{"url": "http://192.0.2.10/BMC_4_22_00_00.EXE", "wait": 300}, {"url": "https://192.0.2.10/NIC_19.0.12_A00.EXE"}]}}]'

.. note::
   Firmware updates may take some time to complete. If a firmware update
   cleaning step consistently times out, then consider performing fewer
   firmware updates in the cleaning step or increasing
   ``clean_callback_timeout`` in ironic.conf to increase the timeout value.

.. warning::
   Warning: Removing power from a server while it is in the process of updating
   firmware may result in devices in the server, or the server itself becoming
   inoperable.
