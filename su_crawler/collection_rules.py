"""Explicit, revision-pinned page recipes; never inferred from untrusted HTML.

A rule key is an exact plan candidate URL, or a site pattern
``site:<host>/<path glob>`` (``*`` matches within the path) that covers
several approved pages of one site, such as every category list. An exact
URL rule wins over patterns; among patterns the longest (most specific) wins.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

PATTERN_PREFIX = 'site:'
_PATTERN = re.compile(r'site:([a-z0-9.-]+)(/[A-Za-z0-9/_.~%*+,;=:@!$\'()-]*)')


def is_pattern(key: str) -> bool:
    return isinstance(key, str) and key.startswith(PATTERN_PREFIX)


def pattern_matches(key: str, url: str) -> bool:
    match = _PATTERN.fullmatch(key)
    parts = urlsplit(url)
    return bool(match and (parts.hostname or '') == match.group(1)
                and fnmatchcase(parts.path or '/', match.group(2)))


def rules_for(rules: dict, urls) -> dict:
    """The rules that apply to any of ``urls`` (exact keys and matching patterns)."""
    urls = set(urls)
    return {key: rule for key, rule in (rules or {}).items()
            if key in urls or (is_pattern(key) and any(pattern_matches(key, url) for url in urls))}


def _rule_key(raw_key, allowed_urls):
    from .research_plans import canonical_url
    if not is_pattern(raw_key):
        url = canonical_url(raw_key)
        if url not in allowed_urls:
            raise ValueError('Collection rules must refer to a plan candidate URL')
        return url
    key = PATTERN_PREFIX + raw_key[len(PATTERN_PREFIX):].strip()
    host, _, path = key[len(PATTERN_PREFIX):].partition('/')
    key = f'{PATTERN_PREFIX}{host.lower()}/{path}'
    if len(key) > 300 or not _PATTERN.fullmatch(key):
        raise ValueError('Site rule patterns look like site:<host>/<path glob> (letters, digits, / . _ - ~ % *)')
    if not any(pattern_matches(key, url) for url in allowed_urls):
        raise ValueError('A site rule pattern must match at least one plan candidate URL')
    return key


def normalize_rules(value, allowed_urls, *, existing=None, allow_trials=False):
    """Validate reviewed or assistant-proposed rules.

    ``origin`` "proposed" marks an assistant proposal, which also passes the
    proposal safety checks. Trial records are written by the trial runner
    only (``allow_trials``); an edit may carry an existing record unchanged.
    """
    from .collectors import _safe_step, _expand_options
    from .rule_trials import ORIGINS, normalize_trial, validate_proposed
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError('collection_rules must map at most 50 selected URLs or site patterns to reviewed rules')
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 65536:
        raise ValueError('Collection rules exceed 64 KiB')
    result = {}
    for raw_url, rule in value.items():
        url = _rule_key(raw_url, allowed_urls)
        if not isinstance(rule, dict) or set(rule) - {'recipe', 'selectors', 'row_selector', 'profile_dir',
                                                      'origin', 'trial'}:
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
        origin = rule.get('origin')
        if origin is not None:
            if origin not in ORIGINS:
                raise ValueError('origin must be reviewed or proposed')
            if origin == 'proposed':
                validate_proposed(clean)
            clean['origin'] = origin
        if 'trial' in rule:
            trial = normalize_trial(rule['trial'])
            if not allow_trials and ((existing or {}).get(url) or {}).get('trial') != trial:
                raise ValueError('Trial records come from trial_collection_rule; run a trial instead of editing one')
            clean['trial'] = trial
        result[url] = clean
    return result


def rule_for(rules, url):
    """The exact rule for ``url``, else the most specific matching site pattern."""
    if url in rules:
        return rules[url]
    patterns = sorted((key for key in rules if is_pattern(key) and pattern_matches(key, url)), key=len, reverse=True)
    return rules[patterns[0]] if patterns else None


def apply_rule(source, rules, root):
    rule = rule_for(rules, source.location)
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
