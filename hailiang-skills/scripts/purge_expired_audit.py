"""Run daily from CronJob/worker to enforce encrypted audit retention."""

from hailiang_skills.storage.audit_store import EncryptedAuditStore
from hailiang_skills.storage.database import build_engine, build_session_factory


if __name__ == "__main__":
    store = EncryptedAuditStore(build_session_factory(build_engine()))
    print(f"purged={store.purge_expired()}")
