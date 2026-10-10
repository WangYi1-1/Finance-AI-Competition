"""离线升级展示样本，金额回归通过后切换指针；不调用模型。"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from finance_agent.replay import replay_result
from verify_public_examples import verify as verify_public
from verify_yili_examples import verify as verify_yili


def main():
    summary = {}
    for group, index, output_root in [('yili',ROOT/'samples/member/yili/latest_run.json',ROOT/'output/member_yili'),
                                     ('public',ROOT/'samples/public/latest_verified_run.json',ROOT/'output')]:
        previous = json.loads(index.read_text(encoding='utf-8'))
        result = replay_result(ROOT / previous['result_path'], output_root)
        path = Path(result.run_dir) / 'result.json'
        if group == 'yili':
            checks = verify_yili(path,ROOT/'output/yili_validation_20261009/manual_reference.json')
            if not (checks['amounts_pass']==checks['amounts_total'] and checks['annual_comparisons_pass']==checks['annual_comparisons_total']
                    and checks['transition_checks_pass']==2 and checks['financial_signal_checks_pass']==4):
                raise ValueError('伊利原有金额/比较回归失败，未切换展示结果。')
            pointer = {'run_id':result.run_id,'result_path':path.relative_to(ROOT).as_posix()}
            index.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding='utf-8')
        else:
            checks = verify_public(path)
            if checks['amount_correct'] != checks['amount_total'] or checks['quarter_correct'] != checks['quarter_total'] or any(x['status']!='正确' for x in checks['rule_checks']):
                raise ValueError('公开样本回归失败，未切换展示结果。')
        summary[group] = {'run_id':result.run_id,'result_path':path.relative_to(ROOT).as_posix(),
            'reasons':sum(len(r.performance_reasons) for r in result.reports),
            'detail_rows':sum(len(r.financial_details) for r in result.reports),
            'parent_metrics':sum(m.scope=='parent' for r in result.reports for m in r.metrics),
            'reconciliation':{str(r.period_end)+r.company:{k:v['status'] for k,v in r.detail_checks.items()} for r in result.reports}}
        print(f'{group}: {result.run_dir}',flush=True)
    folder = ROOT / 'output/improvements_20261010'
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'showcase_validation.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
