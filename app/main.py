import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Load .env early so all os.getenv() calls (OpenAI, Firebase, etc.) see the keys
import os as _os
from dotenv import load_dotenv as _load_dotenv
_load_dotenv(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".env"))

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List, Optional
import asyncio
import json
import os
import csv
import io

# Ensure the parent directory is in sys.path to allow absolute imports when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.schemas import validation as schemas
from app.schemas import fixes as fix_schemas
from app.services.validator import ISOValidator
from app.services.firebase_service import FirebaseHistoryService
from app.services.schema_generator import SchemaGenerator
from app.services.mt_mx_converter import MT2MXConverter
from app.services.bic_refresh_service import BicRefreshService
from app.services.bulk_generator import generate_single_xml, get_blocks_for_message
from app.services.validation.validator import ISOValidator
from app.services.external.firebase_service import FirebaseHistoryService
from app.services.generation.schema_generator import SchemaGenerator
from app.services.conversion.mt_mx_converter import MT2MXConverter
from app.services.external.bic_refresh_service import BicRefreshService
from app.services.generation.bulk_generator import generate_single_xml, get_blocks_for_message
from app.chatbot.routes import router as chatbot_router
from app.chatbot.chat_service import chat_service
from app.routes.fixes import router as fixes_router
from app.services.fix_suggester import fix_suggester

# Initialize services
history_service = FirebaseHistoryService()
validator = ISOValidator(history_service=history_service)
mt_mx_converter = MT2MXConverter()
bic_refresh_service = BicRefreshService(
    bics_dir=validator.bics_path,
    history_service=history_service,
    validator_instance=validator,
)

# DEPRECATED: SQLite Initialization
# database.Base.metadata.create_all(bind=database.engine)

app = FastAPI(title="ISO 20022 Validation API (Firebase Powered)")

