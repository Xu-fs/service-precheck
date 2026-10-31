"""Deterministic fictional checklist; never evaluates authenticity or eligibility."""
import json
from pathlib import Path
from datetime import date
from zhengwu_precheck.core.errors import AppError

RULES_PATH = Path(__file__).resolve().parents[4] / 'data/materials/teaching-rules-v1.json'


def load_rules():
    return json.loads(RULES_PATH.read_text(encoding='utf-8-sig'))


def evaluate(matter_id, scenario_id, rule_snapshot_id, materials):
    rules=load_rules()
    rule=rules['matters'].get(matter_id)
    if not rule or rule['scenario_id']!=scenario_id or rule_snapshot_id!=rules['version']:
        raise AppError(409,'RULE_SNAPSHOT_CHANGED','教学规则版本或事项情形已变化，请重新确认')
    evidence=[]; exceptions=[]; kinds=set(); field_values={}; uncertain=False; unreadable=False
    for item in materials:
        extraction=item.get('extraction',{})
        base={'material_id':item['id'],'material_version':item['version'],'sha256':item.get('sha256'),'source':extraction.get('source','none')}
        if extraction.get('error_code') or not extraction.get('kind'):
            unreadable=True
            evidence.append({**base,'code':'UNREADABLE','message':'材料已上传，但内容或类别尚无法确认；不据此断言缺项'})
            continue
        kinds.add(extraction['kind'])
        fields=extraction.get('fields',{})
        if not extraction.get('confirmed') or any(not fields.get(k,'').strip() for k in rules['required_fields']):
            uncertain=True
            evidence.append({**base,'code':'FIELDS_UNCONFIRMED','message':'关键字段缺失或识别结果待本人核对'})
        else:
            evidence.append({**base,'code':'FIELDS_PRESENT','fields':fields,'message':'字段来自材料转录或识别，不证明真实性'})
        for key in rules['required_fields']:
            if fields.get(key):
                field_values.setdefault(key,set()).add(fields[key].strip())
        for key in ('date','expiry_date','historical_valid_on'):
            if fields.get(key):
                try:
                    date.fromisoformat(fields[key])
                except ValueError:
                    uncertain=True
                    evidence.append({**base,'code':'INVALID_DATE','field':key})
        try:
            expiry=date.fromisoformat(fields.get('expiry_date',''))
            historical=date.fromisoformat(fields.get('historical_valid_on',''))
            if historical<=expiry<date.fromisoformat(rules['reference_date']):
                exceptions.append('expired_historical_valid')
                evidence.append({**base,'code':'HISTORICAL_VALIDITY_CONFLICT','message':'仅依据本地材料所填日期，历史有效性需人工核验'})
        except ValueError:
            pass
    for key,values in field_values.items():
        if len(values)>1:
            uncertain=True
            evidence.append({'code':'FIELD_MISMATCH','field':key,'message':'跨材料字段不一致，需本人或人工核对'})
    missing=[kind for kind in rule['required'] if kind not in kinds]
    if missing and not unreadable:
        evidence.extend({'code':'CHECKLIST_MISSING','kind':kind,'source':rule['source']} for kind in missing)
    result=('需人工核验' if exceptions else '无法判断' if unreadable else '信息待核实' if uncertain else '疑似缺项' if missing else '未发现清单问题')
    return {'result':result,'matter_id':matter_id,'scenario_id':scenario_id,'rule_snapshot_id':rule_snapshot_id,'rule_snapshot':rule,'evidence':evidence,'exceptions':sorted(set(exceptions)),'scope':rules['scope'],'notice':'仅做教学清单辅助检查，未经真实性或法定资格核验；正式补正由工作人员确认'}
