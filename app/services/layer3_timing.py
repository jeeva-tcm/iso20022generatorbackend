import json
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

class ValidationIssue:
    def __init__(self, rule_id: str, severity: str, field: Optional[str], message: str, details: Dict):
        self.rule_id = rule_id
        self.severity = severity
        self.field = field
        self.message = message
        self.details = details

    def to_dict(self):
        res = {
            "ruleId": self.rule_id,
            "severity": self.severity,
            "message": self.message,
            "details": self.details
        }
        if self.field:
            res["field"] = self.field
        return res

class ValidationResult:
    def __init__(self):
        self.layer = 3
        self.status = "PASS"
        self.summary = []
        self.recommended_value_date = None
        self.issues = []

    def add_issue(self, issue: ValidationIssue):
        self.issues.append(issue)
        if issue.severity == "FAIL":
            self.status = "FAIL"
        elif issue.severity == "WARN" and self.status != "FAIL":
            self.status = "WARN"
            
    def set_recommended_value_date(self, dt: str):
        self.recommended_value_date = dt

    def to_dict(self):
        res = {
            "layer": self.layer,
            "status": self.status,
            "summary": " ".join(self.summary) if self.summary else "Timing validation passed.",
            "issues": [i.to_dict() for i in self.issues]
        }
        if self.recommended_value_date:
            res["recommendedValueDate"] = self.recommended_value_date
        return res

def is_holiday(country_code: str, dt: date, cutoff_config: Dict) -> bool:
    """Dynamic lookup for holidays from configuration."""
    holidays = cutoff_config.get("holidays", {}).get(country_code, [])
    if dt.isoformat() in holidays:
        return True
    return False

def get_system_config(country: str, system_key: str, cutoff_config: Dict) -> Optional[Dict]:
    countries_conf = cutoff_config.get("timings", {})
    if country not in countries_conf:
        return None
    systems_conf = countries_conf[country].get("paymentSystems", {})
    if system_key not in systems_conf:
        # Fallback to first system if many are available
        if systems_conf:
            system_key = list(systems_conf.keys())[0]
        else:
            return None
    sys_conf = systems_conf[system_key].copy()
    sys_conf["country"] = country
    return sys_conf

def parse_time(time_str: str) -> time:
    try:
        if ":" in time_str:
            return datetime.strptime(time_str[:5], "%H:%M").time()
        return time(int(time_str[:2]), int(time_str[2:]))
    except:
        return time(18, 0) # Safe default 6 PM

def to_zoned_datetime(dt_str: str, target_tz: str) -> datetime:
    if not dt_str:
        return None
    try:
        dt_str = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            # Use stdlib UTC (works on all platforms, no tzdata needed)
            from datetime import timezone as dt_tz
            dt = dt.replace(tzinfo=dt_tz.utc)
        # Use simple tz lookup with fallback
        try:
            tz = ZoneInfo(target_tz)
        except:
            try:
                tz = ZoneInfo("UTC")
            except:
                from datetime import timezone as dt_tz
                tz = dt_tz.utc
        return dt.astimezone(tz)
    except Exception as e:
        # Final fallback to now in UTC
        from datetime import timezone as dt_tz
        return datetime.now(dt_tz.utc)

def is_business_day(system_config: Dict, dt: date, country: str, cutoff_config: Dict) -> bool:
    day_abbr = dt.strftime("%a").upper()
    if day_abbr not in system_config.get("days", []):
        return False
    if is_holiday(country, dt, cutoff_config):
        return False
    return True

def next_business_day(system_config: Dict, start_date: date, country: str, cutoff_config: Dict) -> date:
    next_day = start_date + timedelta(days=1)
    while not is_business_day(system_config, next_day, country, cutoff_config):
        next_day += timedelta(days=1)
    return next_day

