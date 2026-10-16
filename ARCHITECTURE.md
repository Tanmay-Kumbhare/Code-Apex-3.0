# Allocation Rule Language (ARL) v2 — Architecture Specification

## 1. System Overview

Allocation Rule Language (ARL) v2 is a declarative, typed, extensible domain-specific language designed for industrial IO signal allocation in Emerson DeltaV DCS engineering.

The compiler and runtime execute an 11-phase pipeline:
```
parse → typecheck → resolve → normalize → dag_build → override_apply → lower → conflict_check → solve → validate → report
```

---

## 2. Schema and Entity Model Architecture

### 2.1 Principles
- **Schema is Data, Not Code:** Domain entities, fields, enums, and references are defined declaratively in data (`schema.yaml`). Adding, updating, or removing fields requires zero changes to the underlying compiler or solver engine.
- **Strict Typing:** All entity fields have explicit types governed by a Hindley-Milner-style type system.
- **Reference Integrity:** Inter-entity references (e.g., `SignalRef`, `CableRef`, `CiocRef`) resolve dynamically to target entities without requiring custom wrapper classes per reference type.
- **Dynamic Extensibility:** New fields, entities, and enums can be registered programmatically (`register_field`, `register_entity`, `register_enum`) or loaded from YAML, with comprehensive validation against existing types and defaults.

### 2.2 Entity Hierarchy
```
Project
  └── Cabinet
        └── CIOC
              └── Baseplate
                    └── CHARM
                          └── Signal

Cable
Group
Pair
```

- **Project:** Root container for all cabinets, cables, groups, and signals.
- **Cabinet:** Physical enclosure (`CabinetId`, `Area`, `Unit`, `seq`, `ciocs: CIOC[]`, capacity=4, `spare_target_percent=10.0`).
- **CIOC:** CHARM I/O Card (`CiocId`, `cabinet: CabinetRef`, `index: Int`, `baseplates: Baseplate[]`, capacity=8, `seg_role: SegRole`).
- **Baseplate:** Mounting carrier for CHARM terminal blocks (`BaseplateId`, `cioc: CiocRef`, `index: Int`, `charms: Charm[]`, capacity=12).
- **Charm:** Individual I/O channel (`slot: Int`, `signal: SignalRef?`, `charm_type: CharmType?`, `tb_type: TbType?`).
- **Signal:** Individual I/O instrument signal (`tag: Tag`, `io_type: IoType`, `cable: CableRef`, `pair: Int`, etc.).
- **Cable:** Multi-conductor or multi-pair cable (`id: CableId`, `size: CableSize`, `pairs: Pair[]`, `from: JBId?`).
- **Pair:** Cable pair (`cable: CableRef`, `index: Int`, `signal: SignalRef?`, `wired_spare: Bool`).
- **Group:** Logical grouping of signals and cables (`id: GroupId`, `signals: Signal[]`, `cables: Cable[]`).

### 2.3 Type System & References
The schema recognizes:
1. **Base Types:** `Int`, `Float`, `Bool`, `String`, `Null`, `Void`.
2. **Domain Identifiers:** `Tag`, `CableId`, `CiocId`, `BaseplateId`, `CabinetId`, `Slot`, `Area`, `Unit`, `Rev`, `JBId`, `GroupId`.
3. **Enum Types:** `IoType`, `CableSize`, `Seg`, `CharmType`, `TbType`, `SegRole`, `VoltageClass`, `RedundancyKind`.
4. **Entity Types:** `Signal`, `Cable`, `Pair`, `Group`, `Baseplate`, `CIOC`, `Cabinet`, `Project`.
5. **Reference Types:** `ReferenceType(target_entity)` (e.g. `SignalRef`, `CableRef`, `CiocRef`).
6. **Compound Types:**
   - Option types: `Option<T>` or `T?` (disallows nested `Option<Option<T>>`).
   - Collection types: List `T[]` and Set `Set<T>`.
   - Tuple types: `(T1, T2, ...)`.

