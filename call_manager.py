import csv
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from threading import RLock

logger = logging.getLogger("call-manager")

REQUIRED_COLUMNS = {"patient_name", "member_id", "insurance_phone", "claim_number"}

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

_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


class CallManager:
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self._lock = RLock()
        self.rows: list[dict] = []
        self.fieldnames: list[str] = []

    def validate_csv(self, path: str) -> list[str]:
        """Return list of missing required columns, empty if valid."""
        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                headers = set(reader.fieldnames or [])
        except Exception as e:
            logger.error(f"Failed to read CSV for validation: {e}")
            return list(REQUIRED_COLUMNS)
        missing = sorted(REQUIRED_COLUMNS - headers)
        if missing:
            logger.warning(f"CSV validation failed — missing columns: {missing}")
        else:
            logger.info(f"CSV validation passed ({len(headers)} columns)")
        return missing

    def load_csv(self, path: str | None = None):
        target = path or self.csv_path
        bak_path = target + ".bak"
        # Recover from backup if primary is missing but backup exists
        if not os.path.exists(target) and os.path.exists(bak_path):
            logger.warning(f"CSV missing, recovering from backup: {bak_path}")
            shutil.copy2(bak_path, target)
        with self._lock:
            with open(target, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self.fieldnames = list(reader.fieldnames or [])
                self.rows = list(reader)

            for col, default in OUTPUT_COLUMNS.items():
                if col not in self.fieldnames:
                    self.fieldnames.append(col)
                for row in self.rows:
                    if col not in row or not row[col]:
                        row[col] = default

            self.csv_path = target
            self._save()
            logger.info(f"Loaded CSV: {len(self.rows)} claims from {target}")

    def _save(self):
        if not self.rows or not self.fieldnames:
            return
        dir_name = os.path.dirname(os.path.abspath(self.csv_path))
        tmp_path = None
        try:
            # Write backup before modifying
            bak_path = self.csv_path + ".bak"
            if os.path.exists(self.csv_path):
                shutil.copy2(self.csv_path, bak_path)

            fd, tmp_path = tempfile.mkstemp(suffix=".csv", dir=dir_name)
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                writer.writeheader()
                writer.writerows(self.rows)
            os.replace(tmp_path, self.csv_path)
        except Exception as e:
            logger.error(f"Failed to save CSV: {e}")
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def get_next_pending(self) -> dict | None:
        with self._lock:
            for row in self.rows:
                if row.get("call_status") == "pending":
                    logger.info(f"Next pending claim: {row.get('claim_number')}")
                    return dict(row)
            logger.info("No pending claims remaining")
            return None

    def update_row(self, claim_number: str, results: dict):
        with self._lock:
            for row in self.rows:
                if str(row.get("claim_number")) == str(claim_number):
                    for key, value in results.items():
                        if key in self.fieldnames:
                            row[key] = value
                    self._save()
                    logger.info(f"Updated claim {claim_number}: {results}")
                    return

    def set_call_status(self, claim_number: str, status: str):
        self.update_row(claim_number, {"call_status": status})

    def get_all_rows(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self.rows]

    def get_stats(self) -> dict:
        with self._lock:
            if not self.rows:
                return {"total": 0}
            stats = {
                "total": len(self.rows),
                "pending": sum(1 for r in self.rows if r.get("call_status") == "pending"),
                "in_progress": sum(1 for r in self.rows if r.get("call_status") == "in-progress"),
                "completed": sum(1 for r in self.rows if r.get("call_status") == "completed"),
                "failed": sum(1 for r in self.rows if r.get("call_status") == "failed"),
                "no_answer": sum(1 for r in self.rows if r.get("call_status") == "no-answer"),
                "retrying": sum(1 for r in self.rows if r.get("call_status") == "retrying"),
                "ivr_failed": sum(1 for r in self.rows if r.get("call_status") == "ivr-failed"),
                "dropped": sum(1 for r in self.rows if r.get("call_status") == "dropped"),
            }
            completed = [r for r in self.rows if r.get("call_status") == "completed"]
            if completed:
                stats["approved"] = sum(1 for r in completed if r.get("claim_result") == "approved")
                stats["denied"] = sum(1 for r in completed if r.get("claim_result") == "denied")
                stats["claim_pending"] = sum(1 for r in completed if r.get("claim_result") == "pending")
                stats["in_review"] = sum(1 for r in completed if r.get("claim_result") == "in-review")
            return stats

    def save_transcript(self, claim_number: str, transcript: str, transcripts_dir: str = "transcripts"):
        # Sanitize claim_number for safe filesystem use
        safe_name = claim_number if _SAFE_FILENAME_RE.match(claim_number) else "unknown"
        os.makedirs(transcripts_dir, exist_ok=True)
        filename = f"{safe_name}.txt"
        filepath = os.path.join(transcripts_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(transcript)
        logger.info(f"Transcript saved: {filepath}")
        self.update_row(claim_number, {
            "transcript_file": f"{transcripts_dir}/{filename}",
            "call_timestamp": datetime.now().isoformat(),
        })
        return filepath
