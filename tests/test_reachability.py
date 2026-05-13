import unittest
from unittest.mock import AsyncMock, patch

from custom_components.wem_webinterface.coordinator import WemCoordinator


class TestReachabilityChecks(unittest.IsolatedAsyncioTestCase):
    async def test_async_setup_checks_ip_before_port_80(self):
        coordinator = WemCoordinator(
            ip_address="192.168.179.36",
            username="admin",
            password="secret",
            entries=["seed"],
            hass=None,
            config_entry=None,
        )

        call_order = []

        async def record(name):
            call_order.append(name)

        coordinator._create_session = AsyncMock(side_effect=lambda: record("session"))
        coordinator._check_ip_reachability = AsyncMock(side_effect=lambda timeout_seconds=5: record("ip"))
        coordinator._check_web_port_reachability = AsyncMock(side_effect=lambda timeout_seconds=5: record("port80"))
        coordinator._login = AsyncMock(side_effect=lambda: record("login"))
        coordinator._discover_all = AsyncMock(side_effect=lambda: record("discover"))
        coordinator._polling_loop = AsyncMock()

        await coordinator.async_setup()

        self.assertEqual(call_order, ["session", "ip", "port80", "login", "discover"])

    async def test_ip_reachability_failure_mentions_ping(self):
        coordinator = WemCoordinator(
            ip_address="192.168.179.36",
            username="admin",
            password="secret",
            entries=["seed"],
            hass=None,
            config_entry=None,
        )

        class Result:
            returncode = 1
            stdout = ""
            stderr = "request timed out"

        with patch("custom_components.wem_webinterface.coordinator.platform.system", return_value="Linux"):
            with patch("custom_components.wem_webinterface.coordinator.subprocess.run", return_value=Result()):
                with self.assertRaises(ConnectionError) as ctx:
                    await coordinator._check_ip_reachability(timeout_seconds=1)

        message = str(ctx.exception)
        self.assertIn("not reachable on the network", message)
        self.assertIn("ping failed", message)
        self.assertIn("request timed out", message)

    async def test_dns_hostname_uses_dns_resolution_without_ping(self):
        coordinator = WemCoordinator(
            ip_address="heizung.home",
            username="admin",
            password="secret",
            entries=["seed"],
            hass=None,
            config_entry=None,
        )

        with patch("custom_components.wem_webinterface.coordinator.socket.getaddrinfo", return_value=[object()]):
            with patch("custom_components.wem_webinterface.coordinator.subprocess.run") as ping_mock:
                await coordinator._check_ip_reachability(timeout_seconds=1)

        ping_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
