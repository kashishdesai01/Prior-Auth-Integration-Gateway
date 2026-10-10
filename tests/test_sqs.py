import json
import os
from uuid import uuid4

import boto3
import pytest
from botocore.stub import Stubber

from pagw.queueing import SQSBroker


def test_sqs_partial_success_is_not_whole_batch_success():
    broker = object.__new__(SQSBroker)
    broker.client = boto3.client(
        "sqs", region_name="us-west-2", aws_access_key_id="test", aws_secret_access_key="test"
    )
    broker.urls = {"submit": "https://sqs.us-west-2.amazonaws.com/123456789012/test"}
    entries = [{"Id": "a", "MessageBody": "{}"}, {"Id": "b", "MessageBody": "{}"}]
    with Stubber(broker.client) as stubber:
        stubber.add_response(
            "send_message_batch",
            {
                "Successful": [{"Id": "a", "MessageId": "one", "MD5OfMessageBody": "0" * 32}],
                "Failed": [{"Id": "b", "SenderFault": False, "Code": "ServiceUnavailable"}],
            },
            {"QueueUrl": broker.urls["submit"], "Entries": entries},
        )
        assert broker.publish("submit", entries) == {"a"}
        stubber.assert_no_pending_responses()
    with pytest.raises(ValueError):
        broker.publish("submit", entries * 6)


@pytest.mark.skipif(
    not os.getenv("PAGW_TEST_SQS_ENDPOINT"), reason="explicit live SQS emulator endpoint required"
)
def test_live_sqs_publish_receive_ack(settings):
    endpoint = os.environ["PAGW_TEST_SQS_ENDPOINT"]
    client = boto3.client(
        "sqs",
        region_name="us-west-2",
        endpoint_url=endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    queue = client.create_queue(QueueName="pagw-test-" + str(uuid4()))["QueueUrl"]
    broker = object.__new__(SQSBroker)
    broker.client, broker.urls = client, {"submit": queue}
    try:
        body = json.dumps({"operation_id": str(uuid4())})
        assert broker.publish("submit", [{"Id": "one", "MessageBody": body}]) == {"one"}
        messages = broker.receive("submit")
        assert len(messages) == 1 and messages[0].body == body
        broker.ack(messages[0])
        assert not broker.receive("submit")
    finally:
        client.delete_queue(QueueUrl=queue)
