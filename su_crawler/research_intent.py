"""Shared interpretation guidance for CLI and connected-assistant previews."""

INTENT_GUIDANCE = (
    "Distinguish actual research targets from reference entities, comparison baselines, and exclusions. "
    "A named company is not automatically a target. For 'competitors of A', 'A와 경쟁하는 업체', "
    "or 'A와 경쟁사인 업체', A is the reference company; research its competitors and exclude A's own "
    "products and prices unless the user explicitly asks to include A. For 'compare A and its competitors', "
    "include both. Apply the same distinction to brands, products, suppliers, and all industries. "
    "State the target scope and reference/excluded entities clearly in summary and note before suggesting "
    "candidate links. Reference-company pages may support competitor identification but must not be "
    "proposed as collection targets in a competitors-only request. Do not encode company roles as literal "
    "include_terms or exclude_terms: a competitor page can mention the reference company. "
    "Preserve the original request. If the relationship is genuinely ambiguous, explain the alternatives "
    "in note and leave candidates empty for clarification; do not silently broaden the scope. "
    "Derive industry and product from the request's explicit meaning rather than unrelated saved workspace "
    "defaults. Leave an unspecified geographic market empty; a product or industry name is not a market. "
)
