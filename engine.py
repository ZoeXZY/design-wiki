#!/usr/bin/env python3
"""Deterministic development probe. No model calls; does not read expected.json."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent

def finite_number(x):
    try:
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    except OverflowError:
        return False

def assessment(case, catalog):
    rule = catalog['by_id'][case['rule_id']]
    ctx, measurements, exceptions = (case.get(k, {}) for k in ('context','measurements','exceptions'))
    deps = ['rule:' + rule['id'], 'rule_id']
    trace = []
    result = {'id':case['id'], 'kind':'assessment', 'rule_id':rule['id'], 'question':case['question'], 'sources':rule['source']['urls'], 'dependencies':deps, 'trace':trace}
    def finish(app, outcome, reason):
        result.update(applicability=app,assessment=outcome,reason=reason,assessment_label={'pass':'满足本条','fail':'未满足本条','unknown':'待补充依据','not_required':'本次非必需','exempt':'明确例外成立'}[outcome])
        return result
    def read(section, key, value):
        deps.append(section+'.'+key)
        return value.get(key)
    # A decisive mismatch is distinct from an unknown context.
    unknown = []
    for field, allowed in [('scope',[rule['scope']]),('subject',rule['subjects'])]:
        val = read('context',field,ctx)
        if val is None:
            unknown.append(field)
        elif val not in allowed:
            return finish('不适用','not_required',field+'与本准则对象不符；只针对本次准则判断。')
    version = read('context','document_version',ctx)
    if version not in catalog['supported_document_versions']:
        unknown.append('document_version')
    elif version not in rule['versions']:
        return finish('不适用','not_required','本次采用的规范版本不包含该准则；不自动修改项目版本。')
    level = read('context','level',ctx)
    levels = catalog['level_order']
    if level not in levels:
        unknown.append('level')
    elif levels.index(level) < levels.index(rule['required_level']):
        return finish('不适用','not_required','本次目标级别未要求该准则；更高等级可另列建议。')
    if unknown:
        return finish('待确认','unknown','需要核对情境：'+', '.join(unknown))
    trace.append('版本、级别和对象满足适用条件。')
    ex_unknown=[]
    for key in rule['exceptions']:
        value=read('exceptions',key,exceptions)
        if value is True:
            return finish('适用','exempt','已提供本准则例外成立的明确事实：'+key+'；须由使用者回查原文条件。')
        if value is not False:
            ex_unknown.append(key)
    # A sufficient direct measurement can pass even when exceptions are unknown.
    if rule['kind']=='minimum_rectangle':
        unit=read('measurements','unit',measurements)
        if unit!=rule['unit']:
            return finish('适用','unknown','测量单位不匹配或缺失；本包不自动换算。')
        w=read('measurements','width',measurements)
        h=read('measurements','height',measurements)
        if not all(finite_number(v) and v>=0 for v in (w,h)):
            return finish('适用','unknown','需要有限、非负的目标宽度与高度。')
        trace.append(f"输入尺寸 {w}×{h} {unit}；本准则下限 {rule['min_width']}×{rule['min_height']}。")
        directly_satisfied=w>=rule['min_width'] and h>=rule['min_height']
    elif rule['kind']=='minimum_contrast':
        ratio=read('measurements','contrast_ratio',measurements)
        if not finite_number(ratio) or not 1 <= ratio <= 21:
            return finish('适用','unknown','需要已核对的有限对比度比值，且在1至21之间。')
        category=read('measurements','text_size',measurements)
        thresholds=rule['thresholds']
        if category in thresholds:
            directly_satisfied=ratio>=thresholds[category]
            trace.append(f"已核类别 {category}，比值 {ratio}，阈值 {thresholds[category]}。")
        elif ratio>=max(thresholds.values()):
            directly_satisfied=True
            trace.append('比值满足全部已列类别的阈值，类别未知不改变本次通过结论。')
        elif ratio<min(thresholds.values()):
            directly_satisfied=False
            trace.append('比值低于全部已列类别阈值，需继续检查例外。')
        else:
            return finish('适用','unknown','文字大小类别会改变结论，需要先核对normal或large。')
    else:
        return finish('待确认','unknown','本开发工具尚不支持该规则类型。')
    if directly_satisfied:
        return finish('适用','pass','输入数值满足本次准则的数值条件；不据此判断整页或网站符合性。')
    if ex_unknown:
        return finish('适用','unknown','数值未直接满足，但例外可能改变判断；待核：'+', '.join(ex_unknown))
    return finish('适用','fail','数值未满足，且本准则所列例外均已明确为不成立。')

def relation(case,catalog):
    a,b=(catalog['by_id'][case[k]] for k in ('left_rule','right_rule'))
    context=case.get('comparison_context',{})
    result={'id':case['id'],'kind':'relation','question':case['question'],'rule_ids':[a['id'],b['id']],'sources':list(dict.fromkeys(a['source']['urls']+b['source']['urls'])),'dependencies':['rule:'+a['id'],'rule:'+b['id'],'left_rule','right_rule']}
    def finish(status,reason):
        result.update(relation=status,reason=reason)
        return result
    keys=('same_object','same_task','same_time','exceptions_excluded')
    vals=[]
    for key in keys:
        result['dependencies'].append('comparison_context.'+key)
        vals.append(context.get(key))
    if any(v is False for v in vals):
        return finish('不可比','本探针的共同对象、任务、时间或无例外前提不成立；未建立冲突比较条件。')
    if any(v is not True for v in vals):
        return finish('未知','比较所需前提尚未明确。')
    if a['kind']!=b['kind'] or a.get('unit')!=b.get('unit'):
        return finish('不可比','属性类型或单位不同。')
    if a['kind']=='minimum_rectangle':
        result['joint_lower_bound']={'width':max(a['min_width'],b['min_width']),'height':max(a['min_height'],b['min_height']),'unit':a['unit']}
        return finish('兼容','两个数值下限具有共同可行解；不表示两个准则在每个级别下都必需。')
    return finish('未知','本探针仅实现明确比较条件下的目标尺寸下限兼容检查。')

def load_catalog(path):
    data=json.loads(path.read_text(encoding='utf-8'))
    data['by_id']={r['id']:r for r in data['rules']}
    if len(data['by_id'])!=len(data['rules']):
        raise ValueError('规则编号重复')
    return data

def run(catalog,cases):
    outputs=[]
    for case in cases:
        if case['kind']=='assessment': outputs.append(assessment(case,catalog))
        elif case['kind']=='relation': outputs.append(relation(case,catalog))
        else: raise ValueError('未知case kind: '+str(case['kind']))
    return outputs


def evaluate(case, rules):
    catalog = {"by_id": {r["id"]: r for r in rules}, "supported_document_versions": ["2.1", "2.2"], "level_order": ["A", "AA", "AAA"]}
    return run(catalog, [case])[0]
