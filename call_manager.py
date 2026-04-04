import csv
import os
from datetime import datetime
from threading import Lock

OUTPUT_COLUMNS = {
    "call_status": "pending",
    "claim_result": "",
    "approved_amount": "",
    "denial_reason": "",
    "payment_date": "",
    "appeal_deadline": "",
    "reference_number": "",
    "confirmed": "",
    "call_timestamp": "",
    "transcript_file": "",
}


class CallManager:
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self._lock = Lock()
        self.rows: list[dict] = []
        self.fieldnames: list[str] = []

    def load_csv(self, path: str | None = None):
        target = path or self.csv_path
        with open(target, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.fieldnames = list(reader.fieldnames or [])
            self.rows = list(reader)

        # Add output columns if missing
        for col, default in OUTPUT_COLUMNS.items():
            if col not in self.fieldnames:
                self.fieldnames.append(col)
            for row in self.rows:
                if col not in row or not row[col]:
                    row[col] = default

        self.csv_path = target
        self._save()

    def _save(self):
        if not self.rows or not self.fieldnames:
            return
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(self.rows)

    def get_next_pending(self) -> dict | None:
        with self._lock:
            for row in self.rows:
                if row.get("call_status") == "pending":
                    return dict(row)
            return None

    def update_row(self, claim_number: str, results: dict):
        with self._lock:
            for row in self.rows:
                if str(row.get("claim_number")) == str(claim_number):
                    for key, value in results.items():
                        if key in self.fieldnames:
                            row[key] = value
                    self._save()
                    return

    def set_call_status(self, claim_number: str, status: str):
        self.update_row(claim_number, {"call_status": status})

    def get_all_rows(self) -> list[dict]:
        return [dict(row) for row in self.rows]

    def get_stats(self) -> dict:
        if not self.rows:
            return {"total": 0}
        stats = {
            "total": len(self.rows),
            "pending": sum(1 for r in self.rows if r.get("call_status") == "pending"),
            "in_progress": sum(1 for r in self.rows if r.get("call_status") == "in-progress"),
            "completed": sum(1 for r in self.rows if r.get("call_status") == "completed"),
            "failed": sum(1 for r in self.rows if r.get("call_status") == "failed"),
            "no_answer": sum(1 for r in self.rows if r.get("call_status") == "no-answer"),
        }
        completed = [r for r in self.rows if r.get("call_status") == "completed"]
        if completed:
            stats["approved"] = sum(1 for r in completed if r.get("claim_result") == "approved")
            stats["denied"] = sum(1 for r in completed if r.get("claim_result") == "denied")
            stats["claim_pending"] = sum(1 for r in completed if r.get("claim_result") == "pending")
            stats["in_review"] = sum(1 for r in completed if r.get("claim_result") == "in-review")
        return stats

    def save_transcript(self, claim_number: str, transcript: str, transcripts_dir: str = "transcripts"):
        os.makedirs(transcripts_dir, exist_ok=True)
        filename = f"{claim_number}.txt"
        filepath = os.path.join(transcripts_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(transcript)
        self.update_row(claim_number, {
            "transcript_file": f"{transcripts_dir}/{filename}",
            "call_timestamp": datetime.now().isoformat(),
        })
        return filepath
