"""Public operation cancellation against a deterministic local DNS boundary."""
import queue
import socket
import struct
import threading
import unittest

import pycares


class DNSServer:
    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(0.1)
        self.requests = queue.Queue()
        self.answers = {}
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                packet, address = self.socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            offset, labels = 12, []
            while packet[offset]:
                length = packet[offset]
                labels.append(packet[offset + 1:offset + 1 + length].decode('ascii'))
                offset += length + 1
            qtype = struct.unpack('!H', packet[offset + 1:offset + 3])[0]
            request = ('.'.join(labels).lower(), qtype, packet, address, offset + 5)
            self.requests.put(request)
            answer = self.answers.get((request[0], qtype))
            if answer is not None:
                self.answer(request, answer)

    def answer(self, request, data):
        _, qtype, packet, address, end = request
        header = packet[:2] + struct.pack('!HHHHH', 0x8180, 1, 1, 0, 0)
        rr = b'\xc0\x0c' + struct.pack('!HHIH', qtype, 1, 60, len(data)) + data
        self.socket.sendto(header + packet[12:end] + rr, address)

    def close(self):
        self.stopped.set()
        self.thread.join(2)
        self.socket.close()


class OperationCancellationTest(unittest.TestCase):
    def setUp(self):
        self.server = DNSServer()
        self.channel = pycares.Channel(
            servers=['127.0.0.1'], udp_port=self.server.socket.getsockname()[1],
            timeout=0.2, tries=2, domains=['first.invalid', 'second.invalid'])
        self.addCleanup(self.server.close)
        self.addCleanup(self.channel.close)

    def test_raw_cancel_preserves_unrelated_callback_query(self):
        self.server.answers[('other.example', 1)] = b'\x01\x02\x03\x04'
        cancelled, other = [], []
        operation = self.channel.query_cancellable(
            'pending.example.', pycares.QUERY_TYPE_A,
            callback=lambda result, error: cancelled.append((result, error)))
        self.assertIsInstance(operation, pycares.Operation)
        self.assertIsNone(self.channel.query(
            'other.example.', pycares.QUERY_TYPE_A,
            callback=lambda result, error: other.append((result, error))))
        self.assertIsNone(operation.cancel())
        self.assertTrue(self.channel.wait(2))
        self.assertEqual(cancelled, [(None, pycares.errno.ARES_ECANCELLED)])
        self.assertEqual(len(other), 1)
        self.assertIsNone(other[0][1])
        self.assertEqual(other[0][0].answer[0].data.addr, '1.2.3.4')
        operation.cancel()
        self.assertEqual(len(cancelled), 1)

    def test_composite_cancellation_preserves_unrelated_operation(self):
        self.server.answers[('other.example', 1)] = b'\x01\x02\x03\x04'
        for method in ('search_cancellable', 'getaddrinfo_cancellable'):
            with self.subTest(method=method):
                cancelled, other = [], []
                args = ('pending', pycares.QUERY_TYPE_A) if method.startswith('search') else ('pending', None)
                operation = getattr(self.channel, method)(
                    *args, callback=lambda result, error: cancelled.append((result, error)))
                unrelated = self.channel.query_cancellable(
                    'other.example.', pycares.QUERY_TYPE_A,
                    callback=lambda result, error: other.append((result, error)))
                operation.cancel()
                self.assertTrue(self.channel.wait(2))
                self.assertEqual(cancelled, [(None, pycares.errno.ARES_ECANCELLED)])
                self.assertEqual(len(other), 1)
                self.assertIsNone(other[0][1])
                unrelated.cancel()
                operation.cancel()
                self.assertEqual(len(cancelled), 1)

    def test_synchronous_results_return_terminal_operations(self):
        cases = (
            ('query_cancellable', ('name.onion', pycares.QUERY_TYPE_A), pycares.errno.ARES_ENOTFOUND),
            ('search_cancellable', ('name.onion', pycares.QUERY_TYPE_A), pycares.errno.ARES_ENOTFOUND),
            ('getaddrinfo_cancellable', ('127.0.0.1', 80), None),
            ('getaddrinfo_cancellable', ('localhost', None), None),
        )
        for method, args, expected_error in cases:
            with self.subTest(method=method, args=args):
                results = []
                operation = getattr(self.channel, method)(
                    *args, callback=lambda result, error: results.append((result, error)))
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0][1], expected_error)
                operation.cancel()
                self.assertEqual(len(results), 1)

    def test_dropped_operation_keeps_callback_alive_until_native_terminal(self):
        import gc
        import weakref

        called = threading.Event()
        class Callback:
            def __call__(self, result, error):
                called.set()
        callback = Callback()
        callback_ref = weakref.ref(callback)
        operation = self.channel.query_cancellable(
            'pending.example.', pycares.QUERY_TYPE_A, callback=callback)
        operation_ref = weakref.ref(operation)
        del callback, operation
        gc.collect()
        self.assertIsNotNone(callback_ref())
        self.assertIsNotNone(operation_ref())
        self.channel.cancel()
        self.assertTrue(called.wait(2))
        gc.collect()
        self.assertIsNone(callback_ref())
        self.assertIsNone(operation_ref())

    def test_retained_terminal_token_sheds_channel_and_callback(self):
        import gc
        import weakref

        channel = pycares.Channel()
        class Callback:
            def __call__(self, result, error):
                pass
        callback = Callback()
        channel_ref, callback_ref = weakref.ref(channel), weakref.ref(callback)
        operation = channel.getaddrinfo_cancellable('127.0.0.1', 80, callback=callback)
        channel.close()
        del channel, callback
        gc.collect()
        self.assertIsNone(callback_ref())
        self.assertIsNone(channel_ref())
        operation.cancel()

    def test_callback_exception_does_not_retain_token_callback(self):
        import contextlib
        import gc
        import io
        import weakref

        class Callback:
            def __call__(self, result, error):
                raise ValueError('callback exploded')
        callback = Callback()
        callback_ref = weakref.ref(callback)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            operation = self.channel.query_cancellable(
                'pending.example.', pycares.QUERY_TYPE_A, callback=callback)
            operation.cancel()
        self.assertIn('callback exploded', errors.getvalue())
        del callback
        gc.collect()
        self.assertIsNone(callback_ref())
        operation.cancel()

    def test_failed_ffi_argument_conversion_does_not_leak_callback(self):
        import gc
        import weakref

        calls = []
        class Callback:
            def __call__(self, result, error):
                calls.append((result, error))
        callback = Callback()
        callback_ref = weakref.ref(callback)
        with self.assertRaises(TypeError):
            self.channel.query_cancellable('pending.example.', 1.0, callback=callback)
        del callback
        gc.collect()
        self.assertIsNone(callback_ref())
        self.assertTrue(self.channel.wait(0))
        self.assertEqual(calls, [])

    def _run_concurrency_script(self, body):
        import subprocess
        import sys
        import textwrap

        script = '''import gc, threading, weakref
import pycares
from tests.test_operation_cancellation import DNSServer
server = DNSServer()
channel = pycares.Channel(servers=['127.0.0.1'], udp_port=server.socket.getsockname()[1], timeout=1, tries=1)
'''
        try:
            result = subprocess.run(
                [sys.executable, '-c', script + textwrap.dedent(body)],
                capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired as exc:
            self.fail(f'operation concurrency subprocess deadlocked: {exc}')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cancel_reentry_with_close_does_not_deadlock(self):
        self._run_concurrency_script('''
            calls = []
            def callback(result, error):
                calls.append(error)
                assert channel.timeout() >= 0
                worker = threading.Thread(target=operation.cancel)
                worker.start()
                worker.join(2)
                assert not worker.is_alive()
                channel.close()
            operation = channel.getaddrinfo_cancellable('pending.example.', None, callback=callback)
            worker = threading.Thread(target=operation.cancel)
            worker.start()
            worker.join(3)
            assert not worker.is_alive()
            assert calls == [pycares.errno.ARES_ECANCELLED], calls
            operation.cancel()
            server.close()
        ''')

    def test_cancel_after_close_queued_keeps_pending_callback_until_shutdown(self):
        self._run_concurrency_script('''
            server.answers[('trigger.example', 1)] = b'\\x01\\x02\\x03\\x04'
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            calls = []
            def pending(result, error):
                calls.append(error)
                finished.set()
            pending_ref = weakref.ref(pending)
            operation = channel.search_cancellable('pending.example.', pycares.QUERY_TYPE_A, callback=pending)
            del pending
            def block(result, error):
                entered.set()
                assert release.wait(3)
            channel.query('trigger.example.', pycares.QUERY_TYPE_A, callback=block)
            assert entered.wait(3)
            channel.close()
            operation.cancel()
            assert not finished.is_set()
            gc.collect()
            assert pending_ref() is not None
            release.set()
            assert finished.wait(3)
            operation.cancel()
            assert calls == [pycares.errno.ARES_ECANCELLED], calls
            server.close()
        ''')

    def test_close_cancel_and_submission_races_are_safe(self):
        self._run_concurrency_script('''
            for index in range(40):
                channel = pycares.Channel(servers=['127.0.0.1'], udp_port=server.socket.getsockname()[1], timeout=1, tries=1)
                done = threading.Event()
                calls, errors = [], []
                def callback(result, error):
                    calls.append(error)
                    done.set()
                operation = channel.getaddrinfo_cancellable('pending.example.', None, callback=callback)
                barrier = threading.Barrier(3)
                def cancel():
                    try:
                        barrier.wait(2)
                        operation.cancel()
                    except BaseException as error:
                        errors.append(error)
                def close():
                    barrier.wait(2)
                    channel.close()
                workers = [threading.Thread(target=cancel), threading.Thread(target=close)]
                for worker in workers: worker.start()
                barrier.wait(2)
                for worker in workers:
                    worker.join(3)
                    assert not worker.is_alive()
                assert done.wait(3)
                assert not errors, errors
                assert calls == [pycares.errno.ARES_ECANCELLED], calls
                operation.cancel()
            server.close()
        ''')

    def test_synchronous_callback_can_cancel_nested_work_and_close(self):
        results = []
        def synchronous(result, error):
            results.append(error)
            nested = self.channel.query_cancellable(
                'pending.example.', pycares.QUERY_TYPE_A,
                callback=lambda result, error: results.append(error))
            nested.cancel()
            self.channel.close()
        operation = self.channel.getaddrinfo_cancellable('127.0.0.1', 80, callback=synchronous)
        operation.cancel()
        self.assertEqual(results, [None, pycares.errno.ARES_ECANCELLED])

    def test_fallback_socket_processing_preserves_unrelated_work(self):
        import select
        import time

        events = {}
        def socket_state(fd, readable, writable):
            if readable or writable:
                events[fd] = (readable, writable)
            else:
                events.pop(fd, None)
        channel = pycares.Channel(
            servers=['127.0.0.1'], udp_port=self.server.socket.getsockname()[1],
            timeout=0.2, tries=2, sock_state_cb=socket_state)
        self.addCleanup(channel.close)
        self.server.answers[('other.example', 1)] = b'\x01\x02\x03\x04'
        cancelled, other = [], []
        operation = channel.getaddrinfo_cancellable(
            'pending.example.', None,
            callback=lambda result, error: cancelled.append((result, error)))
        self.assertIsNone(channel.query('other.example.', pycares.QUERY_TYPE_A,
            callback=lambda result, error: other.append((result, error))))
        self.assertIsNone(channel.search('legacy.example.', pycares.QUERY_TYPE_A,
            callback=lambda result, error: None))
        self.assertIsNone(channel.getaddrinfo('127.0.0.1', 80,
            callback=lambda result, error: None))
        operation.cancel()
        deadline = time.monotonic() + 2
        while not other and time.monotonic() < deadline:
            reads = [fd for fd, state in events.items() if state[0]]
            writes = [fd for fd, state in events.items() if state[1]]
            readable, writable, _ = select.select(reads, writes, [], 0.1)
            if not readable and not writable:
                channel.process_fd(pycares.ARES_SOCKET_BAD, pycares.ARES_SOCKET_BAD)
            for fd in set(readable + writable):
                channel.process_fd(fd if fd in readable else pycares.ARES_SOCKET_BAD,
                                   fd if fd in writable else pycares.ARES_SOCKET_BAD)
        self.assertEqual(cancelled, [(None, pycares.errno.ARES_ECANCELLED)])
        self.assertEqual(len(other), 1)
        self.assertIsNone(other[0][1])