### 2.4 Extensibility and Validation
The `Schema` engine validates:
- **Duplicate Registration:** Duplicate entities (`E2017`), enums, or fields on an entity are rejected.
- **Type Reference Integrity:** Every field type must resolve against known primitive, domain, enum, entity, or reference types.
- **Entity Reference Targets:** If a field uses an entity reference (`FooRef`), the target entity (`Foo`) must exist in the schema.
- **Default Value Compatibility:** Default literals are strictly checked against the field's declared type.
- **Semantic Versioning:** Version compatibility ensures backwards-compatible schema upgrades (`MAJOR.MINOR.PATCH`).

### 2.5 Error Codes for Schema & Data
- `E2001`: Unknown entity type
- `E2002`: Unknown field on entity
- `E2005`: Type mismatch
- `E2017`: Duplicate entity registration
- `E4001`: Missing required field on entity instance

---

## 3. Lexer, Parser, and AST Architecture

### 3.1 Lexical Analyzer (`arl/lexer.py`)
- **Tokens and Keywords:** Scans exact keywords, delimiters, operators (`==`, `!=`, `<=`, `>=`, `?.`, etc.), numbers, strings, and identifiers.
- **Source Tracking:** Tracks line, column, and file paths accurately across all tokens.
- **Lexical Errors:**
  - `E1002`: Unterminated string literal.
  - `E1006`: Invalid numeric literal (e.g. malformed floats, attached identifier characters).
  - `E1007`: Invalid escape sequences in strings.