# Configure CORS
origins_str = os.getenv("CORS_ORIGINS", "http://localhost:4200,http://127.0.0.1:4200,http://localhost:8001,http://127.0.0.1:8001,https://iso20022generatorfrontend.vercel.app,https://46lzw3h8-4200.inc1.devtunnels.ms")
origins = [origin.strip() for origin in origins_str.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if origins_str == "*" else origins,
    allow_credentials=True if origins_str != "*" else False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include chatbot router
app.include_router(chatbot_router)

# Include fixes router (Inline Fix Suggester)
app.include_router(fixes_router)

# Initialize chatbot knowledge base and BIC refresh scheduler on startup
@app.on_event("startup")
async def startup_event():
    import threading
    from datetime import datetime
    print(f"\n[Server] Reloaded at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Init LLM on main thread (fast) so it's ready immediately
    chat_service._ensure_llm()
    # Load knowledge base in background (slow - reads 779+ files)
    chat_enabled = os.getenv("CHATBOT_ENABLED", "false").lower() == "true"
    if chat_enabled:
        threading.Thread(target=chat_service.initialize, args=(base_dir,), daemon=True).start()
    else:
        print("[Chatbot] Knowledge base initialization skipped via CHATBOT_ENABLED=false (saves memory).")

    # ── BIC Weekly Refresh Scheduler (BR-1) ───────────────────────────────
    # Runs every Monday at 02:00 UTC; missed runs are retried within 1 hour.
    bic_refresh_enabled = os.getenv("BIC_REFRESH_ENABLED", "true").lower() == "true"
    if bic_refresh_enabled:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger

            app.state.scheduler = BackgroundScheduler(timezone="UTC")
            app.state.scheduler.add_job(
                func=lambda: bic_refresh_service.refresh(trigger="scheduled"),
                trigger=CronTrigger(day_of_week="mon", hour=2, minute=0),
                id="bic_weekly_refresh",
                name="Weekly BIC Dataset Refresh (ISO 9362 / OpenSanctions)",
                misfire_grace_time=3600,  # retry up to 1 h after a missed window
                coalesce=True,            # run once even if multiple windows were missed
                replace_existing=True,
            )
            app.state.scheduler.start()
            print("[BIC Refresh] Weekly scheduler started (every Monday 02:00 UTC).")
        except ImportError:
            print(
                "[BIC Refresh] WARNING: 'apscheduler' not installed. "
                "Automated weekly refresh is DISABLED. "
                "Run: pip install apscheduler"
            )
    else:
        print("[BIC Refresh] Automated refresh disabled via BIC_REFRESH_ENABLED=false.")


@app.on_event("shutdown")
async def shutdown_event():
    """Gracefully stop the APScheduler background scheduler on server shutdown."""
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        print("[BIC Refresh] Scheduler stopped.")


# WARNING-severity rules the auto-fixer is allowed to resolve. These are
# external-code-list validity checks kept as WARNING (the lists may be
# incomplete), but a value that is definitively NOT in the list is safely
# replaceable with a valid code. Deliberately excludes advisory warnings such as
# BIC_NOT_FOUND / CONFIG_NOT_FOUND / HEAD001_TZ_DRIFT, which must not be rewritten.
_AUTOFIX_WARNING_CODES = {
    "L3_CLRSYS_CODE", "L3_ACCT_TYPE_CODE", "L3_LCLINSTRM_CODE",
    # BizSvc format errors (e.g. 'swift..02') are deterministically fixable
    # — the correct value is derived from the message type KB.
    "HEAD001_BIZSVC_FORMAT",
}


async def _auto_fix_iterative(original_xml: str, message_type: str = "Auto-detect",
                              max_rounds: int = 12):
    """
    Iteratively validate → fix → re-validate until the document is clean, no
    further actionable fix can be produced, or no progress is made.

    A single batch pass can never fully clean a badly-broken message because:
      • the XSD validator surfaces only the FIRST sequence error per container
        (later ones are hidden until the first is fixed),
      • fixing/inserting elements shifts line numbers, so a second issue in the
        same snapshot may target the wrong node,
      • only 20 issues are handled per batch.
    Re-validating between rounds gives each subsequent round a FRESH, correct
    issue list (right lines/paths, newly-revealed errors), which is what makes
    "fix everything at once" actually converge.

    Returns (fixed_xml, rounds_used, total_fixes_applied).
    """
    cur = original_xml
    rounds_used = 0
    applied_total = 0
    seen_signatures = {hash(cur)}
    best_xml = cur
    best_errors = None  # fewest ERROR count seen so far
    best_wf = None      # well-formedness of the best state (None until 1st round)

    from lxml import etree as _etree

    def _is_wellformed(x: str) -> bool:
        try:
            _etree.fromstring(x.encode("utf-8"))
            return True
        except Exception:
            return False

    for _ in range(max_rounds):
        report = await validator.validate(
            cur, mode="Full 1-3", message_type=message_type or "Auto-detect"
        )
        details = report.to_dict().get("details", []) or []
        errors = [d for d in details if d.get("severity") in ("ERROR", "CRITICAL")]

        # A few codelist-validity rules are WARNINGs (their external code lists may
        # be incomplete, so we don't hard-fail), but a value that's definitively
        # NOT in the list is still safely auto-correctable to a valid code. Include
        # ONLY those in the fix pass — never broad advisory warnings like
        # BIC_NOT_FOUND (the value may be correct, just unverifiable) which must
        # not be silently rewritten.
        fixable = errors + [
            d for d in details
            if d.get("severity") == "WARNING" and d.get("code") in _AUTOFIX_WARNING_CODES
        ]

        # Track the best state so a fix that accidentally makes things worse can
        # never make the returned document worse than what we'd already achieved —
        # we return the best, not necessarily the last, state.
        #
        # Rule 1 — well-formedness wins over everything. While the XML is NOT
        # well-formed, Layers 2-3 are SKIPPED, so a malformed doc reports
        # artificially FEW errors (often just the single syntax error).
        # Recovering it to well-formed lets those layers run and reveal the real
        # schema/business errors — a HIGHER count that is nonetheless real
        # progress. Comparing error counts across that boundary is meaningless, so
        # a well-formed state ALWAYS beats a malformed one regardless of count.
        # Without this, the count-based logic reverted an "unclosed tag" fix back
        # to the malformed original (the syntax error appeared "unfixable").
        #
        # Rule 2 — within the same well-formedness, fewer errors is better. Use
        # "<=" (not "<") so that when the count TIES we still advance to the
        # more-recently-fixed state: XSD surfaces only ONE sequence error per
        # container, so re-inserting a deleted mandatory element (e.g.
        # <EndToEndId>) clears the ordering error but REVEALS a previously-hidden,
        # possibly-unfixable error, keeping the count constant. A fix that
        # INCREASES the count within the same well-formedness is still rejected.
        cur_wf = _is_wellformed(cur)
        if (best_errors is None
                or (cur_wf and not best_wf)
                or (cur_wf == best_wf and len(errors) <= best_errors)):
            best_errors = len(errors)
            best_wf = cur_wf
            best_xml = cur

        if not fixable:
            break  # clean — no errors and no auto-fixable warnings remain

        rounds_used += 1
        # Fix up to 20 per round; the next round re-validates and continues,
        # so >20 issues are handled across rounds rather than dropped.
        try:
            suggestions = fix_suggester.suggest_batch(cur, fixable[:20])
        except Exception as e:
            print(f"[auto-fix] suggest_batch failed: {e}")
            break

        fixes_list = [
            {"xpath": s.xpath, "fragment_xml": s.fragment_xml}
            for s in suggestions
            if s.confidence in ("high", "low")
            and s.fragment_xml and s.fragment_xml != s.original_fragment
        ]
        if not fixes_list:
            break  # nothing actionable remains (only declines / unavailable)

        new_xml = fix_suggester.apply_batch(cur, fixes_list)
        sig = hash(new_xml)
        if new_xml == cur or sig in seen_signatures:
            break  # no progress / oscillation guard
        seen_signatures.add(sig)
        applied_total += len(fixes_list)
        cur = new_xml

    # Return the best (fewest-error) state we validated, never a worse one.
    return best_xml, rounds_used, applied_total


async def _apply_auto_fixes(report_dict, original_xml, message_type="Auto-detect"):
    if report_dict.get("errors", 0) > 0 and report_dict.get("details"):
        try:
            # This runs inline on EVERY /validate to produce the preview banner's
            # fixed_xml. Cap rounds low for speed — the preview only needs the bulk
            # of fixes; the explicit "Fix All" endpoint (/fixes/auto-fix) still
            # runs the full 12 rounds when the user commits.
            fixed_xml, rounds, applied = await _auto_fix_iterative(
                original_xml, message_type, max_rounds=4
            )
            report_dict["fixed_xml"] = fixed_xml
            report_dict["auto_fix_rounds"] = rounds
            report_dict["auto_fix_applied"] = applied
        except Exception as e:
            report_dict["fixed_xml"] = None
            print(f"Auto-fix failed: {e}")


@app.post("/fixes/auto-fix", response_model=fix_schemas.AutoFixResponse)
async def fixes_auto_fix(request: fix_schemas.AutoFixRequest):
    """
    Fix EVERYTHING in one go: iteratively validate → fix → re-validate until the
    message is clean (or no further actionable fix can be made). Returns the
    fully-fixed XML plus any residual issues that need manual attention.
    """
    fixed_xml, rounds, applied = await _auto_fix_iterative(
        request.xml, request.message_type
    )
    final = await validator.validate(
        fixed_xml, mode="Full 1-3", message_type=request.message_type or "Auto-detect"
    )
    fd = final.to_dict()
    residual = [d for d in (fd.get("details", []) or [])
                if d.get("severity") in ("ERROR", "CRITICAL")]
    return fix_schemas.AutoFixResponse(
        new_xml=fixed_xml,
        rounds=rounds,
        fixes_applied=applied,
        remaining_errors=len(residual),
        remaining_details=residual,
    )


@app.post("/validate", response_model=schemas.ValidationResponse)
async def validate_message(
    request: schemas.ValidationRequest
):
    report = await validator.validate(request.xml_content, request.mode, request.message_type, validation_id=request.batch_id)
    report_dict = report.to_dict()

    # Attach file_id and batch_id to the report dict for frontend display
    if request.file_id:
        report_dict["file_id"] = request.file_id
    if request.batch_id:
        report_dict["batch_id"] = request.batch_id
    
    if request.store_in_history:
        record = {
            "validation_id": report_dict["validation_id"],
            "batch_id": request.batch_id or report_dict["validation_id"],
            "file_id": request.file_id or "",
            "timestamp": report_dict["timestamp"], # Already in report
            "message_type": report_dict["message"],
            "status": report_dict["status"],
            "total_errors": report_dict["errors"],
            "total_warnings": report_dict["warnings"],
            "execution_time_ms": report_dict["total_time_ms"],
            "report_json": report_dict,
            "original_message": request.xml_content,
            "origin": request.origin or "Pasted"
        }
        history_service.save_history(record)
    
    return report_dict

@app.post("/validate-file", response_model=schemas.ValidationResponse)
async def validate_file(
    file: UploadFile = File(...),
    mode: str = Form("Full 1-3"),
    message_type: str = Form("Auto-detect"),
    store_in_history: bool = Form(True),
    batch_id: Optional[str] = Form(None),
    file_id: Optional[str] = Form(None),
    origin: Optional[str] = Form("Uploaded")
):
    content = await file.read()
    xml_content = content.decode("utf-8")
    
    report = await validator.validate(xml_content, mode, message_type, filename=file.filename, validation_id=batch_id)
    report_dict = report.to_dict()

    # Attach file_id and batch_id to the report dict
    if file_id:
        report_dict["file_id"] = file_id
    if batch_id:
        report_dict["batch_id"] = batch_id
    
    if store_in_history:
        record = {
            "validation_id": report_dict["validation_id"],
            "batch_id": batch_id or report_dict["validation_id"],
            "file_id": file_id or "",
            "timestamp": report_dict["timestamp"],
            "message_type": report_dict["message"],
            "status": report_dict["status"],
            "total_errors": report_dict["errors"],
            "total_warnings": report_dict["warnings"],
            "execution_time_ms": report_dict["total_time_ms"],
            "report_json": report_dict,
            "original_message": xml_content,
            "origin": origin or "Uploaded"
        }
        history_service.save_history(record)
    
    return report_dict

@app.post("/convert-mt-to-mx")
async def convert_mt_to_mx(request: schemas.MTConversionRequest):
    try:
        result = mt_mx_converter.validate_and_convert(request.mt_message, forced_mt_type=request.target_mt_type)
        
        # Always include a validation report if conversion didn't fail at the pre-parsing/MT level
        mx_message = result.get("mx_message", "")
        if mx_message:
            validation_report = await validator.validate(mx_message, mode="Full 1-3", message_type="Auto-detect", filename="conversion.xml")
            report_dict = validation_report.to_dict()
            result["validation_report"] = report_dict
            
            # Save conversion validation to history if requested
            if request.store_in_history:
                record = {
                    "validation_id": report_dict["validation_id"],
                    "batch_id": report_dict["validation_id"],
                    "file_id": "",
                    "timestamp": report_dict["timestamp"],
                    "message_type": report_dict["message"],
                    "status": report_dict["status"],
                    "total_errors": report_dict["errors"],
                    "total_warnings": report_dict["warnings"],
                    "execution_time_ms": report_dict["total_time_ms"],
                    "report_json": report_dict,
                    "original_message": mx_message,
                    "origin": "MT to MX"
                }
                history_service.save_history(record)
            
            # If the generated MX fails schema (L2) or mandatory L3 rules, mark as error
            # but keep the successful conversion parts so the user can see the output
            if report_dict.get("status") != "PASS":
                l2_errors = [iss for iss in report_dict.get("issues", []) if iss.get("layer") == 2]
                if l2_errors:
                    result["status"] = "error"
                    # Accumulate error messages for display
                    current_errors = result.get("errors", [])
                    for iss in l2_errors:
                        current_errors.append(f"Layer 2 (Schema Compliance): {iss['message']}")
                    result["errors"] = current_errors
        
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result)

        return result
    except Exception as e:
        raise

@app.get("/history", response_model=List[schemas.HistorySummary])
def get_history(skip: int = 0, limit: int = 5000):
    return history_service.get_history(skip, limit)

@app.get("/dashboard/stats", response_model=schemas.DashboardStats)
def get_dashboard_stats():
    """Get aggregated dashboard statistics from Firestore"""
    return history_service.get_stats()

@app.get("/history/export")
def export_history():
    try:
        results = history_service.get_history(limit=50000) # Max export
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header
        writer.writerow(["Timestamp", "Validation ID", "Message Type", "Status", "Errors", "Warnings", "Duration (ms)"])
        
        for row in results:
            ts = row.get("timestamp")
            # Handle Firestore Timestamp or datetime
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts)
            
            writer.writerow([
                f"{ts_str} (UTC)",
                row.get("validation_id"),
                row.get("message_type"),
                row.get("status"),
                row.get("total_errors"),
                row.get("total_warnings"),
                row.get("execution_time_ms")
            ])
        
        csv_content = output.getvalue()
        output.close()
        
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=iso20022_audit_trail.csv",
                "Access-Control-Expose-Headers": "Content-Disposition"
            }
        )
    except Exception as e:
        print(f"EXPORT ERROR: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")

