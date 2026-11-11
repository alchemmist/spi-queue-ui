"""Generation-only bounds; historical REPORT_SCHEMA and stored evidence stay intact."""
import copy
from core import REPORT_SCHEMA,validate,Failure

# Printable ASCII excluding double quote/backslash: no mandatory JSON escaping.
# A compact ASCII report fits the original2500-token allowance even without
# BPE merges. JSON whitespace/Unicode escaping or an upstream ignoring the
# schema can still exhaust a completion: never salvage partial JSON.
ASCII=r'[\x20-\x21\x23-\x5b\x5d-\x7e]'
TEXT_LIMITS={'title':96,'summary':160,'match_reason':96,'impact':128,'alert_assessment':128}
ARRAY_LIMITS={'facts':(3,120),'hypotheses':(2,96),'service_actions':(2,96),
              'monitoring_actions':(2,96),'missing_evidence':(2,96)}


def guided_report_schema(ids=None,issue_numbers=(0,)):
    result=copy.deepcopy(REPORT_SCHEMA)
    props=result['properties']
    for key,limit in TEXT_LIMITS.items():
        props[key].update(minLength=1,maxLength=limit)
    for key,(count,length) in ARRAY_LIMITS.items():
        props[key].update(maxItems=count)
        props[key]['items'].update(minLength=1,maxLength=length)
    # Multiple bounded pattern fields hit a compiler assertion in xgrammar
    # 0.2.7. Use native maxLength for prose; only facts need a citation regex.
    props['facts']['items']['pattern']='^'+ASCII+'{1,120}$'
    props['issue_number'].update(maximum=2147483647,enum=sorted(set([0,*issue_numbers])))
    if ids is not None:
        if ids:
            # IDs are checked against the exact allowlist again after decoding;
            # this restriction does not replace citation or safety validation.
            import re
            # xgrammar0.2.7 ignores maxLength when pattern is present.
            # Put the actual length bound IN the regex, including citations.
            prefix=ARRAY_LIMITS['facts'][1]-max(len(x)+2 for x in ids)-1
            props['facts']['items']['pattern']='^'+ASCII+'{0,'+str(prefix)+'}'+r'\[(?:'+'|'.join(re.escape(x) for x in ids)+r')\]\.$'
        else:
            props['facts']['maxItems']=0
    return result


def validate_generated_report(value,schema):
    """Reject field-cap fragments, never silently publish them as full findings."""
    validate(value,schema)
    for key in (set(TEXT_LIMITS)-{'title'})|set(ARRAY_LIMITS):
        texts=value[key]if isinstance(value[key],list)else[value[key]]
        for text in texts:
            if not text.rstrip().endswith(('.', '?', '!')) or text.rstrip().endswith('...'):
                raise Failure('INVALID_MODEL_SCHEMA')
    return value
