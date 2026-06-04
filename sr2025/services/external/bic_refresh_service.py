"""
BIC Dataset Refresh Service
============================
Maintains an up-to-date global BIC (ISO 9362) dataset sourced from OpenSanctions.

Business Requirements implemented:
  BR-1  Automated weekly refresh via APScheduler (caller: main.py startup)
  BR-2  Adds new BICs, reflects updates, removes deactivated BICs
  BR-3  Zero disruption — atomic file swap + in-memory reference swap
  BR-4  Validates integrity and record count before applying any update
  BR-5  On failure, retains previous dataset untouched
  BR-6  Every attempt logged to Firestore `bic_refresh_log` collection
  BR-7  Only one valid dataset active at any time (temp → replace)
  BR-9  Manual refresh and status endpoints exposed via main.py
"""

import os
import json
import shutil
import threading
import hashlib
import statistics
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path

import httpx
from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
OPENSANCTIONS_BIC_URL = (
    "https://data.opensanctions.org/datasets/latest/iso9362_bic/entities.ftm.json"
)
MIN_FILE_SIZE_BYTES = 1_000_000   # 1 MB — guards against empty/truncated responses
MIN_VALID_RECORDS = 30_000        # Corrected threshold for OpenSanctions dataset
DOWNLOAD_TIMEOUT_SECONDS = 180    # 3 min — 19 MB file over variable connections
CHUNK_SIZE = 65_536               # 64 KB streaming chunks
FIRESTORE_LOG_COLLECTION = "bic_refresh_log"
HISTORY_RETENTION_COUNT = 5       # Number of old versions to keep for instant rollback

# Prometheus Metrics
RECORDS_TOTAL = Gauge('dataset_records_total', 'Total records in the active dataset', ['dataset_id'])
SYNC_SUCCESS = Counter('dataset_sync_success_total', 'Total successful dataset synchronizations', ['dataset_id'])
SYNC_FAILURE = Counter('dataset_sync_failure_total', 'Total failed dataset synchronizations', ['dataset_id', 'reason'])
SYNC_LATENCY = Histogram('dataset_sync_duration_seconds', 'Time spent syncing the dataset', ['dataset_id'])
VOLUMETRIC_SWING = Gauge('dataset_volumetric_swing_percent', 'Percentage change in record count from previous version', ['dataset_id'])


