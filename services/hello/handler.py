import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    logger.info("hello invoked", extra={"request_id": context.aws_request_id})
    return {"message": "hello"}
