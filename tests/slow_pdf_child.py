"""Stand-ins for the PDF reading child process, imported by it in the
time limit tests. Kept tiny so the child imports nothing else."""

import time


def never_answers(sender, data: bytes, max_pages: int) -> None:
    """A PDF that takes far longer than any time limit to read."""
    time.sleep(60)


def dies(sender, data: bytes, max_pages: int) -> None:
    """A child that crashes (e.g. out of memory) without an answer."""
    sender.close()
