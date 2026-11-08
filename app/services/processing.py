"""Background processing of an uploaded document."""

import logging
import uuid

logger = logging.getLogger(__name__)


def process_document(document_id: uuid.UUID) -> None:
    """Placeholder: Slice 4 extracts the text here.

    It will open its own database session (never the request's) and read
    the file from storage by its key, not from memory.
    """
    logger.info("Document %s uploaded; processing arrives in Slice 4", document_id)