def validateLayer3Timing(payload: Dict, context: Dict, cutoffConfig: Dict) -> Dict:
    result = ValidationResult()

    debtor_country = context.get("debtorCountry", "US")
    debtor_system_key = context.get("debtorPaymentSystem", "FEDWIRE")
    creditor_country = context.get("creditorCountry", "GB")
    creditor_system_key = context.get("creditorPaymentSystem", "CHAPS")
    validation_mode = context.get("validationMode", "STRICT")
    sub_timestamp_str = context.get("submissionTimestamp")

    if not sub_timestamp_str:
        from datetime import timezone as dt_tz
        sub_timestamp_str = datetime.now(dt_tz.utc).isoformat()

    debtor_config = get_system_config(debtor_country, debtor_system_key, cutoffConfig)
    creditor_config = get_system_config(creditor_country, creditor_system_key, cutoffConfig)

    if not debtor_config or not creditor_config:
        result.add_issue(ValidationIssue("CONFIG_NOT_FOUND", "WARN", None, "Missing timing config for parties.", {}))
        return result.to_dict()

    try:
        sub_d_dt = to_zoned_datetime(sub_timestamp_str, debtor_config["timezone"])
        sub_c_dt = to_zoned_datetime(sub_timestamp_str, creditor_config["timezone"])
    except Exception as e:
        result.add_issue(ValidationIssue("TIME_PARSE", "FAIL", None, f"Invalid submission timestamp: {str(e)}", {"ts": sub_timestamp_str}))
        return result.to_dict()

    # CUT001 Logic: Use creation time from payload
    cre_dt_tm = payload.get("CreDtTm") or sub_timestamp_str
    cre_d_dt = to_zoned_datetime(cre_dt_tm, debtor_config["timezone"])
    cre_c_dt = to_zoned_datetime(cre_dt_tm, creditor_config["timezone"])
    
    d_cutoff_time = parse_time(debtor_config["cutoffTime"])
    c_cutoff_time = parse_time(creditor_config["cutoffTime"])
    
    # 1. Check if the message was created after cutoff
    if cre_d_dt.time() >= d_cutoff_time:
        stat = "WARN"
        result.add_issue(ValidationIssue("CUT001", stat, "CreDtTm", 
            f"Creation time {cre_d_dt.strftime('%H:%M')} is after system cutoff {d_cutoff_time.strftime('%H:%M')}.", {}))

    # 2. Recommended Value Date (Dynamic)
    # Determine the "Earliest Possible Execution Date"
    is_sub_bus_day = is_business_day(debtor_config, sub_d_dt.date(), debtor_country, cutoffConfig)
    is_sub_after_cutoff = sub_d_dt.time() >= d_cutoff_time
    
    rec_val_date = sub_d_dt.date()
    if not is_sub_bus_day or is_sub_after_cutoff:
        rec_val_date = next_business_day(debtor_config, sub_d_dt.date(), debtor_country, cutoffConfig)
    
    # Ensure creditor can also receive on that date
    while not is_business_day(creditor_config, rec_val_date, creditor_country, cutoffConfig):
        rec_val_date = next_business_day(creditor_config, rec_val_date, creditor_country, cutoffConfig)
        # Re-check debtor compatibility if moved
        if not is_business_day(debtor_config, rec_val_date, debtor_country, cutoffConfig):
             rec_val_date = next_business_day(debtor_config, rec_val_date, debtor_country, cutoffConfig)

    rec_str = rec_val_date.isoformat()
    result.set_recommended_value_date(rec_str)
    
    computed_details = {
        "recommendedValueDate": rec_str,
        "submissionTime": sub_d_dt.isoformat(),
        "isAfterCutoff": is_sub_after_cutoff,
    }

    # CUT002: Requested Execution Date check
    reqd_exctn_dt_str = payload.get("ReqdExctnDt")
    if reqd_exctn_dt_str:
        try:
            req_dt = date.fromisoformat(reqd_exctn_dt_str)
            if req_dt < rec_val_date:
                stat = "FAIL" if validation_mode == "STRICT" else "WARN"
                result.add_issue(ValidationIssue("CUT004", stat, "ReqdExctnDt", 
                    f"Required Execution Date {reqd_exctn_dt_str} is earlier than earliest possible: {rec_str}.", computed_details))
            
            if not is_business_day(debtor_config, req_dt, debtor_country, cutoffConfig):
                result.add_issue(ValidationIssue("CUT002", "WARN", "ReqdExctnDt", "Execution date is not a business day.", {}))
        except: pass

    # CUT003: Settlement Date check
    intr_bk_sttlm_dt_str = payload.get("IntrBkSttlmDt")
    if intr_bk_sttlm_dt_str:
        try:
            intr_dt = date.fromisoformat(intr_bk_sttlm_dt_str)
            if intr_dt < rec_val_date:
                result.add_issue(ValidationIssue("CUT004", "WARN", "IntrBkSttlmDt", "Settlement date is before possible processing date.", {}))
        except: pass

    if not result.issues:
        result.summary.append("All timing rules passed successfully.")
    else:
        fails = sum(1 for i in result.issues if i.severity == "FAIL")
        warns = sum(1 for i in result.issues if i.severity == "WARN")
        if fails: result.summary.append(f"Validation failed with {fails} error(s).")
        if warns: result.summary.append(f"Validation completed with {warns} warning(s).")
            
    return result.to_dict()

