import subprocess
import json

# Get jobs of run 43473704192
out = subprocess.check_output(["gh", "run", "view", "43473704192", "--json", "jobs"], text=True)
data = json.loads(out)

for job in data.get("jobs", []):
    name = job.get("name", "")
    job_id = job.get("databaseId")
    print(f"\nChecking {name} (ID: {job_id})...")
    log = subprocess.check_output(["gh", "run", "view", f"--job={job_id}", "--log"], text=True, errors="replace")
    alert_lines = [l for l in log.splitlines() if "Invio alert" in l or "Alert inviati in questa scansione: [1-9]" in l]
    if alert_lines:
        for al in alert_lines:
            print("  ->", al.split("Z ")[-1])
    else:
        print("  -> 0 alerts inviati (nessun falso allarme).")
