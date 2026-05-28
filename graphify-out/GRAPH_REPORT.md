# Graph Report - iso20022generatorbackend  (2026-05-28)

## Corpus Check
- 47 files · ~140,919 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 859 nodes · 1371 edges · 106 communities (77 shown, 29 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 44 edges (avg confidence: 0.68)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `9342f0c7`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 98|Community 98]]

## God Nodes (most connected - your core abstractions)
1. `ISOValidator` - 60 edges
2. `MT2MXConverter` - 38 edges
3. `generate_single_xml()` - 37 edges
4. `FixSuggester` - 37 edges
5. `xe()` - 25 edges
6. `get()` - 22 edges
7. `FirebaseHistoryService` - 19 edges
8. `rng_datetime()` - 17 edges
9. `rng_id()` - 17 edges
10. `apphdr_fi()` - 17 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `generate_single_xml()`  [INFERRED]
  scratch/test_pacs009_cov.py → app/services/bulk_generator.py
- `test_pacs009()` --calls--> `generate_single_xml()`  [INFERRED]
  scratch/debug_pacs009.py → app/services/bulk_generator.py
- `test_messages()` --calls--> `generate_single_xml()`  [INFERRED]
  scratch/debug_pain008.py → app/services/bulk_generator.py
- `main()` --calls--> `generate_single_xml()`  [INFERRED]
  scratch/test_bulk_fail.py → app/services/bulk_generator.py
- `test_msg()` --calls--> `generate_single_xml()`  [INFERRED]
  scratch/test_issues.py → app/services/bulk_generator.py

## Communities (106 total, 29 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (50): _codelist_codes(), _detect_msg_type(), _extract_xml_from_fix(), FixApplyError, FixSuggester, FixSuggestion, get(), _kb_field_constraint() (+42 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (39): ChatService, ISO 20022 RAG Chatbot - Chat Service Combines retrieval with an LLM to generate, Force rebuild the knowledge base., Add an uploaded document to the knowledge base., Retrieve relevant context chunks for a question., Format retrieved results into a context string for the LLM., Generate answer using OpenAI ChatGPT., Generate a structured answer without an LLM, using the retrieved chunks directly (+31 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (27): _cbpr_check_value(), CBPRJsonSchemaMixin, _extract_catalog(), _local_name(), CBPR+ JSON Schema Validator (Layer 4) ===================================== Runs, Strip namespace from an lxml tag like '{urn:...}MsgId' → 'MsgId'., Mixin added to ISOValidator. Provides one entry point: _run_cbpr_json_schema_che, Locate a JSON schema file matching the message type.          Examples of expect (+19 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (42): BaseModel, ask_question(), ChatRequest, ChatResponse, get_chatbot_stats(), get_suggestions(), ISO 20022 RAG Chatbot - FastAPI Routes Provides /chatbot/* endpoints for the cha, Ask a question about ISO 20022 / SWIFT messaging. (+34 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (14): BicRefreshService, BIC Dataset Refresh Service ============================ Maintains an up-to-da, Retrieve recent logs and include version history., Roll back to a specific previous version hash (BR-ROLLBACK)., Core refresh logic with Intelligent Change Detection and Volumetric Guardrails., Stream download with incremental SHA-256 calculation., Parse the JSONL file and verify it meets quality thresholds.          Args:, Atomic replacement logic with Windows file-lock handling. (+6 more)

### Community 5 - "Community 5"
Cohesion: 0.14
Nodes (8): _build_credentials(), FirebaseHistoryService, Issue a single tiny Firestore read to verify the credentials actually         w, Update the circuit-breaker state after a Firestore call., Update the circuit-breaker state after a Firestore call., Recursively converts Firestore-specific types to JSON-serializable Python types., Issue a single tiny Firestore read to verify the credentials actually         w, _sanitize_firestore_doc()

### Community 6 - "Community 6"
Cohesion: 0.1
Nodes (17): Layer3Mixin, Executes all rules assigned to a specific layer., Executes validation based on the Generic Field Library and Global Algorithms., Executes validation based on the Generic Field Library and Global Algorithms., Loads the new global algorithms and field library., Advanced Dynamic Rule Dispatcher., Advanced Dynamic Rule Dispatcher., Loads global rules + family rules + message-specific rules. (+9 more)

