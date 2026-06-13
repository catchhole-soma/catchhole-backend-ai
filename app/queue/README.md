# queue

Queue integration lives here.

Expected future files:

- `sqs_consumer.py`: consume analysis job messages from SQS
- `messages.py`: queue message schemas
- `dispatcher.py`: route job messages to workers

For local development, HTTP manual trigger can remain available while queue consumption is added later.