@app.get("/history/{validation_id}")
def get_history_detail(validation_id: str):
    detail = history_service.get_detail(validation_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Validation not found")
    return {
        "report": detail.get("report_json"),
        "original_message": detail.get("original_message")
    }

@app.delete("/history")
def delete_all_history():
    num_deleted = history_service.delete_all()
    return {"message": f"Soft deleted {num_deleted} records. Validation counter continues."}

@app.delete("/history/{validation_id}")
def delete_history_record(validation_id: str):
    success = history_service.delete_record(validation_id)
    if not success:
        raise HTTPException(status_code=404, detail="Validation not found or delete failed")
    return {"message": "Record deleted successfully"}

@app.get("/generate-id")
def generate_id():
    """Generate the next sequential validation ID for batch use"""
    return {"id": validator.generate_next_id()}

@app.post("/validate-batch")
async def validate_batch_init(
    request: schemas.BatchInitRequest
):
    """
    Initialize a validation batch:
    - Generates a single VAL{DDMMYY}{NNNNN} batch ID
    - Generates FILE{NNNN} IDs for each file in the batch
    Returns the batch_id and file_ids list.
    """
    batch_id = validator.generate_next_id()
    file_ids = [f"FILE{str(i + 1).zfill(4)}" for i in range(request.file_count)]
    return {
        "batch_id": batch_id,
        "file_ids": file_ids
    }

@app.get("/messages", response_model=List[str])
def get_messages():
    return validator.get_supported_messages()

@app.get("/messages/{message_type}/schema")
def get_message_schema(message_type: str):
    """Dynamically extract the schema tree for a specific MX message type"""
    xsd_path = validator._get_xsd_path(message_type)
    if not xsd_path or not os.path.exists(xsd_path):
        raise HTTPException(status_code=404, detail=f"Schema not found for {message_type}")
    
    schema_tree = SchemaGenerator.get_schema_tree(xsd_path)
    if not schema_tree:
        raise HTTPException(status_code=500, detail=f"Failed to generate schema tree for {message_type}")
    
    return schema_tree

@app.get("/bulk-generate/blocks/{message_type:path}")
def get_bulk_blocks(message_type: str):
    """Return the block definitions (checkboxes) for a given message type."""
    blocks = get_blocks_for_message(message_type)
    if not blocks:
        raise HTTPException(status_code=404, detail=f"No block config found for {message_type}")
    return {"message_type": message_type, "blocks": blocks}


@app.post("/bulk-generate")
async def bulk_generate(request: dict):
    """
    Generate exactly N VALID ISO 20022 messages of the given type with selected optional blocks.

    Uses an unbounded retry loop: generates, validates via full pipeline, keeps only PASS
    results, and regenerates for every failure.  The loop does NOT stop until exactly `count`
    valid messages are produced.

    The requested count ALWAYS means "exactly this many VALID messages", never
    "this many attempts" or "best effort".

    Validation pipeline per message (ALL must pass):
      1) Field population & XML build
      2) XSD schema validation  (Layer 2 — mandatory gate)
      3) Business rules         (Layer 3)
      4) CBPR+ / Network rules

    Catastrophic safety valve: if total attempts exceed count * 50, the endpoint raises
    an explicit HTTP 500 error — it never returns partial results silently.
    """
    import traceback as tb

    message_type = request.get("message_type")
    count = int(request.get("count", 1))
    selected_blocks = request.get("selected_blocks", [])

    if count < 1 or (count > 500 and not os.environ.get("UNLIMITED_BULK")):
        raise HTTPException(status_code=400, detail="count must be between 1 and 500")
    if not message_type:
        raise HTTPException(status_code=400, detail="message_type is required")

    print(f"\n{'='*70}")
    print(f"[Bulk Gen] START — Type: {message_type}, Requested: {count}, Blocks: {selected_blocks}")
    print(f"{'='*70}")

    valid_messages: list = []
    attempts = 0
    # Catastrophic safety valve — prevents truly infinite loops due to systemic bugs.
    # Generators are being migrated to constructive (valid-by-construction) per the
    # bulk-generate revamp, so the per-message attempt ratio should converge to ~1.
    # Lowered from 10× to 3× — anything more than that indicates the generator for
    # this message type still needs migration; raising 500 quickly is the right call.
    catastrophic_limit = max(count * 3, 30)

    # Track failure reasons for debugging
    failure_reasons: dict = {}   # reason_string -> count
    last_failure_details: list = []  # last N failure details for response
    MAX_FAILURE_LOG = 10  # keep last N failure details in response
    consecutive_failures = 0

    # Batch size — how many gen+validate operations to run concurrently. Bulk-gen used
    # to be a strict serial loop (one attempt at a time), which is the main reason it
    # felt slow. asyncio.gather lets us issue many attempts concurrently against the
    # validator's event-loop-friendly internals.
    BATCH_SIZE = 16

    async def _one_attempt(attempt_idx: int):
        """Generate one XML candidate and run the full validation pipeline.

        Returns a dict with keys: ok (bool), xml (str), report (ValidationReport |
        None), error (str | None), reason (str), error_codes (list[str]).
        """
        try:
            xml = generate_single_xml(message_type, selected_blocks, attempt_idx)
            report = await validator.validate(xml, mode="Full 1-3", message_type="Auto-detect")
            if report.status == "PASS":
                return {"ok": True, "xml": xml, "report": report,
                        "error": None, "reason": "", "error_codes": []}
            issues = report.to_dict().get("details", [])
            error_codes = [f"{iss.get('code', 'UNKNOWN')}: {iss.get('message', '')[:80]}"
                           for iss in issues if iss.get("severity") in ("ERROR", "CRITICAL")]
            reason = "; ".join(error_codes[:3]) if error_codes else "Unknown validation failure"
            return {"ok": False, "xml": xml, "report": report,
                    "error": None, "reason": reason, "error_codes": error_codes}
        except Exception as exc:
            err = f"Generation exception: {exc}"
            return {"ok": False, "xml": "", "report": None,
                    "error": err, "reason": err, "error_codes": []}

    # ── EXACT-COUNT LOOP: keep going until we have exactly `count` valid messages ──
    while len(valid_messages) < count:
        # ── Catastrophic safety valve ──
        if attempts >= catastrophic_limit:
            summary = "; ".join(f"({v}x) {k[:100]}" for k, v in sorted(failure_reasons.items(), key=lambda x: -x[1])[:3])
            error_detail = (
                f"CRITICAL: Could not generate {count} valid {message_type} messages "
                f"after {attempts} attempts (produced {len(valid_messages)}). "
                f"This indicates a systemic generation or validation issue. "
                f"Top failure reasons: {summary}"
            )
            print(f"[Bulk Gen] 🚨 CATASTROPHIC LIMIT HIT: {error_detail}")
            raise HTTPException(status_code=500, detail=error_detail)

        # How many more do we need? Run a concurrent batch sized to either the gap
        # or the configured batch size, whichever is smaller — never burn budget
        # for messages we no longer need.
        remaining = count - len(valid_messages)
        batch_n = min(BATCH_SIZE, remaining)
        # Don't issue more attempts than the catastrophic budget allows.
        batch_n = min(batch_n, catastrophic_limit - attempts)
        if batch_n <= 0:
            break

        batch_start = attempts + 1
        coros = [_one_attempt(batch_start + i) for i in range(batch_n)]
        results = await asyncio.gather(*coros)
        attempts += batch_n

        for r in results:
            if r["ok"] and len(valid_messages) < count:
                valid_messages.append({
                    "index": len(valid_messages) + 1,
                    "xml": r["xml"],
                    "message_type": message_type,
                    "status": "VALID",
                    "validation_report": r["report"].to_dict()
                })
                consecutive_failures = 0
                continue

            # Failed (validation or exception)
            reason = r["reason"]
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
            consecutive_failures += 1

            if r["error"]:
                print(f"[Bulk Gen] 💥 ERROR — {r['error']}")
            else:
                print(f"[Bulk Gen] ❌ INVALID — {reason[:120]}")

            if len(last_failure_details) >= MAX_FAILURE_LOG:
                last_failure_details.pop(0)
            if r["report"]:
                last_failure_details.append({
                    "attempt": attempts,
                    "status": r["report"].status,
                    "error_count": r["report"].errors,
                    "reasons": r["error_codes"][:3]
                })

            if consecutive_failures % 50 == 0:
                print(f"[Bulk Gen] [!] {consecutive_failures} consecutive failures - still retrying for {count - len(valid_messages)} more valid messages")

    # -- Summary Log ----------------------------------------------------------
    print(f"\n{'-'*70}")
    print(f"[Bulk Gen] COMPLETE - Valid: {len(valid_messages)}/{count}, Total attempts: {attempts}")
    if failure_reasons:
        print(f"[Bulk Gen] Failure breakdown:")
        for reason, cnt in sorted(failure_reasons.items(), key=lambda x: -x[1]):
            print(f"           ({cnt}x) {reason[:150]}")
    print(f"{'-'*70}\n")

    # ── Build response ───────────────────────────────────────────────────────
    # At this point len(valid_messages) == count ALWAYS (or we raised HTTP 500)
    response = {
        "message_type": message_type,
        "requested": count,
        "count": len(valid_messages),
        "total_attempts": attempts,
        "messages": valid_messages,
    }

    return response


@app.post("/bulk-generate/stream")
async def bulk_generate_stream(request: dict):
    """
    SSE variant of /bulk-generate. Streams per-attempt progress events so the
    UI can show "Generated X/Y, retrying Z failures..." in real time instead
    of one big blocking POST.

    Event types (each is one Server-Sent Event with `event: <type>` and
    `data: <json>`):
      - start    : { requested, message_type }
      - progress : { produced, attempts, failure_top: [{reason, count}, ...] }
      - message  : { index, xml, validation_report }   (only for VALID messages)
      - done     : { count, total_attempts, failure_summary: {...} }
      - error    : { detail }                          (terminal — stream closes)
    """
    message_type = request.get("message_type")
    count = int(request.get("count", 1))
    selected_blocks = request.get("selected_blocks", [])

    if count < 1 or (count > 500 and not os.environ.get("UNLIMITED_BULK")):
        raise HTTPException(status_code=400, detail="count must be between 1 and 500")
    if not message_type:
        raise HTTPException(status_code=400, detail="message_type is required")

    def _sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"

    async def generate_events():
        # Start
        yield _sse("start", {"requested": count, "message_type": message_type})

        produced = 0
        attempts = 0
        catastrophic_limit = max(count * 3, 30)
        failure_reasons: dict[str, int] = {}
        BATCH_SIZE = 16
        last_progress_emit = -1  # emit progress at most after every change

        async def _one_attempt(i: int):
            try:
                xml = generate_single_xml(message_type, selected_blocks, i)
                report = await validator.validate(xml, mode="Full 1-3", message_type="Auto-detect")
                if report.status == "PASS":
                    return {"ok": True, "xml": xml, "report": report, "reason": ""}
                issues = report.to_dict().get("details", [])
                error_codes = [f"{iss.get('code', 'UNKNOWN')}: {iss.get('message', '')[:80]}"
                               for iss in issues if iss.get("severity") in ("ERROR", "CRITICAL")]
                return {"ok": False, "xml": xml, "report": report,
                        "reason": "; ".join(error_codes[:3]) if error_codes else "Unknown validation failure"}
            except Exception as exc:
                return {"ok": False, "xml": "", "report": None,
                        "reason": f"Generation exception: {exc}"}

        while produced < count:
            if attempts >= catastrophic_limit:
                top3 = sorted(failure_reasons.items(), key=lambda x: -x[1])[:3]
                summary = "; ".join(f"({v}x) {k[:120]}" for k, v in top3)
                yield _sse("error", {
                    "detail": (
                        f"Could not generate {count} valid {message_type} messages "
                        f"after {attempts} attempts (produced {produced}). "
                        f"Top failure reasons: {summary}"
                    ),
                    "produced": produced,
                    "attempts": attempts,
                    "failure_top": [{"reason": r, "count": c} for r, c in top3],
                })
                return

            remaining = count - produced
            batch_n = max(1, min(BATCH_SIZE, remaining, catastrophic_limit - attempts))
            batch_start = attempts + 1
            results = await asyncio.gather(*[_one_attempt(batch_start + i) for i in range(batch_n)])
            attempts += batch_n

            for r in results:
                if r["ok"] and produced < count:
                    produced += 1
                    yield _sse("message", {
                        "index": produced,
                        "xml": r["xml"],
                        "message_type": message_type,
                        "status": "VALID",
                        "validation_report": r["report"].to_dict(),
                    })
                else:
                    failure_reasons[r["reason"]] = failure_reasons.get(r["reason"], 0) + 1

            # Progress event (one per batch is enough)
            if produced != last_progress_emit:
                top3 = sorted(failure_reasons.items(), key=lambda x: -x[1])[:3]
                yield _sse("progress", {
                    "produced": produced,
                    "attempts": attempts,
                    "failure_top": [{"reason": r, "count": c} for r, c in top3],
                })
                last_progress_emit = produced

        # Done
        full_top = sorted(failure_reasons.items(), key=lambda x: -x[1])
        yield _sse("done", {
            "count": produced,
            "total_attempts": attempts,
            "failure_summary": {r: c for r, c in full_top},
        })

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # tell nginx not to buffer
            "Connection": "keep-alive",
        },
    )


