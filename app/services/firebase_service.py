import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

# Load .env from the backend root (two levels up from this file)
_backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(_backend_root, ".env"))


def _sanitize_firestore_doc(obj: Any) -> Any:
    """
    Recursively converts Firestore-specific types to JSON-serializable Python types.
    - DatetimeWithNanoseconds / datetime  →  ISO-8601 string (UTC, ending in 'Z')
    - dict  →  recursively sanitized dict
    - list  →  recursively sanitized list
    All other types pass through unchanged.
    """
    if obj is None:
        return obj
    # Firestore DatetimeWithNanoseconds is a subclass of datetime
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(obj, dict):
        return {k: _sanitize_firestore_doc(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_firestore_doc(item) for item in obj]
    return obj


class FirebaseHistoryService:
    # Per-call timeout for any Firestore RPC. The default of 300s caused
    # the whole site to feel frozen when credentials were bad — every endpoint
    # waited 5 minutes per call. Hot-path calls (get_next_sequence,
    # check_duplicate_msg_uetr) run on EVERY validation, so this needs to fail
    # fast: 4s is comfortably above a healthy round-trip yet short enough that a
    # brief startup window (before the boot health check trips the breaker)
    # doesn't make validation feel like it hangs.
    FIRESTORE_CALL_TIMEOUT_S = 4.0

    # Circuit breaker — after this many consecutive auth failures we disable
    # Firestore at runtime and fall back to local JSON. Prevents the same
    # 300s-timeout dance from repeating endlessly.
    AUTH_FAILURE_THRESHOLD = 3

    def __init__(self):
        self.db = None
        self.enabled = False
        self.local_fallback = True
        # Circuit-breaker state. self.enabled is the user-visible flag; this
        # tracks whether we tripped the breaker at runtime due to auth errors.
        self._auth_failure_count = 0
        self._circuit_broken_reason: Optional[str] = None

        # Local JSON database paths
        self.local_db_path = os.path.join(_backend_root, "validation_history_local.json")
        self.local_counters_path = os.path.join(_backend_root, "validation_counters_local.json")

        # In-memory cache for local JSON database and counters
        self._local_db_cache = None
        self._local_counters_cache = None

        # In-memory cache for Firestore stats
        self._firebase_stats_cache = None
        self._firebase_stats_cache_time = None

        # Check if Firebase is enabled via env var
        firebase_enabled = os.getenv("FIREBASE_ENABLED", "true").lower() == "true"
        if not firebase_enabled:
            print("ALERT: Firebase is explicitly disabled via FIREBASE_ENABLED=false. Falling back to local JSON database.")
            self.enabled = False
            return

        try:
            cred = self._build_credentials()
            if cred is not None:
                # Avoid re-initialization if app already initialized
                if not firebase_admin._apps:
                    firebase_admin.initialize_app(cred)
                self.db = firestore.client()
                self.enabled = True
                print("Firebase Firestore initialized successfully.")

                # Boot-time health check: credential parsing succeeded but that
                # only proves the JSON shape is valid. The actual OAuth
                # signature is only verified when we make a real RPC. Do a
                # tiny throwaway read to surface "invalid_grant" / "Invalid
                # JWT Signature" at boot, BEFORE the first user request
                # hangs for 5 minutes. Run in a background thread to avoid blocking.
                import threading
                threading.Thread(target=self._run_boot_health_check, daemon=True).start()
            else:
                print("ALERT: No Firebase credentials found. Falling back to local JSON database.")
                self.enabled = False
        except Exception as e:
            print(f"CRITICAL: Error initializing Firebase: {str(e)}. Falling back to local JSON database.")
            self.enabled = False

    def _run_boot_health_check(self) -> None:
        """Issue a single tiny Firestore read to verify the credentials actually
        work against Google's OAuth endpoint. If it fails, disable Firestore
        immediately so subsequent calls don't sit on 300-second timeouts."""
        # First do a quick TCP check to avoid gRPC connection hang when offline
        import socket
        try:
            # Try to connect to firestore.googleapis.com:443 with a 2-second timeout
            socket.create_connection(("firestore.googleapis.com", 443), timeout=2.0).close()
        except Exception as e:
            self.enabled = False
            self._circuit_broken_reason = f"Network unreachable: {e}"
            print(f"[Firebase] Network check failed: firestore.googleapis.com is unreachable ({e}). Falling back to local JSON.")
            return

        try:
            # Limit to 1 doc, short timeout. We don't care about the result —
            # we only care whether the RPC authenticates and responds.
            list(self.db.collection("__health_check").limit(1).stream(
                timeout=self.FIRESTORE_CALL_TIMEOUT_S
            ))
            print("[Firebase] Boot health check passed — Firestore reachable.")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            # The common bad-cred error texts. We detect them so the logged
            # banner gives the operator a clear action item.
            cred_signals = ("invalid_grant", "Invalid JWT", "Invalid JWT Signature",
                            "PERMISSION_DENIED", "UNAUTHENTICATED")
            looks_like_auth = any(s in str(e) for s in cred_signals)

            self.enabled = False
            self._circuit_broken_reason = msg
            if looks_like_auth:
                print("=" * 70)
                print("[Firebase] CREDENTIALS REJECTED BY GOOGLE.")
                print(f"[Firebase] Underlying error: {msg}")
                print("[Firebase] The service-account JSON key is most likely revoked,")
                print("[Firebase] rotated, or corrupted in the env var. Generate a fresh")
                print("[Firebase] key in GCP → IAM → Service Accounts and update")
                print("[Firebase] FIREBASE_CREDENTIALS_BASE64 on Render, then redeploy.")
                print("[Firebase] Falling back to local JSON for this session.")
                print("=" * 70)
            else:
                print(f"[Firebase] Boot health check failed ({msg}). Falling back to local JSON.")

    def _note_call_outcome(self, success: bool, error: Optional[Exception] = None) -> None:
        """Update the circuit-breaker state after a Firestore call."""
        if success:
            self._auth_failure_count = 0
            return

        looks_like_auth = error is not None and any(
            s in str(error) for s in ("invalid_grant", "Invalid JWT",
                                      "PERMISSION_DENIED", "UNAUTHENTICATED")
        )
        if looks_like_auth:
            self._auth_failure_count += 1
            if self._auth_failure_count >= self.AUTH_FAILURE_THRESHOLD:
                self.enabled = False
                self._circuit_broken_reason = f"{type(error).__name__}: {error}"
                print(f"[Firebase] 🚨 Circuit breaker tripped after "
                      f"{self._auth_failure_count} consecutive auth failures. "
                      f"Disabling Firestore for this session and using local JSON.")

    def _read_local_db(self) -> list:
        if self._local_db_cache is not None:
            return self._local_db_cache
        if not os.path.exists(self.local_db_path):
            self._local_db_cache = []
            return self._local_db_cache
        try:
            with open(self.local_db_path, "r", encoding="utf-8") as f:
                self._local_db_cache = json.load(f)
                return self._local_db_cache
        except Exception as e:
            print(f"[LocalDB] Error reading local history: {e}")
            return []

    def _write_local_db(self, data: list):
        self._local_db_cache = data
        try:
            with open(self.local_db_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[LocalDB] Error writing local history: {e}")

    def _read_local_counters(self) -> dict:
        if self._local_counters_cache is not None:
            return self._local_counters_cache
        if not os.path.exists(self.local_counters_path):
            self._local_counters_cache = {}
            return self._local_counters_cache
        try:
            with open(self.local_counters_path, "r", encoding="utf-8") as f:
                self._local_counters_cache = json.load(f)
                return self._local_counters_cache
        except Exception as e:
            print(f"[LocalDB] Error reading local counters: {e}")
            return {}

    def _write_local_counters(self, data: dict):
        self._local_counters_cache = data
        try:
            with open(self.local_counters_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[LocalDB] Error writing local counters: {e}")

    @staticmethod
    def _build_credentials():
        b64_creds = os.getenv("FIREBASE_CREDENTIALS_BASE64", "").strip()
        if b64_creds:
            print("[Firebase] Option 0 — found FIREBASE_CREDENTIALS_BASE64, decoding...")
            try:
                import base64 as _b64
                cred_json = _b64.b64decode(b64_creds).decode("utf-8")
                cred_dict = json.loads(cred_json)
                cred = credentials.Certificate(cred_dict)
                print(f"[Firebase] Option 0 — credentials decoded successfully (project: {cred_dict.get('project_id')})")
                return cred
            except Exception as e:
                print(f"[Firebase] Option 0 FAILED: {type(e).__name__}: {e}")

        project_id   = os.getenv("FIREBASE_PROJECT_ID",   "").strip()
        private_key  = os.getenv("FIREBASE_PRIVATE_KEY",  "").strip()
        client_email = os.getenv("FIREBASE_CLIENT_EMAIL", "").strip()

        print(f"[Firebase] Credential check — project_id={'SET' if project_id else 'MISSING'}, client_email={'SET' if client_email else 'MISSING'}, private_key_length={len(private_key)}")

        if project_id and client_email and private_key:
            if (private_key.startswith('"') and private_key.endswith('"')) or (private_key.startswith("'") and private_key.endswith("'")):
                private_key = private_key[1:-1].strip()
                print("[Firebase] Stripped surrounding quotes from FIREBASE_PRIVATE_KEY")

            if "\\n" in private_key:
                private_key = private_key.replace("\\n", "\n")
            if "\n" in private_key:
                private_key = private_key.replace("\n", "\n")

            if "BEGIN PRIVATE KEY" not in private_key:
                print(f"[Firebase] WARNING: private key missing 'BEGIN PRIVATE KEY'. First 50 chars: {repr(private_key[:50])}")
            else:
                print("[Firebase] Private key header looks valid — attempting to build credentials")

            cert_dict = {
                "type": "service_account",
                "project_id": project_id,
                "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID", ""),
                "private_key": private_key,
                "client_email": client_email,
                "client_id": os.getenv("FIREBASE_CLIENT_ID", ""),
                "auth_uri": os.getenv("FIREBASE_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
                "token_uri": os.getenv("FIREBASE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
                "auth_provider_x509_cert_url": os.getenv("FIREBASE_AUTH_PROVIDER_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
                "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_CERT_URL", ""),
            }
            try:
                cred = credentials.Certificate(cert_dict)
                print("[Firebase] Option 1 (inline env vars) — credentials created successfully")
                return cred
            except Exception as e:
                print(f"[Firebase] Option 1 FAILED: {type(e).__name__}: {e}")

        key_path_env = os.getenv("FIREBASE_KEY_PATH", "").strip()
        if key_path_env:
            if not os.path.isabs(key_path_env):
                key_path_env = os.path.join(_backend_root, key_path_env)
            if os.path.exists(key_path_env):
                print(f"[Firebase] Option 2 — using key file: {key_path_env}")
                try:
                    return credentials.Certificate(key_path_env)
                except Exception as e:
                    print(f"[Firebase] Option 2 FAILED: {e}")
            else:
                print(f"[Firebase] Option 2 — FIREBASE_KEY_PATH set but file not found: {key_path_env}")

        legacy_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "firebase-key.json")
        if os.path.exists(legacy_path):
            print(f"[Firebase] Option 3 — using legacy key file: {legacy_path}")
            try:
                return credentials.Certificate(legacy_path)
            except Exception as e:
                print(f"[Firebase] Option 3 FAILED: {e}")

        print("[Firebase] CRITICAL: No valid credentials found via any option.")
        return None

    def save_history(self, record: dict) -> str:
        # Invalidate stats cache since history is changing
        self._firebase_stats_cache = None
        if not self.enabled:
            if self.local_fallback:
                try:
                    if "timestamp" not in record:
                        record["timestamp"] = datetime.now(timezone.utc).isoformat()
                    elif isinstance(record["timestamp"], datetime):
                        record["timestamp"] = record["timestamp"].isoformat()
                    
                    if "deleted" not in record:
                        record["deleted"] = False
                        
                    doc_id = record["validation_id"]
                    if record.get("file_id"):
                        doc_id = f"{record['validation_id']}_{record['file_id']}"
                    
                    db_data = self._read_local_db()
                    db_data = [r for r in db_data if r.get("validation_id") != record["validation_id"] or r.get("file_id") != record.get("file_id")]
                    
                    sanitized = _sanitize_firestore_doc(record)
                    db_data.append(sanitized)
                    self._write_local_db(db_data)
                    print(f"[LocalDB] Saved local history document: {doc_id}")
                    return doc_id
                except Exception as e:
                    print(f"[LocalDB] Error in save_history: {e}")
                    return None
            print("[Firebase] SKIPPED save_history: Firebase is disabled (credentials not loaded).")
            return None

        try:
            if "timestamp" not in record:
                record["timestamp"] = datetime.now(timezone.utc)
            elif isinstance(record["timestamp"], str):
                try:
                    record["timestamp"] = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                except Exception:
                    record["timestamp"] = datetime.now(timezone.utc)

            if "deleted" not in record:
                record["deleted"] = False

            saved_timestamp = record.get("timestamp")
            sanitized = _sanitize_firestore_doc(record)
            sanitized["timestamp"] = saved_timestamp

            doc_id = sanitized["validation_id"]
            if sanitized.get("file_id"):
                doc_id = f"{sanitized['validation_id']}_{sanitized['file_id']}"

            doc_ref = self.db.collection("validation_history").document(doc_id)
            doc_ref.set(sanitized)
            print(f"[Firebase] Saved document: {doc_id}")
            return doc_id
        except Exception as e:
            print(f"[Firebase] ERROR in save_history: {type(e).__name__}: {e}")
            return None

    def get_history(self, skip: int = 0, limit: int = 5000) -> list:
        if not self.enabled:
            if self.local_fallback:
                try:
                    db_data = self._read_local_db()
                    non_deleted = [r for r in db_data if not r.get("deleted", False)]
                    non_deleted.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
                    
                    results = []
                    for r in non_deleted[skip:]:
                        if "origin" not in r or not r["origin"]:
                            r["origin"] = "Pasted"
                        results.append(r)
                        if len(results) >= limit:
                            break
                    return results
                except Exception as e:
                    print(f"[LocalDB] Error fetching local history: {e}")
                    return []
            return []
            
        try:
            query = self.db.collection("validation_history") \
                           .select(["validation_id", "batch_id", "file_id", "timestamp", "message_type", "status", "total_errors", "total_warnings", "execution_time_ms", "deleted", "origin"]) \
                           .order_by("timestamp", direction=firestore.Query.DESCENDING)

            # Limit the query size to prevent streaming the entire collection when only a subset is requested.
            # Add 100 extra docs to account for any soft-deleted records.
            if limit < 10000:
                query = query.limit(limit + skip + 100)

            # Short timeout — don't let a broken/slow Firestore freeze the
            # /history endpoint for 5 minutes. 10s is plenty for healthy reads.
            docs = query.stream(timeout=self.FIRESTORE_CALL_TIMEOUT_S)
            results = []
            non_deleted_count = 0
            for doc in docs:
                data = doc.to_dict()
                if data.get("deleted", False):
                    continue

                non_deleted_count += 1
                if non_deleted_count <= skip:
                    continue
                sanitized_data = _sanitize_firestore_doc(data)
                if "origin" not in sanitized_data or not sanitized_data["origin"]:
                    sanitized_data["origin"] = "Pasted"
                results.append(sanitized_data)
                if len(results) >= limit:
                    break
            self._note_call_outcome(True)
            return results
        except Exception as e:
            self._note_call_outcome(False, e)
            print(f"Error fetching Firestore history: {e}")
            # Best-effort fallback to local JSON so the UI gets *something*
            # rather than an empty list while the operator fixes credentials.
            if self.local_fallback:
                try:
                    db_data = self._read_local_db()
                    non_deleted = [r for r in db_data if not r.get("deleted", False)]
                    non_deleted.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
                    return non_deleted[skip:skip + limit]
                except Exception:
                    pass
            return []

    def get_stats(self) -> dict:
        now = datetime.now(timezone.utc)
        if self.enabled:
            # Return cached Firestore stats if they are less than 15 seconds old
            if self._firebase_stats_cache is not None and self._firebase_stats_cache_time is not None:
                if (now - self._firebase_stats_cache_time).total_seconds() < 15:
                    return self._firebase_stats_cache

        if not self.enabled:
            if self.local_fallback:
                try:
                    db_data = self._read_local_db()
                    non_deleted = [r for r in db_data if not r.get("deleted", False)]
                    total = len(non_deleted)
                    passed = sum(1 for r in non_deleted if r.get("status") == "PASS")
                    failed = sum(1 for r in non_deleted if r.get("status") == "FAIL")
                    quality = round((passed / total) * 100) if total > 0 else 0
                    return {
                        "total_audits": total,
                        "passed_messages": passed,
                        "failed_messages": failed,
                        "validation_quality": quality
                    }
                except Exception as e:
                    print(f"[LocalDB] Error fetching local stats: {e}")
                    return {"total_audits": 0, "passed_messages": 0, "failed_messages": 0, "validation_quality": 0}
            return {"total_audits": 0, "passed_messages": 0, "failed_messages": 0, "validation_quality": 0}
            
        try:
            docs = self.db.collection("validation_history").stream(
                timeout=self.FIRESTORE_CALL_TIMEOUT_S
            )
            total = 0
            passed = 0
            failed = 0

            for doc in docs:
                data = doc.to_dict()
                if data.get("deleted", False):
                    continue

                total += 1
                if data.get("status") == "PASS":
                    passed += 1
                elif data.get("status") == "FAIL":
                    failed += 1

            quality = round((passed / total) * 100) if total > 0 else 0
            self._note_call_outcome(True)
            stats_result = {
                "total_audits": total,
                "passed_messages": passed,
                "failed_messages": failed,
                "validation_quality": quality
            }
            # Cache the result
            self._firebase_stats_cache = stats_result
            self._firebase_stats_cache_time = now
            return stats_result
        except Exception as e:
            self._note_call_outcome(False, e)
            print(f"Error calculating stats from Firestore: {e}")
            return {"total_audits": 0, "passed_messages": 0, "failed_messages": 0, "validation_quality": 0}

    def delete_record(self, validation_id: str) -> bool:
        self._firebase_stats_cache = None
        if not self.enabled:
            if self.local_fallback:
                try:
                    db_data = self._read_local_db()
                    modified = False
                    for r in db_data:
                        if r.get("batch_id") == validation_id or r.get("validation_id") == validation_id:
                            r["deleted"] = True
                            modified = True
                    if modified:
                        self._write_local_db(db_data)
                        return True
                    return False
                except Exception as e:
                    print(f"[LocalDB] Error deleting local record: {e}")
                    return False
            return False
            
        try:
            docs = list(self.db.collection("validation_history").where("batch_id", "==", validation_id).stream())
            if docs:
                batch = self.db.batch()
                for doc in docs:
                    batch.update(doc.reference, {"deleted": True})
                batch.commit()
                return True

            # No batch-id match — try direct document ID
            doc_ref = self.db.collection("validation_history").document(validation_id)
            doc_snap = doc_ref.get()
            if doc_snap.exists:
                doc_ref.update({"deleted": True})
                return True
            return False
        except Exception as e:
            print(f"[Firebase] ERROR in delete_record: {type(e).__name__}: {e}")
            return False

    def delete_all(self) -> int:
        self._firebase_stats_cache = None
        if not self.enabled:
            if self.local_fallback:
                try:
                    db_data = self._read_local_db()
                    count = 0
                    for r in db_data:
                        if not r.get("deleted", False):
                            r["deleted"] = True
                            count += 1
                    if count > 0:
                        self._write_local_db(db_data)
                    return count
                except Exception as e:
                    print(f"[LocalDB] Error soft deleting local all: {e}")
                    return 0
            return 0
        
        try:
            batch = self.db.batch()
            docs = list(self.db.collection("validation_history").stream())
            count = 0
            for doc in docs:
                if not doc.to_dict().get("deleted", False):
                    batch.update(doc.reference, {"deleted": True})
                    count += 1
                    if count % 500 == 0:
                        batch.commit()
                        batch = self.db.batch()
            
            if count % 500 != 0:
                batch.commit()
            return count
        except Exception as e:
            print(f"Error during batch delete: {e}")
            return 0

    def reset_all_counters(self):
        if not self.enabled:
            if self.local_fallback:
                self._write_local_counters({})
                print("[LocalDB] Reset local counters database.")
            return
        try:
            docs = list(self.db.collection("validation_counters").stream())
            batch = self.db.batch()
            count = 0
            for doc in docs:
                batch.delete(doc.reference)
                count += 1
                if count % 500 == 0:
                    batch.commit()
                    batch = self.db.batch()
            if count % 500 != 0:
                batch.commit()
            print(f"Reset {count} counter document(s) in Firebase.")
        except Exception as e:
            print(f"Error resetting counters: {e}")

    def hard_delete_all(self) -> int:
        self._firebase_stats_cache = None
        if not self.enabled:
            if self.local_fallback:
                try:
                    self._write_local_db([])
                    self.reset_all_counters()
                    return 1
                except Exception as e:
                    print(f"[LocalDB] Error hard deleting local all: {e}")
                    return 0
            return 0
        
        total_deleted = 0
        try:
            docs = list(self.db.collection("validation_history").stream())
            batch = self.db.batch()
            count = 0
            for doc in docs:
                batch.delete(doc.reference)
                count += 1
                if count % 500 == 0:
                    batch.commit()
                    batch = self.db.batch()
            if count % 500 != 0:
                batch.commit()
            total_deleted = count
            
            self.reset_all_counters()
            print(f"Hard deleted {total_deleted} history documents and reset all counters.")
        except Exception as e:
            print(f"Error during hard delete: {e}")
        return total_deleted

    def get_detail(self, validation_id: str) -> Optional[dict]:
        if not self.enabled:
            if self.local_fallback:
                try:
                    db_data = self._read_local_db()
                    for r in db_data:
                        if r.get("deleted", False):
                            continue
                        if r.get("validation_id") == validation_id or r.get("batch_id") == validation_id:
                            return r
                    return None
                except Exception as e:
                    print(f"[LocalDB] Error fetching local detail: {e}")
                    return None
            return None
            
        try:
            collection = self.db.collection("validation_history")
            doc = collection.document(validation_id).get()
            if doc.exists:
                data = doc.to_dict()
                if not data.get("deleted", False):
                    return _sanitize_firestore_doc(data)

            docs = list(collection.where("validation_id", "==", validation_id).where("deleted", "==", False).limit(1).stream())
            if docs:
                return _sanitize_firestore_doc(docs[0].to_dict())

            docs = list(collection.where("batch_id", "==", validation_id).where("deleted", "==", False).limit(1).stream())
            if docs:
                return _sanitize_firestore_doc(docs[0].to_dict())
            return None
        except Exception as e:
            print(f"Error fetching detail for '{validation_id}': {e}")
            return None

    def get_next_sequence(self, date_str: str) -> int:
        if not self.enabled:
            if self.local_fallback:
                try:
                    import threading
                    if not hasattr(self, "_local_counter_lock"):
                        self._local_counter_lock = threading.Lock()
                        
                    with self._local_counter_lock:
                        counters = self._read_local_counters()
                        seq = counters.get(date_str, 0) + 1
                        counters[date_str] = seq
                        self._write_local_counters(counters)
                        return seq
                except Exception as e:
                    print(f"[LocalDB] Error generating local sequence: {e}")
                    return None
            return None
            
        doc_ref = self.db.collection("validation_counters").document(date_str)

        # ── PATH 1: idiomatic Firestore transaction ────────────────────────────
        # Notes:
        #   - doc_ref is passed as a parameter (recommended pattern; avoids any
        #     closure quirks the SDK retry decorator sometimes trips on).
        #   - We use transaction.set() in both branches; if the field is missing
        #     for any reason we default to 0 so we never crash on None + 1.
        #   - A per-RPC timeout is mandatory here. This call runs on EVERY
        #     validation; without it, a reachable-but-slow (or briefly
        #     unreachable, e.g. before the boot health check trips the breaker)
        #     Firestore makes the read block on gRPC's multi-minute default,
        #     which is exactly what makes local validation appear to "hang".
        timeout_s = self.FIRESTORE_CALL_TIMEOUT_S

        @firestore.transactional
        def update_in_transaction(transaction, ref):
            snapshot = ref.get(transaction=transaction, timeout=timeout_s)
            current = 0
            if snapshot.exists:
                val = snapshot.get("seq")
                if isinstance(val, int):
                    current = val
            new_seq = current + 1
            transaction.set(ref, {"seq": new_seq})
            return new_seq

        try:
            transaction = self.db.transaction()
            return update_in_transaction(transaction, doc_ref)
        except Exception as e:
            # The misleading "transaction has no id so it cannot be rolled back"
            # surfaces here when the first read inside the transaction fails before
            # Firestore returns a transaction ID — usually a permission, network,
            # or schema problem. We log it for diagnosis and fall back below.
            print(f"[Firebase] counter transaction failed ({type(e).__name__}: {e}). "
                  f"Trying non-transactional Increment fallback.")

        # ── PATH 2: server-side atomic Increment fallback ──────────────────────
        # firestore.Increment is atomic on the server side — no transaction needed.
        # The read-back is racy across concurrent writers, but for VAL-id sequencing
        # we only need uniqueness, not strict ordering, and the caller already has
        # its own in-memory fallback if this returns None.
        try:
            doc_ref.set({"seq": firestore.Increment(1)}, merge=True, timeout=timeout_s)
            snap = doc_ref.get(timeout=timeout_s)
            if snap.exists:
                val = snap.get("seq")
                if isinstance(val, int):
                    return val
            return None
        except Exception as e:
            print(f"[Firebase] counter Increment fallback also failed "
                  f"({type(e).__name__}: {e}). Caller will use in-memory counter.")
            return None

    def check_duplicate_msg_uetr(self, msg_id: str, uetr: str) -> bool:
        if not self.enabled:
            if self.local_fallback:
                try:
                    db_data = self._read_local_db()
                    for r in db_data:
                        if r.get("deleted", False):
                            continue
                        report = r.get("report_json", {})
                        metadata = report.get("metadata", {})
                        if metadata.get("MsgId") == msg_id and metadata.get("UETR") == uetr:
                            return True
                    return False
                except Exception as e:
                    print(f"[LocalDB] Error checking local duplicate: {e}")
                    return False
            return False
            
        try:
            # Per-RPC timeout: this runs inside Layer 3, i.e. on the validation
            # hot path. Without a bound, a slow/unreachable Firestore stalls the
            # whole validation instead of degrading to "no duplicate found".
            query = (
                self.db.collection("validation_history")
                .where("deleted", "==", False)
                .where("report_json.metadata.MsgId", "==", msg_id)
                .stream(timeout=self.FIRESTORE_CALL_TIMEOUT_S)
            )
            for doc in query:
                data = doc.to_dict()
                report = data.get("report_json", {})
                metadata = report.get("metadata", {})
                if metadata.get("UETR") == uetr:
                    return True
            return False
        except Exception as e:
            print(f"Error checking duplicate MsgId/UETR in Firestore: {e}")
            return False

    def test_write(self) -> dict:
        if not self.enabled:
            if self.local_fallback:
                return {"success": True, "note": "Local JSON database is operational."}
            return {"success": False, "error": "Firebase not enabled — credentials missing or failed to load."}
        try:
            test_id = f"_write_test_{int(datetime.now(timezone.utc).timestamp())}"
            doc_ref = self.db.collection("validation_history").document(test_id)
            doc_ref.set({
                "_test": True,
                "timestamp": datetime.now(timezone.utc),
                "deleted": True
            })
            doc_ref.delete()
            print(f"[Firebase] test_write succeeded (doc_id={test_id})")
            return {"success": True, "doc_id": test_id}
        except Exception as e:
            print(f"[Firebase] test_write FAILED: {type(e).__name__}: {e}")
            return {"success": False, "error": str(e)}