### Community 7 - "Community 7"
Cohesion: 0.08
Nodes (23): _iban_check_digits(), Bulk ISO 20022 Message Generator Generates N valid randomized ISO 20022 message, Returns a valid (currency, country_code) pair that will pass business rules., # NOTE: pacs.010 CdtInstr XSD sequence does NOT permit IntrmyAgt between, # NOTE: pacs.010 CdtInstr XSD sequence does NOT permit IntrmyAgt between, # NOTE: OrgnlTxRef intentionally omitted — strict CBPR+ profile validators, # NOTE: OrgnlTxRef intentionally omitted — strict CBPR+ profile validators, # NOTE: OrgnlTxRef intentionally omitted — strict CBPR+ profile validators (+15 more)

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (23): Agent, anchor(), _bban_for(), _iban_check_digits(), make_address_country(), make_bic(), make_company_name(), make_iban() (+15 more)

### Community 9 - "Community 9"
Cohesion: 0.19
Nodes (25): account_othr_xml(), account_xml(), agent_xml(), el(), _fi_party(), _gen_pacs003(), _gen_pacs008(), _gen_pacs009() (+17 more)

### Community 10 - "Community 10"
Cohesion: 0.09
Nodes (15): CBPRJsonSchemaMixin, Layer1Mixin, Layer2Mixin, Layer3Mixin, Pacs004Mixin, test_pacs009(), test_messages(), main() (+7 more)