@app.get("/codelists/{list_name}")
def get_codelist(list_name: str):
    """Serve JSON codelists (like country.json, currency.json) to the frontend"""
    codelist_dir = os.path.join(os.path.dirname(__file__), "resources", "codelists")
    file_path = os.path.join(codelist_dir, f"{list_name}.json")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Codelist not found")
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

@app.get("/bics/search")
def search_bics(query: str = "", limit: int = 20):
    """Search for BIC codes and bank information"""
    return validator.search_bics(query, limit)


@app.post("/bics/refresh")
def trigger_bic_refresh():
    """
    Manually trigger a BIC dataset refresh (BR-9).

    The refresh runs in a background thread so this endpoint returns
    immediately.  Poll ``GET /bics/refresh/status`` to track progress.

    Returns 409 if a refresh is already running.
    """
    if bic_refresh_service.is_running:
        raise HTTPException(
            status_code=409,
            detail="A BIC dataset refresh is already in progress. "
                   "Check GET /bics/refresh/status for updates.",
        )
    import threading
    threading.Thread(
        target=lambda: bic_refresh_service.refresh(trigger="manual"),
        daemon=True,
        name="bic-manual-refresh",
    ).start()
    return {
        "status": "refresh_started",
        "message": (
            "BIC dataset refresh has been triggered. "
            "Check GET /bics/refresh/status for the outcome."
        ),
    }


