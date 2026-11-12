APP = "socratic-trade"
CRON_SCHEDULES = {
    "CI": "47 7 * * *",
    "Security": "41 10 * * 1",
    "Gone Workflow": "0 0 * * *",
}

def report(workflow_name: str) -> None:
    monitor_slug = f"ci-{APP}-{slugify(workflow_name)}"
    send(monitor_slug)
