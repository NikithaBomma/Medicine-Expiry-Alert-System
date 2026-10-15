import os
import threading


os.environ["SCHEDULER_ENABLED"] = "false"

from app import app
from notifications import start_scheduler


if __name__ == "__main__":
    scheduler = start_scheduler(app)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        scheduler.shutdown(wait=True)
