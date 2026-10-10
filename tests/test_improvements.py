"""Financial reconciliation, abstention and trace integrity regression cases."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest

from finance_agent.analysis import analyze
from finance_agent.audit import AuditLog
from finance_agent.enrichment import enrich_report
from finance_agent.evaluation import evaluate
from finance_agent.provenance import seal_run, verify_artifacts
from finance_agent.schemas import AnalysisResult, Evidence, ExtractedReport, Metric, ParsedDocument, RunResult


def fixture():
    doc_id = "0123456789abcdef"
    header = Evidence(id=f"{doc_id}:p1:table0", document_id=doc_id, document_name="原表.pdf", pdf_page=1,
        kind="table", cells=[["非经常性损益项目", "2025年金额", "2024年金额"], ["政府补助", "100.00", "999.00"],
                             ["处置损益", "-20.00", "888.00"]], text="非经常性损益项目\n政府补助 | 100.00 | 999.00\n处置损益 | -20.00 | 888.00")
    table = Evidence(id=f"{doc_id}:p2:table0", document_id=doc_id, document_name="原表.pdf", pdf_page=2,
        kind="table", header_evidence_id=header.id, cells=[["减：所得税影响额", "10.00", "0"],
                            ["少数股东权益影响额（税后）", "5.00", "0"], ["合计", "65.00", "1887.00"]], text="减：所得税影响额 | 10.00 | 0\n少数股东权益影响额（税后） | 5.00 | 0\n合计 | 65.00 | 1887.00")
    doc = ParsedDocument(id=doc_id, name="原表.pdf", sha256=doc_id*4, page_count=2, blocks=[header,table])
    metric = Metric(name="nonrecurring_total_parent", company="测试公司", value=Decimal("65"), raw_value="65.00", unit="元",
        period_start=date(2025,1,1), period_end=date(2025,12,31), basis="ytd", scope="consolidated",
        evidence_ids=[table.id,header.id], source_row_index=2, source_column_index=1)
    report = ExtractedReport(document_id=doc.id, document_name=doc.name, company=metric.company, report_type="annual", period_end=metric.period_end, metrics=[metric])
    return report, doc


def test_nonrecurring_cross_page_sign_and_current_column():
    report, doc = fixture()
    result = enrich_report(report,doc)
    check = result.detail_checks['nonrecurring']
    assert check['status'] == 'reconciled' and Decimal(check['residual_yuan']) == 0
    assert [Decimal(row['contribution_yuan']) for row in result.financial_details] == [100,-20,-10,-5]
    assert all(row['source_column_index'] == 1 for row in result.financial_details)


def test_nonrecurring_missing_tax_never_claims_complete():
    report, doc = fixture()
    doc.blocks[1].cells[0][1] = ''
    check = enrich_report(report,doc).detail_checks['nonrecurring']
    assert check['status'] == 'incomplete'
    assert Decimal(check['residual_yuan']) == -10


def test_nonrecurring_wrong_detail_cannot_pass_total():
    report, doc = fixture()
    doc.blocks[0].cells[1][1] = '999'
    check = enrich_report(report,doc).detail_checks['nonrecurring']
    assert check['status'] == 'inconsistent'


def test_reason_rejects_auditor_sensitivity_and_non_subject():
    report,doc = fixture()
    texts = ['营业收入变动原因说明：主要是本期产品销量减少所致。',
             '营业收入增长主要由于收入重要，因此我们将收入确认识别为关键审计事项。',
             '利息支出将增加或减少100元，导致利润总额减少100元。',
             '应收账款较期初增加20%，主要系本期营业收入增加所致。',
             '所得税费用较上年同期下降10%，主要系本期利润总额下降所致。']
    for index,text in enumerate(texts):
        doc.blocks.append(Evidence(id=f'{doc.id}:p2:text{index}',document_id=doc.id,document_name=doc.name,pdf_page=2,text=text))
    reasons = enrich_report(report,doc).performance_reasons
    assert len(reasons) == 1 and reasons[0]['quote'] == texts[0]


def test_business_small_base_uses_revenue_scale_and_keeps_delta():
    report,doc = fixture()
    template = report.metrics[0]
    current = template.model_copy(update={'name':'net_profit','value':Decimal('1000000')})
    base = current.model_copy(update={'value':Decimal('1000'),'period_start':date(2024,1,1),'period_end':date(2024,12,31)})
    revenue = current.model_copy(update={'name':'revenue','value':Decimal('1000000000')})
    report.metrics = [current,base,revenue]
    comparison = next(c for c in analyze([report]).comparisons if c.metric=='net_profit' and c.kind=='yoy')
    assert comparison.status=='near_zero_base' and comparison.percent is None and comparison.delta==999000
    revenue.value=Decimal('1000000')
    comparison = next(c for c in analyze([report]).comparisons if c.metric=='net_profit' and c.kind=='yoy')
    assert comparison.status=='ok'


def test_evaluation_without_labels_is_not_evaluated(tmp_path):
    report,doc = fixture()
    result=RunResult(run_id='test',mode='demo',model='offline',reports=[report],analysis=AnalysisResult(),evidence={b.id:b for b in doc.blocks},run_dir=str(tmp_path))
    scores = evaluate(result, {'fields':[], 'comparisons':[], 'signals':[]})
    assert scores['evaluation_status']=='not_evaluated'
    assert scores['scores']['fields']['agreement'] is None


def test_evaluation_missing_and_incorrect_amount_fail(tmp_path):
    report,doc=fixture()
    result=RunResult(run_id='test',mode='demo',model='offline',reports=[report],analysis=AnalysisResult(),evidence={b.id:b for b in doc.blocks},run_dir=str(tmp_path))
    m=report.metrics[0].model_dump(mode='json')
    identity={k:m[k] for k in ('company','name','period_start','period_end','basis','scope')}
    scores=evaluate(result, {'fields':[{'match':identity,'expected':{'value':'66'}}]})
    assert scores['scores']['fields']['correct']==0 and len(scores['failures'])==1
    identity['scope']='parent'
    scores=evaluate(result, {'fields':[{'match':identity,'expected':{'value':'65'}}]})
    assert scores['scores']['fields']['coverage']==0


def test_excel_snapshot_preserves_exact_values_and_never_injects_formula(tmp_path):
    from finance_agent.workbook import export_workbook
    report,doc=fixture()
    report.company='=HYPERLINK("https://example.com")'
    report.metrics[0].value=Decimal('123456789012345.67')
    result=RunResult(run_id='test',mode='demo',model='offline',reports=[report],analysis=AnalysisResult(),evidence={b.id:b for b in doc.blocks},run_dir=str(tmp_path))
    path=tmp_path/'analysis.xlsx'
    export_workbook(result,path)
    with ZipFile(path) as z:
        for name in z.namelist():
            ElementTree.fromstring(z.read(name))
        content=z.read('xl/worksheets/sheet1.xml').decode()
        assert '123456789012345.67' in content and '<f>' not in content
        assert len(ElementTree.fromstring(z.read('xl/workbook.xml')).find('{*}sheets'))==6


def test_sealed_files_detect_tampering_and_cross_run_manifest(tmp_path):
    report,doc=fixture()
    result=RunResult(run_id='test',mode='demo',model='offline',reports=[report],analysis=AnalysisResult(),evidence={b.id:b for b in doc.blocks},run_dir=str(tmp_path))
    (tmp_path/'result.json').write_text(result.model_dump_json(),encoding='utf-8')
    (tmp_path/'report.md').write_text('content',encoding='utf-8')
    (tmp_path/'metrics.csv').write_text('rows',encoding='utf-8')
    audit=AuditLog(tmp_path/'trace.jsonl')
    seal_run(result,audit)
    assert verify_artifacts(tmp_path)==[]
    (tmp_path/'report.md').write_text('changed',encoding='utf-8')
    assert any('report.md' in error for error in verify_artifacts(tmp_path))


def test_exception_trace_redacts_keys_and_does_not_capture_locals(tmp_path):
    audit=AuditLog(tmp_path/'trace.jsonl')
    audit.secrets=['test-secret']
    try:
        raise RuntimeError('test-secret Bearer private_token')
    except RuntimeError as exc:
        audit.exception('test_failure',exc)
    raw=audit.path.read_text(encoding='utf-8')
    event=json.loads(raw)
    assert 'test-secret' not in raw and 'private_token' not in raw
    assert event['output']['exception_type']=='RuntimeError' and 'stack' in event['output']


def bridge_fixture():
    report,doc=fixture()
    profit=report.metrics[0].model_copy(update={'name':'net_profit','value':Decimal('100')})
    cash=profit.model_copy(update={'name':'operating_cashflow','value':Decimal('120')})
    report.metrics += [profit,cash]
    unit=Evidence(id=f'{doc.id}:p3:text0',document_id=doc.id,document_name=doc.name,pdf_page=3,
                  text='单位：元 币种：人民币',bbox=[0,60,200,75])
    root=Evidence(id=f'{doc.id}:p3:table0',document_id=doc.id,document_name=doc.name,pdf_page=3,
        text='补充资料\n1．将净利润调节为经营活动现金流量：',kind='table',bbox=[0,100,400,750],
        cells=[['补充资料','本期金额','上期金额'],['1．将净利润调节为经营活动现金流量：','',''],['净利润','100','999'],['加：折旧','30','0']])
    continuation=Evidence(id=f'{doc.id}:p4:table0',document_id=doc.id,document_name=doc.name,pdf_page=4,
        kind='table',header_evidence_id=root.id,text='存货减少 | -10 | 0\n经营活动产生的现金流量净额 | 120 | 999',
        cells=[['存货减少（增加以负号填列）','-10','0'],['经营活动产生的现金流量净额','120','999'],['现金期末余额','10000','0']])
    doc.blocks += [unit,root,continuation]
    return report,doc


def test_cashflow_bridge_matches_both_anchors_and_ignores_cash_balance():
    report,doc=bridge_fixture()
    enriched=enrich_report(report,doc)
    check=enriched.detail_checks['cashflow_bridge']
    assert check['status']=='reconciled' and Decimal(check['residual_yuan'])==0
    rows=[r for r in enriched.financial_details if r['category']=='cashflow_bridge']
    assert [Decimal(r['value_yuan']) for r in rows]==[100,30,-10]


def test_cashflow_bridge_conflicting_scope_anchor_is_rejected():
    report,doc=bridge_fixture()
    report.metrics[-1].value=Decimal('119')
    check=enrich_report(report,doc).detail_checks['cashflow_bridge']
    assert check['status']=='inconsistent'


def test_cashflow_bridge_retains_unexplained_residual():
    report,doc=bridge_fixture()
    doc.blocks[-1].cells[0][1]='-9'
    check=enrich_report(report,doc).detail_checks['cashflow_bridge']
    assert check['status']=='inconsistent' and Decimal(check['residual_yuan'])==-1


def test_signal_abstention_is_a_missed_positive_in_recall(tmp_path):
    from finance_agent.schemas import Signal
    report,doc=fixture()
    signal=Signal(id='test',company=report.company,period_end=report.period_end,category='nonrecurring',
        status='insufficient_evidence',severity='unknown',title='unknown',message='missing proof')
    result=RunResult(run_id='test',mode='demo',model='offline',reports=[report],analysis=AnalysisResult(signals=[signal]),
                     evidence={b.id:b for b in doc.blocks},run_dir=str(tmp_path))
    labels={'signals':[{'match':{'company':report.company,'period_end':str(report.period_end),'category':'nonrecurring'},'expected':{'status':'detected'}}]}
    score=evaluate(result,labels)['scores']['signal_detection']
    assert score['recall']==0 and score['fn']==1 and score['abstention_rate']==1


def test_tool_contract_refs_resolve_and_do_not_accept_extra_input():
    import jsonschema
    from finance_agent.tools import tool_catalog
    report,doc=fixture()
    catalog={item['name']:item for item in tool_catalog()}
    payload={'report':report.model_dump(mode='json'),'document':doc.model_dump(mode='json')}
    jsonschema.validate(payload,catalog['enrich_financial_details']['input_schema'])
    jsonschema.validate([report.model_dump(mode='json')],catalog['financial_analysis']['input_schema'])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**payload,'unrequested_file':'private.txt'},catalog['enrich_financial_details']['input_schema'])


def test_parent_annual_metrics_use_correct_table_scope_and_year():
    from finance_agent.schemas import Evidence
    report,doc=fixture()
    cover=Evidence(id=f'{doc.id}:p1:text0',document_id=doc.id,document_name=doc.name,pdf_page=1,text='测试公司 2025年年度报告')
    title=Evidence(id=f'{doc.id}:p3:text0',document_id=doc.id,document_name=doc.name,pdf_page=3,text='母公司利润表')
    units=Evidence(id=f'{doc.id}:p3:text1',document_id=doc.id,document_name=doc.name,pdf_page=3,text='单位：元 币种：人民币')
    table=Evidence(id=f'{doc.id}:p3:table0',document_id=doc.id,document_name=doc.name,pdf_page=3,kind='table',
        text='项目 | 2025年度 | 2024年度\n一、营业收入 | 1000 | 800\n四、净利润（净亏损以“－”号填列） | 100 | 50',
        cells=[['项目','2025年度','2024年度'],['一、营业收入','1000','800'],['四、净利润（净亏损以“－”号填列）','100','50']],
        context_evidence_ids=[title.id,units.id])
    doc.blocks += [cover,title,units,table]
    result=enrich_report(report,doc)
    parent=[m for m in result.metrics if m.scope=='parent']
    assert len(parent)==4 and {(m.name,m.period_end.year,m.value) for m in parent}=={
        ('revenue',2025,Decimal(1000)),('revenue',2024,Decimal(800)),('net_profit',2025,Decimal(100)),('net_profit',2024,Decimal(50))}
    assert all(m.scope=='parent' for m in parent)


def test_multiline_company_reason_keeps_contiguous_evidence():
    report,doc=fixture()
    first=Evidence(id=f'{doc.id}:p3:text0',document_id=doc.id,document_name=doc.name,pdf_page=3,
        text='经营现金流变动原因说明：主要是本期收款减',bbox=[100,100,500,112])
    continuation=Evidence(id=f'{doc.id}:p3:text1',document_id=doc.id,document_name=doc.name,pdf_page=3,
        text='少，同时支付采购货款增加所致。',bbox=[80,135,500,147])
    next_item=Evidence(id=f'{doc.id}:p3:text2',document_id=doc.id,document_name=doc.name,pdf_page=3,
        text='投资现金流变动原因说明：主要是处置资产所致。',bbox=[100,169,500,180])
    doc.blocks += [first,continuation,next_item]
    result=enrich_report(report,doc)
    assert len(result.performance_reasons)==1
    assert result.performance_reasons[0]['evidence_ids']==[first.id,continuation.id]
    assert '支付采购货款增加' in result.performance_reasons[0]['quote']


@pytest.mark.parametrize('nested', [False, True])
def test_legacy_model_history_survives_portable_second_replay(tmp_path, monkeypatch, nested):
    import shutil
    import pymupdf
    from finance_agent.config import Settings
    from finance_agent.llm import DeepSeekClient
    from finance_agent.parser import parse_financial_doc
    from finance_agent.replay import replay_result

    def forbidden(*args, **kwargs):
        raise AssertionError('离线回放不能读取密钥或创建模型客户端')
    monkeypatch.setattr(Settings, 'load', forbidden)
    monkeypatch.setattr(DeepSeekClient, '__init__', forbidden)
    original = tmp_path / 'original'
    (original / 'sources').mkdir(parents=True)
    pdf = original / 'input.pdf'
    with pymupdf.open() as document:
        document.new_page().insert_text((72,72), 'Annual report 2025')
        document.save(pdf)
    parsed = parse_financial_doc(pdf)
    shutil.copyfile(pdf, original / 'sources' / f'{parsed.id}.pdf')
    report = ExtractedReport(document_id=parsed.id, document_name=parsed.name, company='测试公司',
        report_type='annual', period_end=date(2025,12,31), metrics=[])
    result = RunResult(run_id='revalidated', mode='live', model='saved', run_dir=str(original), reports=[report],
        analysis=AnalysisResult(), evidence={b.id:b for b in parsed.blocks}, usage={'model_calls':1})
    (original/'result.json').write_text(result.model_dump_json(), encoding='utf-8')
    AuditLog(original/'trace.jsonl').record('revalidate_live_run')
    history = original / 'history' / ('initial_model_run' if nested else '')
    history.mkdir(parents=True)
    saved = result.model_copy(update={'run_id':'initial_model_run'})
    (history/'result.json').write_text(saved.model_dump_json(), encoding='utf-8')
    audit = AuditLog(history/'trace.jsonl')
    audit.record('model_request', inputs={'parameters':{'temperature':0}})
    audit.record('model_response', outputs={'content':'{"metrics":[]}'})
    first = replay_result(original/'result.json', tmp_path/'runs')
    assert verify_artifacts(Path(first.run_dir)) == []
    metadata = first.provenance['replay']['model_history']
    assert len(metadata)==1 and metadata[0]['run_id']=='initial_model_run'
    assert metadata[0]['model_requests']==metadata[0]['model_responses']==1
    assert first.provenance['replay']['model_calls']==0
    portable = tmp_path/'portable_copy'
    shutil.copytree(first.run_dir, portable)
    # 原运行及第一轮目录不可访问时，便携成果仍保留真正的模型响应。
    monkeypatch.setattr('finance_agent.replay.ROOT', tmp_path/'absent_project')
    second = replay_result(portable/'result.json', tmp_path/'second_runs')
    second_metadata = second.provenance['replay']['model_history']
    assert second_metadata==metadata
    assert verify_artifacts(Path(second.run_dir))==[]
    assert (Path(second.run_dir)/second_metadata[0]['trace_path']).read_bytes()==(history/'trace.jsonl').read_bytes()


def test_history_does_not_label_recalculation_as_model_response(tmp_path):
    from finance_agent.replay import preserve_model_history
    result = RunResult(run_id='only_recalc', mode='demo', model='offline', run_dir=str(tmp_path), reports=[],
                       analysis=AnalysisResult(), evidence={})
    (tmp_path/'result.json').write_text(result.model_dump_json(), encoding='utf-8')
    AuditLog(tmp_path/'trace.jsonl').record('financial_analysis', outputs={'comparisons':[]})
    destination=tmp_path/'new'
    destination.mkdir()
    assert preserve_model_history(tmp_path/'result.json', destination, search_roots=[]) == []
    assert (destination/'history/previous_trace.jsonl').read_bytes()==(tmp_path/'trace.jsonl').read_bytes()
