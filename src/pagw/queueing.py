"""Transport adapters. PostgreSQL broker is a local alternative, not an SQS emulator."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from psycopg.types.json import Jsonb

from pagw.config import Settings
from pagw.db import Database


@dataclass
class Message:
    body: str
    receipt: str
    queue: str


class Broker(Protocol):
    def publish(self, queue: str, entries: list[dict]) -> set[str]: ...
    def receive(self, queue: str) -> list[Message]: ...
    def ack(self, message: Message) -> None: ...


class PostgresBroker:
    def __init__(self, db: Database):
        self.db = db

    def publish(self, queue: str, entries: list[dict]) -> set[str]:
        import json

        with self.db.transaction() as c:
            for entry in entries:
                # Same outbox row may legitimately publish more than once.
                c.execute(
                    "INSERT INTO local_messages(id,body,queue) VALUES(%s,%s,%s)",
                    (uuid4(), Jsonb(json.loads(entry["MessageBody"])), queue),
                )
        return {x["Id"] for x in entries}

    def receive(self, queue: str) -> list[Message]:
        import json

        with self.db.transaction() as c:
            row = c.execute(
                "SELECT * FROM local_messages WHERE queue=%s AND available_at<=clock_timestamp() ORDER BY available_at LIMIT 1 FOR UPDATE SKIP LOCKED",
                (queue,),
            ).fetchone()
            if not row:
                return []
            receipt = uuid4()
            c.execute(
                "UPDATE local_messages SET available_at=clock_timestamp()+interval '60 seconds',receives=receives+1,receipt=%s WHERE id=%s",
                (receipt, row["id"]),
            )
            return [Message(json.dumps(row["body"]), str(receipt), queue)]

    def ack(self, message: Message):
        with self.db.transaction() as c:
            c.execute("DELETE FROM local_messages WHERE receipt=%s", (UUID(message.receipt),))


class SQSBroker:
    def __init__(self, settings: Settings):
        self.client = boto3.client(
            "sqs",
            region_name=settings.aws_region,
            endpoint_url=settings.sqs_endpoint,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        )
        self.urls = {"submit": settings.submit_queue_url, "webhook": settings.webhook_queue_url}
        if not all(self.urls.values()):
            raise ValueError("both SQS queue URLs are required")

    def publish(self, queue: str, entries: list[dict]) -> set[str]:
        if len(entries) > 10:
            raise ValueError("SQS batch maximum is ten")
        result = self.client.send_message_batch(QueueUrl=self.urls[queue], Entries=entries)
        return {entry["Id"] for entry in result.get("Successful", [])}

    def receive(self, queue: str) -> list[Message]:
        result = self.client.receive_message(
            QueueUrl=self.urls[queue],
            MaxNumberOfMessages=1,
            WaitTimeSeconds=1,
            VisibilityTimeout=60,
        )
        return [Message(x["Body"], x["ReceiptHandle"], queue) for x in result.get("Messages", [])]

    def ack(self, message: Message):
        self.client.delete_message(QueueUrl=self.urls[message.queue], ReceiptHandle=message.receipt)


def broker_for(settings: Settings, db: Database) -> Broker:
    return SQSBroker(settings) if settings.broker == "sqs" else PostgresBroker(db)


def relay(db: Database, broker: Broker) -> int:
    import json

    sent = 0
    for queue in ("submit", "webhook"):
        with db.transaction() as c:
            c.execute(
                "UPDATE outbox b SET published_at=clock_timestamp() FROM operations o WHERE b.operation_id=o.id AND b.published_at IS NULL AND (b.generation<o.generation OR o.state IN ('DONE','DEAD'))"
            )
            rows = c.execute(
                "SELECT b.* FROM outbox b JOIN operations o ON o.id=b.operation_id WHERE b.published_at IS NULL AND b.queue=%s AND o.due_at<=clock_timestamp() AND o.state IN ('READY','RUNNING') ORDER BY b.created_at LIMIT 10 FOR UPDATE OF b SKIP LOCKED",
                (queue,),
            ).fetchall()
            if not rows:
                continue
            entries = [
                {
                    "Id": str(row["id"]),
                    "MessageBody": json.dumps(
                        {
                            "operation_id": str(row["operation_id"]),
                            "traceparent": row["traceparent"],
                        }
                    ),
                }
                for row in rows
            ]
            successful = broker.publish(queue, entries)
            for row in rows:
                if str(row["id"]) in successful:
                    c.execute(
                        "UPDATE outbox SET published_at=clock_timestamp() WHERE id=%s", (row["id"],)
                    )
                    sent += 1
    return sent
