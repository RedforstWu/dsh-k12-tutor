"""Versioned deterministic assessment contract shared with study's domain helper."""
from fractions import Fraction

VERSION = 'tutor-math/v1'
MANIFEST = {
    'linear-function-value/v1': ('math/junior:linear-function-graph', 'math_8b_rjb_cpt50'),
    'linear-equation/v1': ('math/junior:linear-eq-solve', 'math_7a_rjb_skl17'),
    'linear-inequality/v1': ('math/junior:linear-inequality-solve', 'math_7b_rjb_skl17'),
}

def build(value):
    if not isinstance(value, dict) or set(value) != {'templateId', 'params'} or value['templateId'] not in MANIFEST:
        raise ValueError('invalid assessment template')
    name, p = value['templateId'], value['params']
    keys = {'k','b','x'} if name == 'linear-function-value/v1' else {'a','b','c'}
    if name == 'linear-inequality/v1':
        keys.add('op')
    if not isinstance(p, dict) or set(p) != keys or any(type(p[k]) is not int or not -20 <= p[k] <= 20 for k in keys - {'op'}):
        raise ValueError('invalid assessment parameters')
    if p.get('k', p.get('a')) == 0 or ('op' in p and p['op'] not in ('<','>')):
        raise ValueError('invalid assessment coefficient or operator')
    if 'k' in p:
        stem = f"已知一次函数 y = {p['k']}x + ({p['b']})，当 x = {p['x']} 时，求 y。"
        answer = str(p['k'] * p['x'] + p['b'])
        fingerprint = f"{name}:{p['k']}:{p['b']}:{p['x']}"
    else:
        op = p.get('op','=')
        stem = f"{'解方程' if op == '=' else '解不等式'}：{p['a']}x + ({p['b']}) {op} {p['c']}。"
        boundary = Fraction(p['c'] - p['b'], p['a'])
        if p['a'] < 0 and op != '=':
            op = '>' if op == '<' else '<'
        answer = f'x {op} {boundary}'
        fingerprint = f'{name}:{boundary.numerator}/{boundary.denominator}:{op}'
    return {'question': {'type':'free','stem':stem,'options':None,'referenceAnswer':answer},
            'deliveryText':stem, 'fingerprint':fingerprint,
            'teacherKnowledgeIds':[MANIFEST[name][0]], 'knowledgeIds':[MANIFEST[name][1]], 'mappingVersion':VERSION}
