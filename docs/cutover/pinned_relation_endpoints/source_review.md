# Source review

PROVEN: mysql_tool._row_to_relation_dict preserves source/target declared physical
columns, including historical unqualified names. The current snapshot binds each
entity to its base table, data source and explicit sub-table mappings. SQL consumes
those facts in one pin. This change canonicalizes only within these endpoint owners.

PROVEN: prior _SnapshotLoader checked endpoint membership in the global field set
without checking the declared owner. The fresh capture demonstrates a source-owned
bridge field presented as an endpoint of a different target entity. That declaration
is now unavailable to path search. No missing second bridge endpoint is inferred.

PROVEN: five unqualified declarations have exactly one field per endpoint owner.
One remaining source column has two matches across its base and sub-table. A unique
semantic attribute mapping does not prove which of those physical occurrences the
relationship intended; physical ambiguity stays unresolved.

Implementation review: no new dependencies, prompt or regex; strict field membership,
scope and data-source checks remain. Copying occurs in _index, so normalization never
mutates the sealed source snapshot. The same pin is finished after SQL validation;
all pinned execution methods remain denied. Public and legacy routes are untouched.

UNKNOWN / CATALOG_GOVERNANCE_GAP: primary query subject of two multi-entity metrics.
The business descriptions/formulas/global filters and unordered bind_entity list do
not establish a typed primary-subject contract. The old prompt's first-list-element
choice does not authorize that decision. Keep their six typed plans unsupported.

Acceptance limits: no relationship-specific V2 ASL lowering, actual source result,
native publication, query accuracy or deployed trust evidence is supplied here.
Metadata rejection of one formerly accepted declaration is intentional; every
existing regression nodeid still has its previous result. Source/index checks and
explicit-file publication are required before the commit is accepted.
