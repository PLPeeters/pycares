"""Regression tests for callbacks that re-enter Channel methods."""
import subprocess
import sys
import textwrap
import unittest


def _start_dns_responder():
    import socket
    import threading

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))

    def answer_query():
        request, address = server.recvfrom(512)
        response = (
            request[:2]
            + bytes((129, 128))
            + request[4:6]
            + bytes(6)
            + request[12:]
        )
        server.sendto(response, address)

    thread = threading.Thread(target=answer_query, daemon=True)
    thread.start()
    return server, thread


class CallbackReentryTest(unittest.TestCase):
    def test_timeout_reentry_during_cancel_does_not_deadlock(self):
        script = textwrap.dedent(
            """
            import os, socket, sys, threading
            sys.path.insert(0, os.path.join(os.getcwd(), "src"))
            from unittest.mock import patch
            import pycares

            native_lib = pycares._lib
            callback_started = threading.Event()
            cancel_started = threading.Event()
            callback_finished = threading.Event()
            errors = []

            class ControlledLib:
                def __getattr__(self, name):
                    return getattr(native_lib, name)

                def ares_cancel(self, channel_pointer):
                    cancel_started.set()
                    if not callback_finished.wait(2):
                        raise AssertionError("cancel waited for callback re-entry")
                    native_lib.ares_cancel(channel_pointer)

            server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            server.bind(("127.0.0.1", 0))
            server.settimeout(2)
            def answer_query():
                request, address = server.recvfrom(512)
                response = request[:2] + bytes((129, 128)) + request[4:6] + bytes(6) + request[12:]
                server.sendto(response, address)
            server_thread = threading.Thread(target=answer_query, daemon=True)
            server_thread.start()

            channel = pycares.Channel(timeout=1, tries=1, servers=["127.0.0.1"],
                                      udp_port=server.getsockname()[1])
            def result_callback(result, error):
                callback_started.set()
                if not cancel_started.wait(2):
                    errors.append("cancel did not start")
                channel.timeout()
                callback_finished.set()

            def cancel():
                try:
                    channel.cancel()
                except Exception as exc:
                    errors.append(repr(exc))

            try:
                with patch.object(pycares, "_lib", ControlledLib()):
                    channel.query("reentry.example.invalid", pycares.QUERY_TYPE_A,
                                  callback=result_callback)
                    assert callback_started.wait(2), "DNS result callback did not run"
                    cancel_thread = threading.Thread(target=cancel, daemon=True)
                    cancel_thread.start()
                    assert cancel_started.wait(2), "cancel did not enter native call"
                    cancel_thread.join(2)
                    assert not cancel_thread.is_alive(), "cancel did not complete"
                    assert callback_finished.is_set(), "callback did not complete"
                    assert not errors, errors
            finally:
                channel.close()
                server.close()
            """
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=4,
            )
        except subprocess.TimeoutExpired as exc:
            self.fail(f"callback re-entry subprocess deadlocked: {exc}")
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_callback_reentry_is_safe_while_cancel_runs(self):
        import threading
        from unittest.mock import patch

        import pycares

        native_lib = pycares._lib
        callback_started = threading.Event()
        callback_barrier = threading.Barrier(2)
        cancel_started = threading.Event()
        callback_finished = threading.Event()
        cancel_finished = threading.Event()
        timeout_results = []
        errors = []
        server, _ = _start_dns_responder()

        class ControlledLib:
            def __getattr__(self, name):
                return getattr(native_lib, name)

            def ares_cancel(self, channel_pointer):
                cancel_started.set()
                if not callback_finished.wait(2):
                    errors.append("cancel waited for callback re-entry")
                native_lib.ares_cancel(channel_pointer)

        channel = pycares.Channel(timeout=1, tries=1, servers=["127.0.0.1"],
                                  udp_port=server.getsockname()[1])

        def result_callback(result, error):
            callback_started.set()
            callback_barrier.wait(timeout=2)
            timeout_results.append(channel.timeout())
            callback_finished.set()

        try:
            with patch.object(pycares, "_lib", ControlledLib()):
                channel.query("cancel-reentry.example.invalid", pycares.QUERY_TYPE_A,
                              callback=result_callback)

                def cancel():
                    try:
                        channel.cancel()
                    except Exception as exc:
                        errors.append(repr(exc))
                    finally:
                        cancel_finished.set()

                self.assertTrue(callback_started.wait(2), "DNS query callback did not run")
                cancel_thread = threading.Thread(target=cancel, daemon=True)
                cancel_thread.start()
                self.assertTrue(cancel_started.wait(2), "cancel did not enter native call")
                callback_barrier.wait(timeout=2)
                self.assertTrue(callback_finished.wait(2), "callback re-entry did not finish")
                self.assertTrue(cancel_finished.wait(2), "cancel did not finish")
                self.assertFalse(errors, errors)
                self.assertEqual(len(timeout_results), 1)
        finally:
            callback_barrier.abort()
            channel.close()
            server.close()

    def test_close_waits_for_inflight_query_submission(self):
        import threading
        from unittest.mock import patch

        import pycares

        native_lib = pycares._lib
        query_entered = threading.Event()
        allow_query = threading.Event()
        cancel_entered = threading.Event()
        destroyed = threading.Event()
        query_errors = []

        class ControlledLib:
            def __getattr__(self, name):
                return getattr(native_lib, name)

            def ares_query_dnsrec(self, *args):
                query_entered.set()
                if not allow_query.wait(2):
                    raise AssertionError("query submission release timed out")
                return native_lib.ares_query_dnsrec(*args)

            def ares_cancel(self, channel_pointer):
                cancel_entered.set()
                native_lib.ares_cancel(channel_pointer)

            def ares_destroy(self, channel_pointer):
                native_lib.ares_destroy(channel_pointer)
                destroyed.set()

        channel = pycares.Channel(timeout=1, tries=1, servers=["192.0.2.1"])

        def query():
            try:
                channel.query("close-race.example.invalid", pycares.QUERY_TYPE_A,
                              callback=lambda result, error: None)
            except RuntimeError:
                pass
            except Exception as exc:
                query_errors.append(repr(exc))

        try:
            with patch.object(pycares, "_lib", ControlledLib()):
                query_thread = threading.Thread(target=query, daemon=True)
                query_thread.start()
                self.assertTrue(query_entered.wait(2), "query did not reach native submission")
                channel.close()
                self.assertFalse(
                    cancel_entered.wait(0.1),
                    "native cancellation started before query submission returned",
                )
                allow_query.set()
                query_thread.join(2)
                self.assertFalse(query_thread.is_alive(), "query submission did not finish")
                self.assertFalse(query_errors, query_errors)
                self.assertTrue(cancel_entered.wait(2), "deferred cancellation did not start")
                self.assertTrue(destroyed.wait(2), "native channel was not destroyed")
        finally:
            allow_query.set()
            channel.close()

    def test_close_during_callback_reentry_rejects_new_channel_use_safely(self):
        import threading
        from unittest.mock import patch

        import pycares

        native_lib = pycares._lib
        callback_started = threading.Event()
        callback_barrier = threading.Barrier(2)
        callback_finished = threading.Event()
        cancel_started = threading.Event()
        destroyed = threading.Event()
        callback_errors = []
        server, _ = _start_dns_responder()

        class ControlledLib:
            def __getattr__(self, name):
                return getattr(native_lib, name)

            def ares_cancel(self, channel_pointer):
                cancel_started.set()
                native_lib.ares_cancel(channel_pointer)

            def ares_destroy(self, channel_pointer):
                native_lib.ares_destroy(channel_pointer)
                destroyed.set()

        channel = pycares.Channel(timeout=1, tries=1, servers=["127.0.0.1"],
                                  udp_port=server.getsockname()[1])

        def result_callback(result, error):
            callback_started.set()
            callback_barrier.wait(timeout=2)
            try:
                channel.timeout()
            except RuntimeError:
                callback_errors.append("closed")
            except Exception as exc:
                callback_errors.append(repr(exc))
            callback_finished.set()

        try:
            with patch.object(pycares, "_lib", ControlledLib()):
                channel.query("close-reentry.example.invalid", pycares.QUERY_TYPE_A,
                              callback=result_callback)
                self.assertTrue(callback_started.wait(2), "DNS query callback did not run")
                channel.close()
                self.assertIsNone(channel._channel)
                self.assertTrue(cancel_started.wait(2), "close did not cancel the query")
                callback_barrier.wait(timeout=2)
                self.assertTrue(callback_finished.wait(2), "callback did not finish")
                self.assertTrue(destroyed.wait(2), "channel was not destroyed after callback")
                self.assertEqual(callback_errors, ["closed"])
        finally:
            callback_barrier.abort()
            channel.close()
            server.close()


if __name__ == "__main__":
    unittest.main()
