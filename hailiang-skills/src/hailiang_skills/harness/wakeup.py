"""Redis is a wakeup hint; database jobs/events remain authoritative."""
from __future__ import annotations
import logging
import os
import threading

log = logging.getLogger(__name__)


class Wakeup:
    channel = 'hailiang:harness:wakeup'

    def __init__(self):
        self.channel = os.getenv('HAILIANG_REDIS_KEY_PREFIX', 'hailiang:') + 'harness:wakeup'
        self.event = threading.Event()
        self.closed = threading.Event()
        self.client = None
        self.thread = None
        url = os.getenv('HAILIANG_REDIS_URL', '')
        if url:
            import redis
            self.client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)

    def publish(self):
        if self.closed.is_set():
            return
        self.event.set()
        if self.client:
            try:
                self.client.publish(self.channel, 'changed')
            except Exception:
                log.debug('Harness wakeup unavailable; database polling continues.')

    def start(self):
        if self.client and not self.thread:
            self.thread = threading.Thread(target=self._listen, daemon=True, name='harness-wakeup')
            self.thread.start()

    def _listen(self):
        while not self.closed.is_set():
            try:
                with self.client.pubsub() as subscriber:
                    subscriber.subscribe(self.channel)
                    while not self.closed.is_set():
                        message = subscriber.get_message(ignore_subscribe_messages=True, timeout=1)
                        if message:
                            self.event.set()
            except Exception:
                self.closed.wait(2)

    def wait(self):
        self.event.wait(.5)
        self.event.clear()

    def close(self):
        self.closed.set()
        self.event.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.client:
            self.client.close()