# =========================================================================
# Unit Tests
# =========================================================================
if __name__ == "__main__":
    cutoff_config = {
      "timings": {
        "US": {
          "paymentSystems": {
            "FEDWIRE": {
              "cutoffTime": "18:00",
              "timezone": "America/New_York",
              "days": ["MON", "TUE", "WED", "THU", "FRI"]
            }
          }
        },
        "GB": {
          "paymentSystems": {
            "CHAPS": {
              "cutoffTime": "16:00",
              "timezone": "Europe/London",
              "days": ["MON", "TUE", "WED", "THU", "FRI"]
            }
          }
        }
      }
    }

    # Format output helper
    def pp(res):
        print(json.dumps(res, indent=2))

    print("--- Test 1: Happy Path ---")
    payload = {
        "CreDtTm": "2023-10-10T10:00:00Z",
        "ReqdExctnDt": "2023-10-10",
        "IntrBkSttlmDt": "2023-10-10"
    } # 10 AM UTC = 6 AM NY = 11 AM London -> Tuesday
    context = {
        "debtorCountry": "US", "debtorPaymentSystem": "FEDWIRE",
        "creditorCountry": "GB", "creditorPaymentSystem": "CHAPS",
        "submissionTimestamp": "2023-10-10T10:00:00Z",
        "validationMode": "STRICT"
    }
    pp(validateLayer3Timing(payload, context, cutoff_config))

    print("\n--- Test 2: After debtor cutoff (STRICT) ---")
    # 2023-10-10T23:00:00Z -> 19:00 NY (After 18:00 cutoff)
    payload = {"CreDtTm": "2023-10-10T23:00:00Z", "ReqdExctnDt": "2023-10-10"}
    context["submissionTimestamp"] = "2023-10-10T23:00:00Z"
    pp(validateLayer3Timing(payload, context, cutoff_config))

    print("\n--- Test 3: After debtor cutoff (LENIENT) ---")
    context["validationMode"] = "LENIENT"
    pp(validateLayer3Timing(payload, context, cutoff_config))

    print("\n--- Test 4: Weekend / Non-business day ---")
    # 2023-10-14 is Saturday
    payload = {"CreDtTm": "2023-10-14T10:00:00Z"}
    context["submissionTimestamp"] = "2023-10-14T10:00:00Z"
    context["validationMode"] = "STRICT"
    pp(validateLayer3Timing(payload, context, cutoff_config))

    print("\n--- Test 5: Missing Config ---")
    context["debtorCountry"] = "XX"
    pp(validateLayer3Timing(payload, context, cutoff_config))