@app.get("/bics/refresh/status")
def get_bic_refresh_status(limit: int = 10):
    """
    Return the most recent BIC dataset refresh log entries (BR-6, BR-9).

    Args:
        limit: Number of log entries to return (default 10, max 100).

    Each entry contains:
        - timestamp       ISO-8601 datetime of the attempt
        - trigger         ``"scheduled"`` or ``"manual"``
        - status          ``"SUCCESS"``, ``"FAILED"``, or ``"SKIPPED"``
        - total_records   Number of JSONL records in the downloaded file
        - records_added   BICs present in the new dataset but not the old
        - records_removed BICs present in the old dataset but not the new
        - error_message   Error detail when status is ``"FAILED"``
        - dataset_url     Source URL used for the download
    """
    limit = min(max(1, limit), 100)
    logs = bic_refresh_service.get_last_status(limit=limit)
    return {
        "refresh_in_progress": bic_refresh_service.is_running,
        "logs": logs,
    }


@app.post("/bics/rollback/{version_hash}")
def rollback_bic_dataset(version_hash: str):
    """
    Instantly roll back the active BIC dataset to a previous version hash (BR-ROLLBACK).
    
    This updates the 'entities.ftm.json' symlink and reloads the validator cache.
    """
    try:
        result = bic_refresh_service.rollback(version_hash)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

