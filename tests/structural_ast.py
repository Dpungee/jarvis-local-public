"""Stable Python 3.13-style AST fingerprints across supported interpreters.

The recorded extraction proofs used 3.13's omission of empty list fields.
This formatter preserves that exact representation on 3.11 and 3.12 without
changing a recorded digest or discarding nonempty syntax/binding information.
"""
import ast


def structural_dump(node):
    if isinstance(node, ast.AST):
        fields = []
        for name, value in ast.iter_fields(node):
            if value is None and getattr(type(node), name, ...) is None:
                continue
            if isinstance(value, list) and not value:
                continue
            fields.append(f"{name}={structural_dump(value)}")
        return f"{type(node).__name__}({', '.join(fields)})"
    if isinstance(node, list):
        return '[' + ', '.join(structural_dump(item) for item in node) + ']'
    return repr(node)
