"""
ARL Lexer and Tokenizer.

Implements tokenization according to Part 2 Grammar and Part 6 Syntax Errors (E1002, E1006, E1007).
Tracks exact source locations (file, line, column).
"""

from __future__ import annotations

import re
from enum import Enum, auto
from typing import List, Optional, Tuple

from arl.errors import ArlError, ErrorCollector, Phase, Severity, SourceLocation, make_error


class TokenType(Enum):
    # Keywords
    RULESET = "ruleset"
    EXTENDS = "extends"
    INCLUDE = "include"
    RULE = "rule"
    OBJECTIVE = "objective"
    OVERRIDE = "override"
    SCOPE = "scope"
    WHEN = "when"
    REQUIRE = "require"
    FORBID = "forbid"
    ASSIGN = "assign"
    PREFER = "prefer"
    PRIORITY = "priority"
    HARD = "hard"
    SOFT = "soft"
    WEIGHT = "weight"
    MESSAGE = "message"
    TAGS = "tags"
    ID = "id"
    SELF = "self"
    AND = "and"
    OR = "or"
    NOT = "not"
    IN = "in"
    MATCHES = "matches"
    IS = "is"
    FORALL = "forall"
    EXISTS = "exists"
    COUNT = "count"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    AVG = "avg"
    DISTINCT = "distinct"
    PRIMARY = "primary"
    SECONDARY = "secondary"
    WEIGHTS = "weights"
    TRUE = "true"
    FALSE = "false"
    NULL = "null"
    WHERE = "where"
    AS = "as"
    SOME = "some"
    NONE = "none"

    # Symbols & Delimiters
    LBRACE = "{"
    RBRACE = "}"
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["
    RBRACKET = "]"
    COLON = ":"
    COMMA = ","
    EQUALS = "="
    DOT = "."
    QUESTION_DOT = "?."

    # Comparison Operators
    EQ = "=="
    NE = "!="
    LTE = "<="
    LT = "<"
    GTE = ">="
    GT = ">"

    # Arithmetic Operators
    PLUS = "+"
    MINUS = "-"
    STAR = "*"
    SLASH = "/"
    PERCENT = "%"

    # Literals & Identifiers
    IDENT = "IDENT"
    STRING = "STRING"
    NUMBER = "NUMBER"
    EOF = "EOF"


RESERVED_KEYWORDS = {
    "ruleset": TokenType.RULESET,
    "extends": TokenType.EXTENDS,
    "include": TokenType.INCLUDE,
    "rule": TokenType.RULE,
    "objective": TokenType.OBJECTIVE,
    "override": TokenType.OVERRIDE,
    "scope": TokenType.SCOPE,
    "when": TokenType.WHEN,
    "require": TokenType.REQUIRE,
    "forbid": TokenType.FORBID,
    "assign": TokenType.ASSIGN,
    "prefer": TokenType.PREFER,
    "priority": TokenType.PRIORITY,
    "hard": TokenType.HARD,
    "soft": TokenType.SOFT,
    "weight": TokenType.WEIGHT,
    "message": TokenType.MESSAGE,
    "tags": TokenType.TAGS,
    "id": TokenType.ID,
    "self": TokenType.SELF,
    "and": TokenType.AND,
    "or": TokenType.OR,
    "not": TokenType.NOT,
    "in": TokenType.IN,
    "matches": TokenType.MATCHES,
    "is": TokenType.IS,
    "forall": TokenType.FORALL,
    "exists": TokenType.EXISTS,
    "count": TokenType.COUNT,
    "sum": TokenType.SUM,
    "min": TokenType.MIN,
    "max": TokenType.MAX,
    "avg": TokenType.AVG,
    "distinct": TokenType.DISTINCT,
    "primary": TokenType.PRIMARY,
    "secondary": TokenType.SECONDARY,
    "weights": TokenType.WEIGHTS,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
    "null": TokenType.NULL,
    "where": TokenType.WHERE,
    "as": TokenType.AS,
    "some": TokenType.SOME,
    "none": TokenType.NONE,
}


class Token:
    def __init__(
        self,
        type: TokenType,
        value: str,
        location: SourceLocation,
        parsed_value: Optional[object] = None,
    ) -> None:
        self.type = type
        self.value = value
        self.location = location
        self.parsed_value = parsed_value if parsed_value is not None else value

    def __repr__(self) -> str:
        return f"Token({self.type.name}, {self.value!r}, loc={self.location})"


class Lexer:
    """Lexical analyzer for ARL source code."""

    def __init__(
        self,
        source: str,
        file: Optional[str] = None,
        collector: Optional[ErrorCollector] = None,
    ) -> None:
        self.source = source
        self.file = file
        self.collector = collector or ErrorCollector()
        self.pos = 0
        self.line = 1
        self.column = 1
        self.length = len(source)

    def current_location(self) -> SourceLocation:
        return SourceLocation(file=self.file, line=self.line, column=self.column)

    def advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return ch

    def peek(self, offset: int = 0) -> str:
        idx = self.pos + offset
        if idx < self.length:
            return self.source[idx]
        return ""

    def match(self, expected: str) -> bool:
        if self.source.startswith(expected, self.pos):
            for _ in range(len(expected)):
                self.advance()
            return True
        return False

    def skip_whitespace_and_comments(self) -> None:
        while self.pos < self.length:
            ch = self.peek()
            if ch in (" ", "\t", "\r", "\n"):
                self.advance()
            elif ch == "#":
                # Single-line comment # ...
                while self.pos < self.length and self.peek() != "\n":
                    self.advance()
            elif ch == "/" and self.peek(1) == "/":
                # Single-line comment // ...
                while self.pos < self.length and self.peek() != "\n":
                    self.advance()
            elif ch == "/" and self.peek(1) == "*":
                # Multi-line comment /* ... */
                self.advance()
                self.advance()
                while self.pos < self.length:
                    if self.peek() == "*" and self.peek(1) == "/":
                        self.advance()
                        self.advance()
                        break
                    self.advance()
            else:
                break

    def read_string(self) -> Token:
        quote = self.advance()  # " or '
        loc = self.current_location()
        chars: List[str] = []
        is_terminated = False

        while self.pos < self.length:
            ch = self.peek()
            if ch == "\n":
                # Unterminated string before newline
                break
            if ch == quote:
                self.advance()
                is_terminated = True
                break
            if ch == "\\":
                self.advance()
                if self.pos >= self.length:
                    break
                esc = self.advance()
                if esc == "n":
                    chars.append("\n")
                elif esc == "t":
                    chars.append("\t")
                elif esc == "r":
                    chars.append("\r")
                elif esc == "\\":
                    chars.append("\\")
                elif esc == quote:
                    chars.append(quote)
                else:
                    # Invalid escape sequence: E1007
                    self.collector.add_error(
                        code="E1007",
                        message=f"Invalid escape sequence '\\{esc}' in string literal",
                        location=self.current_location(),
                        phase=Phase.LEX,
                    )
                    chars.append(esc)
            else:
                chars.append(self.advance())

        if not is_terminated:
            self.collector.add_error(
                code="E1002",
                message=f"Unterminated string literal starting at line {loc.line}",
                location=loc,
                phase=Phase.LEX,
            )

        content = "".join(chars)
        return Token(TokenType.STRING, content, loc, parsed_value=content)

    def read_number(self) -> Token:
        loc = self.current_location()
        start_pos = self.pos
        has_dot = False

        while self.pos < self.length:
            ch = self.peek()
            if ch.isdigit():
                self.advance()
            elif ch == "." and self.peek(1).isdigit():
                if has_dot:
                    # Double dot in number: invalid numeric literal E1006
                    self.collector.add_error(
                        code="E1006",
                        message="Invalid numeric literal with multiple decimal points",
                        location=loc,
                        phase=Phase.LEX,
                    )
                has_dot = True
                self.advance()
            else:
                break

        # Check if next char is an invalid identifier char attached to number (e.g. 123abc)
        if self.pos < self.length and (self.peek().isalpha() or self.peek() == "_"):
            while self.pos < self.length and (self.peek().isalnum() or self.peek() == "_"):
                self.advance()
            num_str = self.source[start_pos:self.pos]
            self.collector.add_error(
                code="E1006",
                message=f"Invalid numeric literal '{num_str}'",
                location=loc,
                phase=Phase.LEX,
            )
            return Token(TokenType.NUMBER, num_str, loc, parsed_value=0)

        num_str = self.source[start_pos:self.pos]
        try:
            val = float(num_str) if has_dot else int(num_str)
        except ValueError:
            self.collector.add_error(
                code="E1006",
                message=f"Invalid numeric literal '{num_str}'",
                location=loc,
                phase=Phase.LEX,
            )
            val = 0

        return Token(TokenType.NUMBER, num_str, loc, parsed_value=val)

    def read_ident(self) -> Token:
        loc = self.current_location()
        start_pos = self.pos
        while self.pos < self.length:
            ch = self.peek()
            if ch.isalnum() or ch == "_":
                self.advance()
            else:
                break

        word = self.source[start_pos:self.pos]
        if word in RESERVED_KEYWORDS:
            tok_type = RESERVED_KEYWORDS[word]
            parsed_val: Optional[object] = None
            if tok_type == TokenType.TRUE:
                parsed_val = True
            elif tok_type == TokenType.FALSE:
                parsed_val = False
            elif tok_type == TokenType.NULL:
                parsed_val = None
            return Token(tok_type, word, loc, parsed_value=parsed_val)

        return Token(TokenType.IDENT, word, loc, parsed_value=word)

    def tokenize(self) -> List[Token]:
        """Scan full input string and return list of tokens."""
        tokens: List[Token] = []

        while self.pos < self.length:
            self.skip_whitespace_and_comments()
            if self.pos >= self.length:
                break

            loc = self.current_location()
            ch = self.peek()

            # String literal
            if ch in ('"', "'"):
                tokens.append(self.read_string())
                continue

            # Number literal
            if ch.isdigit():
                tokens.append(self.read_number())
                continue

            # Identifier or keyword
            if ch.isalpha() or ch == "_":
                tokens.append(self.read_ident())
                continue

            # Two-character operators / delimiters
            if ch == "?" and self.peek(1) == ".":
                self.advance()
                self.advance()
                tokens.append(Token(TokenType.QUESTION_DOT, "?.", loc))
                continue
            if ch == "=" and self.peek(1) == "=":
                self.advance()
                self.advance()
                tokens.append(Token(TokenType.EQ, "==", loc))
                continue
            if ch == "!" and self.peek(1) == "=":
                self.advance()
                self.advance()
                tokens.append(Token(TokenType.NE, "!=", loc))
                continue
            if ch == "<" and self.peek(1) == "=":
                self.advance()
                self.advance()
                tokens.append(Token(TokenType.LTE, "<=", loc))
                continue
            if ch == ">" and self.peek(1) == "=":
                self.advance()
                self.advance()
                tokens.append(Token(TokenType.GTE, ">=", loc))
                continue

            # Single-character tokens
            single_map = {
                "{": TokenType.LBRACE,
                "}": TokenType.RBRACE,
                "(": TokenType.LPAREN,
                ")": TokenType.RPAREN,
                "[": TokenType.LBRACKET,
                "]": TokenType.RBRACKET,
                ":": TokenType.COLON,
                ",": TokenType.COMMA,
                "=": TokenType.EQUALS,
                ".": TokenType.DOT,
                "<": TokenType.LT,
                ">": TokenType.GT,
                "+": TokenType.PLUS,
                "-": TokenType.MINUS,
                "*": TokenType.STAR,
                "/": TokenType.SLASH,
                "%": TokenType.PERCENT,
            }

            if ch in single_map:
                self.advance()
                tokens.append(Token(single_map[ch], ch, loc))
                continue

            # Unexpected character: E1001
            bad_char = self.advance()
            self.collector.add_error(
                code="E1001",
                message=f"Unexpected character '{bad_char}'",
                location=loc,
                phase=Phase.LEX,
            )

        tokens.append(Token(TokenType.EOF, "", self.current_location()))
        return tokens
