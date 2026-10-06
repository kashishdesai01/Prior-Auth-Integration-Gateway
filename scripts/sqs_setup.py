"""Create queues in a deliberately explicit local SQS emulator endpoint."""

import json
import os

import boto3

endpoint = os.environ.get("PAGW_SQS_ENDPOINT", "http://127.0.0.1:45679")
if not endpoint.startswith("http://127.0.0.1:"):
    raise SystemExit("This bootstrap script is local-only; use Terraform for AWS")
client = boto3.client(
    "sqs",
    endpoint_url=endpoint,
    region_name="us-west-2",
    aws_access_key_id="test",
    aws_secret_access_key="test",  # noqa: S106 -- local emulator credentials
)
urls = {}
for name in ("submit", "webhook"):
    dead = client.create_queue(
        QueueName=f"pagw-{name}-dlq", Attributes={"MessageRetentionPeriod": "1209600"}
    )["QueueUrl"]
    arn = client.get_queue_attributes(QueueUrl=dead, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    urls[name] = client.create_queue(
        QueueName=f"pagw-{name}",
        Attributes={
            "VisibilityTimeout": "60",
            "MessageRetentionPeriod": "1209600",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": "6"}),
        },
    )["QueueUrl"]
print(json.dumps(urls, indent=2))
