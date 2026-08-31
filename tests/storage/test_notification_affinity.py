from operator_journal.notifications import NotificationMessage
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


def test_notification_outbox_is_private_to_its_operator_pc():
    notice_a = NotificationMessage(owner_node_id="operator-01", text="job A")
    notice_b = NotificationMessage(owner_node_id="operator-02", text="job B")

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        repo.save_notifications([notice_a, notice_b])
        db.commit()

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        pending_a = repo.list_pending_notifications(owner_node_id="operator-01")
        assert [item["notification_id"] for item in pending_a] == [
            notice_a.notification_id
        ]
        assert pending_a[0]["owner_node_id"] == "operator-01"

        # A local agent must neither see nor acknowledge another PC's notice.
        assert not repo.mark_notification_sent(
            notice_b.notification_id,
            owner_node_id="operator-01",
        )
        assert repo.mark_notification_sent(
            notice_a.notification_id,
            owner_node_id="operator-01",
        )
        db.commit()

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        assert repo.list_pending_notifications(owner_node_id="operator-01") == []
        pending_b = repo.list_pending_notifications(owner_node_id="operator-02")
        assert [item["notification_id"] for item in pending_b] == [
            notice_b.notification_id
        ]
