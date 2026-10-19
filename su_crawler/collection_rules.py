"""Explicit, revision-pinned page recipes; never inferred from untrusted HTML."""
from __future__ import annotations

import json
from pathlib import Path


def normalize_rules(value, allowed_urls):
    from .research_plans import canonical_url
    from .collectors import _safe_step, _expand_options
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError('collection_rules must map at most 50 selected URLs to reviewed rules')
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 65536:
        raise ValueError('Collection rules exceed 64 KiB')
    result = {}
    for raw_url, rule in value.items():
        url = canonical_url(raw_url)
        if url not in allowed_urls:
            raise ValueError('Collection rules must refer to a plan candidate URL')
        if not isinstance(rule, dict) or set(rule) - {'recipe', 'selectors', 'row_selector', 'profile_dir'}:
            raise ValueError('Invalid collection rule fields')
        recipe = rule.get('recipe', [])
        if not isinstance(recipe, list) or len(recipe) > 20:
            raise ValueError('Recipe must contain at most 20 reviewed steps')
        for step in recipe:
            if not isinstance(step, dict):
                raise ValueError('Recipe steps must be objects')
            action, _ = _safe_step(step)
            if action == 'expand_list':
                _expand_options(step)
            elif action in {'enumerate_options', 'paginate', 'scroll_list', 'enumerate_buttons'}:
                from .browser_enumeration import validate_enumeration_step
                validate_enumeration_step(step)
        selectors = rule.get('selectors', {})
        if not isinstance(selectors, dict) or len(selectors) > 40 or any(
            not isinstance(k, str) or not isinstance(v, str) or not k or not v or len(v) > 1000
            for k, v in selectors.items()):
            raise ValueError('selectors must map field names to bounded CSS selectors')
        clean = {'recipe': recipe, 'selectors': selectors}
        row = rule.get('row_selector')
        if row is not None:
            if not isinstance(row, str) or not row.strip() or len(row) > 1000:
                raise ValueError('row_selector must be a nonempty CSS selector')
            clean['row_selector'] = row
        profile = rule.get('profile_dir')
        if profile is not None:
            if not isinstance(profile, str) or not profile or Path(profile).is_absolute() or '..' in Path(profile).parts or ':' in profile:
                raise ValueError('profile_dir must be a workspace-relative existing browser profile')
            clean['profile_dir'] = profile
        result[url] = clean
    return result


def apply_rule(source, rules, root):
    rule = rules.get(source.location)
    if not rule:
        return source
    from dataclasses import replace
    from .models import stable_id
    profile = rule.get('profile_dir')
    if profile:
        base = Path(root).resolve()
        candidate = base / profile
        if not candidate.is_dir() or candidate.resolve() == base or not candidate.resolve().is_relative_to(base):
            raise ValueError('Browser profile must be an existing directory inside the workspace')
        if any(p.is_symlink() for p in [candidate, *candidate.parents] if p != base and p.is_relative_to(base)):
            raise ValueError('Browser profile must not be a symbolic link')
    return replace(source, recipe=rule.get('recipe', []), selectors=rule.get('selectors', {}),
                   row_selector=rule.get('row_selector'), profile_dir=profile,
                   account_scope='authorized_profile' if profile else source.account_scope,
                   adapter=None if rule.get('selectors') else source.adapter,
                   backends=['playwright'], recipe_version='reviewed-' + stable_id(rule))