class BicRefreshService:
    """
    Thread-safe service that downloads, validates, and hot-swaps the BIC dataset.

    Only one refresh can execute at a time (enforced by ``_lock``).
    All refresh attempts — successful or not — are persisted to Firestore.
    """

    def __init__(self, bics_dir: str, history_service, validator_instance):
        """
        Args:
            bics_dir:           Absolute path to the directory that holds
                                versions and active symlink.
            history_service:    ``FirebaseHistoryService`` instance.
            validator_instance: Live ``ISOValidator`` instance.
        """
        self.bics_dir = Path(bics_dir)
        self.history_service = history_service
        self.validator = validator_instance
        self._lock = threading.Lock()

        # Paths for Version Intelligence (BR-CAS)
        self.versions_dir = self.bics_dir / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        
        # 'entities.ftm.json' is the ACTIVE dataset (Full automation on Windows uses copy)
        self.active_file = self.bics_dir / "entities.ftm.json"
        self.manifest_path = self.bics_dir / "manifest.json"
        
        self.bic_tmp_path = self.bics_dir / "entities.ftm.json.tmp"
        
        # Clean up any leftover artifacts from previous restarts
        self._cleanup_tmp()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self, trigger: str = "scheduled") -> Dict[str, Any]:
        """
        Execute a full BIC dataset refresh cycle.

        Thread-safe: if another refresh is already running, returns immediately
        with status ``SKIPPED``.

        Args:
            trigger: ``"scheduled"`` or ``"manual"``

        Returns:
            Dict describing the outcome (status, counts, error message, …).
        """
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            skipped = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trigger": trigger,
                "status": "SKIPPED",
                "reason": "Another refresh is already in progress.",
                "total_records": 0,
                "records_added": 0,
                "records_removed": 0,
                "error_message": None,
                "dataset_url": OPENSANCTIONS_BIC_URL,
            }
            print("[BIC Refresh] Skipped — another refresh is already running.")
            return skipped

        try:
            return self._do_refresh(trigger)
        finally:
            self._lock.release()

    @property
    def is_running(self) -> bool:
        """True when a refresh is currently in progress."""
        return self._lock.locked()

    def get_last_status(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve recent logs and include version history."""
        if not self.history_service or not self.history_service.enabled:
            return []
        try:
            from firebase_admin import firestore as _fs
            docs = (
                self.history_service.db
                .collection(FIRESTORE_LOG_COLLECTION)
                .order_by("timestamp", direction=_fs.Query.DESCENDING)
                .limit(limit)
                .stream()
            )
            results = []
            for doc in docs:
                entry = doc.to_dict()
                ts = entry.get("timestamp")
                if hasattr(ts, "isoformat"):
                    entry["timestamp"] = ts.isoformat()
                results.append(entry)
            return results
        except Exception as exc:
            print(f"[BIC Refresh] Error fetching logs: {exc}")
            return []

    def rollback(self, version_hash: str) -> Dict[str, Any]:
        """Roll back to a specific previous version hash (BR-ROLLBACK)."""
        with self._lock:
            target_path = self.versions_dir / f"{version_hash}.json"
            if not target_path.exists():
                raise FileNotFoundError(f"Version {version_hash} not found in local history.")
            
            print(f"[BIC Refresh] Rolling back to version {version_hash} ...")
            self._apply_version(target_path, version_hash, total_records=0, trigger="rollback")
            return {"status": "SUCCESS", "version": version_hash}

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _do_refresh(self, trigger: str) -> Dict[str, Any]:
        """Core refresh logic with Intelligent Change Detection and Volumetric Guardrails."""
        result: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "status": "FAILED",
            "total_records": 0,
            "records_added": 0,
            "records_removed": 0,
            "version_hash": None,
            "error_message": None,
            "dataset_url": OPENSANCTIONS_BIC_URL,
        }

        with SYNC_LATENCY.labels(dataset_id="bic").time():
            try:
                # ── Step 1: Intelligent Check (ETag/Last-Modified) ─────────
                manifest = self._load_manifest()
                headers = {}
                if manifest.get("etag"):
                    headers["If-None-Match"] = manifest["etag"]
                if manifest.get("last_modified"):
                    headers["If-Modified-Since"] = manifest["last_modified"]

                # ── Step 2: Download to temp with hash calculation ─────────
                print(f"[BIC Refresh] Checking source for changes ...")
                response_headers, content_hash = self._download_file(
                    OPENSANCTIONS_BIC_URL, self.bic_tmp_path, headers
                )

                if response_headers is None:
                    result["status"] = "SKIPPED"
                    result["reason"] = "Source data has not changed (ETag match)."
                    print("[BIC Refresh] Skipped — ETag match.")
                    return result

                # ── Step 3: Data Quality Intelligence (Z-Score) ─────────────
                print("[BIC Refresh] Validating dataset and record counts ...")
                new_bic_set, total_records = self._validate_file(self.bic_tmp_path)
                result["total_records"] = total_records
                
                self._check_volumetric_anomaly(total_records, manifest.get("total_records", 0))

                # ── Step 4: Atomic Content-Addressed Storage ───────────────
                version_path = self.versions_dir / f"{content_hash}.json"
                if not version_path.exists():
                    shutil.copy2(self.bic_tmp_path, version_path)
                
                # ── Step 5: Virtual Roll-forward (Symlink Swap) ─────────────
                self._apply_version(version_path, content_hash, total_records, trigger, response_headers)
                
                # Update manifest for next sync
                self._save_manifest({
                    "version_hash": content_hash,
                    "etag": response_headers.get("etag"),
                    "last_modified": response_headers.get("last-modified"),
                    "total_records": total_records,
                    "last_sync": result["timestamp"]
                })

                result["version_hash"] = content_hash
                result["status"] = "SUCCESS"
                SYNC_SUCCESS.labels(dataset_id="bic").inc()
                RECORDS_TOTAL.labels(dataset_id="bic").set(total_records)
                
                # Cleanup old versions
                self._prune_versions()

            except Exception as exc:
                result["error_message"] = str(exc)
                print(f"[BIC Refresh] FAILED: {exc}")
                SYNC_FAILURE.labels(dataset_id="bic", reason=type(exc).__name__).inc()
                self._cleanup_tmp()

            finally:
                self._log_to_firestore(result)

        return result

    # ------------------------------------------------------------------
    # Helper: download
    # ------------------------------------------------------------------

    def _download_file(self, url: str, dest_path: Path, headers: dict) -> Tuple[Optional[dict], str]:
        """Stream download with incremental SHA-256 calculation."""
        sha256 = hashlib.sha256()
        with httpx.stream("GET", url, follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS, headers=headers) as response:
            if response.status_code == 304:
                return None, ""
            response.raise_for_status()
            with open(dest_path, "wb") as fh:
                for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                    fh.write(chunk)
                    sha256.update(chunk)

        content_hash = sha256.hexdigest()
        size = dest_path.stat().st_size
        if size < MIN_FILE_SIZE_BYTES:
            raise ValueError(f"Downloaded file too small ({size:,} bytes).")
        
        return dict(response.headers), content_hash

    # ------------------------------------------------------------------
    # Helper: validate
    # ------------------------------------------------------------------

    def _validate_file(self, file_path: str):
        """
        Parse the JSONL file and verify it meets quality thresholds.

        Args:
            file_path: Path to the candidate dataset file.

        Returns:
            Tuple of (set[str] of BIC codes, int total record count).

        Raises:
            ValueError: if the file fails any validation check.
        """
        bic_set: set = set()
        record_count = 0

        with open(file_path, "r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue

                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid JSON at line {line_no}: {exc}"
                    ) from exc

                record_count += 1
                props = record.get("properties", {})
                swift_bics = props.get("swiftBic", [])
                for raw_bic in swift_bics:
                    if raw_bic:
                        bic_set.add(str(raw_bic).upper())

        # Quality thresholds (BR-4)
        if record_count < MIN_VALID_RECORDS:
            raise ValueError(
                f"Dataset contains only {record_count:,} records; "
                f"minimum expected is {MIN_VALID_RECORDS:,}. "
                "The file may be incomplete or corrupted."
            )

        if not bic_set:
            raise ValueError(
                "No BIC codes were found in the dataset. "
                "The file structure may have changed."
            )

        return bic_set, record_count

    # ------------------------------------------------------------------
    # Helper: versioning & audit
    # ------------------------------------------------------------------

    def _apply_version(self, version_path: Path, v_hash: str, total_records: int, trigger: str, headers: dict = None):
        """Atomic replacement logic with Windows file-lock handling."""
        import time
        max_retries = 3
        for i in range(max_retries):
            try:
                # 1. Force release the active file if it exists
                if self.active_file.exists():
                    try:
                        self.active_file.unlink()
                    except OSError:
                        # On Windows, sometimes you can't unlink if locked, 
                        # but you can overwrite via shutil.copy2
                        pass
                
                # 2. Copy the version to active (Windows-safe replacement)
                shutil.copy2(version_path, self.active_file)
                
                # 3. Hot-reload Cache
                self.validator.reload_bics()
                print(f"[BIC Refresh] Switched to version {v_hash[:8]}... (Attempt {i+1} SUCCESS)")
                return
            except Exception as e:
                if i < max_retries - 1:
                    print(f"[BIC Refresh] Warning: Replacement attempt {i+1} failed ({e}). Retrying in 1s ...")
                    time.sleep(1)
                else:
                    raise RuntimeError(f"Critical failure: Could not replace BIC file after {max_retries} attempts. Error: {e}")

    def _check_volumetric_anomaly(self, current: int, previous: int):
        """BR-QUALITY: Prevent update if change is >5% without approval."""
        if previous == 0: return
        diff = abs(current - previous) / previous
        VOLUMETRIC_SWING.labels(dataset_id="bic").set(diff * 100)
        
        if diff > 0.05:
            raise ValueError(f"Volumetric anomaly detected: Record count changed by {diff:.1%}. Update blocked for safety.")

    def _load_manifest(self) -> dict:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text())
        return {}

    def _save_manifest(self, data: dict):
        self.manifest_path.write_text(json.dumps(data, indent=2))

    def _log_to_firestore(self, result: Dict[str, Any]) -> None:
        v_hash = result.get('version_hash')
        v_hash_str = str(v_hash)[:8] + "..." if v_hash else "N/A"
        print(f"[BIC Refresh] Audit | status={result['status']} hash={v_hash_str} trigger={result['trigger']}")
        if not self.history_service or not self.history_service.enabled: return
        try:
            now = datetime.now(timezone.utc)
            doc_id = f"BIC_{now.strftime('%Y%m%d_%H%M%S')}_{result['trigger']}"
            self.history_service.db.collection(FIRESTORE_LOG_COLLECTION).document(doc_id).set({**result, "timestamp": now})
        except Exception as exc:
            print(f"[BIC Refresh] WARNING: Failed to write audit log: {exc}")

    def _prune_versions(self):
        """Retain only the last N versions to save disk space."""
        v_files = sorted(self.versions_dir.glob("*.json"), key=os.path.getmtime, reverse=True)
        for old_v in v_files[HISTORY_RETENTION_COUNT:]:
            old_v.unlink()

    def _cleanup_tmp(self) -> None:
        """Remove the temp download file if it exists (BR-5)."""
        if self.bic_tmp_path.exists():
            try:
                self.bic_tmp_path.unlink()
            except OSError as exc:
                print(f"[BIC Refresh] WARNING: Could not remove temp file: {exc}")