### 3.2 Parser (`arl/parser.py`)
- **Deterministic Recursive-Descent:** Zero grammar ambiguity; implements exact EBNF productions for rulesets, includes, rules, objectives, overrides, conditions, and expressions.
- **Operator Precedence:**
  `not` > `and` > `or`; comparisons (`==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `matches`, `is`); arithmetic (`*`, `/`, `%` > `+`, `-`); primary/postfix (`.`, `?.`, `()`, `[]`).
- **Syntax Error Handling:**
  - `E1001`: Unexpected token.
  - `E1003`: Missing colon after rule name.
  - `E1004`: Duplicate explicit rule ID in ruleset.
  - `E1005`: Invalid block structure (missing/unclosed `{` or `}`).
  - `E2016`: Reserved keyword used as an identifier.
  - `E2017`: Duplicate rule name in same ruleset.
- **Rule-Level Quarantine:** When a syntax error occurs within a rule, the parser records the error, quarantines the broken rule, synchronizes to the next declaration boundary, and continues parsing the remainder of the ruleset.

### 3.3 Abstract Syntax Tree (`arl/ast.py`)
- Strongly typed Pydantic models.
- Retains `SourceLocation` (`file`, `line`, `column`, `end_line`, `end_column`) on all nodes.
- Preserves explicit node distinctions for rules, effects, metadata, objectives, overrides, expressions, quantifiers, and aggregates without evaluating any user-authored code.

---

## 4. Type Checking, Resolution, and Normalization Architecture

### 4.1 Type Checker (`arl/typecheck.py`)
- **Hindley-Milner Style Invariant Enforcement:**
  - Evaluates scopes against registered schema entities (`E2001`).
  - Validates field presence and type declarations (`E2002`).
  - Checks built-in and registered predicate signatures and arities (`E2003`, `E2004`).
  - Enforces strict explicit-only coercions: `Int` $\to$ `Float` (arithmetic), `Int` $\to$ `Slot` (via `as Slot`), `Tag` $\to$ `String`. Rejecting all invalid casts and comparisons (`E2005`).
  - Option unwrap enforcement (`E2006`): Direct `.` access on `Option<T>` is forbidden; requires `?.` or unwrapping via `is some`.
  - Non-entity field access detection (`E2007`).
  - Aggregate collection validation (`E2008`): aggregates (`count`, `sum`, etc.) require collection operands.
  - Objective weight alignment (`E2014`): weight keys must match known objective terms.
  - Regular expression compilation validation (`E2015`).

### 4.2 Resolver & Dependency Analysis (`arl/resolve.py`)
- **Inheritance Resolution:** Verifies `extends` references existing rulesets (`E2018`).
- **Rule Reference Graph:** Builds a directed dependency graph of rule invocations (`require rule IDENT`).
  - Detects cyclic dependencies using cycle detection (`E2009`).
  - Flags undefined rule references (`E2010`).
- **Override Verification:**
  - Flags overrides targeting non-existent rules (`E2013`).
  - Enforces scope compatibility between override effects and target rules (`E2012`).
- **Multiple Writer Check (§5.3):**
  - Detects duplicate assignment effects targeting the same field on the same entity across active rules (`E2011`).

### 4.3 AST Normalization (`arl/normalize.py`)
- **Canonical Representation:** Transforms the bound AST into a deterministic, order-independent canonical form.
- **Commutative Reordering:** Normalizes operands of commutative operators (`==`, `!=`, `+`, `*`, `and`, `or`) using deterministic keys, while strictly preserving operand order for non-commutative operators (`-`, `/`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `matches`, `is`).
- **Deterministic Collections:** Canonicalizes include orders, metadata tags, and objective weight mappings.
- **Idempotence:** Guaranteed `normalize(normalize(ast)) == normalize(ast)`.

---

## 5. Predicates, DAG, Overrides, and Constraint IR Architecture

### 5.1 Predicate Registry (`arl/predicates.py`, `predicates.yaml`)
- **Typed Predicate Catalog:** Implements all 28+ built-in predicates across relational, capacity, contiguity, cable/pair, hardware, naming, and revision categories.
- **One-Time Registration:** Extensible via `registry.register(name, args, returns, description, lower=...)` preventing duplicate registration.
- **Persistence & Offline Operation:** Default predicates stored in `predicates.yaml` for offline operation without LLM dependencies.
- **Lowering Contract:** Maps high-level AST predicate invocations into solver-agnostic IR constraints.

### 5.2 Dependency DAG & Validation (`arl/dag.py`)
- **Topological Ordering:** Computes execution order of rules so prerequisites are satisfied first.
- **Cycle Detection:** Identifies rule dependency cycles (`E2009`) and multi-file include cycles (`E3004`).
- **Priority Consistency:** Flags hard rules referencing soft-only rules (`E3005`).

### 5.3 Overrides & Effective Rules (`arl/override.py`)
- **Override Application:** Replaces or merges guard conditions, effects, and priority metadata on target rules.
- **Scope & Writer Integrity:**
  - Verifies override scope matches target rule scope (`E2012`).
  - Flags undefined override targets (`E2013`).
  - Detects if an override removes the only writer of an assigned field (`E3003`).
  - Validates writer uniqueness after overrides are applied (`E2011`).

### 5.4 Constraint IR (`arl/ir.py`, `arl/lower.py`)
- **Solver-Agnostic IR:** Explicit node types (`Var`, `Eq`, `Neq`, `Lt`, `Lte`, `Gt`, `Gte`, `And`, `Or`, `Not`, `Implies`, `AllDifferent`, `Count`, `Sum`, `Contiguous`, `ForAll`, `Exists`, `Soft`, `Objective`).
- **Traceability & Provenance:** Every lowered IR node retains `rule_id`, `priority` (`hard` vs `soft`), `weight`, and source `location`.
- **Pure Declarative IR:** No raw Python code or dynamic execution used; serialized cleanly into dictionaries and inspectable structures.

---

## 6. Greedy Solver, Technical Queries (TQ), Data Quality, and Independent Validation

### 6.1 Greedy Solver (`arl/solver_greedy.py`)
- **Deterministic Placement:** Allocates signals following the hierarchical order:
  `groups → cabinets → CIOCs → baseplates → CHARMs`
- **Data-Driven Hardware Mapping:** Maps signal IO types to CHARM and Terminal Block (TB) types (e.g. `AI` -> `STANDARD`, `DO_SOV` -> `FUSED_INJECTED_POWER`, `DO_RELAY` -> `ISOLATED_DO` / `RELAY_OUTPUT`).
- **Segregation & Area Constraints:** Enforces CIOC `seg_role` (`A_ONLY`, `B_ONLY`, `MIXED`) and cabinet area matching.
- **Capacity & Occupancy Tracking:** Tracks occupied slots across `(cabinet_id, cioc_id, baseplate_id, slot)` preventing double booking.
- **High-Yield Placement:** Exceeds the $\ge 90\%$ success benchmark requirement on clean project data (100% on clean benchmark).
- **Strict Determinism:** Repeated runs on identical inputs yield bitwise identical allocations.

### 6.2 Technical Query (TQ) Framework (`arl/tq.py`)
- **Part 6.4 Schema Conformance:** Generates structured TQs with:
  `tq_id`, `severity`, `code`, `title`, `rules`, `affected` (`signals`, `cables`, `pairs`), `evidence`, `impact`, `suggestions`, and `auto_fix_available`.
- **Unplaced Signal Tracking:** Whenever hardware capacity is exhausted or signals cannot be placed, structured TQs (`E4007`) are generated with remediation suggestions.
- **Zero Silent Drops:** The allocator never claims complete success if any signal remains unplaced.

### 6.3 Data Quality Checking (`arl/data_quality.py`)
- Detects business data inconsistencies prior to solving:
  - `E4001`: Missing required entity fields (`tag`, `io_type`, `cable`, `pair`).
  - `E4002`: Non-standard cable sizes (must be 2, 6, 12, or 24 pairs).
  - `E4003`: Orphan cable pairs.
  - `E4004`: Duplicate pair indices within a cable.
  - `E4005`: A/B Segregation mismatch within logical signal groups.
  - `E4006`: Unsupported/unmapped instrument IO types.
  - `E4007`: Project slot capacity overflow.
  - `E4008`: Duplicate cable identifiers.
  - `E4009`: Duplicate instrument signal tags.
  - `E4010`: Cable pair index exceeding cable pair size.
- Generates actionable TQs to enable business-side data remediation.

### 6.4 Independent Validation (`arl/validate.py`)
- **Solver-Independent Verification:** Operates independently of the solver to audit the final placement against physical DCS constraints and DSL rulesets.
- **Completeness & Coverage Check:** Every project signal must be either successfully allocated or explicitly referenced by an unplaced TQ.
- **Physical Integrity & Bounds:**
  - Verifies slot numbers $\in [1, 12]$ and baseplate capacities.
  - Detects slot collisions and duplicate signal placements.
  - Audits CIOC segregation (`A_ONLY` vs `B_ONLY`).
  - Flags CHARM / TB hardware incompatibilities.
- **Active Hard-Rule Verification:** Evaluates guard conditions (`when`) and asserts that every `require` and `forbid` effect in all active hard rules in the effective ruleset evaluates to True without dynamic Python execution.
- **Strict Rejection:** If any hard-rule or physical constraint is violated, `ValidationResult.valid` is set to `False` and the allocation is rejected.

---

## 7. Excel Ingestion, Template-Preserving Output, and Pipeline

### 7.1 Excel MDB Ingestion (`arl/excel_ingest.py`)
- **Multi-File Ingestion:** Ingests single or multiple `.xlsx` workbooks into a unified project model.
- **Inspectable Fuzzy Header Matching:** Auto-maps non-standard column headers (`Tag Name`, `I/O Type`, `Multicore Cable`, `Pair #`, `Segregation`, `Plant Area`) to canonical schema attributes using alias token matching and string distance metrics.
- **Manual Mapping Overrides & LLM Hook:** Supports explicit dictionary overrides taking precedence over fuzzy matching; includes an optional LLM mapper hook disabled by default with deterministic fallback.
- **Data Model Normalization:** Normalizes raw workbook rows into strongly-typed `Signal`, `Cable`, `Pair`, and `Group` entities, computing standard cable capacities (2, 6, 12, 24).
- **Early Data Quality Auditing:** Validates raw input against `E4001`–`E4010` rules and populates actionable Technical Queries.

### 7.2 Template-Preserving Output (`arl/excel_output.py`)
- **Zero-Loss Cell Population:** Loads pre-existing Excel allocation templates using `openpyxl`, writing values into cells while preserving merged cells, formulas, fonts, fills, borders, and data validation rules.
- **Atomic Writing:** Saves workbooks to a temporary file (`.tmp_write_<uuid>_<filename>`), verifies file integrity upon reopening, and atomically replaces the destination path (`os.replace`).
- **Standard Required Sheets:**
  - `Allocation`: Tag, Cabinet, CIOC, Baseplate, CHARM slot, CHARM type, TB type, Applied rule IDs, Rule version.
  - `TQ_List`: Conforms to Part 6.4 schema with TQ ID, severity, code, title, rules, affected signals/cables/pairs, evidence, impact metrics, suggestions, and auto-fix availability.
  - `Rule_Check`: Audits every DSL rule and physical constraint, reporting pass/fail status, violation counts, and diagnostic messages.
  - `Summary`: Executive KPI dashboard reporting total signals, placed, unplaced, spare slots, spare capacity ratio, and validation outcome.
  - `Impact`: Tracks revision delta metrics (added, deleted, moved, unchanged signals).

### 7.3 End-to-End Orchestration Pipeline (`arl/pipeline.py`)
- `run_allocation_pipeline`: Drives the end-to-end allocation workflow from Excel input through normalization, rule compilation, greedy allocation, independent validation, and template-preserving export.

---

## 8. Error Control Framework, Static Conflicts, and Audit

### 8.1 Error Classification & Recovery (`arl/errors.py`)
- **Strict Error Schema:** Every error object retains `code`, `severity` (FATAL, ERROR, WARNING, INFO), compiler `phase`, `rule_id`, `location` (file, line, col), `message`, `context`, and remediation `suggestion`.
- **E1xxx to E6xxx Catalog:**
  - Syntax (`E1001`–`E1007`): fatal per rule, quarantines affected rule.
  - Semantic (`E2001`–`E2018`): fatal per rule, quarantines affected rule.
  - Conflict (`E3001`–`E3005`): fatal, rejects ruleset, strictly blocks solver.
  - Data (`E4001`–`E4010`): recoverable, raises Technical Queries (TQs).
  - Solver (`E5001`–`E5005`): solver fallback / allocation rejection.
  - Revision (`E6001`–`E6004`): recoverable pin / move tracking.

### 8.2 Static Conflict Checking (`arl/conflict.py`)
- **Pre-Solver Gate:** Positioned strictly between IR lowering and solving.
- **Unsat Core Identification:**
  - `E3001`: Disjoint slot bounds, mutually exclusive category requirements, or contradictory require/forbid assertions on the same scope.
  - `E3002`: Hard rule requirement or forbid effect contradicting a field assignment.
  - `E3003`: Override eliminating the sole writer of an assigned entity field.
  - `E3004`: Circular multi-file includes.
  - `E3005`: Hard rule depending on or referencing a soft-only rule.
- **Strict Hard Gate:** If any fatal `E3xxx` code is flagged, `can_solve = False` and solver invocation is strictly blocked.

### 8.3 Structured Run Report (`arl/report.py`)
- **Part 6.3 Compliance:** Generates structured JSON reports containing `ruleset`, `version`, `status` (`success`, `compiled_with_warnings`, `rejected`, `failed`), `summary` (total, active, quarantined rules, errors, warnings, TQs), `quarantine` details, `warnings`, and `tqs`.

### 8.4 Observability and Event Logging (`arl/audit.py`)
- **Append-Only JSONL Logging:** Emits deterministic event streams containing ISO 8601 `timestamp`, stable `run_id`, compiler `phase`, `event_type`, `outcome`, `rule_id`, and contextual `details`.

---

## 9. OR-Tools CP-SAT Backend and Unsat Core Minimization

### 9.1 CP-SAT Decision Variables & Formulation (`arl/solver_cpsat.py`)
- **Decision Variables:**
  - `cabinet[group]`: Ensures all signals in a group co-locate within the same physical cabinet enclosure.
  - `cioc[cable]`: Ensures all signals in a cable trunk connect to the same CIOC.
  - `baseplate[cable, index]`: Models carrier assignments across cables and consecutive pair indices.
  - `charm[signal]`: Integer channel slot index $\in [1, 12]$ on the selected baseplate.
  - `slot_choice[signal]`: Flattened global physical slot index across all project enclosures.
- **Uniqueness & Cohesion:**
  - `AddAllDifferent([slot_choice[s] for s in signals])` prevents physical slot collision.
  - Channels physical properties (`slot`, `baseplate`, `cioc`, `cabinet`) using `AddElement`.
  - Enforces cable pair ordering ($p_i < p_{i+1}$) and segregation domain restrictions (`A_ONLY`, `B_ONLY`).

### 9.2 IR Translation & Objective Formulation
- **Reification & Assumptions:** Translates solver-agnostic IR nodes into linear CP-SAT constraints, associating each hard rule with an assumption Boolean literal `b_r`.
- **Soft Rules & Optimization:** Reifies soft rule conditions and weights into an objective function maximized by the CP-SAT solver.
- **Node Validation:** Detects unknown or unsupported IR nodes, rejecting them cleanly with `E5005` without engine crashes.

### 9.3 Assumption-Based Unsat Core Minimization
- **Iterative Pruning:**
  1. Identifies initial sufficient assumptions for infeasibility via `SufficientAssumptionsForInfeasibility()`.
  2. Iteratively tests removal of assumptions one by one to isolate minimal conflict subsets.
  3. Bounded by a configurable minimization budget (default 50 iterations).
  4. Labels the resulting unsat core accurately as `minimal` or `approximate`.
  5. Populates structured `E3001` Technical Queries with explicit rule citations.

### 9.4 Solver Fallback Policy
- **Configurable Time Limit:** Defaults to 30.0 seconds.
- **Deterministic Greedy Fallback:** If CP-SAT times out (`UNKNOWN`) or relaxation fails, invokes the deterministic `GreedySolver`, marks the output `degraded = True`, emits `E5001`, and records the fallback in audit logs.
- **Strict Output Validation:** Even fallback solutions are audited by the independent `Validator`; allocations with hard-rule violations are rejected.

---

## 10. Revision Comparison, Pinning, and Move Minimization

### 10.1 Multi-Dimensional Diffs (`arl/revision.py`)
- **MDB Diff (`diff_mdb`):** Compares Project Rev A vs Rev B models, classifying signals into:
  - `added`: newly introduced instruments.
  - `deleted`: decommissioned instruments.
  - `modified`: instruments with changes to field values (`io_type`, `cable`, `pair`, `group`, `area`, `unit`, `seg`, `safety`), tracking precise `(old_val, new_val)` deltas.
  - `unchanged`: instruments with completely identical properties.
- **Ruleset Diff (`diff_rulesets`):** Compares DSL rulesets across revisions, categorizing rules into:
  - `added`: newly authored rules in Rev B.
  - `removed`: deprecated rules removed from Rev B.
  - `modified`: rules with updated priority, weight, condition (`when`), or effect clauses.
  - `overridden`: rules targeted by `override` blocks in Rev B.
- **Allocation Diff (`diff_allocations`):** Compares physical placements between Rev A and Rev B, isolating moved signals, unchanged signals, and affected cabinets (`cabinets_hit`).

### 10.2 Pinning Engine & Priority Relaxation Order
- **Pin Assignment Rules:**
  - Unchanged signals are pinned to their exact Rev A physical hardware coordinates `(cabinet, cioc, baseplate, slot)`.
  - Signals with unchanged group and cable receive **hard pins** (`PinKind.HARD`).
  - Modified signals receive **soft pins** (`PinKind.SOFT`).
- **Strict Relaxation Priority Order:**
  Pins are relaxed in exact priority order when conflicts or physical constraints demand:
  1. `UNGROUPED` (lowest retention priority; relaxed first)
  2. `SOFT_SIGNAL` (standard soft pins)
  3. `GROUP_CHANGED` (signals whose group membership changed)
  4. `SAFETY_LAST` (safety-instrumented signals `safety == True`; preserved longest, relaxed last)
  5. `HARD_PIN` (hard pinned signals; relaxed only if physically impossible)
- **Revision Error Handling:**
  - `E6001`: Pinned slot no longer valid (hardware carrier/slot removed or area mismatch).
  - `E6002`: New rule in Rev B invalidates pins (segregation role changes or explicit forbid/require contradictions).
  - `E6003`: Move budget exceeded (`move_count > max_moves`), raising an ERROR and failing the revision gate.
  - `E6004`: Rev A allocation missing or empty workbook.

### 10.3 Comprehensive Impact Report Sheet
- **Executive KPI Dashboard:** Displays total added, deleted, moved, unchanged counts; move budget; move budget exceeded flag; and cabinets hit.
- **Rule Changes Trail:** Structured table detailing rule name, ID, change type (`ADDED`, `REMOVED`, `MODIFIED`, `OVERRIDDEN`), and diff details.
- **Moved Signals Trail:** Full audit table showing Tag, Rev A Location, Rev B Location, Cabinet Changed flag, and move reason.
- **Revision TQs:** Detailed Technical Queries generated during revision validation.

---

## 11. Natural-Language Bridge (`arl/nlp.py`)

### 11.1 Pipeline Flow
The Natural-Language Bridge implements the strict Part 11 compiler pipeline:
```
User text -> Intent classifier -> DSL draft (LLM)
  -> Parser -> Typecheck -> Scope check -> Static conflict check
  -> Dry-run on sample -> Impact preview -> Human approval -> Activate
```

### 11.2 Strict Compiler Guardrails
- **Isolated Provider Interface (`LLMProvider`):** The LLM is abstracted behind a mockable/disableable interface (`DisabledLLMProvider`, `MockLLMProvider`).
- **Zero-Trust Compilation Gate (`NLPValidator`):** Regardless of provider claims, every generated DSL rule draft must pass all five compiler stages:
  1. **Parser & Lexer:** Rejects malformed tokens, syntax errors, or block violations (`E1001`, `E1005`).
  2. **Scope Validation:** Rejects undefined entities or invalid scope targets (`E2001`).
  3. **Typecheck & Resolution:** Validates typed expressions, detects undefined fields (`E2002`), unknown predicates (`E2003`), arity mismatches (`E2004`), duplicate rule names (`E2017`), or duplicate rule IDs (`E1004`).
  4. **Static Conflict Analysis:** Verifies against the current ruleset using `ConflictChecker`, catching contradiction unsat cores (`E3001`, `E3002`). Isolates and reports all involved rules.
  5. **Dry-Run & Impact Preview:** Executes a sample solve with `CPSATSolver`, calculating total moved signals, unplaced delta, and movement ratio.
- **Human Confirmation Gate:** Proposals with movement ratio exceeding the configured threshold (default: 10%) or causing unplaced signal deltas flag `high_impact = True` and require explicit human confirmation before activation. Activating without approval raises `PermissionError`.
- **Append-Only Proposal Audit (`NLPProposalLogger`):** Records every proposal, validation stage, dry-run impact metrics, rejection reason, and human activation disposition in persistent JSONL.

### 11.3 Deterministic Offline Fallback
- **No LLM-Only Features:** If no LLM provider is configured or when the provider is disabled (`DisabledLLMProvider`), the system remains 100% operational via:
  - Direct `.arl` file editing.
  - Deterministic form-based rule builder (`FormRuleSpec`, `build_rule_from_form`) that constructs verified ARL rules programmatically.
  - Offline heuristic intent routing (`classify_intent`) and safe deterministic drafting.

### 11.4 Ambiguity & Missing Predicate Protocols
- **Ambiguity Detection:** Vague prompts (e.g. "segregate signals" without dimension) return `is_ambiguous = True` with a clarifying question offering 2-3 concrete DeltaV engineering options (e.g. Segregation by Channel Seg A/B, by Baseplate, or by CIOC).
- **Missing Predicate Suggestion & Registration (`register_proposed_predicate`):** When prompts request predicates not in the catalog, the bridge drafts a structured `ProposedPredicate` containing name, typed arguments, return type, category, and lowering requirements. Predicates must be explicitly registered into `PredicateRegistry` without arbitrary code execution.

---

## 12. CLI, Streamlit UI, and Rule Lifecycle (`arl/cli.py`, `arl/ui.py`, `arl/lifecycle.py`)

### 12.1 Command-Line Interface (`arl/cli.py`)
The `arl` CLI exposes all Part 12 and Part 13 compiler and runtime operations through structured commands:
- `arl compile <ruleset.arl>`: Lexes, parses, typechecks, builds rule DAG, checks static conflicts, and reports status.
- `arl check <ruleset.arl> --data <mdb>`: Performs dry-run validation of ruleset against MDB data without full placement.
- `arl allocate <ruleset.arl> --mdb <xlsx> --template <xlsx> --out <xlsx>`: Executes full end-to-end allocation using CP-SAT or greedy fallback, emitting template-preserving Excel workbook with Allocation, TQ_List, Rule_Check, Summary, and Impact sheets.
- `arl revise <ruleset.arl> --revA <xlsx> --revB <xlsx> --out <xlsx>`: Performs revision pipeline with automatic pin generation, priority relaxation, move minimization, and budget enforcement.
- `arl diff-rules <a.arl> <b.arl>`: Reports AST-level rule differences (added, removed, modified, overridden).
- `arl diff-alloc <a.xlsx> <b.xlsx>`: Reports physical placement differences (moved, unchanged, affected cabinets).
- `arl predicates list`: Displays all registered predicates in the typed catalog with argument signatures, return types, and descriptions.
- `arl predicates register <file.yaml>`: Registers new domain predicates from YAML definitions and persists to `predicates.yaml`.

#### Strict Exit Code Precedence
CLI commands exit with strict numerical status codes:
- `0`: Success (complete placement, no unresolved errors).
- `1`: Compile error (syntax error, typecheck error, DAG cycle, or static conflict `E1xxx`, `E2xxx`, `E3xxx`).
- `2`: Data error (schema violation, missing required field, duplicate tag, or corrupt input `E4xxx`).
- `3`: Solver fallback (greedy degraded mode, unplaced signals, or move budget exceeded `E5xxx`, `E6xxx`).
- `4`: Internal error (unhandled exceptions, system failure).

When multiple conditions arise, exit-code precedence is strictly enforced:
$$\text{Internal (4)} > \text{Compile (1)} > \text{Data (2)} > \text{Solver Fallback (3)} > \text{Success (0)}$$

### 12.2 Streamlit User Interface (`arl/ui.py`)
A single, modular Streamlit web interface provides engineering access across 6 required tabs:
1. **Upload:** MDB workbook ingestion, fuzzy column mapping inspector, manual mapping overrides, and template loader.
2. **Rules:** Interactive DSL code editor and visual Form Rule Builder (`FormRuleSpec`), with one-click compile validation, conflict checking, and dry-run impact simulation.
3. **Allocate:** Allocation trigger with solver backend selection (CP-SAT vs Greedy), timeout configuration, and live progress indicators.
4. **Reports:** Tabular and graphical inspection of run metrics, solver statistics, quarantined rules, and hardware fill rates.
5. **TQs:** Interactive Technical Query review table with filtering by severity, code, tag, and suggested resolution.
6. **Revision:** Side-by-side Rev A vs Rev B comparison, pinning status view, move budget tracking, and revision diff export.

**Zero Divergence Guarantee:** The UI imports and calls the exact same underlying compiler, schema, solver, and reporting APIs as the CLI and test suite; no business logic is duplicated or bypassed.

### 12.3 Rule Lifecycle & Immutability (`arl/lifecycle.py`)
Rulesets transition through a strictly governed lifecycle managed by `LifecycleManager`:
- **States:** `DRAFT` $\to$ `PUBLISHED` $\to$ `ARCHIVED`.
- **Semantic Versioning:** Follows `MAJOR.MINOR.PATCH` format via `SemVer`:
  - `PATCH`: Non-breaking rule tweaks or documentation changes.
  - `MINOR`: New rules, objectives, or backward-compatible constraints.
  - `MAJOR`: Breaking changes or radical ruleset overhauls.
- **Strict Immutability:** Once published, a ruleset version is frozen and immutable. Any edit attempt automatically spawns a new `DRAFT` record with a bumped semantic version.
- **Pre-Publication Gate:** A ruleset cannot be published unless it passes a full compiler check (parsing, resolution, typecheck, conflict detection) with zero fatal errors.






