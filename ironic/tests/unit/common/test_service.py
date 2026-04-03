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

import multiprocessing.reduction
import pickle

from oslo_config import cfg

from ironic.common import service as ironic_service
from ironic.conf import CONF
from ironic.tests import base


class TestConfSpawnSafe(base.TestCase):
    """Tests for the ConfigOpts spawn-safety."""

    def setUp(self):
        super().setUp()
        self._had_reduce = hasattr(cfg.ConfigOpts, '__reduce__')
        self._old_reduce = getattr(cfg.ConfigOpts, '__reduce__', None)
        self._had_sentinel = hasattr(cfg.ConfigOpts, '_ironic_spawn_safe')
        self.addCleanup(self._restore_configopts)

    def _strip_patch(self):
        """Remove any existing patch so we start with a clean class."""
        for attr in ('__reduce__', '_ironic_spawn_safe'):
            try:
                delattr(cfg.ConfigOpts, attr)
            except AttributeError:
                pass

    def _restore_configopts(self):
        """Restore ConfigOpts to its pre-test state."""
        if self._had_reduce:
            cfg.ConfigOpts.__reduce__ = self._old_reduce
        else:
            try:
                delattr(cfg.ConfigOpts, '__reduce__')
            except AttributeError:
                pass
        if not self._had_sentinel:
            try:
                delattr(cfg.ConfigOpts, '_ironic_spawn_safe')
            except AttributeError:
                pass

    def test_patch_makes_conf_picklable(self):
        """ForkingPickler.dumps(CONF) succeeds after the patch.

        This is the exact call oslo.service makes in
        ``_select_service_manager_context()``.
        """
        self._strip_patch()
        ironic_service._make_conf_spawn_safe()

        data = multiprocessing.reduction.ForkingPickler.dumps(CONF)
        self.assertIsNotNone(data)

    def test_patch_returns_conf_singleton(self):
        """Unpickling CONF returns the module-level CONF singleton.

        In a spawned child process ``_get_global_conf()`` must return the
        child's own CONF (which ``BaseRPCService.__setstate__`` will have
        already populated).  It must NOT return an empty ConfigOpts.
        """
        self._strip_patch()
        ironic_service._make_conf_spawn_safe()

        data = multiprocessing.reduction.ForkingPickler.dumps(CONF)
        restored = pickle.loads(data)
        # Should be the very same object as the module-level CONF global
        self.assertIs(restored, CONF)

    def test_patch_is_idempotent(self):
        """Calling the patch function twice does not break anything."""
        self._strip_patch()
        ironic_service._make_conf_spawn_safe()
        ironic_service._make_conf_spawn_safe()

        data = multiprocessing.reduction.ForkingPickler.dumps(CONF)
        self.assertIsNotNone(data)

    def test_patch_sets_sentinel(self):
        """The sentinel attribute is set after patching."""
        self._strip_patch()
        ironic_service._make_conf_spawn_safe()
        self.assertTrue(
            getattr(cfg.ConfigOpts, '_ironic_spawn_safe', False))
