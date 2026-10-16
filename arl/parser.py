"""
ARL Parser.

Recursive-descent parser implementing Part 2 Grammar and Part 6 Syntax Errors
(E1001, E1003, E1004, E1005, E2016, E2017).
Retains exact SourceLocation on all AST nodes and supports rule quarantine on syntax errors.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from arl.ast import (
    AggregateExpr,
    AsExpr,
    AssignEffect,
    BinaryOp,
    CallExpr,
    Effect,
    Expr,
    FieldAccessExpr,
    ForbidEffect,
    IdentifierExpr,
    Include,
    IndexExpr,
    IsNoneExpr,
    IsSomeExpr,
    ListLiteralExpr,
    LiteralExpr,
    Objective,
    Override,
    PreferEffect,
    QuantifiedExpr,
    RequireEffect,
    Rule,
    RuleMeta,
    RuleRef,
    Ruleset,
    SelfExpr,
    UnaryOp,
)
from arl.errors import ArlError, ErrorCollector, Phase, Severity, SourceLocation, make_error
from arl.lexer import Lexer, Token, TokenType


class Parser:
    """Recursive-descent parser for ARL."""

    def __init__(
        self,
        tokens: List[Token],
        source_file: Optional[str] = None,
        collector: Optional[ErrorCollector] = None,
    ) -> None:
        self.tokens = tokens
        self.file = source_file
        self.collector = collector or ErrorCollector()
        self.pos = 0

    @classmethod
    def from_source(
        cls,
        source: str,
        source_file: Optional[str] = None,
        collector: Optional[ErrorCollector] = None,
    ) -> Parser:
        col = collector or ErrorCollector()
        lexer = Lexer(source, file=source_file, collector=col)
        tokens = lexer.tokenize()
        return cls(tokens, source_file=source_file, collector=col)

    def current_token(self) -> Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return self.tokens[-1]

    def peek(self, offset: int = 0) -> Token:
        idx = self.pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return self.tokens[-1]

    def advance(self) -> Token:
        tok = self.current_token()
        if self.pos < len(self.tokens):
            self.pos += 1
        return tok

    def match(self, *token_types: TokenType) -> bool:
        if self.current_token().type in token_types:
            self.advance()
            return True
        return False

    def expect(self, token_type: TokenType, err_code: str = "E1001", message: Optional[str] = None) -> Token:
        tok = self.current_token()
        if tok.type == token_type:
            return self.advance()

        msg = message or f"Expected '{token_type.value}', found '{tok.value}'"
        self.collector.add_error(
            code=err_code,
            message=msg,
            location=tok.location,
            phase=Phase.PARSE,
        )
        raise SyntaxError(msg)

    def check_identifier(self, tok: Token) -> str:
        """Validate that token is an identifier and not a reserved keyword."""
        if tok.type == TokenType.IDENT:
            return tok.value

        # Reserved keyword used as identifier: E2016
        self.collector.add_error(
            code="E2016",
            message=f"Reserved keyword '{tok.value}' cannot be used as an identifier",
            location=tok.location,
            phase=Phase.PARSE,
        )
        return tok.value

    # ========================================================================
    # Top-Level Grammar Productions
    # ========================================================================

    def parse_ruleset(self) -> Ruleset:
        """
        ruleset := "ruleset" IDENT ("extends" IDENT)? "{" ruleset_body "}"
        """
        start_tok = self.expect(TokenType.RULESET, message="Expected 'ruleset' declaration")
        name_tok = self.advance()
        name = self.check_identifier(name_tok)

        extends_name: Optional[str] = None
        if self.match(TokenType.EXTENDS):
            ext_tok = self.advance()
            extends_name = self.check_identifier(ext_tok)

        if self.current_token().type != TokenType.LBRACE:
            self.collector.add_error(
                code="E1005",
                message=f"Expected '{{' to open ruleset body, found '{self.current_token().value}'",
                location=self.current_token().location,
                phase=Phase.PARSE,
            )
            # Try to recover if { appears next
            if not self.match(TokenType.LBRACE):
                return Ruleset(name=name, extends=extends_name, location=start_tok.location)
        else:
            self.advance()

        ruleset = Ruleset(name=name, extends=extends_name, location=start_tok.location)

        # Track rule names and explicit rule IDs for duplicates (E1004, E2017)
        seen_rule_names: Set[str] = set()
        seen_rule_ids: Set[str] = set()

        while self.current_token().type not in (TokenType.RBRACE, TokenType.EOF):
            tok = self.current_token()

            if tok.type == TokenType.INCLUDE:
                try:
                    inc = self.parse_include()
                    ruleset.includes.append(inc)
                except Exception:
                    self.synchronize_declaration()

            elif tok.type == TokenType.RULE:
                try:
                    rule = self.parse_rule()
                    if rule.name in seen_rule_names:
                        self.collector.add_error(
                            code="E2017",
                            message=f"Duplicate rule name '{rule.name}' in ruleset '{name}'",
                            rule_id=rule.name,
                            location=rule.location,
                            phase=Phase.PARSE,
                        )
                    seen_rule_names.add(rule.name)

                    if rule.meta.rule_id:
                        if rule.meta.rule_id in seen_rule_ids:
                            self.collector.add_error(
                                code="E1004",
                                message=f"Duplicate rule ID '{rule.meta.rule_id}' in ruleset '{name}'",
                                rule_id=rule.meta.rule_id,
                                location=rule.location,
                                phase=Phase.PARSE,
                            )
                        seen_rule_ids.add(rule.meta.rule_id)

                    ruleset.rules.append(rule)
                except Exception:
                    self.synchronize_declaration()

            elif tok.type == TokenType.OBJECTIVE:
                try:
                    obj = self.parse_objective()
                    ruleset.objectives.append(obj)
                except Exception:
                    self.synchronize_declaration()

            elif tok.type == TokenType.OVERRIDE:
                try:
                    ov = self.parse_override()
                    ruleset.overrides.append(ov)
                except Exception:
                    self.synchronize_declaration()

            else:
                self.collector.add_error(
                    code="E1001",
                    message=f"Unexpected token '{tok.value}' in ruleset body",
                    location=tok.location,
                    phase=Phase.PARSE,
                )
                self.advance()

        if self.current_token().type != TokenType.RBRACE:
            self.collector.add_error(
                code="E1005",
                message="Unclosed ruleset body: missing closing '}'",
                location=self.current_token().location,
                phase=Phase.PARSE,
            )
        else:
            self.advance()

        return ruleset

    def synchronize_declaration(self) -> None:
        """Error recovery: advance until next top-level keyword or RBRACE."""
        while self.current_token().type not in (
            TokenType.RULE,
            TokenType.OBJECTIVE,
            TokenType.OVERRIDE,
            TokenType.INCLUDE,
            TokenType.RBRACE,
            TokenType.EOF,
        ):
            self.advance()

    def parse_include(self) -> Include:
        """include := "include" STRING"""
        inc_tok = self.expect(TokenType.INCLUDE)
        str_tok = self.expect(TokenType.STRING, message="Expected string path after 'include'")
        return Include(path=str(str_tok.parsed_value), location=inc_tok.location)

    def parse_rule(self) -> Rule:
        """
        rule := "rule" IDENT ":" rulebody
        rulebody := scope when? effect+ meta*
        """
        rule_tok = self.expect(TokenType.RULE)
        name_tok = self.advance()
        name = self.check_identifier(name_tok)

        # Check for missing colon: E1003
        if self.current_token().type != TokenType.COLON:
            self.collector.add_error(
                code="E1003",
                message=f"Missing colon after rule name '{name}'",
                rule_id=name,
                location=self.current_token().location,
                phase=Phase.PARSE,
            )
            if self.current_token().type == TokenType.SCOPE:
                pass  # recover if scope immediately follows
            else:
                raise SyntaxError(f"Missing colon after rule name '{name}'")
        else:
            self.advance()

        # scope := "scope" ":" entitytype
        self.expect(TokenType.SCOPE, message="Expected 'scope' declaration in rule body")
        self.expect(TokenType.COLON, message="Expected ':' after 'scope'")
        scope_tok = self.advance()
        scope = self.check_identifier(scope_tok)

        # when?
        when_cond: Optional[Expr] = None
        if self.match(TokenType.WHEN):
            self.expect(TokenType.COLON, message="Expected ':' after 'when'")
            when_cond = self.parse_condition()

        # effect+
        effects: List[Effect] = []
        while self.current_token().type in (
            TokenType.REQUIRE,
            TokenType.FORBID,
            TokenType.ASSIGN,
            TokenType.PREFER,
        ):
            effects.append(self.parse_effect())

        if not effects:
            self.collector.add_error(
                code="E1001",
                message=f"Rule '{name}' must have at least one effect (require, forbid, assign, prefer)",
                rule_id=name,
                location=self.current_token().location,
                phase=Phase.PARSE,
            )

        # meta*
        meta = self.parse_meta_items()

        return Rule(
            name=name,
            scope=scope,
            when=when_cond,
            effects=effects,
            meta=meta,
            location=rule_tok.location,
        )

    def parse_override(self) -> Override:
        """
        override := "override" IDENT ":" overridebody
        overridebody := ("when" ":" condition)? effect+ meta*
        """
        ov_tok = self.expect(TokenType.OVERRIDE)
        target_tok = self.advance()
        target = self.check_identifier(target_tok)

        self.expect(TokenType.COLON, message="Expected ':' after override target name")

        # when?
        when_cond: Optional[Expr] = None
        if self.match(TokenType.WHEN):
            self.expect(TokenType.COLON, message="Expected ':' after 'when'")
            when_cond = self.parse_condition()

        # effect+
        effects: List[Effect] = []
        while self.current_token().type in (
            TokenType.REQUIRE,
            TokenType.FORBID,
            TokenType.ASSIGN,
            TokenType.PREFER,
        ):
            effects.append(self.parse_effect())

        if not effects:
            self.collector.add_error(
                code="E1001",
                message=f"Override '{target}' must have at least one effect",
                location=self.current_token().location,
                phase=Phase.PARSE,
            )

        # meta*
        meta = self.parse_meta_items()

        return Override(
            target_rule=target,
            when=when_cond,
            effects=effects,
            meta=meta,
            location=ov_tok.location,
        )

    def parse_objective(self) -> Objective:
        """
        objective := "objective" IDENT ":" objbody
        objbody := "primary" ":" "[" objterm ("," objterm)* "]"
                   ("secondary" ":" "[" objterm ("," objterm)* "]")?
                   ("weights" ":" "{" (IDENT ":" NUMBER)* "}")?
        """
        obj_tok = self.expect(TokenType.OBJECTIVE)
        name_tok = self.advance()
        name = self.check_identifier(name_tok)

        self.expect(TokenType.COLON, message="Expected ':' after objective name")

        # primary : [objterm, ...]
        self.expect(TokenType.PRIMARY, message="Objective must declare 'primary'")
        self.expect(TokenType.COLON, message="Expected ':' after 'primary'")
        self.expect(TokenType.LBRACKET, message="Expected '[' for primary objective list")

        primary_terms: List[Expr] = []
        if self.current_token().type != TokenType.RBRACKET:
            primary_terms.append(self.parse_condition())
            while self.match(TokenType.COMMA):
                if self.current_token().type == TokenType.RBRACKET:
                    break
                primary_terms.append(self.parse_condition())
        self.expect(TokenType.RBRACKET, message="Expected ']' closing primary list")

        # secondary?
        secondary_terms: List[Expr] = []
        if self.match(TokenType.SECONDARY):
            self.expect(TokenType.COLON, message="Expected ':' after 'secondary'")
            self.expect(TokenType.LBRACKET, message="Expected '[' for secondary list")
            if self.current_token().type != TokenType.RBRACKET:
                secondary_terms.append(self.parse_condition())
                while self.match(TokenType.COMMA):
                    if self.current_token().type == TokenType.RBRACKET:
                        break
                    secondary_terms.append(self.parse_condition())
            self.expect(TokenType.RBRACKET, message="Expected ']' closing secondary list")

        # weights?
        weights_dict: Dict[str, float] = {}
        if self.match(TokenType.WEIGHTS):
            self.expect(TokenType.COLON, message="Expected ':' after 'weights'")
            self.expect(TokenType.LBRACE, message="Expected '{' for weights dictionary")
            while self.current_token().type not in (TokenType.RBRACE, TokenType.EOF):
                key_tok = self.advance()
                key = self.check_identifier(key_tok)
                self.expect(TokenType.COLON, message="Expected ':' after weight key")
                num_tok = self.expect(TokenType.NUMBER, message="Expected number for weight value")
                weights_dict[key] = float(num_tok.parsed_value)  # type: ignore
                self.match(TokenType.COMMA)
            self.expect(TokenType.RBRACE, message="Expected '}' closing weights dictionary")

        return Objective(
            name=name,
            primary=primary_terms,
            secondary=secondary_terms,
            weights=weights_dict,
            location=obj_tok.location,
        )

    # ========================================================================
    # Effects and Metadata
    # ========================================================================

    def parse_effect(self) -> Effect:
        """
        effect := require | forbid | assign | prefer
        require := "require" (ruleRef | expr)
        forbid := "forbid" expr
        assign := "assign" field "=" expr
        prefer := "prefer" expr
        """
        tok = self.current_token()
        loc = tok.location

        if self.match(TokenType.REQUIRE):
            # Check for ruleRef: "rule" IDENT
            if self.current_token().type == TokenType.RULE:
                self.advance()
                ref_tok = self.advance()
                ref_name = self.check_identifier(ref_tok)
                return RequireEffect(target=RuleRef(rule_name=ref_name, location=loc), location=loc)
            expr = self.parse_condition()
            return RequireEffect(target=expr, location=loc)

        if self.match(TokenType.FORBID):
            expr = self.parse_condition()
            return ForbidEffect(expr=expr, location=loc)

        if self.match(TokenType.ASSIGN):
            field_tok = self.advance()
            fname = self.check_identifier(field_tok)
            self.expect(TokenType.EQUALS, message=f"Expected '=' in assign effect for '{fname}'")
            expr = self.parse_condition()
            return AssignEffect(field_name=fname, expr=expr, location=loc)

        if self.match(TokenType.PREFER):
            expr = self.parse_condition()
            return PreferEffect(expr=expr, location=loc)

        raise SyntaxError(f"Unexpected token '{tok.value}', expected effect")

    def parse_meta_items(self) -> RuleMeta:
        """
        meta := "priority" ":" ("hard" | "soft")
              | "weight" ":" NUMBER
              | "message" ":" STRING
              | "tags" ":" "[" IDENT ("," IDENT)* "]"
              | "id" ":" IDENT
        """
        meta = RuleMeta()

        while True:
            if self.current_token().type == TokenType.PRIORITY:
                self.advance()
                self.expect(TokenType.COLON)
                val_tok = self.advance()
                if val_tok.type not in (TokenType.HARD, TokenType.SOFT):
                    self.collector.add_error(
                        code="E1001",
                        message=f"Priority must be 'hard' or 'soft', found '{val_tok.value}'",
                        location=val_tok.location,
                        phase=Phase.PARSE,
                    )
                meta.priority = val_tok.value

            elif self.current_token().type == TokenType.WEIGHT:
                self.advance()
                self.expect(TokenType.COLON)
                num_tok = self.expect(TokenType.NUMBER, message="Expected number for 'weight'")
                meta.weight = float(num_tok.parsed_value)  # type: ignore

            elif self.current_token().type == TokenType.MESSAGE:
                self.advance()
                self.expect(TokenType.COLON)
                str_tok = self.expect(TokenType.STRING, message="Expected string for 'message'")
                meta.message = str(str_tok.parsed_value)

            elif self.current_token().type == TokenType.TAGS:
                self.advance()
                self.expect(TokenType.COLON)
                self.expect(TokenType.LBRACKET)
                tags: List[str] = []
                if self.current_token().type != TokenType.RBRACKET:
                    t_tok = self.advance()
                    tags.append(self.check_identifier(t_tok))
                    while self.match(TokenType.COMMA):
                        if self.current_token().type == TokenType.RBRACKET:
                            break
                        t_tok = self.advance()
                        tags.append(self.check_identifier(t_tok))
                self.expect(TokenType.RBRACKET)
                meta.tags = tags

            elif self.current_token().type == TokenType.ID:
                self.advance()
                self.expect(TokenType.COLON)
                id_tok = self.advance()
                meta.rule_id = self.check_identifier(id_tok)

            else:
                break

        return meta

    # ========================================================================
    # Conditions and Expressions
    # ========================================================================

    def parse_condition(self) -> Expr:
        return self.parse_disjunction()

    def parse_disjunction(self) -> Expr:
        """disjunction := conjunction ("or" conjunction)*"""
        expr = self.parse_conjunction()
        while self.match(TokenType.OR):
            loc = self.tokens[self.pos - 1].location
            right = self.parse_conjunction()
            expr = BinaryOp(left=expr, op="or", right=right, location=loc)
        return expr

    def parse_conjunction(self) -> Expr:
        """conjunction := negation ("and" negation)*"""
        expr = self.parse_negation()
        while self.match(TokenType.AND):
            loc = self.tokens[self.pos - 1].location
            right = self.parse_negation()
            expr = BinaryOp(left=expr, op="and", right=right, location=loc)
        return expr

    def parse_negation(self) -> Expr:
        """negation := "not" negation | comparison"""
        if self.match(TokenType.NOT):
            loc = self.tokens[self.pos - 1].location
            operand = self.parse_negation()
            return UnaryOp(op="not", operand=operand, location=loc)
        return self.parse_comparison()

    def parse_comparison(self) -> Expr:
        """
        comparison := additive (compop additive)?
        compop := "==" | "!=" | "<" | "<=" | ">" | ">=" | "in" | "not in" | "matches" | "is"
        """
        left = self.parse_additive()

        # Handle 'not in'
        if self.current_token().type == TokenType.NOT and self.peek(1).type == TokenType.IN:
            loc = self.current_token().location
            self.advance()  # not
            self.advance()  # in
            right = self.parse_additive()
            return BinaryOp(left=left, op="not in", right=right, location=loc)

        # Check for 'is some [IDENT]' or 'is none'
        if self.current_token().type == TokenType.IS:
            loc = self.current_token().location
            if self.peek(1).type == TokenType.SOME:
                self.advance()  # is
                self.advance()  # some
                binding: Optional[str] = None
                if self.current_token().type == TokenType.IDENT:
                    binding = self.advance().value
                return IsSomeExpr(expr=left, binding_var=binding, location=loc)
            elif self.peek(1).type == TokenType.NONE:
                self.advance()  # is
                self.advance()  # none
                return IsNoneExpr(expr=left, location=loc)

        comp_types = {
            TokenType.EQ: "==",
            TokenType.NE: "!=",
            TokenType.LTE: "<=",
            TokenType.LT: "<",
            TokenType.GTE: ">=",
            TokenType.GT: ">",
            TokenType.IN: "in",
            TokenType.MATCHES: "matches",
            TokenType.IS: "is",
        }

        tok = self.current_token()
        if tok.type in comp_types:
            op = comp_types[tok.type]
            loc = tok.location
            self.advance()
            right = self.parse_additive()
            return BinaryOp(left=left, op=op, right=right, location=loc)

        return left

    def parse_additive(self) -> Expr:
        """additive := multiplicative (("+" | "-") multiplicative)*"""
        expr = self.parse_multiplicative()
        while self.current_token().type in (TokenType.PLUS, TokenType.MINUS):
            tok = self.advance()
            right = self.parse_multiplicative()
            expr = BinaryOp(left=expr, op=tok.value, right=right, location=tok.location)
        return expr

    def parse_multiplicative(self) -> Expr:
        """multiplicative := unary (("*" | "/" | "%") unary)*"""
        expr = self.parse_unary()
        while self.current_token().type in (TokenType.STAR, TokenType.SLASH, TokenType.PERCENT):
            tok = self.advance()
            right = self.parse_unary()
            expr = BinaryOp(left=expr, op=tok.value, right=right, location=tok.location)
        return expr

    def parse_unary(self) -> Expr:
        """unary := "-" unary | postfix"""
        if self.match(TokenType.MINUS):
            loc = self.tokens[self.pos - 1].location
            operand = self.parse_unary()
            return UnaryOp(op="-", operand=operand, location=loc)
        return self.parse_postfix()

    def parse_postfix(self) -> Expr:
        """
        postfix := primary ("." IDENT | "?." IDENT | "(" args? ")" | "[" expr "]")*
        """
        expr = self.parse_primary()

        while True:
            # Field access: . IDENT
            if self.match(TokenType.DOT):
                loc = self.tokens[self.pos - 1].location
                id_tok = self.advance()
                fname = self.check_identifier(id_tok)
                expr = FieldAccessExpr(target=expr, field_name=fname, is_optional=False, location=loc)

            # Optional chaining: ?. IDENT
            elif self.match(TokenType.QUESTION_DOT):
                loc = self.tokens[self.pos - 1].location
                id_tok = self.advance()
                fname = self.check_identifier(id_tok)
                expr = FieldAccessExpr(target=expr, field_name=fname, is_optional=True, location=loc)

            # Call: ( args? )
            elif self.match(TokenType.LPAREN):
                loc = self.tokens[self.pos - 1].location
                args: List[Expr] = []
                if self.current_token().type != TokenType.RPAREN:
                    args.append(self.parse_condition())
                    while self.match(TokenType.COMMA):
                        if self.current_token().type == TokenType.RPAREN:
                            break
                        args.append(self.parse_condition())
                self.expect(TokenType.RPAREN, message="Expected ')' closing argument list")
                expr = CallExpr(callee=expr, args=args, location=loc)

            # Indexing: [ expr ]
            elif self.match(TokenType.LBRACKET):
                loc = self.tokens[self.pos - 1].location
                idx = self.parse_condition()
                self.expect(TokenType.RBRACKET, message="Expected ']' closing index expression")
                expr = IndexExpr(target=expr, index=idx, location=loc)

            # Explicit cast: as IDENT
            elif self.match(TokenType.AS):
                loc = self.tokens[self.pos - 1].location
                type_tok = self.advance()
                target_type = self.check_identifier(type_tok)
                expr = AsExpr(expr=expr, target_type=target_type, location=loc)

            else:
                break

        return expr

    def parse_primary(self) -> Expr:
        """
        primary := literal | IDENT | "self" | "(" condition ")" | quantified | aggregate
        """
        tok = self.current_token()
        loc = tok.location

        # Literals
        if tok.type == TokenType.NUMBER:
            self.advance()
            ltype = "float" if isinstance(tok.parsed_value, float) else "int"
            return LiteralExpr(value=tok.parsed_value, literal_type=ltype, location=loc)

        if tok.type == TokenType.STRING:
            self.advance()
            return LiteralExpr(value=tok.parsed_value, literal_type="string", location=loc)

        if tok.type in (TokenType.TRUE, TokenType.FALSE):
            self.advance()
            return LiteralExpr(value=tok.parsed_value, literal_type="bool", location=loc)

        if tok.type == TokenType.NULL:
            self.advance()
            return LiteralExpr(value=None, literal_type="null", location=loc)

        # self
        if tok.type == TokenType.SELF:
            self.advance()
            return SelfExpr(location=loc)

        # Parenthesized condition: "(" condition ")"
        if self.match(TokenType.LPAREN):
            cond = self.parse_condition()
            self.expect(TokenType.RPAREN, message="Expected ')' closing parenthesized condition")
            return cond

        # List / Collection literal: "[" (expr ("," expr)*)? "]"
        if self.match(TokenType.LBRACKET):
            elements: List[Expr] = []
            if self.current_token().type != TokenType.RBRACKET:
                elements.append(self.parse_condition())
                while self.match(TokenType.COMMA):
                    if self.current_token().type == TokenType.RBRACKET:
                        break
                    elements.append(self.parse_condition())
            self.expect(TokenType.RBRACKET, message="Expected ']' closing list literal")
            return ListLiteralExpr(elements=elements, location=loc)

        # Quantified: ("forall" | "exists") IDENT "in" expr ("where" condition)? ":" condition
        if tok.type in (TokenType.FORALL, TokenType.EXISTS):
            kind = tok.value
            self.advance()
            var_tok = self.advance()
            var_name = self.check_identifier(var_tok)
            self.expect(TokenType.IN, message=f"Expected 'in' after quantified variable '{var_name}'")
            coll_expr = self.parse_condition()

            where_cond: Optional[Expr] = None
            if self.match(TokenType.WHERE):
                where_cond = self.parse_condition()

            self.expect(TokenType.COLON, message="Expected ':' before quantified condition")
            cond = self.parse_condition()
            return QuantifiedExpr(
                kind=kind,
                var_name=var_name,
                collection=coll_expr,
                where_clause=where_cond,
                condition=cond,
                location=loc,
            )

        # Aggregate: ("count" | "sum" | "min" | "max" | "avg" | "distinct") "(" expr ")"
        if tok.type in (
            TokenType.COUNT,
            TokenType.SUM,
            TokenType.MIN,
            TokenType.MAX,
            TokenType.AVG,
            TokenType.DISTINCT,
        ):
            func_name = tok.value
            self.advance()
            self.expect(TokenType.LPAREN, message=f"Expected '(' after aggregate '{func_name}'")
            inner_expr = self.parse_condition()
            self.expect(TokenType.RPAREN, message=f"Expected ')' closing aggregate '{func_name}'")
            return AggregateExpr(func_name=func_name, expr=inner_expr, location=loc)

        # Identifier
        if tok.type == TokenType.IDENT:
            self.advance()
            return IdentifierExpr(name=tok.value, location=loc)

        # If we hit an unexpected token
        self.collector.add_error(
            code="E1001",
            message=f"Unexpected token '{tok.value}' in expression",
            location=loc,
            phase=Phase.PARSE,
        )
        self.advance()
        return IdentifierExpr(name="<error>", location=loc)


def parse_ruleset(
    source: str,
    source_file: Optional[str] = None,
    collector: Optional[ErrorCollector] = None,
) -> Ruleset:
    """Convenience entry point to parse ARL source text into an AST."""
    parser = Parser.from_source(source, source_file=source_file, collector=collector)
    return parser.parse_ruleset()


def parse_expr(
    source: str,
    source_file: Optional[str] = None,
    collector: Optional[ErrorCollector] = None,
) -> Expr:
    """Convenience entry point to parse a standalone ARL expression into an AST."""
    parser = Parser.from_source(source, source_file=source_file, collector=collector)
    return parser.parse_condition()

