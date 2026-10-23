import os
import time
from uuid import uuid4

import pytest

from hailiang_skills.harness.wakeup import Wakeup


@pytest.mark.skipif(not os.getenv('HAILIANG_HARNESS_TEST_REDIS_URL'), reason='Requires an explicit integration Redis URL')
def test_real_redis_wakes_other_worker_without_database_state(monkeypatch):
    monkeypatch.setenv('HAILIANG_REDIS_URL', os.environ['HAILIANG_HARNESS_TEST_REDIS_URL'])
    monkeypatch.setenv('HAILIANG_REDIS_KEY_PREFIX', 'harness-integration:' + uuid4().hex + ':')
    subscriber, publisher = Wakeup(), Wakeup()
    try:
        subscriber.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if publisher.client.pubsub_numsub(subscriber.channel)[0][1]:
                break
            time.sleep(.05)
        else:
            pytest.fail('Redis subscriber did not become ready')
        publisher.publish()
        assert subscriber.event.wait(2)
        assert publisher.event.is_set()
        subscriber.event.clear()
        publisher.close()
        publisher.publish()
        assert not subscriber.event.wait(.1)
    finally:
        subscriber.close()
        publisher.close()
