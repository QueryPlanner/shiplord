from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

from fakes import REGISTRATION, record  # noqa: E402
from shiplord.provision import ProvisionError  # noqa: E402
from shiplord.retire import make_retirer  # noqa: E402
from shiplord.routing import RoutingError  # noqa: E402
from shiplord.runtime import RuntimeFailure  # noqa: E402


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.order = []
        self.runtime = MagicMock()
        self.runtime.stop.side_effect = lambda registration: self.order.append("stop")
        self.runtime.discard.side_effect = lambda registration, release: self.order.append(("discard", release["commit"][0]))
        patcher = patch("shiplord.retire.RouteManager")
        self.routes = patcher.start().return_value
        self.routes.remove.side_effect = lambda app: self.order.append(("unroute", app))
        self.addCleanup(patcher.stop)
        self.retire = make_retirer(MagicMock(caddy_root=Path("/x")), MagicMock(), MagicMock(), MagicMock(),
                                   runtime_for=lambda registration: self.runtime)
        self.state = {"current": record(commit="a"), "previous": record(commit="b")}

    def test_the_route_goes_first_then_the_runtime_then_both_releases_are_discarded(self):
        self.retire(REGISTRATION, self.state)
        self.assertEqual(self.order, [("unroute", "orders"), "stop", ("discard", "a"), ("discard", "b")])

    def test_a_missing_previous_release_is_skipped(self):
        self.retire(REGISTRATION, dict(self.state, previous=None))
        self.assertEqual(self.order, [("unroute", "orders"), "stop", ("discard", "a")])

    def test_failures_become_provision_errors_and_nothing_is_discarded(self):
        for failing, error in ((self.routes.remove, RoutingError("bad", "validation")),
                               (self.runtime.stop, RuntimeFailure("stuck", "stop"))):
            with self.subTest(error=type(error).__name__):
                self.order.clear()
                failing.side_effect = error
                with self.assertRaisesRegex(ProvisionError, "could not retire"):
                    self.retire(REGISTRATION, self.state)
                self.assertNotIn(("discard", "a"), self.order)


if __name__ == "__main__":
    unittest.main()
