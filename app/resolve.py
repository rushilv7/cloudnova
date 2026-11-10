"""Duplicate collapse and the conflict policy (specs/solution.md section 3, "Which duplicate wins").

1. Collapse exact raw duplicates, keeping every source-row reference.
2. Normalize what remains and collapse records that are identical across all
   canonical fields. The smallest source row represents the group.
3. Validate. If exactly one distinct candidate is valid, it wins.
4. If several distinct valid candidates remain, none is eligible: the invoice
   goes to the conflict report and stays out of modeled totals.

Every source row ends with exactly one disposition, so the counts reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.normalize import RAW_COLUMNS, Candidate, normalize_row

ELIGIBLE = "eligible"
EXACT_DUPLICATE = "exact_duplicate"
NORMALIZED_DUPLICATE = "normalized_duplicate"
REJECTED_INVALID = "rejected_invalid"
HELD_CONFLICT = "held_conflict"


@dataclass
class Group:
    """Records for one invoice ID that are identical after normalization."""

    candidate: Candidate          # from the smallest source row
    source_rows: list[int]        # every source row in the group, ascending

    @property
    def representative_row(self) -> int:
        return self.source_rows[0]


@dataclass
class Resolution:
    eligible: list[Group] = field(default_factory=list)                 # one per eligible invoice ID
    conflicts: dict[str, list[Group]] = field(default_factory=dict)     # invoice ID -> distinct valid candidates
    rejected: list[Group] = field(default_factory=list)                 # invalid candidates
    dispositions: dict[int, str] = field(default_factory=dict)          # source row -> disposition
    row_candidates: dict[int, Candidate] = field(default_factory=dict)  # source row -> its normalized record

    def count(self, disposition: str) -> int:
        return sum(1 for value in self.dispositions.values() if value == disposition)


def resolve(rows: list[dict]) -> Resolution:
    """Resolve raw records. `rows[0]` is source row 1."""
    result = Resolution()

    by_invoice: dict[str, list[int]] = {}
    for index, raw in enumerate(rows, start=1):
        by_invoice.setdefault((raw.get("invoice_id") or "").strip(), []).append(index)

    for invoice_id in sorted(by_invoice):
        # Step 1: exact raw duplicates.
        exact: dict[tuple, list[int]] = {}
        for source_row in by_invoice[invoice_id]:
            raw = rows[source_row - 1]
            exact.setdefault(tuple(raw.get(column, "") for column in RAW_COLUMNS), []).append(source_row)

        # Step 2: normalize each distinct raw record and group equivalent ones.
        groups: dict[tuple, Group] = {}
        raw_key_of: dict[int, tuple] = {}
        for raw_key, raw_rows in exact.items():
            candidate = normalize_row(rows[raw_rows[0] - 1])
            for source_row in raw_rows:
                result.row_candidates[source_row] = candidate
                raw_key_of[source_row] = raw_key
            key = (candidate.canonical_key(), candidate.errors)
            if key in groups:
                groups[key].source_rows.extend(raw_rows)
            else:
                groups[key] = Group(candidate, list(raw_rows))
        for group in groups.values():
            group.source_rows.sort()

        # Steps 3 and 4: validate and decide.
        valid = [g for g in groups.values() if g.candidate.valid]
        result.rejected.extend(g for g in groups.values() if not g.candidate.valid)
        if len(valid) == 1:
            result.eligible.append(valid[0])
        elif len(valid) > 1:
            result.conflicts[invoice_id] = sorted(valid, key=lambda g: g.representative_row)

        # The smallest row of each group carries the group's outcome. Every later
        # row is a duplicate: exact if an earlier row in the group has the same raw
        # text, otherwise equal only after normalization.
        for group in groups.values():
            if not group.candidate.valid:
                outcome = REJECTED_INVALID
            elif len(valid) == 1:
                outcome = ELIGIBLE
            else:
                outcome = HELD_CONFLICT
            seen: set[tuple] = set()
            for position, source_row in enumerate(group.source_rows):
                raw_key = raw_key_of[source_row]
                if position == 0:
                    result.dispositions[source_row] = outcome
                else:
                    result.dispositions[source_row] = EXACT_DUPLICATE if raw_key in seen else NORMALIZED_DUPLICATE
                seen.add(raw_key)

    result.rejected.sort(key=lambda g: g.representative_row)
    return result
