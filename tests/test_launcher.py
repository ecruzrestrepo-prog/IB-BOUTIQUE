"""Ciclo de vida del launcher sin abrir puertos ni túneles reales."""
import contextlib
import errno
import io
import json
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import app


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.settings = app.Settings(user="ROPA_TEST", password="db-secret", dsn="test-service",
                                     ngrok_token="ngrok-token-secret", ngrok_user="reviewer",
                                     ngrok_password="private-password")
        self.sdk = MagicMock()
        self.sdk.forward.return_value.url.return_value = "https://ropa-test.ngrok.app"
        self.child = MagicMock()
        self.child.wait.return_value = 0
        self.child.poll.return_value = 0
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))
        self.stack.enter_context(patch.dict(sys.modules, {"ngrok": self.sdk}))
        self.free_port = self.stack.enter_context(patch("app.require_free_port"))
        self.spawn = self.stack.enter_context(patch("app.subprocess.Popen", return_value=self.child))
        self.ready = self.stack.enter_context(patch("app.wait_for_server"))
        self.signals = self.stack.enter_context(patch("app.signal.signal"))
        self.register = self.stack.enter_context(patch("app.atexit.register"))
        self.unregister = self.stack.enter_context(patch("app.atexit.unregister"))

    def test_authenticated_launch_keeps_secrets_out_of_arguments_and_logs(self):
        self.assertEqual(app.launch(self.settings), 0)
        policy = json.loads(self.sdk.forward.call_args.kwargs["traffic_policy"])
        config = policy["on_http_request"][0]["actions"][0]["config"]
        self.assertTrue(config["enforce"])
        self.assertEqual(config["credentials"], ["reviewer:private-password"])
        command = self.spawn.call_args.args[0]
        self.assertIn("--server.address=127.0.0.1", command)
        self.assertIn("--browser.serverAddress=ropa-test.ngrok.app", command)
        self.assertIn("--server.enableCORS=true", command)
        self.assertIn("--server.enableXsrfProtection=true", command)
        self.assertIn("--ui", command)
        for secret in (self.settings.password, self.settings.ngrok_token, self.settings.ngrok_password):
            self.assertNotIn(secret, " ".join(command))
            self.assertNotIn(secret, self.stdout.getvalue() + self.stderr.getvalue())
        self.ready.assert_called_once_with(self.child, self.settings.port)
        self.sdk.disconnect.assert_called_once_with("https://ropa-test.ngrok.app")
        self.child.terminate.assert_not_called()
        self.unregister.assert_called_once()

    def test_ngrok_failure_falls_back_to_local_and_sanitizes_error(self):
        self.sdk.forward.side_effect = RuntimeError("bad token " + self.settings.ngrok_token)
        self.assertEqual(app.launch(self.settings), 0)
        command = self.spawn.call_args.args[0]
        self.assertNotIn("--public-url", command)
        self.assertIn("Acceso local únicamente", self.stdout.getvalue())
        self.assertNotIn(self.settings.ngrok_token, self.stdout.getvalue())
        self.assertNotIn("Enlace para compartir", self.stdout.getvalue())

    def test_missing_remote_password_never_opens_unauthenticated_tunnel(self):
        self.settings = app.Settings(ngrok_token="test-token", ngrok_user="reviewer")
        self.assertEqual(app.launch(self.settings), 0)
        self.sdk.forward.assert_not_called()
        self.assertNotIn("--public-url", self.spawn.call_args.args[0])

    def test_disabled_ngrok_uses_only_local_server(self):
        self.assertEqual(app.launch(app.Settings(ngrok_enabled=False)), 0)
        self.sdk.forward.assert_not_called()
        self.assertNotIn("--public-url", self.spawn.call_args.args[0])

    def test_server_failure_closes_tunnel_and_terminates_child(self):
        self.child.poll.return_value = None
        self.ready.side_effect = RuntimeError("Server readiness failed")
        self.assertEqual(app.launch(self.settings), 1)
        self.sdk.disconnect.assert_called_once_with("https://ropa-test.ngrok.app")
        self.child.terminate.assert_called_once()
        self.child.wait.assert_called_once_with(timeout=5)
        self.assertNotIn("Enlace para compartir", self.stdout.getvalue())

    def test_ctrl_c_closes_resources_and_forces_stuck_child(self):
        self.child.poll.return_value = None
        self.child.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("streamlit", 5), 0]
        self.assertEqual(app.launch(self.settings), 0)
        self.sdk.disconnect.assert_called_once_with("https://ropa-test.ngrok.app")
        self.child.terminate.assert_called_once()
        self.child.kill.assert_called_once()
        self.assertEqual(self.child.wait.call_count, 3)

    def test_occupied_port_does_not_open_tunnel_or_spawn(self):
        self.free_port.side_effect = ValueError("Puerto ocupado")
        with self.assertRaisesRegex(ValueError, "ocupado"):
            app.launch(self.settings)
        self.sdk.forward.assert_not_called()
        self.spawn.assert_not_called()

    def test_public_link_is_not_announced_before_health_check(self):
        def check(child, port):
            self.assertNotIn("Enlace para compartir", self.stdout.getvalue())
        self.ready.side_effect = check
        self.assertEqual(app.launch(self.settings), 0)
        self.assertIn("Enlace para compartir: https://ropa-test.ngrok.app", self.stdout.getvalue())

    def test_invalid_tunnel_url_is_disconnected_and_falls_back(self):
        self.sdk.forward.return_value.url.return_value = "http://insecure.example.test"
        self.assertEqual(app.launch(self.settings), 0)
        self.sdk.disconnect.assert_called_once_with("http://insecure.example.test")
        self.assertNotIn("--public-url", self.spawn.call_args.args[0])

    def test_policy_requires_valid_credentials(self):
        for settings in [app.Settings(), app.Settings(ngrok_token="token", ngrok_user="bad:name", ngrok_password="password1"), app.Settings(ngrok_token="token", ngrok_user="name", ngrok_password="short")]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                app.ngrok_policy(settings)


class ServerReadinessTests(unittest.TestCase):
    def test_health_check_waits_for_200(self):
        child = MagicMock()
        child.poll.return_value = None
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch("app.urlopen", side_effect=[OSError("not ready"), response]) as request, patch("app.time.sleep"):
            app.wait_for_server(child, 8501, timeout=2)
        self.assertEqual(request.call_count, 2)

    def test_child_exit_stops_health_polling(self):
        child = MagicMock()
        child.poll.return_value = 1
        with patch("app.urlopen") as request, self.assertRaisesRegex(RuntimeError, "terminó"):
            app.wait_for_server(child, 8501)
        request.assert_not_called()

    def test_deadline_stops_health_polling(self):
        child = MagicMock()
        child.poll.return_value = None
        with patch("app.time.monotonic", side_effect=[0, 31]), patch("app.urlopen") as request, self.assertRaises(RuntimeError):
            app.wait_for_server(child, 8501)
        request.assert_not_called()


class PortAvailabilityTests(unittest.TestCase):
    def test_occupied_port_has_actionable_message(self):
        with patch("app.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = OSError(errno.EADDRINUSE, "Address already in use")
            with self.assertRaisesRegex(ValueError, "ocupado"):
                app.require_free_port(8501)

    def test_permission_denied_is_not_reported_as_occupied_port(self):
        with patch("app.socket.socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.bind.side_effect = OSError(errno.EPERM, "Operation not permitted")
            with self.assertRaisesRegex(ValueError, "permisos de red") as caught:
                app.require_free_port(8501)
            self.assertNotIn("ocupado", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
