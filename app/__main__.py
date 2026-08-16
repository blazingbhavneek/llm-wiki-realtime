"""``python -m app`` - the agent worker and the web server in one process."""

from app.runtime.worker import main

if __name__ == "__main__":
    main()