### Community 11 - "Community 11"
Cohesion: 0.1
Nodes (14): Step 4.8 — IBAN / BBAN Account Identifier Validation          For every accoun, Step 4.8 — IBAN / BBAN Account Identifier Validation          For every accoun, Step 4.10 — SWIFT Character Set Validation         Checks <Ustrd> (unstructured, Step 4.10 — SWIFT Character Set Validation         Checks <Ustrd> (unstructured, Step 4.13 — Address CBPR+ Rules Validation         Validates all <PstlAdr> bloc, Step 4.13 — Address CBPR+ Rules Validation         Validates all <PstlAdr> bloc, Robust Message Type Detection - Prioritizes Payload over Header, Robust Message Type Detection - Prioritizes Payload over Header (+6 more)

### Community 12 - "Community 12"
Cohesion: 0.11
Nodes (18): main(), main(), generate_bulk_messages(), generate_single_xml(), get_blocks_for_message(), _normalize_cbpr_r9(), _normalize_charges_information(), _normalize_postal_addresses() (+10 more)

### Community 13 - "Community 13"
Cohesion: 0.13
Nodes (18): apphdr_fi(), _gen_camt052(), _gen_camt055(), _gen_camt057(), _gen_pacs002(), Generate a valid pacs.002.001.10 (FI-to-FI Payment Status Report) message., Generate a valid pacs.002.001.10 (FI-to-FI Payment Status Report) message., Generate a random ID with prefix. Total length capped at max_total (default 35 f (+10 more)

### Community 14 - "Community 14"
Cohesion: 0.19
Nodes (10): get_system_config(), is_business_day(), is_holiday(), next_business_day(), parse_time(), Dynamic lookup for holidays from configuration., to_zoned_datetime(), validateLayer3Timing() (+2 more)

### Community 15 - "Community 15"
Cohesion: 0.12
Nodes (8): Attempts to detect the MT type from Block 2 and subtypes from Block 3 (Tag 119)., Parses all blocks of an MT message into a dictionary of tags.         Includes, Walk every <PstlAdr>; if it contains <AdrLine>, strip detail structured siblings, Normalise generated ISODateTime values to the CBPR+ offset form., Parses SWIFT MT Tag 61 (Statement Line).         Format: 6!n[4!n]2a[1!c]15d1!a3, Validates that the MT message only contains SWIFT X Character Set or allowed blo, Validates the structure of MT Block 4, ensuring all lines are either tags or, Validates the data content based on the standard SWIFT fields types.

### Community 16 - "Community 16"
Cohesion: 0.14
Nodes (16): _gen_camt056(), _gen_pain002(), _gen_pain008(), Generate a CBPR+ compliant datetime string.     CBPR+ rules:       - No 'Z' UT, camt.056 — FI to FI Payment Cancellation Request — constructive variant., camt.056 — FI to FI Payment Cancellation Request — constructive variant., camt.056 — FI to FI Payment Cancellation Request — constructive variant., pain.002 — Customer Payment Status Report — constructive variant.      Anchore (+8 more)

### Community 17 - "Community 17"
Cohesion: 0.25
Nodes (5): MT2MXConverter, Final sweep to ensure Ntry nodes are compliant with mandatory reporting rules., Final sweep to ensure Ntry nodes are compliant with mandatory reporting rules., Final sweep to ensure Ntry nodes are compliant with mandatory reporting rules., Internal helper to navigate XML tree.          Robustly handles namespaces and

### Community 18 - "Community 18"
Cohesion: 0.15
Nodes (15): _gen_camt053(), _gen_camt054(), _gen_pacs004(), pacs.004 — Payment Return — constructive variant.      Anchored to one Message, Generate a random valid amount string., pacs.004 — Payment Return — constructive variant.      Anchored to one Message, Generate a date string. Default is today; use offset_days >= 1 for future dates., camt.053 — Bank to Customer Statement — constructive variant.      Same anchor (+7 more)

### Community 19 - "Community 19"
Cohesion: 0.14
Nodes (10): Extracts date, currency, or amount from composite SWIFT fields like 32A, 33B, et, Recursively applies mandatory field rules from swift_validation_rules.json (v2.0, Extracts date, currency, or amount from composite SWIFT fields like 32A, 33B, et, Recursively applies mandatory field rules from swift_validation_rules.json (v2.0, Extracts date, currency, or amount from composite SWIFT fields like 32A, 33B, et, Process children using mx_root for path navigation to ensure absolute paths work, Recursively applies mandatory field rules from swift_validation_rules.json (v2.0, Process children using mx_root for path navigation to ensure absolute paths work (+2 more)

### Community 21 - "Community 21"
Cohesion: 0.15
Nodes (8): Loads the dynamic configuration file, Loads BIC codes from the entities.ftm.json file (JSONL format), Loads BIC codes from the entities.ftm.json file (JSONL format), High-Performance Extraction Engine:         Automatically unzips all XSD bluepr, High-Performance Extraction Engine:         Automatically unzips all XSD bluepr, Loads all JSON codelists from the resource directory (Lowercased keys), Loads all JSON codelists from the resource directory (Lowercased keys), Loads the dynamic configuration file

### Community 22 - "Community 22"
Cohesion: 0.17
Nodes (9): Locates the XSD file for the given message type.         1. Exact Match (e.g. p, Locates the XSD file for the given message type.         1. Exact Match (e.g. p, Locates the XSD file for the given message type.         1. Exact Match (e.g. p, Helper to build a simple non-indexed XPath for an lxml element, Step 4.18 — Duplicate Tag Validation         Checks for tags that appear more t, Helper to build a simple non-indexed XPath for an lxml element, Helper to build a simple non-indexed XPath for an lxml element, Step 4.18 — Duplicate Tag Validation         Checks for tags that appear more t (+1 more)

### Community 23 - "Community 23"
Cohesion: 0.2
Nodes (6): Map MT 71F (sender's charges) and 71G (receiver's charges) into pacs.008 / pacs., Map MT 71F (sender's charges) and 71G (receiver's charges) into pacs.008 / pacs., CBPR+ rule PACS008_CHRGSINF_REQUIRED_CRED: if ChrgBr == CRED, at least one, Locate a node by slash-delimited local-name path (namespace-aware)., Locate a node by slash-delimited local-name path (namespace-aware)., Travels the path and returns the text if it exists.

### Community 24 - "Community 24"
Cohesion: 0.22
Nodes (6): BUG 1 + BUG 4 FIX:         MT202COV carries two sequences:           Sequence, BUG 1 + BUG 4 FIX:         MT202COV carries two sequences:           Sequence, BUG 1 + BUG 4 FIX:         MT202COV carries two sequences:           Sequence, Helper to navigate path and create nodes if missing., Navigates or creates the XML path and sets the text., Adds a new sibling element even if it already exists (useful for multiple Instru

### Community 25 - "Community 25"
Cohesion: 0.22
Nodes (7): Sorts the children of 'element' according to the order in 'sequence'., Recursively sorts children of an element based on a comprehensive list of ISO 20, Sorts the children of 'element' according to the order in 'sequence'., Recursively sorts children of an element based on a comprehensive list of ISO 20, Sorts the children of 'element' according to the order in 'sequence'., Recursively sorts children of an element based on a comprehensive list of ISO 20, Re-orders the children of an element based on a provided tag order list.

### Community 26 - "Community 26"
Cohesion: 0.46
Nodes (7): _camel_to_words(), _find_elements_in_container(), get_schema_tree(), _parse_complex_type(), _parse_element(), Convert ISO 20022 CamelCase names to readable English words., SchemaGenerator

### Community 27 - "Community 27"
Cohesion: 0.25
Nodes (6): Validates Legal Entity Identifier (LEI) using ISO 7064 MOD 97-10.         Retur, Validates Legal Entity Identifier (LEI) using ISO 7064 MOD 97-10.         Retur, Validates Legal Entity Identifier (LEI) using ISO 7064 MOD 97-10.         Retur, Step 4.19 — Scheme Name Validation (Strict Policy + Structural Rules + LEI Check, Step 4.19 — Scheme Name Validation (Strict Policy + Structural Rules + LEI Check, Step 4.19 — Scheme Name Validation (Strict Policy + Structural Rules + LEI Check

### Community 28 - "Community 28"
Cohesion: 0.4
Nodes (5): complete(), _get_client(), OpenAI ChatGPT client for XML fix generation. Using gpt-4o-mini: faster, cheaper, Lazy-init OpenAI client. Returns None if key not configured., Call ChatGPT with a system + user prompt.      max_tokens: max output tokens (de

### Community 29 - "Community 29"
Cohesion: 0.33
Nodes (4): Runs post-processing on the report:         1. Deduplicates issues (especially, Helper to adjust counters when removing a duplicate issue., Helper to adjust counters when removing a duplicate issue., Helper to adjust counters when removing a duplicate issue.

### Community 31 - "Community 31"
Cohesion: 0.4
Nodes (4): content, fs, i, lines

### Community 32 - "Community 32"
Cohesion: 0.4
Nodes (4): content, fs, i, lines

### Community 33 - "Community 33"
Cohesion: 0.4
Nodes (5): bulk_generate(), get_bulk_blocks(), Return the block definitions (checkboxes) for a given message type., Return the block definitions (checkboxes) for a given message type., Generate exactly N VALID ISO 20022 messages of the given type with selected opti

### Community 34 - "Community 34"
Cohesion: 0.5
Nodes (4): _gen_pain001(), pain.001 — Customer Credit Transfer Initiation — constructive variant.      An, pain.001 — Customer Credit Transfer Initiation — constructive variant.      An, pain.001 — Customer Credit Transfer Initiation — constructive variant.      An

### Community 35 - "Community 35"
Cohesion: 0.5
Nodes (3): Recursively removes elements that have no text and no children., Recursively removes elements that have no text and no children., Recursively removes elements that have no text and no children.

### Community 36 - "Community 36"
Cohesion: 0.5
Nodes (3): STEP 4.16 - Character Set Validation for Name and Address Tags          Checks, STEP 4.16 - Character Set Validation for Name and Address Tags          Checks, STEP 4.16 - Character Set Validation for Name and Address Tags          Checks

### Community 37 - "Community 37"
Cohesion: 0.5
Nodes (3): Step 4.15 — Clearing System Specific Rules         1. TARGET2 (T2) -> Settlemen, Step 4.15 — Clearing System Specific Rules         1. TARGET2 (T2) -> Settlemen, Step 4.15 — Clearing System Specific Rules         1. TARGET2 (T2) -> Settlemen

### Community 56 - "Community 56"
Cohesion: 0.67
Nodes (3): get_dashboard_stats(), Get aggregated dashboard statistics from Firestore, Get aggregated dashboard statistics from Firestore

### Community 57 - "Community 57"
Cohesion: 0.67
Nodes (3): Gracefully stop the APScheduler background scheduler on server shutdown., Gracefully stop the APScheduler background scheduler on server shutdown., shutdown_event()

### Community 58 - "Community 58"
Cohesion: 0.67
Nodes (3): bulk_generate_stream(), SSE variant of /bulk-generate. Streams per-attempt progress events so the     U, SSE variant of /bulk-generate. Streams per-attempt progress events so the     U

### Community 59 - "Community 59"
Cohesion: 0.67
Nodes (3): Search for BIC codes and bank information, Search for BIC codes and bank information, search_bics()

### Community 60 - "Community 60"
Cohesion: 0.67
Nodes (3): Instantly roll back the active BIC dataset to a previous version hash (BR-ROLLBA, Instantly roll back the active BIC dataset to a previous version hash (BR-ROLLBA, rollback_bic_dataset()

### Community 61 - "Community 61"
Cohesion: 0.67
Nodes (3): get_codelist(), Serve JSON codelists (like country.json, currency.json) to the frontend, Serve JSON codelists (like country.json, currency.json) to the frontend

### Community 62 - "Community 62"
Cohesion: 0.67
Nodes (3): Manually trigger a BIC dataset refresh (BR-9).      The refresh runs in a back, Manually trigger a BIC dataset refresh (BR-9).      The refresh runs in a back, trigger_bic_refresh()

### Community 63 - "Community 63"
Cohesion: 0.67
Nodes (3): generate_id(), Generate the next sequential validation ID for batch use, Generate the next sequential validation ID for batch use

### Community 64 - "Community 64"
Cohesion: 0.67
Nodes (3): firebase_write_test(), Diagnostic: tries to write a test document to Firestore and deletes it.     Hit, Diagnostic: tries to write a test document to Firestore and deletes it.     Hit

### Community 65 - "Community 65"
Cohesion: 0.67
Nodes (3): Initialize a validation batch:     - Generates a single VAL{DDMMYY}{NNNNN} batc, Initialize a validation batch:     - Generates a single VAL{DDMMYY}{NNNNN} batc, validate_batch_init()

### Community 66 - "Community 66"
Cohesion: 0.67
Nodes (3): get_message_schema(), Dynamically extract the schema tree for a specific MX message type, Dynamically extract the schema tree for a specific MX message type

### Community 67 - "Community 67"
Cohesion: 0.67
Nodes (3): get_bic_refresh_status(), Return the most recent BIC dataset refresh log entries (BR-6, BR-9).      Args, Return the most recent BIC dataset refresh log entries (BR-6, BR-9).      Args

## Knowledge Gaps
- **385 isolated node(s):** `Gracefully stop the APScheduler background scheduler on server shutdown.`, `Get aggregated dashboard statistics from Firestore`, `Generate the next sequential validation ID for batch use`, `Initialize a validation batch:     - Generates a single VAL{DDMMYY}{NNNNN} batc`, `Dynamically extract the schema tree for a specific MX message type` (+380 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **29 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ISOValidator` connect `Community 10` to `Community 2`, `Community 6`, `Community 11`, `Community 21`, `Community 22`, `Community 27`, `Community 29`, `Community 36`, `Community 37`, `Community 38`, `Community 39`, `Community 40`, `Community 41`, `Community 42`, `Community 43`, `Community 44`, `Community 45`, `Community 46`, `Community 47`, `Community 48`, `Community 49`, `Community 50`, `Community 51`, `Community 52`, `Community 53`, `Community 54`, `Community 55`?**
  _High betweenness centrality (0.167) - this node is a cross-community bridge._
- **Why does `generate_single_xml()` connect `Community 12` to `Community 34`, `Community 7`, `Community 9`, `Community 10`, `Community 13`, `Community 16`, `Community 49`, `Community 18`?**
  _High betweenness centrality (0.126) - this node is a cross-community bridge._
- **Why does `get_blocks_for_message()` connect `Community 12` to `Community 33`, `Community 7`?**
  _High betweenness centrality (0.054) - this node is a cross-community bridge._
- **Are the 16 inferred relationships involving `ISOValidator` (e.g. with `ValidationIssue` and `ValidationReport`) actually correct?**
  _`ISOValidator` has 16 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `generate_single_xml()` (e.g. with `main()` and `main()`) actually correct?**
  _`generate_single_xml()` has 9 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Gracefully stop the APScheduler background scheduler on server shutdown.`, `Get aggregated dashboard statistics from Firestore`, `Generate the next sequential validation ID for batch use` to the rest of the system?**
  _385 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.05 - nodes in this community are weakly interconnected._