# Allocation Rule Language (ARL) v2 — DSL Syntax Guide

This guide documents the formal grammar and syntax of ARL v2.

---

## 1. Ruleset Structure

Every ARL rule file defines a `ruleset`, which may optionally extend another ruleset:

```arl
ruleset ProjectAR1 extends BaseRules {
    include "shared_segregation.arl"

    // Rules, objectives, and overrides go here
}
```

---

## 2. Rule Definition

A `rule` consists of a name, mandatory `scope`, optional `when` guard condition, one or more `effect` statements, and optional metadata:

```arl
rule R_Safety_Segregation:
    scope: Signal
    when: self.safety == true
    require same_baseplate(self)
    priority: hard
    message: "Safety signals must be placed on dedicated baseplates"
    tags: [safety, segregation]
    id: R001
```

### 2.1 Scope
Specifies the target entity type being evaluated (e.g., `Signal`, `Cable`, `Pair`, `Group`, `Baseplate`, `Charm`, `CIOC`, `Cabinet`, `Project`).

### 2.2 When Guard Condition
Optional condition evaluated in the scope of `self`. If omitted, the rule applies unconditionally to all instances of the scope.

### 2.3 Effects
A rule must specify at least one effect:
- `require <expr>`: Requires `<expr>` to evaluate to `true`.
- `require rule <IDENT>`: Inherits effects from another named rule.
- `forbid <expr>`: Requires `<expr>` to evaluate to `false`.
- `assign <field> = <expr>`: Hard assignment of an entity field.
- `prefer <expr>`: Soft optimization preference with associated weight.

### 2.4 Metadata
- `priority: hard | soft` (default: `hard`)
- `weight: NUMBER` (used with soft rules)
- `message: "STRING"` (explanation emitted in TQs and validation reports)
- `tags: [TAG1, TAG2]` (filtering and categorization)
- `id: IDENT` (unique identifier)

---

## 3. Objective Definition

Objectives define optimization targets consumed by the CP-SAT and greedy backends:

```arl
objective Maximize_Packing:
    primary: [sum(self.used_slots), count(self.signals)]
    secondary: [spare_percent(self)]
    weights: {
        packing: 100.0,
        spare_balance: 50.0
    }
```

---

## 4. Override Definition

Overrides modify or replace existing rules under specific conditions (e.g. for special areas or revisions):

```arl
override R_Safety_Segregation:
    when: self.area == "Offshore_Skid"
    forbid self.voltage == "HV"
    priority: hard
    id: OVR_001
```

---

## 5. Expressions, Operators, and Precedence

Expressions are evaluated according to standard operator precedence:

| Operator Category | Operators | Associativity |
| :--- | :--- | :--- |
| **Postfix / Primary** | `.`, `?.`, `()`, `[]` | Left-to-right |
| **Unary** | `-`, `not` | Right-to-left |
| **Multiplicative** | `*`, `/`, `%` | Left-to-right |
| **Additive** | `+`, `-` | Left-to-right |
| **Comparison** | `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `matches`, `is` | None |
| **Conjunction** | `and` | Left-to-right |
| **Disjunction** | `or` | Left-to-right |

### 5.1 Optional Chaining (`?.`)
Safe access on optional fields. Field access (`.`) on an unverified optional field triggers compile error `E2006`.

```arl
when: self?.group != null and self.group.id == "GRP_01"
```

### 5.2 Quantifiers
ARL supports first-order logic quantifiers over collections:

```arl
// Universal quantification
require forall bp in self.baseplates where bp.capacity > 0 : count(bp.charms) <= 12

// Existential quantification
require exists c in self.cables : c.size >= 12
```

### 5.3 Aggregates
Aggregate functions operate over collections:
- `count(collection)`
- `sum(collection)`
- `min(collection)`
- `max(collection)`
- `avg(collection)`
- `distinct(collection)`

Example:
```arl
when: count(self.signals) > 0 and spare_percent(self) >= 10.0
```

---

## 6. Reserved Keywords

The following words are reserved in ARL and cannot be used as identifiers:
`ruleset`, `extends`, `include`, `rule`, `objective`, `override`, `scope`, `when`, `require`, `forbid`, `assign`, `prefer`, `priority`, `hard`, `soft`, `weight`, `message`, `tags`, `id`, `self`, `and`, `or`, `not`, `in`, `matches`, `is`, `forall`, `exists`, `count`, `sum`, `min`, `max`, `avg`, `distinct`, `primary`, `secondary`, `weights`, `true`, `false`, `null`, `where`.
