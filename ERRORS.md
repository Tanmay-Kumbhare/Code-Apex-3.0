# ARL v2 — Error Control Framework and Catalog (`ERRORS.md`)

This document provides the authoritative reference for the Allocation Rule Language (ARL) v2 error handling system, covering error object schema, lifecycle recovery behaviors, compiler phases, and the complete catalog of error codes (`E1001` through `E6004`).

---

## 1. Error Framework Architecture

ARL implements a zero-exception compiler and runtime architecture. Every diagnostic, warning, fatal error, and recoverable data inconsistency is captured in a unified structured [`ArlError`](file:///Users/kasturirasekar/Desktop/codeapex/arl/errors.py) model.

### 1.1 Structured Error Model
Every error emitted by any phase contains:
- **`code`:** Unique identifier (`E1xxx`–`E6xxx`).
- **`severity`:** `FATAL`, `ERROR`, `WARNING`, `INFO`.
- **`phase`:** Compiler or runtime phase where the issue originated.
- **`rule_id`:** Optional explicit ID or name of the offending rule.
- **`location`:** Source file location (`file`, `line`, `column`, `end_line`, `end_column`).
- **`message`:** Human-readable diagnostic description.
- **`context`:** Typed dictionary containing AST nodes, token strings, signal tags, or coordinate values.
- **`suggestion`:** Concrete, actionable guidance for correcting the defect.

### 1.2 Severity Levels
- **`FATAL`:** Critical compilation or conflict defect that renders the ruleset or operation mathematically unexecutable (e.g. syntax error, cyclic dependency, static unsat core). Halts execution or quaratines the rule.
- **`ERROR`:** Data quality or validation defect (e.g. missing required field, duplicate tag, invalid IO type) that prevents full placement or violates hard safety rules.
- **`WARNING`:** Non-blocking anomaly (e.g. orphan cable pair, soft pin relaxation, solver timeout falling back to greedy) that generates a TQ or audit record without stopping execution.
- **`INFO`:** Diagnostic trace or telemetry event (e.g. rule quarantine notice, optimization step).

### 1.3 Compiler Phase Recovery & Rule Quarantine
- **Syntax / Lexical (`E1xxx`):** When a syntax error occurs within a rule declaration, the rule is **quarantined** with an attached error record. The parser recovers at the next declaration boundary, allowing subsequent rules to compile.
- **Semantic / Resolution (`E2xxx`):** Type mismatch or invalid field access isolates the offending rule or expression. Include cycles (`E3004`) and inheritance cycles reject the affected ruleset.
- **Static Conflict Detection (`E3xxx`):** Disjoint slot domains, contradictory hard requirements, or missing field writers trigger static UNSAT core analysis. The solver is never invoked if any fatal `E3xxx` conflict is active.
- **Data Quality (`E4xxx`):** Ingestion validates cables, pairs, and tags. Fatal errors generate Technical Queries (TQs) and block placement; recoverable warnings create informative TQs.
- **Solver Execution (`E5xxx`):** If CP-SAT times out or encounters unsupported nodes, the system transitions to the greedy fallback allocator (`degraded = True`) without crashing.
- **Revision (`E6xxx`):** Invalid pinned slots or rule contradictions relax pins according to the 5-level relaxation order. Move budget overflows (`E6003`) fail the revision gate.

### 1.4 CLI Exit-Code Precedence
When multiple error types occur during an execution, the CLI enforces strict priority:
$$\text{Internal Error (4)} > \text{Compile Error (1)} > \text{Data Error (2)} > \text{Solver Fallback (3)} > \text{Success (0)}$$

---

## 2. Complete Error Catalog (E1001 – E6004)

### 2.1 Syntax & Lexical Errors (`E1xxx`)

| Code | Severity | Phase | Default Message | Diagnostic Cause | Actionable Mitigation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `E1001` | `FATAL` | `PARSE` | Unexpected token | Parser encountered a token that violates EBNF grammar productions. | Check DSL syntax around the reported line and column. |
| `E1002` | `FATAL` | `LEX` | Unterminated string | String literal missing closing quote before newline or EOF. | Add matching closing quote (`"` or `'`). |
| `E1003` | `FATAL` | `PARSE` | Missing colon after rule name | Rule header omitted the mandatory trailing colon. | Add `:` after the rule identifier (e.g. `rule R_Example:`). |
| `E1004` | `FATAL` | `PARSE` | Duplicate rule ID in ruleset | Multiple rules share the exact same explicit metadata `id`. | Assign a globally unique `id` to each rule. |
| `E1005` | `FATAL` | `PARSE` | Invalid block structure | Unbalanced curly braces `{}` or improper block closure. | Ensure all opened blocks `{` have matching closing `}`. |
| `E1006` | `FATAL` | `LEX` | Invalid numeric literal | Malformed float, double decimal point, or invalid numeric token. | Use standard integer or decimal notation (e.g., `10` or `10.5`). |
| `E1007` | `FATAL` | `LEX` | Invalid escape sequence | Unrecognized escape character in string literal (e.g., `\c`). | Use valid escapes like `\n`, `\t`, `\\`, or raw string format. |

---

### 2.2 Semantic, Type Checking, and Resolution Errors (`E2xxx`)

| Code | Severity | Phase | Default Message | Diagnostic Cause | Actionable Mitigation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `E2001` | `FATAL` | `TYPECHECK` | Unknown entity type | Rule scope or type reference specifies an unregistered entity. | Verify entity exists in `schema.yaml` (`Signal`, `Charm`, etc.). |
| `E2002` | `FATAL` | `TYPECHECK` | Unknown field on entity | Expression accesses a field not defined on target entity schema. | Check field spelling or register the new field using `register_field`. |
| `E2003` | `FATAL` | `TYPECHECK` | Unknown predicate | Call expression invokes a predicate not in `PredicateRegistry`. | Register predicate via `arl predicates register` or check name. |
| `E2004` | `FATAL` | `TYPECHECK` | Predicate arity mismatch | Predicate call provides incorrect number of arguments. | Review predicate signature in `arl predicates list` and match args. |
| `E2005` | `FATAL` | `TYPECHECK` | Type mismatch | Incompatible types in operator, assignment, or comparison. | Ensure operand types match or apply explicit conversion (e.g. `as Slot`). |
| `E2006` | `FATAL` | `TYPECHECK` | Missing Option unwrap | Direct access on optional `T?` field without unwrap or `?.`. | Use optional chaining (`?.`) or unwrap with `is some x`. |
| `E2007` | `FATAL` | `TYPECHECK` | Field access on non-entity | Attempted member navigation (`.`) on primitive or scalar type. | Only access fields on entity instances or compound types. |
| `E2008` | `FATAL` | `TYPECHECK` | Aggregate over non-collection | Aggregation function (`count`, `sum`) passed scalar instead of collection. | Ensure target argument is an array (`T[]`) or set (`Set<T>`). |
| `E2009` | `FATAL` | `DAG_BUILD` | Cyclic rule reference | Direct or indirect circular dependency detected in rule DAG. | Break the cycle by refactoring rule preconditions or effects. |
| `E2010` | `FATAL` | `RESOLVE` | Unknown rule reference | Rule precondition references a rule name not found in ruleset. | Check rule name spelling or ensure the target rule is included. |
| `E2011` | `FATAL` | `TYPECHECK` | Duplicate field writer | Multiple rules in same ruleset attempt to write the same field. | Consolidate writes into a single rule or use conditional logic. |
| `E2012` | `FATAL` | `OVERRIDE_APPLY`| Override scope mismatch | Override block declares a different scope than the overridden rule. | Align override `scope:` with original target rule scope. |
| `E2013` | `FATAL` | `OVERRIDE_APPLY`| Override targets undefined rule | `override Foo` references a rule name that does not exist. | Verify target rule name matches an active or inherited rule. |
| `E2014` | `FATAL` | `TYPECHECK` | Objective references unknown weight | Objective weights map references a non-existent metric name. | Ensure weight keys match primary or secondary objective metrics. |
| `E2015` | `FATAL` | `TYPECHECK` | Invalid regex | Regular expression pattern in `matches` operator fails compilation. | Correct regex syntax (e.g. escape brackets, check qualifiers). |
| `E2016` | `FATAL` | `PARSE` | Reserved keyword used as identifier| DSL keyword (`rule`, `scope`, `when`, `hard`, etc.) used as name. | Choose an alternative identifier that is not a reserved keyword. |
| `E2017` | `FATAL` | `PARSE` | Duplicate rule name in same ruleset | Two rules share the same identifier within the same ruleset body. | Rename one of the conflicting rules. |
| `E2018` | `FATAL` | `RESOLVE` | Extends target not found | Ruleset `extends Foo` targets a ruleset that cannot be resolved. | Ensure parent ruleset file is included or loaded into the catalog. |

---

### 2.3 Static Conflict & DAG Errors (`E3xxx`)

| Code | Severity | Phase | Default Message | Diagnostic Cause | Actionable Mitigation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `E3001` | `FATAL` | `CONFLICT_CHECK`| Unsat core: rules conflict on data | Static analysis identified mutually exclusive hard constraints. | Inspect reported unsat core rules and relax overlapping bounds. |
| `E3002` | `FATAL` | `CONFLICT_CHECK`| Hard rule contradicts assignment | Hard rule requires/forbids a value that conflicts with assignment. | Harmonize `assign` value with active `require` or `forbid` rules. |
| `E3003` | `FATAL` | `OVERRIDE_APPLY`| Override removes only writer of required field | Override eliminates the sole assignment rule for a required field. | Ensure an alternate writer rule exists before removing assignment. |
| `E3004` | `FATAL` | `RESOLVE` | Include cycle | Circular include loop detected across ruleset files. | Refactor shared rules into a common base ruleset without loops. |
| `E3005` | `FATAL` | `TYPECHECK` | Hard rule references soft-only rule | A `hard` rule precondition depends on a `soft` priority rule. | Change dependent rule to `soft`, or elevate referenced rule to `hard`. |

---

### 2.4 Data Quality & Ingestion Errors (`E4xxx`)

| Code | Severity | Phase | Default Message | Diagnostic Cause | Actionable Mitigation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `E4001` | `ERROR` | `DATA` | Missing required field | Imported row lacks mandatory field (`Tag Name`, `I/O Type`, etc.). | Fill missing value in MDB workbook or configure default mapping. |
| `E4002` | `ERROR` | `DATA` | Cable size unknown or inconsistent | Declared cable pair count does not match standard sizes (2/6/12/24). | Correct cable size in source data or update `CableSize` enum. |
| `E4003` | `WARNING` | `DATA` | Orphan pair | Cable pair is unassigned to any instrument and not marked wired spare. | Assign instrument or mark pair as wired spare to prevent warnings. |
| `E4004` | `ERROR` | `DATA` | Duplicate pair index | Multiple rows in the same cable share the same pair number. | Renumber pairs sequentially starting from 1. |
| `E4005` | `ERROR` | `DATA` | A/B mismatch within group | Instruments in same group declare conflicting segregation channels. | Ensure all cables and instruments in a group share consistent Seg. |
| `E4006` | `ERROR` | `DATA` | IO type not in CHARM map | Signal declares an IO type without a valid CHARM hardware mapping. | Update signal IO type or add hardware mapping entry. |
| `E4007` | `ERROR` | `DATA` | Capacity overflow | Total imported signals exceed total physical hardware slot capacity. | Add additional CIOC/Baseplate hardware or split signals across cabinets. |
| `E4008` | `ERROR` | `DATA` | Naming collision | Hardware naming or identifier collision in physical enclosure model. | Verify unique cabinet and CIOC numbering. |
| `E4009` | `ERROR` | `DATA` | Duplicate tag | Two or more instrument rows share the identical `Tag Name`. | Ensure each instrument tag is globally unique in the MDB. |
| `E4010` | `ERROR` | `DATA` | Pair index out of range for cable size | Pair index (e.g. 15) exceeds maximum declared cable size (e.g. 12). | Re-assign pair number to fit within cable size bounds. |

---

### 2.5 Solver & Optimization Errors (`E5xxx`)

| Code | Severity | Phase | Default Message | Diagnostic Cause | Actionable Mitigation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `E5001` | `WARNING` | `SOLVE` | Solver timeout | CP-SAT solver exceeded configured time limit before proving optimality. | Increase `--timeout` or rely on best-found / greedy fallback. |
| `E5002` | `FATAL` | `SOLVE` | Infeasible after relaxation | Placement impossible even after relaxing all eligible soft constraints. | Verify hardware capacity, review unplaced TQs, and add baseplates. |
| `E5003` | `ERROR` | `SOLVE` | Objective unbounded | Optimization objective function is numerically unstable or unbounded. | Constrain objective metric weights or set explicit bounds. |
| `E5004` | `FATAL` | `SOLVE` | Memory limit exceeded | Model construction exceeded configured memory threshold. | Partition signals into smaller subprojects or increase RAM limit. |
| `E5005` | `ERROR` | `SOLVE` | Invalid IR node | Solver encountered an unsupported or malformed Constraint IR node. | Lower expression into supported primitives or update IR translator. |

---

### 2.6 Revision, Pinning, and Diff Errors (`E6xxx`)

| Code | Severity | Phase | Default Message | Diagnostic Cause | Actionable Mitigation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `E6001` | `WARNING` | `REVISION` | Pinned slot no longer valid | Previously pinned slot was removed or became incompatible in Rev B. | Soft-relax pin and allocate signal to adjacent available slot. |
| `E6002` | `WARNING` | `REVISION` | New rule invalidates pins | Newly introduced rule in Rev B contradicts Rev A pin assignment. | Relax conflicting pin according to relaxation priority order. |
| `E6003` | `ERROR` | `REVISION` | Move budget exceeded | Number of moved signals exceeded configured `--max-moves` threshold. | Increase move budget or relax Rev B rules to preserve Rev A layout. |
| `E6004` | `ERROR` | `REVISION` | Rev A allocation missing | Revision command invoked without a valid Rev A baseline allocation. | Provide a populated Rev A allocation workbook via `--revA`. |