# --- GLOBALLY READY: Serve Frontend ---
# This allows the backend to serve the frontend UI in a production environment
frontend_path = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(frontend_path):
    app.mount("/ui", StaticFiles(directory=frontend_path, html=True), name="ui")
    
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        # If it looks like an API call or file with extension, don't interfere
        if full_path.startswith("api") or "." in full_path:
            return None # standard fastapi behavior
        
        # Otherwise, serve index.html for SPA routing
        index_file = os.path.join(frontend_path, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return {"status": "ok", "info": "Frontend build folder found but index.html missing."}

@app.get("/firebase-status")
def firebase_status():
    import os, base64 as _b64, json as _json
    from app.services.external.firebase_service import FirebaseHistoryService

    pk = os.getenv("FIREBASE_PRIVATE_KEY", "")
    raw_b64 = os.getenv("FIREBASE_CREDENTIALS_BASE64", "")
    cleaned_b64 = FirebaseHistoryService._clean_b64(raw_b64)

    # Decode-time diagnostics for the b64 var (the actual failure point)
    b64_decode_ok = False
    b64_decode_error = None
    decoded_project_id = None
    decoded_client_email = None
    decoded_has_private_key = False
    decoded_keys = None
    pk_starts_with = None
    pk_ends_with = None
    pk_inner_length = 0
    pk_has_real_newlines = False
    pk_has_escaped_newlines = False
    cert_build_ok = False
    cert_build_error = None
    if cleaned_b64:
        try:
            decoded_bytes = _b64.b64decode(cleaned_b64, validate=False)
            try:
                decoded_str = decoded_bytes.decode("utf-8")
            except UnicodeDecodeError as ue:
                raise ValueError(f"decoded bytes are not UTF-8: {ue}")
            try:
                d = _json.loads(decoded_str)
            except Exception as je:
                raise ValueError(f"decoded text is not valid JSON: {je}")
            if not isinstance(d, dict):
                raise ValueError("decoded JSON is not an object")
            decoded_project_id = d.get("project_id") or None
            decoded_client_email = d.get("client_email") or None
            decoded_has_private_key = bool(d.get("private_key"))
            decoded_keys = sorted(list(d.keys()))
            b64_decode_ok = True

            # Inspect private_key shape
            pk_val = d.get("private_key") or ""
            if pk_val:
                pk_inner_length = len(pk_val)
                pk_starts_with = pk_val[:30]
                pk_ends_with = pk_val[-30:]
                pk_has_real_newlines = "\n" in pk_val
                pk_has_escaped_newlines = "\\n" in pk_val

            # Try to actually build a firebase Certificate to surface the real error
            try:
                from firebase_admin import credentials as _fc
                _fc.Certificate(d)
                cert_build_ok = True
            except Exception as ce:
                cert_build_error = f"{type(ce).__name__}: {ce}"
        except Exception as e:
            b64_decode_error = f"{type(e).__name__}: {e}"

    stripped = raw_b64.strip() if raw_b64 else ""
    return {
        "enabled": history_service.enabled,
        "circuit_broken_reason": getattr(history_service, "_circuit_broken_reason", None),

        # --- Single-var approach (recommended for Render) ---
        "b64_creds_set": bool(raw_b64),
        "b64_creds_raw_length": len(raw_b64),
        "b64_creds_cleaned_length": len(cleaned_b64),
        "b64_had_surrounding_quotes": bool(stripped) and (
            (stripped.startswith('"') and stripped.endswith('"')) or
            (stripped.startswith("'") and stripped.endswith("'"))
        ),
        "b64_had_internal_whitespace": any(c in stripped for c in (" ", "\n", "\r", "\t")) if stripped else False,
        "b64_decode_ok": b64_decode_ok,
        "b64_decode_error": b64_decode_error,
        "decoded_project_id": decoded_project_id,
        "decoded_client_email": decoded_client_email,
        "decoded_has_private_key": decoded_has_private_key,
        "decoded_keys": decoded_keys,
        "pk_inner_length": pk_inner_length,
        "pk_starts_with": pk_starts_with,
        "pk_ends_with": pk_ends_with,
        "pk_has_real_newlines": pk_has_real_newlines,
        "pk_has_escaped_newlines": pk_has_escaped_newlines,
        "cert_build_ok": cert_build_ok,
        "cert_build_error": cert_build_error,

        # --- Legacy per-var approach ---
        "project_id": os.getenv("FIREBASE_PROJECT_ID", "MISSING"),
        "client_email": os.getenv("FIREBASE_CLIENT_EMAIL", "MISSING"),
        "key_path": os.getenv("FIREBASE_KEY_PATH", "NOT SET"),
        "pk_length": len(pk),
        "pk_contains_escaped_newlines": "\\n" in pk,
        "pk_contains_real_newlines": "\n" in pk,
        "cors": os.getenv("CORS_ORIGINS", "MISSING")
    }

@app.get("/firebase-write-test")
def firebase_write_test():
    """
    Diagnostic: tries to write a test document to Firestore and deletes it.
    Hit this URL on Render to instantly confirm Firebase connectivity.
    """
    result = history_service.test_write()
    if not result.get("success"):
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=result)
    return result


@app.get("/")
def health_check():
    # If frontend exists, redirect to UI
    if os.path.exists(frontend_path):
        return FileResponse(os.path.join(frontend_path, "index.html"))
    return {"status": "ok", "service": "ISO 20022 Validator", "info": "Run frontend on port 4200 or build it into backend/app/static"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True)
